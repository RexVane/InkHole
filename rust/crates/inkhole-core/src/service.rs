use std::{
    collections::{HashMap, VecDeque},
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::{Path, PathBuf},
    sync::{Arc, Mutex as StdMutex},
    time::Duration,
};

use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Map, Value, json};
use tokio::{
    sync::{Mutex, Notify, mpsc},
    task::JoinHandle,
};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use crate::{
    CORE_PROTOCOL_VERSION, CoreError, DeviceIdentity, InboxCategoryRoots, Result,
    discovery::{
        DiscoveredPeer, DiscoveryPeersCallback, UDP_DISCOVERY_PORT, UdpDiscovery,
        UdpDiscoveryConfig, UdpDiscoveryTimings,
    },
    protocol::{MAX_CONTROL_FRAME, valid_instance_id},
    ssh::{SshPeer, SshProfile, SshRelayConfig, SshRelaySession, check_ssh},
    transport::{
        PeerEndpoint, QuicServer, QuicServerConfig, SendFileOptions, TransferEvent,
        TransferEventCallback, send_file,
    },
    wormhole::{
        ReceivedOffer, SenderConnection, SenderSession, WormholeSettings, summarize_paths,
        temporary_secret,
    },
};

const DROPPABLE_EVENT_CAPACITY: usize = 128;

#[derive(Debug, Deserialize)]
struct JsonRequest {
    #[serde(default)]
    id: String,
    #[serde(default)]
    method: String,
    #[serde(default)]
    params: Value,
}

#[derive(Debug, Serialize)]
struct JsonResponse {
    id: String,
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ServiceEvent {
    pub event: String,
    #[serde(skip_serializing_if = "Value::is_null")]
    pub data: Value,
}

struct EventQueue {
    state: StdMutex<EventQueueState>,
    notify: Notify,
}

#[derive(Default)]
struct EventQueueState {
    items: VecDeque<ServiceEvent>,
    closed: bool,
}

impl EventQueue {
    fn new() -> Self {
        Self {
            state: StdMutex::new(EventQueueState::default()),
            notify: Notify::new(),
        }
    }

    fn emit(&self, event: ServiceEvent) {
        let mut state = self.state.lock().unwrap_or_else(|lock| lock.into_inner());
        if state.closed {
            return;
        }
        if snapshot_event(&event.event) {
            // A snapshot supersedes any queued snapshot for the same stream, so the
            // newest peer list is always delivered even under back pressure.
            if let Some(queued) = state
                .items
                .iter_mut()
                .find(|queued| same_snapshot_stream(queued, &event))
            {
                *queued = event;
                drop(state);
                self.notify.notify_one();
                return;
            }
            if state.items.len() >= DROPPABLE_EVENT_CAPACITY
                && let Some(index) = state
                    .items
                    .iter()
                    .position(|queued| droppable_event(&queued.event))
            {
                state.items.remove(index);
            }
        } else if droppable_event(&event.event) && state.items.len() >= DROPPABLE_EVENT_CAPACITY {
            return;
        }
        state.items.push_back(event);
        drop(state);
        self.notify.notify_one();
    }

    fn try_pop(&self) -> Option<ServiceEvent> {
        self.state
            .lock()
            .unwrap_or_else(|lock| lock.into_inner())
            .items
            .pop_front()
    }

    async fn pop(&self, timeout: Option<Duration>) -> Option<ServiceEvent> {
        if timeout == Some(Duration::ZERO) {
            return self.try_pop();
        }
        let wait = async {
            loop {
                let notified = self.notify.notified();
                tokio::pin!(notified);
                // Register before inspecting the queue, otherwise a `close()` landing in
                // this window notifies nobody and the consumer waits forever.
                notified.as_mut().enable();
                {
                    let mut state = self.state.lock().unwrap_or_else(|lock| lock.into_inner());
                    if let Some(event) = state.items.pop_front() {
                        return Some(event);
                    }
                    if state.closed {
                        return None;
                    }
                }
                notified.await;
            }
        };
        match timeout {
            Some(timeout) => tokio::time::timeout(timeout, wait).await.ok().flatten(),
            None => wait.await,
        }
    }

    fn clear(&self) {
        self.state
            .lock()
            .unwrap_or_else(|lock| lock.into_inner())
            .items
            .clear();
    }

    fn close(&self) {
        let mut state = self.state.lock().unwrap_or_else(|lock| lock.into_inner());
        state.closed = true;
        drop(state);
        self.notify.notify_waiters();
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.state
            .lock()
            .unwrap_or_else(|lock| lock.into_inner())
            .items
            .len()
    }
}

fn droppable_event(name: &str) -> bool {
    matches!(name, "lan.progress" | "lan.status")
}

/// Events that carry a full replacement view rather than an increment: dropping one
/// silently would leave the UI pinned to a stale list forever.
fn snapshot_event(name: &str) -> bool {
    matches!(name, "lan.peers")
}

fn same_snapshot_stream(left: &ServiceEvent, right: &ServiceEvent) -> bool {
    left.event == right.event && left.data["session_id"] == right.data["session_id"]
}

#[derive(Clone)]
pub struct JsonService {
    inner: Arc<ServiceInner>,
}

struct ServiceInner {
    state: Mutex<ServiceState>,
    events: Arc<EventQueue>,
    cancellation: CancellationToken,
}

#[derive(Default)]
struct ServiceState {
    sessions: HashMap<String, Arc<LanSession>>,
    wormholes: HashMap<String, Arc<WormholeSession>>,
    ssh_relays: HashMap<String, Arc<SshRelaySession>>,
    closed: bool,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct LanStartParams {
    peer_name: String,
    instance_id: String,
    identity_private: String,
    secret: String,
    inbox: String,
    inbox_category_roots: InboxCategoryRoots,
    listen_port: u16,
    capabilities: Vec<String>,
    discovery_targets: Vec<String>,
    disable_mdns: bool,
    disable_udp: bool,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct LanSendParams {
    session_id: String,
    path: String,
    instance_id: String,
    host: String,
    port: u16,
    fingerprint: String,
    endpoint_token: String,
}

struct SendRoutes {
    direct: Vec<PeerEndpoint>,
    relay: Option<Arc<SshRelaySession>>,
    instance_id: String,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct SessionParams {
    session_id: String,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct SendCancelParams {
    session_id: String,
    send_id: String,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct WormholeSettingsParams {
    rendezvous_url: String,
    transit_relay: String,
    timeout_minutes: u32,
}

impl WormholeSettingsParams {
    fn into_settings(self) -> WormholeSettings {
        WormholeSettings {
            rendezvous_url: self.rendezvous_url,
            transit_relay: self.transit_relay,
        }
    }

    fn timeout(&self) -> Duration {
        Duration::from_secs(u64::from(self.timeout_minutes.clamp(1, 60)) * 60)
    }
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct WormholeCreateParams {
    session_id: String,
    paths: Vec<String>,
    settings: WormholeSettingsParams,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct WormholeJoinParams {
    session_id: String,
    code: String,
    settings: WormholeSettingsParams,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct WormholeSessionParams {
    session_id: String,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct SshCheckParams {
    profile: SshProfile,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct SshListenParams {
    session_id: String,
    profile: SshProfile,
    remote_port: u16,
    peers: Vec<SshPeer>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct SshPairJoinParams {
    session_id: String,
    code: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LanPeer {
    pub instance_id: String,
    pub name: String,
    pub host: String,
    pub hosts: Vec<String>,
    pub port: u16,
    pub capabilities: Vec<String>,
    pub fingerprint: String,
    pub public_key: String,
    pub service_name: String,
}

impl From<DiscoveredPeer> for LanPeer {
    fn from(peer: DiscoveredPeer) -> Self {
        Self {
            instance_id: peer.instance_id,
            name: peer.name,
            host: peer.host,
            hosts: peer.hosts,
            port: peer.port,
            capabilities: peer.capabilities,
            fingerprint: peer.fingerprint,
            public_key: peer.public_key,
            service_name: peer.service_name,
        }
    }
}

struct LanSession {
    session_id: String,
    identity: DeviceIdentity,
    shared_secret: String,
    inbox: PathBuf,
    inbox_category_roots: InboxCategoryRoots,
    capabilities: Vec<String>,
    server: Arc<QuicServer>,
    discovery: Option<Arc<UdpDiscovery>>,
    cancellation: CancellationToken,
    sends: Arc<Mutex<SendRegistry>>,
}

#[derive(Debug, Clone, Copy)]
enum ReceiverDecision {
    Accept,
    Reject,
}

struct WormholeSession {
    session_id: String,
    lan_session_id: String,
    cancellation: CancellationToken,
    decision: Mutex<Option<mpsc::Sender<ReceiverDecision>>>,
    handle: Mutex<Option<JoinHandle<()>>>,
}

impl std::fmt::Debug for WormholeSession {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("WormholeSession")
            .field("session_id", &self.session_id)
            .field("lan_session_id", &self.lan_session_id)
            .field("cancelled", &self.cancellation.is_cancelled())
            .finish_non_exhaustive()
    }
}

impl WormholeSession {
    async fn set_handle(&self, handle: JoinHandle<()>) {
        let mut slot = self.handle.lock().await;
        if self.cancellation.is_cancelled() {
            drop(slot);
            handle.abort();
            let _ = handle.await;
        } else {
            *slot = Some(handle);
        }
    }

    async fn close(&self) -> Result<()> {
        self.cancellation.cancel();
        self.decision.lock().await.take();
        let Some(mut handle) = self.handle.lock().await.take() else {
            return Ok(());
        };
        match tokio::time::timeout(Duration::from_secs(5), &mut handle).await {
            Ok(Ok(())) => Ok(()),
            Ok(Err(error)) if error.is_cancelled() => Ok(()),
            Ok(Err(error)) => Err(CoreError::Protocol(format!(
                "short-code task failed: {error}"
            ))),
            Err(_) => {
                handle.abort();
                let _ = handle.await;
                Ok(())
            }
        }
    }

    async fn mark_finished(&self) {
        self.handle.lock().await.take();
    }
}

impl Drop for WormholeSession {
    fn drop(&mut self) {
        self.cancellation.cancel();
        self.decision.get_mut().take();
        if let Some(handle) = self.handle.get_mut().take() {
            handle.abort();
        }
    }
}

#[derive(Default)]
struct SendRegistry {
    closed: bool,
    cancellations: HashMap<String, CancellationToken>,
    handles: Vec<JoinHandle<()>>,
}

impl Default for JsonService {
    fn default() -> Self {
        Self::new()
    }
}

impl JsonService {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(ServiceInner {
                state: Mutex::new(ServiceState::default()),
                events: Arc::new(EventQueue::new()),
                cancellation: CancellationToken::new(),
            }),
        }
    }

    pub async fn call_json(&self, request_json: &str) -> String {
        if request_json.len() > MAX_CONTROL_FRAME {
            return encode_response(JsonResponse {
                id: String::new(),
                ok: false,
                result: None,
                error: Some("invalid request: request is too large".into()),
            });
        }
        let request: JsonRequest = match serde_json::from_str(request_json) {
            Ok(request) => request,
            Err(error) => {
                return encode_response(JsonResponse {
                    id: String::new(),
                    ok: false,
                    result: None,
                    error: Some(format!("invalid request: {error}")),
                });
            }
        };
        let id = request.id.clone();
        if request.id.is_empty() || request.method.is_empty() {
            return encode_response(JsonResponse {
                id,
                ok: false,
                result: None,
                error: Some("id and method are required".into()),
            });
        }
        match self.handle(request.method, request.params).await {
            Ok(result) => encode_response(JsonResponse {
                id,
                ok: true,
                result: Some(result),
                error: None,
            }),
            Err(error) => encode_response(JsonResponse {
                id,
                ok: false,
                result: None,
                error: Some(error.to_string()),
            }),
        }
    }

    pub async fn poll_event(&self, timeout: Option<Duration>) -> Option<String> {
        self.inner
            .events
            .pop(timeout)
            .await
            .map(|event| serde_json::to_string(&event).expect("service events are serializable"))
    }

    pub async fn reset(&self) -> Result<()> {
        let (sessions, wormholes, ssh_relays) = {
            let mut state = self.inner.state.lock().await;
            if state.closed {
                return Err(CoreError::InvalidRequest("service is closed".into()));
            }
            (
                state.sessions.drain().map(|(_, session)| session).collect(),
                state
                    .wormholes
                    .drain()
                    .map(|(_, session)| session)
                    .collect(),
                state
                    .ssh_relays
                    .drain()
                    .map(|(_, session)| session)
                    .collect(),
            )
        };
        close_ssh_relays(ssh_relays).await?;
        close_wormholes(wormholes).await?;
        close_sessions(sessions).await?;
        self.inner.events.clear();
        Ok(())
    }

    pub async fn close(&self) -> Result<()> {
        self.inner.cancellation.cancel();
        let (sessions, wormholes, ssh_relays) = {
            let mut state = self.inner.state.lock().await;
            if state.closed {
                return Ok(());
            }
            state.closed = true;
            (
                state.sessions.drain().map(|(_, session)| session).collect(),
                state
                    .wormholes
                    .drain()
                    .map(|(_, session)| session)
                    .collect(),
                state
                    .ssh_relays
                    .drain()
                    .map(|(_, session)| session)
                    .collect(),
            )
        };
        let ssh_result = close_ssh_relays(ssh_relays).await;
        let wormhole_result = close_wormholes(wormholes).await;
        let lan_result = close_sessions(sessions).await;
        let result = ssh_result.and(wormhole_result).and(lan_result);
        self.inner.events.close();
        result
    }

    async fn handle(&self, method: String, params: Value) -> Result<Value> {
        self.ensure_open().await?;
        match method.as_str() {
            "ping" => Ok(json!({ "protocol": CORE_PROTOCOL_VERSION })),
            "lan.start" => self.start_lan(decode_params(params)?).await,
            "lan.stop" => self.stop_lan(decode_params(params)?).await,
            "lan.peers" => self.lan_peers(decode_params(params)?, false).await,
            "lan.refresh" => self.lan_peers(decode_params(params)?, true).await,
            "lan.send" => self.lan_send(decode_params(params)?).await,
            "lan.send.cancel" => self.lan_send_cancel(decode_params(params)?).await,
            "wormhole.create" => self.wormhole_create(decode_params(params)?).await,
            "wormhole.join.start" => self.wormhole_join(decode_params(params)?).await,
            "wormhole.accept" => self.wormhole_accept(decode_params(params)?).await,
            "wormhole.reject" => self.wormhole_reject(decode_params(params)?).await,
            "ssh.check" => self.ssh_check(decode_params(params)?).await,
            "ssh.listen" => self.ssh_listen(decode_params(params)?).await,
            "ssh.pair.create" => self.ssh_pair_create(decode_params(params)?).await,
            "ssh.pair.join" => self.ssh_pair_join(decode_params(params)?).await,
            "session.cancel" => self.cancel_transport_session(decode_params(params)?).await,
            _ => Err(CoreError::InvalidRequest(format!(
                "unknown method: {method}"
            ))),
        }
    }

    async fn ensure_open(&self) -> Result<()> {
        if self.inner.state.lock().await.closed {
            return Err(CoreError::InvalidRequest("service is closed".into()));
        }
        Ok(())
    }

    async fn start_lan(&self, mut params: LanStartParams) -> Result<Value> {
        params.instance_id = params.instance_id.trim().to_ascii_lowercase();
        if params.peer_name.trim().is_empty()
            || params.inbox.trim().is_empty()
            || !valid_instance_id(&params.instance_id)
        {
            return Err(CoreError::InvalidRequest(
                "peer_name, inbox and a valid instance_id are required".into(),
            ));
        }
        let identity = if params.identity_private.trim().is_empty() {
            DeviceIdentity::generate(Some(&params.instance_id), &params.peer_name)?
        } else {
            DeviceIdentity::from_private_export(
                &params.identity_private,
                Some(&params.instance_id),
                &params.peer_name,
            )?
        };
        let identity_private = identity.export_private()?;
        let inbox = PathBuf::from(&params.inbox);
        let inbox_category_roots = params.inbox_category_roots.clone();
        let capabilities = params.capabilities.clone();
        let discovery_targets = if params.disable_udp {
            Vec::new()
        } else {
            let mut targets = resolve_discovery_targets(&params.discovery_targets).await;
            targets.push(SocketAddr::new(
                IpAddr::V4(Ipv4Addr::BROADCAST),
                UDP_DISCOVERY_PORT,
            ));
            targets.sort_unstable();
            targets.dedup();
            targets
        };
        let session_id = new_id("lan");
        let cancellation = self.inner.cancellation.child_token();
        let callback_events = self.inner.events.clone();
        let callback_cancellation = cancellation.clone();
        let callback_session_id = session_id.clone();
        let callback: TransferEventCallback = Arc::new(move |event| {
            emit_transfer_event(
                &callback_events,
                &callback_cancellation,
                &callback_session_id,
                event,
            );
        });
        let server = Arc::new(
            QuicServer::bind_with_event_handler(
                QuicServerConfig {
                    bind_address: SocketAddr::new(
                        IpAddr::V4(Ipv4Addr::UNSPECIFIED),
                        params.listen_port,
                    ),
                    inbox: inbox.clone(),
                    inbox_category_roots: inbox_category_roots.clone(),
                    identity: identity.clone(),
                    capabilities: capabilities.clone(),
                    shared_secret: params.secret.clone(),
                },
                Some(callback),
            )
            .await?,
        );
        let port = server.local_address().port();
        let fingerprint = server.certificate_fingerprint().to_owned();
        let discovery = if params.disable_udp && params.disable_mdns {
            None
        } else {
            let discovery_events = self.inner.events.clone();
            let discovery_cancellation = cancellation.clone();
            let discovery_session_id = session_id.clone();
            let on_peers: DiscoveryPeersCallback = Arc::new(move |peers| {
                emit_session_event(
                    &discovery_events,
                    &discovery_cancellation,
                    &discovery_session_id,
                    "lan.peers",
                    json!({
                        "peers": peers.into_iter().map(LanPeer::from).collect::<Vec<_>>()
                    }),
                );
            });
            match UdpDiscovery::start(UdpDiscoveryConfig {
                enable_udp: !params.disable_udp,
                enable_mdns: !params.disable_mdns,
                bind_address: SocketAddr::new(
                    IpAddr::V4(Ipv4Addr::UNSPECIFIED),
                    UDP_DISCOVERY_PORT,
                ),
                targets: discovery_targets,
                instance_id: params.instance_id.clone(),
                quic_port: port,
                certificate_fingerprint: fingerprint.clone(),
                shared_secret: params.secret.clone(),
                timings: UdpDiscoveryTimings::default(),
                on_peers: Some(on_peers),
            })
            .await
            {
                Ok(discovery) => Some(Arc::new(discovery)),
                Err(error) => {
                    emit_session_event(
                        &self.inner.events,
                        &cancellation,
                        &session_id,
                        "lan.status",
                        json!({ "message": format!("LAN discovery unavailable: {error}") }),
                    );
                    None
                }
            }
        };
        let session = Arc::new(LanSession {
            session_id: session_id.clone(),
            identity: identity.clone(),
            shared_secret: params.secret,
            inbox,
            inbox_category_roots,
            capabilities,
            server,
            discovery,
            cancellation,
            sends: Arc::new(Mutex::new(SendRegistry::default())),
        });
        let inserted = {
            let mut state = self.inner.state.lock().await;
            if state.closed {
                false
            } else {
                state.sessions.insert(session_id.clone(), session.clone());
                true
            }
        };
        if !inserted {
            session.close().await?;
            return Err(CoreError::InvalidRequest("service is closed".into()));
        }

        let public_key = identity.public_key_base64();
        self.emit_peers(&session).await;
        Ok(json!({
            "session_id": session_id,
            "port": port,
            "instance_id": params.instance_id,
            "fingerprint": fingerprint,
            "public_key": public_key,
            "identity_private": identity_private,
        }))
    }

    async fn stop_lan(&self, params: SessionParams) -> Result<Value> {
        if params.session_id.is_empty() {
            return Err(CoreError::InvalidRequest("session_id is required".into()));
        }
        let (session, relays) = {
            let mut state = self.inner.state.lock().await;
            let session = state.sessions.remove(&params.session_id);
            let relay_ids = state
                .ssh_relays
                .iter()
                .filter(|(_, relay)| relay.lan_session_id() == params.session_id)
                .map(|(id, _)| id.clone())
                .collect::<Vec<_>>();
            let relays = relay_ids
                .into_iter()
                .filter_map(|id| state.ssh_relays.remove(&id))
                .collect();
            (session, relays)
        };
        let stopped = session.is_some();
        close_ssh_relays(relays).await?;
        if let Some(session) = session {
            session.close().await?;
        }
        Ok(json!({ "stopped": stopped }))
    }

    async fn lan_peers(&self, params: SessionParams, emit: bool) -> Result<Value> {
        let session = self.session(&params.session_id).await?;
        if emit && let Some(discovery) = &session.discovery {
            discovery.refresh();
        }
        let peers = self.combined_peers(&session).await;
        if emit {
            emit_session_event(
                &self.inner.events,
                &session.cancellation,
                &session.session_id,
                "lan.peers",
                json!({ "peers": peers }),
            );
        }
        Ok(json!({ "peers": peers }))
    }

    async fn emit_peers(&self, session: &LanSession) {
        emit_session_event(
            &self.inner.events,
            &session.cancellation,
            &session.session_id,
            "lan.peers",
            json!({ "peers": self.combined_peers(session).await }),
        );
    }

    async fn combined_peers(&self, session: &LanSession) -> Vec<LanPeer> {
        let mut peers = session.peer_list().await;
        let relays = self.relays_for_lan(&session.session_id).await;
        for relay in relays {
            for peer in relay.peer_list().await {
                if let Some(direct) = peers
                    .iter_mut()
                    .find(|candidate| candidate.instance_id == peer.instance_id)
                {
                    direct.service_name = "lan+ssh".into();
                    if !direct
                        .capabilities
                        .iter()
                        .any(|value| value == "ssh-relay-v2")
                    {
                        direct.capabilities.push("ssh-relay-v2".into());
                        direct.capabilities.sort();
                    }
                    continue;
                }
                peers.push(LanPeer {
                    instance_id: peer.instance_id,
                    name: peer.name,
                    host: String::new(),
                    hosts: Vec::new(),
                    port: peer.remote_port,
                    capabilities: vec![
                        "blake3".into(),
                        "folder-v1".into(),
                        "quic-v2".into(),
                        "ssh-relay-v2".into(),
                    ],
                    fingerprint: String::new(),
                    public_key: peer.noise_public,
                    service_name: "ssh".into(),
                });
            }
        }
        peers.sort_by(|left, right| {
            left.name
                .cmp(&right.name)
                .then_with(|| left.instance_id.cmp(&right.instance_id))
        });
        peers
    }

    async fn lan_send(&self, params: LanSendParams) -> Result<Value> {
        let session = self.session(&params.session_id).await?;
        if params.path.trim().is_empty() {
            return Err(CoreError::InvalidRequest("path is required".into()));
        }
        let source = PathBuf::from(&params.path);
        let metadata = tokio::fs::metadata(&source).await?;
        let routes = self.resolve_send_routes(&session, &params).await?;
        let send_id = new_id("send");
        let target = if routes.instance_id.is_empty() {
            format!("{}:{}", params.host.trim(), params.port)
        } else {
            routes.instance_id.clone()
        };
        let transfer_id =
            derive_transfer_id(session.identity.instance_id(), &source, &metadata, &target);
        let cancellation = session.cancellation.child_token();
        let mut sends = session.sends.lock().await;
        if sends.closed || session.cancellation.is_cancelled() {
            return Err(CoreError::InvalidRequest("lan session is closed".into()));
        }
        sends
            .cancellations
            .insert(send_id.clone(), cancellation.clone());

        let identity = session.identity.clone();
        let events = self.inner.events.clone();
        let session_cancellation = session.cancellation.clone();
        let session_id = session.session_id.clone();
        let progress_send_id = send_id.clone();
        let completed_send_id = send_id.clone();
        let path = params.path.clone();
        let sends_state = session.sends.clone();
        let on_progress = Arc::new(move |progress: crate::transport::TransferProgress| {
            emit_session_event(
                &events,
                &session_cancellation,
                &session_id,
                "lan.progress",
                json!({
                    "kind": "send",
                    "filename": progress.filename,
                    "done": progress.done,
                    "total": progress.total,
                    "send_id": progress_send_id,
                    "transfer_id": progress.transfer_id,
                }),
            );
        });
        let lifetime = session.cancellation.clone();
        let completion_events = self.inner.events.clone();
        let completion_session_id = session.session_id.clone();
        let shared_secret = session.shared_secret.clone();
        let handle = tokio::spawn(async move {
            let result = send_with_routes(
                &identity,
                &source,
                routes,
                SendFileOptions {
                    transfer_id: Some(transfer_id),
                    shared_secret,
                    cancellation,
                    on_progress: Some(on_progress),
                },
            )
            .await;
            let mut payload = json!({
                "send_id": completed_send_id,
                "path": path,
                "ok": result.is_ok(),
                "transfer_id": transfer_id,
            });
            if let Err(error) = result {
                payload["error"] = Value::String(error.to_string());
            }
            emit_session_event(
                &completion_events,
                &lifetime,
                &completion_session_id,
                "lan.sent",
                payload,
            );
            sends_state
                .lock()
                .await
                .cancellations
                .remove(&completed_send_id);
        });
        sends.handles.retain(|handle| !handle.is_finished());
        sends.handles.push(handle);
        drop(sends);
        Ok(json!({
            "send_id": send_id,
            "transfer_id": transfer_id,
        }))
    }

    async fn lan_send_cancel(&self, params: SendCancelParams) -> Result<Value> {
        let session = self.session(&params.session_id).await?;
        if params.send_id.is_empty() {
            return Err(CoreError::InvalidRequest("send_id is required".into()));
        }
        let cancellation = session
            .sends
            .lock()
            .await
            .cancellations
            .get(&params.send_id)
            .cloned();
        if let Some(cancellation) = cancellation {
            cancellation.cancel();
            Ok(json!({ "cancelled": true }))
        } else {
            Ok(json!({ "cancelled": false }))
        }
    }

    async fn session(&self, session_id: &str) -> Result<Arc<LanSession>> {
        if session_id.is_empty() {
            return Err(CoreError::InvalidRequest("session_id is required".into()));
        }
        self.inner
            .state
            .lock()
            .await
            .sessions
            .get(session_id)
            .cloned()
            .ok_or_else(|| CoreError::InvalidRequest("unknown lan session".into()))
    }

    async fn resolve_send_routes(
        &self,
        session: &LanSession,
        params: &LanSendParams,
    ) -> Result<SendRoutes> {
        let instance_id = params.instance_id.trim().to_ascii_lowercase();
        let discovered = !instance_id.is_empty() && session.peer(&instance_id).await.is_some();
        let direct = if discovered || !params.host.trim().is_empty() {
            resolve_targets(session, params).await?
        } else {
            Vec::new()
        };
        let relay = if instance_id.is_empty() {
            None
        } else {
            self.relay_for_peer(&session.session_id, &instance_id).await
        };
        if direct.is_empty() && relay.is_none() {
            return Err(CoreError::InvalidRequest(
                "target device is not available".into(),
            ));
        }
        Ok(SendRoutes {
            direct,
            relay,
            instance_id,
        })
    }

    async fn ssh_check(&self, params: SshCheckParams) -> Result<Value> {
        let cancellation = self.inner.cancellation.child_token();
        serde_json::to_value(check_ssh(params.profile, &cancellation).await?)
            .map_err(CoreError::from)
    }

    async fn ssh_listen(&self, params: SshListenParams) -> Result<Value> {
        let lan = self.session(&params.session_id).await?;
        if !self.relays_for_lan(&lan.session_id).await.is_empty() {
            return Err(CoreError::InvalidRequest(
                "SSH relay is already active for this LAN session".into(),
            ));
        }
        let relay_session_id = new_id("ssh");
        let event_events = self.inner.events.clone();
        let event_cancellation = lan.cancellation.clone();
        let event_session_id = relay_session_id.clone();
        let event_lan_session_id = lan.session_id.clone();
        let on_event = Arc::new(move |event: crate::ssh::SshRelayEvent| {
            let mut data = event.data;
            if !data.is_object() {
                data = Value::Object(Map::new());
            }
            data.as_object_mut()
                .expect("SSH event data was initialized as an object")
                .insert(
                    "lan_session_id".into(),
                    Value::String(event_lan_session_id.clone()),
                );
            emit_session_event(
                &event_events,
                &event_cancellation,
                &event_session_id,
                event.event,
                data,
            );
        });
        let transfer_events = self.inner.events.clone();
        let transfer_cancellation = lan.cancellation.clone();
        let transfer_session_id = lan.session_id.clone();
        let on_transfer: TransferEventCallback = Arc::new(move |event| {
            emit_transfer_event(
                &transfer_events,
                &transfer_cancellation,
                &transfer_session_id,
                event,
            );
        });
        let start = SshRelaySession::start(SshRelayConfig {
            lan_session_id: lan.session_id.clone(),
            profile: params.profile,
            requested_port: params.remote_port,
            identity: lan.identity.clone(),
            inbox: lan.inbox.clone(),
            inbox_category_roots: lan.inbox_category_roots.clone(),
            capabilities: lan.capabilities.clone(),
            peers: params.peers,
            cancellation: lan.cancellation.clone(),
            on_event: Some(on_event),
            on_transfer: Some(on_transfer),
        })
        .await?;
        let peers = start.session.peer_list().await;
        let remote_port = start.session.remote_port();
        let public_key = lan.identity.public_key_base64();
        let inserted = {
            let mut state = self.inner.state.lock().await;
            if state.closed
                || state
                    .ssh_relays
                    .values()
                    .any(|relay| relay.lan_session_id() == lan.session_id)
            {
                false
            } else {
                state
                    .ssh_relays
                    .insert(relay_session_id.clone(), start.session.clone());
                true
            }
        };
        if !inserted {
            start.session.close().await?;
            return Err(CoreError::InvalidRequest(
                "SSH relay could not be registered".into(),
            ));
        }
        let mut result = json!({
            "session_id": relay_session_id,
            "remote_port": remote_port,
            "noise_public": public_key,
            "peers": peers,
            "connected": start.connected,
        });
        if let Some(error) = start.initial_error {
            result["error"] = Value::String(error);
        }
        Ok(result)
    }

    async fn ssh_pair_create(&self, params: SessionParams) -> Result<Value> {
        let relay = self.ssh_relay(&params.session_id).await?;
        let code = relay.create_pairing().await?;
        Ok(json!({ "code": code }))
    }

    async fn ssh_pair_join(&self, params: SshPairJoinParams) -> Result<Value> {
        if params.code.trim().is_empty() {
            return Err(CoreError::InvalidRequest(
                "SSH pairing code is required".into(),
            ));
        }
        let relay = self.ssh_relay(&params.session_id).await?;
        let peer = relay.join_pairing(&params.code).await?;
        Ok(json!({ "peer": peer }))
    }

    async fn ssh_relay(&self, session_id: &str) -> Result<Arc<SshRelaySession>> {
        if session_id.is_empty() {
            return Err(CoreError::InvalidRequest("session_id is required".into()));
        }
        self.inner
            .state
            .lock()
            .await
            .ssh_relays
            .get(session_id)
            .cloned()
            .ok_or_else(|| CoreError::InvalidRequest("unknown SSH relay session".into()))
    }

    async fn relays_for_lan(&self, lan_session_id: &str) -> Vec<Arc<SshRelaySession>> {
        self.inner
            .state
            .lock()
            .await
            .ssh_relays
            .values()
            .filter(|relay| relay.lan_session_id() == lan_session_id)
            .cloned()
            .collect()
    }

    async fn relay_for_peer(
        &self,
        lan_session_id: &str,
        instance_id: &str,
    ) -> Option<Arc<SshRelaySession>> {
        let relays = self.relays_for_lan(lan_session_id).await;
        for relay in relays {
            if relay.has_peer(instance_id).await {
                return Some(relay);
            }
        }
        None
    }
}

impl JsonService {
    async fn wormhole_create(&self, params: WormholeCreateParams) -> Result<Value> {
        let lan = self.session(&params.session_id).await?;
        let timeout = params.settings.timeout();
        let settings = params.settings.into_settings();
        let paths = normalize_wormhole_paths(params.paths)?;
        let cancellation = lan.cancellation.child_token();
        let summary = summarize_paths(
            lan.identity.peer_name(),
            lan.identity.instance_id(),
            &paths,
            cancellation.child_token(),
        )
        .await?;
        let (code, sender) = SenderSession::create(settings, summary, cancellation.clone()).await?;
        let session_id = new_id("wh");
        let send_ids = (0..paths.len()).map(|_| new_id("send")).collect::<Vec<_>>();
        let session = Arc::new(WormholeSession {
            session_id: session_id.clone(),
            lan_session_id: lan.session_id.clone(),
            cancellation: cancellation.clone(),
            decision: Mutex::new(None),
            handle: Mutex::new(None),
        });
        self.insert_wormhole(session.clone()).await?;

        let inner = self.inner.clone();
        let task_session_id = session_id.clone();
        let task_cancellation = cancellation.clone();
        let handle = tokio::spawn(async move {
            let result = tokio::time::timeout(
                timeout,
                run_wormhole_sender(
                    inner.clone(),
                    task_session_id.clone(),
                    lan,
                    sender,
                    paths,
                    send_ids,
                    task_cancellation.clone(),
                ),
            )
            .await;
            finish_wormhole_task(
                &inner,
                &task_session_id,
                &task_cancellation,
                timeout_result(result),
            )
            .await;
        });
        session.set_handle(handle).await;

        let expires_at = (time::OffsetDateTime::now_utc()
            + time::Duration::seconds(timeout.as_secs() as i64))
        .format(&time::format_description::well_known::Rfc3339)
        .map_err(|error| CoreError::Protocol(format!("format short-code expiry: {error}")))?;
        Ok(json!({
            "session_id": session_id,
            "code": code,
            "uri": format!("inkhole://receive?code={code}"),
            "expires_at": expires_at,
        }))
    }

    async fn wormhole_join(&self, params: WormholeJoinParams) -> Result<Value> {
        let lan = self.session(&params.session_id).await?;
        let code = params.code.trim().to_owned();
        if code.is_empty() {
            return Err(CoreError::InvalidRequest("short code is required".into()));
        }
        let timeout = params.settings.timeout();
        let settings = params.settings.into_settings();
        let cancellation = lan.cancellation.child_token();
        let session_id = new_id("wh");
        let (decision_tx, decision_rx) = mpsc::channel(1);
        let session = Arc::new(WormholeSession {
            session_id: session_id.clone(),
            lan_session_id: lan.session_id.clone(),
            cancellation: cancellation.clone(),
            decision: Mutex::new(Some(decision_tx)),
            handle: Mutex::new(None),
        });
        self.insert_wormhole(session.clone()).await?;

        let inner = self.inner.clone();
        let task_session_id = session_id.clone();
        let task_cancellation = cancellation.clone();
        let handle = tokio::spawn(async move {
            let result = tokio::time::timeout(
                timeout,
                run_wormhole_receiver(
                    inner.clone(),
                    task_session_id.clone(),
                    lan,
                    settings,
                    code,
                    decision_rx,
                    task_cancellation.clone(),
                ),
            )
            .await;
            finish_wormhole_task(
                &inner,
                &task_session_id,
                &task_cancellation,
                timeout_result(result),
            )
            .await;
        });
        session.set_handle(handle).await;
        Ok(json!({ "session_id": session_id }))
    }

    async fn wormhole_accept(&self, params: WormholeSessionParams) -> Result<Value> {
        self.send_wormhole_decision(&params.session_id, ReceiverDecision::Accept)
            .await?;
        Ok(json!({ "accepted": true }))
    }

    async fn wormhole_reject(&self, params: WormholeSessionParams) -> Result<Value> {
        self.send_wormhole_decision(&params.session_id, ReceiverDecision::Reject)
            .await?;
        Ok(json!({ "rejected": true }))
    }

    async fn cancel_transport_session(&self, params: WormholeSessionParams) -> Result<Value> {
        if params.session_id.is_empty() {
            return Err(CoreError::InvalidRequest("session_id is required".into()));
        }
        let (wormhole, relay) = {
            let mut state = self.inner.state.lock().await;
            (
                state.wormholes.remove(&params.session_id),
                state.ssh_relays.remove(&params.session_id),
            )
        };
        let cancelled = wormhole.is_some() || relay.is_some();
        if let Some(session) = wormhole {
            session.close().await?;
        }
        if let Some(relay) = relay {
            relay.close().await?;
        }
        Ok(json!({ "cancelled": cancelled }))
    }

    async fn insert_wormhole(&self, session: Arc<WormholeSession>) -> Result<()> {
        let mut state = self.inner.state.lock().await;
        if state.closed {
            return Err(CoreError::InvalidRequest("service is closed".into()));
        }
        state.wormholes.insert(session.session_id.clone(), session);
        Ok(())
    }

    async fn send_wormhole_decision(
        &self,
        session_id: &str,
        decision: ReceiverDecision,
    ) -> Result<()> {
        if session_id.is_empty() {
            return Err(CoreError::InvalidRequest("session_id is required".into()));
        }
        let session = self
            .inner
            .state
            .lock()
            .await
            .wormholes
            .get(session_id)
            .cloned()
            .ok_or_else(|| CoreError::InvalidRequest("wormhole offer not found".into()))?;
        let sender =
            session.decision.lock().await.take().ok_or_else(|| {
                CoreError::InvalidRequest("wormhole offer is already handled".into())
            })?;
        sender
            .send(decision)
            .await
            .map_err(|_| CoreError::InvalidRequest("wormhole offer is no longer active".into()))
    }
}

async fn run_wormhole_sender(
    inner: Arc<ServiceInner>,
    wormhole_session_id: String,
    lan: Arc<LanSession>,
    sender: SenderSession,
    paths: Vec<PathBuf>,
    send_ids: Vec<String>,
    cancellation: CancellationToken,
) -> Result<()> {
    let SenderConnection {
        peer,
        shared_secret,
        tunnel,
    } = sender.connect().await?;
    let send_tokens = send_ids
        .iter()
        .map(|send_id| (send_id.clone(), cancellation.child_token()))
        .collect::<Vec<_>>();
    // Held for the rest of the function: if an outer `timeout` drops this future the
    // guard still unregisters the tokens, so `lan.send.cancel` cannot keep reporting
    // success for send ids that no longer have a running transfer.
    let _tokens = SendTokenGuard::register(&lan, &send_tokens).await?;
    emit_session_event(
        &inner.events,
        &cancellation,
        &wormhole_session_id,
        "wormhole.ready",
        json!({
            "role": "sender",
            "send_ids": send_ids,
            "item_count": paths.len(),
        }),
    );

    for ((path, send_id), (_, send_cancellation)) in paths
        .into_iter()
        .zip(send_ids.iter())
        .zip(send_tokens.iter())
    {
        execute_wormhole_send(
            &inner.events,
            &lan,
            send_id,
            path,
            &peer,
            &shared_secret,
            send_cancellation.clone(),
        )
        .await;
        let _ = tunnel.reset_sender_peer().await;
    }
    tunnel.close().await
}

/// RAII registration for the per-item cancellation tokens of a wormhole send. Cleanup
/// has to survive the future being dropped mid-flight (an outer `timeout` never runs the
/// tail of a function), otherwise dead send ids linger in the registry and
/// `lan.send.cancel` answers `cancelled: true` for transfers that no longer exist.
struct SendTokenGuard {
    sends: Arc<Mutex<SendRegistry>>,
    send_ids: Vec<String>,
}

impl SendTokenGuard {
    async fn register(lan: &LanSession, tokens: &[(String, CancellationToken)]) -> Result<Self> {
        let mut registry = lan.sends.lock().await;
        if registry.closed || lan.cancellation.is_cancelled() {
            return Err(CoreError::InvalidRequest("lan session is closed".into()));
        }
        for (send_id, token) in tokens {
            registry
                .cancellations
                .insert(send_id.clone(), token.clone());
        }
        drop(registry);
        Ok(Self {
            sends: lan.sends.clone(),
            send_ids: tokens.iter().map(|(send_id, _)| send_id.clone()).collect(),
        })
    }
}

impl Drop for SendTokenGuard {
    fn drop(&mut self) {
        let send_ids = std::mem::take(&mut self.send_ids);
        if send_ids.is_empty() {
            return;
        }
        // `Drop` cannot await, so take the uncontended path when it is available and
        // otherwise finish the removal on the runtime.
        if let Ok(mut registry) = self.sends.try_lock() {
            for send_id in &send_ids {
                registry.cancellations.remove(send_id);
            }
            return;
        }
        let sends = self.sends.clone();
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                let mut registry = sends.lock().await;
                for send_id in &send_ids {
                    registry.cancellations.remove(send_id);
                }
            });
        }
    }
}

async fn execute_wormhole_send(
    events: &Arc<EventQueue>,
    lan: &LanSession,
    send_id: &str,
    path: PathBuf,
    peer: &PeerEndpoint,
    shared_secret: &str,
    cancellation: CancellationToken,
) {
    // Keyed on the peer certificate rather than its address: a wormhole tunnel gets a
    // fresh ephemeral port every session, but resuming the same file must reuse the id.
    let transfer_id = match tokio::fs::metadata(&path).await {
        Ok(metadata) => derive_transfer_id(
            lan.identity.instance_id(),
            &path,
            &metadata,
            &peer.certificate_fingerprint,
        ),
        Err(_) => Uuid::new_v4(),
    };
    let progress_events = events.clone();
    let progress_lifetime = lan.cancellation.clone();
    let progress_session = lan.session_id.clone();
    let progress_send_id = send_id.to_owned();
    let on_progress = Arc::new(move |progress: crate::transport::TransferProgress| {
        emit_session_event(
            &progress_events,
            &progress_lifetime,
            &progress_session,
            "lan.progress",
            json!({
                "kind": "send",
                "filename": progress.filename,
                "done": progress.done,
                "total": progress.total,
                "send_id": progress_send_id,
                "transfer_id": progress.transfer_id,
            }),
        );
    });
    let result = send_file(
        &lan.identity,
        peer,
        &path,
        SendFileOptions {
            transfer_id: Some(transfer_id),
            shared_secret: shared_secret.to_owned(),
            cancellation,
            on_progress: Some(on_progress),
        },
    )
    .await;
    let mut payload = json!({
        "send_id": send_id,
        "path": path.to_string_lossy(),
        "ok": result.is_ok(),
        "transfer_id": transfer_id,
    });
    if let Err(error) = result {
        payload["error"] = Value::String(error.to_string());
    }
    emit_session_event(
        events,
        &lan.cancellation,
        &lan.session_id,
        "lan.sent",
        payload,
    );
}

async fn run_wormhole_receiver(
    inner: Arc<ServiceInner>,
    wormhole_session_id: String,
    lan: Arc<LanSession>,
    settings: WormholeSettings,
    code: String,
    mut decision: mpsc::Receiver<ReceiverDecision>,
    cancellation: CancellationToken,
) -> Result<()> {
    let offer = ReceivedOffer::join(settings, &code, cancellation.child_token()).await?;
    emit_session_event(
        &inner.events,
        &cancellation,
        &wormhole_session_id,
        "wormhole.offer",
        json!({ "summary": offer.summary() }),
    );
    let selected = tokio::select! {
        _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
        selected = decision.recv() => selected
            .ok_or_else(|| CoreError::InvalidRequest("wormhole offer was cancelled".into()))?,
    };
    if matches!(selected, ReceiverDecision::Reject) {
        return offer.reject("transfer rejected").await;
    }

    let secret = temporary_secret();
    let callback_events = inner.events.clone();
    let callback_cancellation = cancellation.clone();
    let callback_session_id = lan.session_id.clone();
    let callback: TransferEventCallback = Arc::new(move |event| {
        emit_transfer_event(
            &callback_events,
            &callback_cancellation,
            &callback_session_id,
            event,
        );
    });
    let server = QuicServer::bind_with_event_handler(
        QuicServerConfig {
            bind_address: SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0),
            inbox: lan.inbox.clone(),
            inbox_category_roots: lan.inbox_category_roots.clone(),
            identity: lan.identity.clone(),
            capabilities: lan.capabilities.clone(),
            shared_secret: secret.clone(),
        },
        Some(callback),
    )
    .await?;
    let tunnel = offer
        .accept(
            server.local_address(),
            server.certificate_fingerprint().to_owned(),
            secret,
        )
        .await?;
    emit_session_event(
        &inner.events,
        &cancellation,
        &wormhole_session_id,
        "wormhole.ready",
        json!({ "role": "receiver" }),
    );
    let tunnel_result = tunnel.wait().await;
    let server_result = server.close().await;
    tunnel_result.and(server_result)
}

fn timeout_result(
    result: std::result::Result<Result<()>, tokio::time::error::Elapsed>,
) -> Result<()> {
    result.unwrap_or_else(|_| Err(CoreError::Protocol("short-code session expired".into())))
}

async fn finish_wormhole_task(
    inner: &Arc<ServiceInner>,
    session_id: &str,
    cancellation: &CancellationToken,
    result: Result<()>,
) {
    if let Err(error) = result
        && !cancellation.is_cancelled()
    {
        emit_session_event(
            &inner.events,
            cancellation,
            session_id,
            "wormhole.error",
            json!({ "error": error.to_string() }),
        );
    }
    let session = inner.state.lock().await.wormholes.remove(session_id);
    if let Some(session) = session {
        session.mark_finished().await;
    }
}

fn normalize_wormhole_paths(paths: Vec<String>) -> Result<Vec<PathBuf>> {
    if paths.is_empty() || paths.len() > 256 {
        return Err(CoreError::InvalidRequest(
            "one to 256 source paths are required".into(),
        ));
    }
    let mut normalized = Vec::with_capacity(paths.len());
    for raw in paths {
        let value = raw.trim();
        if value.is_empty() {
            return Err(CoreError::InvalidRequest("source path is empty".into()));
        }
        let path = PathBuf::from(value);
        #[cfg(windows)]
        let duplicate = normalized.iter().any(|existing: &PathBuf| {
            existing
                .to_string_lossy()
                .eq_ignore_ascii_case(&path.to_string_lossy())
        });
        #[cfg(not(windows))]
        let duplicate = normalized.contains(&path);
        if !duplicate {
            normalized.push(path);
        }
    }
    if normalized.is_empty() {
        return Err(CoreError::InvalidRequest("source paths are empty".into()));
    }
    Ok(normalized)
}

impl LanSession {
    async fn peer_list(&self) -> Vec<LanPeer> {
        self.discovery
            .as_ref()
            .map(|discovery| discovery.peers().into_iter().map(LanPeer::from).collect())
            .unwrap_or_default()
    }

    async fn peer(&self, instance_id: &str) -> Option<LanPeer> {
        self.peer_list()
            .await
            .into_iter()
            .find(|peer| peer.instance_id == instance_id)
    }

    async fn close(&self) -> Result<()> {
        self.cancellation.cancel();
        let handles = {
            let mut sends = self.sends.lock().await;
            if sends.closed {
                return Ok(());
            }
            sends.closed = true;
            for cancellation in sends.cancellations.values() {
                cancellation.cancel();
            }
            sends.cancellations.clear();
            std::mem::take(&mut sends.handles)
        };

        let mut first_error = None;
        if let Some(discovery) = &self.discovery
            && let Err(error) = discovery.close().await
        {
            first_error = Some(error);
        }
        if let Err(error) = self.server.close().await
            && first_error.is_none()
        {
            first_error = Some(error);
        }
        for handle in handles {
            if let Err(error) = handle.await
                && first_error.is_none()
            {
                first_error = Some(CoreError::Protocol(format!("send task failed: {error}")));
            }
        }
        if let Some(error) = first_error {
            Err(error)
        } else {
            Ok(())
        }
    }
}

impl Drop for LanSession {
    fn drop(&mut self) {
        self.cancellation.cancel();
        if let Some(discovery) = &self.discovery {
            discovery.close_immediately();
        }
        self.server.close_immediately();
    }
}

impl Drop for ServiceInner {
    fn drop(&mut self) {
        self.cancellation.cancel();
        self.events.close();
        for session in self.state.get_mut().sessions.values() {
            session.cancellation.cancel();
            if let Some(discovery) = &session.discovery {
                discovery.close_immediately();
            }
            session.server.close_immediately();
        }
        for session in self.state.get_mut().wormholes.values_mut() {
            session.cancellation.cancel();
            if let Some(session) = Arc::get_mut(session) {
                session.decision.get_mut().take();
                if let Some(handle) = session.handle.get_mut().take() {
                    handle.abort();
                }
            }
        }
        for relay in self.state.get_mut().ssh_relays.values() {
            relay.close_immediately();
        }
    }
}

async fn close_sessions(sessions: Vec<Arc<LanSession>>) -> Result<()> {
    let mut first_error = None;
    for session in sessions {
        if let Err(error) = session.close().await
            && first_error.is_none()
        {
            first_error = Some(error);
        }
    }
    if let Some(error) = first_error {
        Err(error)
    } else {
        Ok(())
    }
}

async fn close_wormholes(sessions: Vec<Arc<WormholeSession>>) -> Result<()> {
    let mut first_error = None;
    for session in sessions {
        if let Err(error) = session.close().await
            && first_error.is_none()
        {
            first_error = Some(error);
        }
    }
    if let Some(error) = first_error {
        Err(error)
    } else {
        Ok(())
    }
}

async fn close_ssh_relays(sessions: Vec<Arc<SshRelaySession>>) -> Result<()> {
    let mut first_error = None;
    for session in sessions {
        if let Err(error) = session.close().await
            && first_error.is_none()
        {
            first_error = Some(error);
        }
    }
    if let Some(error) = first_error {
        Err(error)
    } else {
        Ok(())
    }
}

async fn send_with_routes(
    identity: &DeviceIdentity,
    source: &Path,
    routes: SendRoutes,
    options: SendFileOptions,
) -> Result<()> {
    let mut direct_error = None;
    for peer in routes.direct {
        match send_file(identity, &peer, source, options.clone()).await {
            Ok(_) => return Ok(()),
            Err(error) if !relay_fallback_allowed(&error) => {
                return Err(error);
            }
            Err(error) => direct_error = Some(error),
        }
    }
    let Some(relay) = routes.relay else {
        return Err(direct_error.unwrap_or_else(|| {
            CoreError::InvalidRequest("target device is not available".into())
        }));
    };
    let connection = relay
        .connect_sender(&routes.instance_id, options.cancellation.child_token())
        .await
        .map_err(|relay_error| {
            if let Some(direct_error) = direct_error {
                CoreError::Protocol(format!(
                    "LAN route failed ({direct_error}); SSH relay failed ({relay_error})"
                ))
            } else {
                relay_error
            }
        })?;
    let mut relay_options = options;
    relay_options.shared_secret = connection.shared_secret;
    let send_result = send_file(identity, &connection.peer, source, relay_options)
        .await
        .map(|_| ());
    let close_result = connection.tunnel.close().await;
    send_result.and(close_result)
}

/// Derives a stable transfer id from the sender, the source file and the destination.
/// Resending the same unchanged file to the same peer reproduces the id, which is what
/// lets the receiver resume its `.part` file instead of starting over.
fn derive_transfer_id(
    instance_id: &str,
    source: &Path,
    metadata: &std::fs::Metadata,
    target: &str,
) -> Uuid {
    let modified_ms = metadata
        .modified()
        .ok()
        .and_then(|modified| modified.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|elapsed| elapsed.as_millis() as u64)
        .unwrap_or_default();
    let mut hasher = blake3::Hasher::new();
    for field in [
        instance_id.as_bytes(),
        source.to_string_lossy().as_bytes(),
        target.as_bytes(),
    ] {
        hasher.update(&(field.len() as u64).to_le_bytes());
        hasher.update(field);
    }
    hasher.update(&metadata.len().to_le_bytes());
    hasher.update(&modified_ms.to_le_bytes());
    let mut bytes = [0_u8; 16];
    bytes.copy_from_slice(&hasher.finalize().as_bytes()[..16]);
    uuid::Builder::from_random_bytes(bytes).into_uuid()
}

fn relay_fallback_allowed(error: &CoreError) -> bool {
    !matches!(
        error,
        CoreError::Cancelled
            | CoreError::InvalidRequest(_)
            | CoreError::InvalidTransfer(_)
            | CoreError::DigestMismatch { .. }
    )
}

async fn resolve_targets(
    session: &LanSession,
    params: &LanSendParams,
) -> Result<Vec<PeerEndpoint>> {
    let instance_id = params.instance_id.trim().to_ascii_lowercase();
    if !instance_id.is_empty()
        && let Some(peer) = session.peer(&instance_id).await
    {
        let mut hosts = peer.hosts;
        if hosts.is_empty() {
            hosts.push(peer.host);
        }
        let mut endpoints = Vec::with_capacity(hosts.len());
        let mut first_error = None;
        for host in hosts {
            match resolve_address(&host, peer.port).await {
                Ok(address)
                    if !endpoints
                        .iter()
                        .any(|endpoint: &PeerEndpoint| endpoint.address == address) =>
                {
                    endpoints.push(PeerEndpoint {
                        address,
                        certificate_fingerprint: peer.fingerprint.clone(),
                    });
                }
                Ok(_) => {}
                Err(error) => {
                    first_error.get_or_insert(error);
                }
            }
        }
        if endpoints.is_empty() {
            return Err(first_error.unwrap_or_else(|| {
                CoreError::InvalidRequest("target device is not available".into())
            }));
        }
        return Ok(endpoints);
    }
    if params.host.trim().is_empty() || params.port == 0 {
        return Err(CoreError::InvalidRequest(
            "target device is not available".into(),
        ));
    }
    if params.fingerprint.trim().is_empty() {
        return Err(CoreError::InvalidRequest(
            "target certificate fingerprint is required".into(),
        ));
    }
    let _endpoint_token = &params.endpoint_token;
    Ok(vec![PeerEndpoint {
        address: resolve_address(&params.host, params.port).await?,
        certificate_fingerprint: params.fingerprint.trim().to_ascii_lowercase(),
    }])
}

async fn resolve_address(host: &str, port: u16) -> Result<SocketAddr> {
    let host = host.trim();
    let host = host
        .strip_prefix('[')
        .and_then(|value| value.strip_suffix(']'))
        .unwrap_or(host);
    if let Ok(address) = host.parse::<IpAddr>() {
        return Ok(SocketAddr::new(address, port));
    }
    tokio::net::lookup_host((host, port))
        .await?
        .next()
        .ok_or_else(|| CoreError::InvalidRequest("target host did not resolve".into()))
}

async fn resolve_discovery_targets(hosts: &[String]) -> Vec<SocketAddr> {
    let mut targets = Vec::new();
    for raw_host in hosts.iter().take(64) {
        let host = raw_host.trim();
        let host = host
            .strip_prefix('[')
            .and_then(|value| value.strip_suffix(']'))
            .unwrap_or(host);
        if host.is_empty() {
            continue;
        }
        if let Ok(IpAddr::V4(address)) = host.parse::<IpAddr>() {
            targets.push(SocketAddr::new(IpAddr::V4(address), UDP_DISCOVERY_PORT));
            continue;
        }
        match tokio::net::lookup_host((host, UDP_DISCOVERY_PORT)).await {
            Ok(addresses) => targets.extend(addresses.filter(SocketAddr::is_ipv4)),
            Err(error) => tracing::debug!(%host, %error, "manual discovery target did not resolve"),
        }
    }
    targets.sort_unstable();
    targets.dedup();
    targets
}

fn emit_transfer_event(
    events: &EventQueue,
    cancellation: &CancellationToken,
    session_id: &str,
    event: TransferEvent,
) {
    match event {
        TransferEvent::Progress(progress) => emit_session_event(
            events,
            cancellation,
            session_id,
            "lan.progress",
            json!({
                "kind": "recv",
                "filename": progress.filename,
                "done": progress.done,
                "total": progress.total,
                "transfer_id": progress.transfer_id,
            }),
        ),
        TransferEvent::Received {
            transfer_id,
            path,
            filename,
            size,
            blake3,
            sender_instance_id,
            sender_name,
        } => emit_session_event(
            events,
            cancellation,
            session_id,
            "lan.received",
            json!({
                "path": path.to_string_lossy(),
                "filename": filename,
                "size": size,
                "blake3": blake3,
                "transfer_id": transfer_id,
                "sender_instance_id": sender_instance_id,
                "sender_name": sender_name,
            }),
        ),
        TransferEvent::Status { message } => emit_session_event(
            events,
            cancellation,
            session_id,
            "lan.status",
            json!({ "message": message }),
        ),
    }
}

fn emit_session_event(
    events: &EventQueue,
    cancellation: &CancellationToken,
    session_id: &str,
    event: &str,
    mut data: Value,
) {
    if cancellation.is_cancelled() {
        return;
    }
    if !data.is_object() {
        data = Value::Object(Map::new());
    }
    data.as_object_mut()
        .expect("JSON object was initialized")
        .insert("session_id".into(), Value::String(session_id.to_owned()));
    events.emit(ServiceEvent {
        event: event.to_owned(),
        data,
    });
}

fn decode_params<T: DeserializeOwned>(params: Value) -> Result<T> {
    if params.is_null() {
        return Err(CoreError::InvalidRequest("missing params".into()));
    }
    serde_json::from_value(params)
        .map_err(|error| CoreError::InvalidRequest(format!("invalid params: {error}")))
}

fn encode_response(response: JsonResponse) -> String {
    match serde_json::to_string(&response) {
        Ok(encoded) => encoded,
        Err(error) => serde_json::json!({
            "id": "",
            "ok": false,
            "error": format!("response encoding failed: {error}"),
        })
        .to_string(),
    }
}

fn new_id(prefix: &str) -> String {
    format!("{prefix}-{}", Uuid::new_v4().simple())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    use crate::wormhole::test_support::MockRendezvous;
    use tokio::net::TcpListener;

    async fn call(service: &JsonService, method: &str, params: Value) -> Value {
        let response: Value = serde_json::from_str(
            &service
                .call_json(
                    &json!({
                        "id": format!("test-{method}"),
                        "method": method,
                        "params": params,
                    })
                    .to_string(),
                )
                .await,
        )
        .unwrap();
        assert_eq!(response["ok"], true, "{response}");
        response["result"].clone()
    }

    async fn wait_event(service: &JsonService, name: &str) -> Value {
        loop {
            let encoded = service
                .poll_event(Some(Duration::from_secs(10)))
                .await
                .unwrap_or_else(|| panic!("event {name} did not arrive"));
            let event: Value = serde_json::from_str(&encoded).unwrap();
            if event["event"] == name {
                return event["data"].clone();
            }
        }
    }

    async fn next_event(service: &JsonService) -> ServiceEvent {
        let encoded = service
            .poll_event(Some(Duration::from_secs(10)))
            .await
            .expect("service event did not arrive");
        serde_json::from_str(&encoded).unwrap()
    }

    async fn wait_wormhole_finished(service: &JsonService, session_id: &str) {
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                if !service
                    .inner
                    .state
                    .lock()
                    .await
                    .wormholes
                    .contains_key(session_id)
                {
                    return;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("short-code session did not finish");
    }

    async fn wait_for_peer(
        service: &JsonService,
        session_id: &Value,
        instance_id: &str,
        present: bool,
    ) -> Option<Value> {
        tokio::time::timeout(Duration::from_secs(8), async {
            loop {
                let result =
                    call(service, "lan.refresh", json!({ "session_id": session_id })).await;
                let peer = result["peers"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|peer| peer["instance_id"] == instance_id)
                    .cloned();
                if peer.is_some() == present {
                    return peer;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("peer {instance_id} presence did not become {present}"))
    }

    #[tokio::test]
    async fn critical_events_survive_a_full_snapshot_queue() {
        let queue = EventQueue::new();
        for index in 0..DROPPABLE_EVENT_CAPACITY * 4 {
            queue.emit(ServiceEvent {
                event: "lan.progress".into(),
                data: json!({ "done": index }),
            });
        }
        assert_eq!(queue.len(), DROPPABLE_EVENT_CAPACITY);
        queue.emit(ServiceEvent {
            event: "lan.sent".into(),
            data: json!({ "ok": true }),
        });
        assert_eq!(queue.len(), DROPPABLE_EVENT_CAPACITY + 1);

        loop {
            let event = queue.pop(Some(Duration::ZERO)).await.unwrap();
            if event.event == "lan.sent" {
                assert_eq!(event.data["ok"], true);
                break;
            }
        }
    }

    #[tokio::test]
    async fn dropping_a_wormhole_send_unregisters_its_cancel_tokens() {
        let sends = Arc::new(Mutex::new(SendRegistry::default()));
        let register = || async {
            sends
                .lock()
                .await
                .cancellations
                .insert("send-1".to_owned(), CancellationToken::new());
            SendTokenGuard {
                sends: sends.clone(),
                send_ids: vec!["send-1".to_owned()],
            }
        };

        // The uncontended path: dropping the future (what an outer `timeout` does)
        // unregisters immediately.
        drop(register().await);
        assert!(sends.lock().await.cancellations.is_empty());

        // The contended path: the removal is deferred to the runtime instead of lost.
        let guard = register().await;
        let held = sends.clone().lock_owned().await;
        drop(guard);
        drop(held);
        tokio::time::timeout(Duration::from_secs(2), async {
            while !sends.lock().await.cancellations.is_empty() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("deferred token cleanup never ran");
    }

    #[tokio::test]
    async fn transfer_ids_are_stable_across_resends_of_the_same_file() {
        let root = tempfile::tempdir().unwrap();
        let source = root.path().join("resumable.bin");
        tokio::fs::write(&source, b"resume me").await.unwrap();
        let metadata = tokio::fs::metadata(&source).await.unwrap();
        let sender = "12121212121212121212121212121212";
        let target = "34343434343434343434343434343434";

        let first = derive_transfer_id(sender, &source, &metadata, target);
        assert_eq!(
            first,
            derive_transfer_id(sender, &source, &metadata, target),
            "resending the same file must reuse the id so the receiver can resume"
        );
        assert_ne!(
            first,
            derive_transfer_id(sender, &source, &metadata, "other")
        );

        let other = root.path().join("other.bin");
        tokio::fs::write(&other, b"resume me").await.unwrap();
        let other_metadata = tokio::fs::metadata(&other).await.unwrap();
        assert_ne!(
            first,
            derive_transfer_id(sender, &other, &other_metadata, target)
        );

        tokio::fs::write(&source, b"changed contents")
            .await
            .unwrap();
        let changed = tokio::fs::metadata(&source).await.unwrap();
        assert_ne!(first, derive_transfer_id(sender, &source, &changed, target));
    }

    #[tokio::test]
    async fn a_full_queue_still_delivers_the_newest_peer_snapshot() {
        let queue = EventQueue::new();
        for index in 0..DROPPABLE_EVENT_CAPACITY * 2 {
            queue.emit(ServiceEvent {
                event: "lan.progress".into(),
                data: json!({ "session_id": "s1", "done": index }),
            });
        }
        for generation in 0..8 {
            queue.emit(ServiceEvent {
                event: "lan.peers".into(),
                data: json!({ "session_id": "s1", "generation": generation }),
            });
        }
        queue.emit(ServiceEvent {
            event: "lan.peers".into(),
            data: json!({ "session_id": "s2", "generation": 0 }),
        });

        let mut snapshots = Vec::new();
        while let Some(event) = queue.pop(Some(Duration::ZERO)).await {
            if event.event == "lan.peers" {
                snapshots.push(event.data);
            }
        }
        // One live snapshot per session, each holding the most recent generation.
        assert_eq!(
            snapshots,
            vec![
                json!({ "session_id": "s1", "generation": 7 }),
                json!({ "session_id": "s2", "generation": 0 }),
            ]
        );
    }

    #[tokio::test]
    async fn two_json_services_transfer_a_file_and_reuse_identity() {
        let root = tempfile::tempdir().unwrap();
        let receiver = JsonService::new();
        let sender = JsonService::new();
        let receiver_inbox = root.path().join("receiver");
        let sender_inbox = root.path().join("sender");
        let receiver_start = call(
            &receiver,
            "lan.start",
            json!({
                "peer_name": "Receiver",
                "instance_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "inbox": receiver_inbox,
                "disable_mdns": true,
                "disable_udp": true,
            }),
        )
        .await;
        let sender_start = call(
            &sender,
            "lan.start",
            json!({
                "peer_name": "Sender",
                "instance_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "inbox": sender_inbox,
                "disable_mdns": true,
                "disable_udp": true,
            }),
        )
        .await;

        let source = root.path().join("rpc-sample.bin");
        let payload = vec![0x6d; 3 * 1024 * 1024];
        tokio::fs::write(&source, &payload).await.unwrap();
        let send = call(
            &sender,
            "lan.send",
            json!({
                "session_id": sender_start["session_id"],
                "path": source,
                "host": "127.0.0.1",
                "port": receiver_start["port"],
                "fingerprint": receiver_start["fingerprint"],
            }),
        )
        .await;
        assert!(send["send_id"].as_str().unwrap().starts_with("send-"));

        let received = wait_event(&receiver, "lan.received").await;
        let delivered = PathBuf::from(received["path"].as_str().unwrap());
        assert_eq!(tokio::fs::read(delivered).await.unwrap(), payload);
        let sent = wait_event(&sender, "lan.sent").await;
        assert_eq!(sent["ok"], true);
        assert_eq!(sent["send_id"], send["send_id"]);

        call(
            &receiver,
            "lan.stop",
            json!({ "session_id": receiver_start["session_id"] }),
        )
        .await;
        let restarted = call(
            &receiver,
            "lan.start",
            json!({
                "peer_name": "Receiver renamed",
                "instance_id": "cccccccccccccccccccccccccccccccc",
                "inbox": root.path().join("receiver-restarted"),
                "identity_private": receiver_start["identity_private"],
                "disable_mdns": true,
                "disable_udp": true,
            }),
        )
        .await;
        assert_eq!(restarted["fingerprint"], receiver_start["fingerprint"]);

        sender.close().await.unwrap();
        receiver.close().await.unwrap();
    }

    #[tokio::test]
    async fn json_services_discover_send_by_instance_id_and_remove_goodbye() {
        let root = tempfile::tempdir().unwrap();
        let receiver = JsonService::new();
        let sender = JsonService::new();
        let receiver_id = "12121212121212121212121212121212";
        let sender_id = "34343434343434343434343434343434";
        let receiver_start = call(
            &receiver,
            "lan.start",
            json!({
                "peer_name": "Discovered receiver",
                "instance_id": receiver_id,
                "inbox": root.path().join("discovered-receiver"),
                "capabilities": ["quic-v1", "blake3"],
                "disable_mdns": true,
            }),
        )
        .await;
        let sender_start = call(
            &sender,
            "lan.start",
            json!({
                "peer_name": "Discovered sender",
                "instance_id": sender_id,
                "inbox": root.path().join("discovered-sender"),
                "capabilities": ["quic-v1"],
                "disable_mdns": true,
            }),
        )
        .await;

        let peer = wait_for_peer(&sender, &sender_start["session_id"], receiver_id, true)
            .await
            .unwrap();
        assert_eq!(peer["name"], "Discovered receiver");
        assert_eq!(peer["capabilities"], json!(["blake3", "quic-v1"]));

        let source = root.path().join("discovered.txt");
        tokio::fs::write(&source, b"verified discovery")
            .await
            .unwrap();
        call(
            &sender,
            "lan.send",
            json!({
                "session_id": sender_start["session_id"],
                "path": source,
                "instance_id": receiver_id,
            }),
        )
        .await;
        let received = wait_event(&receiver, "lan.received").await;
        assert_eq!(
            tokio::fs::read(received["path"].as_str().unwrap())
                .await
                .unwrap(),
            b"verified discovery"
        );

        call(
            &receiver,
            "lan.stop",
            json!({ "session_id": receiver_start["session_id"] }),
        )
        .await;
        wait_for_peer(&sender, &sender_start["session_id"], receiver_id, false).await;
        sender.close().await.unwrap();
        receiver.close().await.unwrap();
    }

    #[tokio::test]
    async fn json_send_can_be_cancelled_immediately() {
        let root = tempfile::tempdir().unwrap();
        let receiver = JsonService::new();
        let sender = JsonService::new();
        let receiver_start = call(
            &receiver,
            "lan.start",
            json!({
                "peer_name": "Cancel receiver",
                "instance_id": "dddddddddddddddddddddddddddddddd",
                "inbox": root.path().join("cancel-receiver"),
            }),
        )
        .await;
        let sender_start = call(
            &sender,
            "lan.start",
            json!({
                "peer_name": "Cancel sender",
                "instance_id": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                "inbox": root.path().join("cancel-sender"),
            }),
        )
        .await;
        let source = root.path().join("cancel.bin");
        let file = tokio::fs::File::create(&source).await.unwrap();
        file.set_len(64 * 1024 * 1024).await.unwrap();
        drop(file);

        let send = call(
            &sender,
            "lan.send",
            json!({
                "session_id": sender_start["session_id"],
                "path": source,
                "host": "127.0.0.1",
                "port": receiver_start["port"],
                "fingerprint": receiver_start["fingerprint"],
            }),
        )
        .await;
        let cancelled = call(
            &sender,
            "lan.send.cancel",
            json!({
                "session_id": sender_start["session_id"],
                "send_id": send["send_id"],
            }),
        )
        .await;
        assert_eq!(cancelled["cancelled"], true);

        let sent = wait_event(&sender, "lan.sent").await;
        assert_eq!(sent["ok"], false);
        assert!(sent["error"].as_str().unwrap().contains("cancelled"));
        let cancelled_again = call(
            &sender,
            "lan.send.cancel",
            json!({
                "session_id": sender_start["session_id"],
                "send_id": send["send_id"],
            }),
        )
        .await;
        assert_eq!(cancelled_again["cancelled"], false);

        sender.close().await.unwrap();
        receiver.close().await.unwrap();
    }

    #[tokio::test]
    async fn json_short_code_transfers_file_and_folder_over_quic_tunnel() {
        let root = tempfile::tempdir().unwrap();
        let rendezvous = MockRendezvous::start().await;
        let closed_port = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let transit_relay = closed_port.local_addr().unwrap().to_string();
        drop(closed_port);

        let sender = JsonService::new();
        let receiver = JsonService::new();
        let sender_inbox = root.path().join("sender-inbox");
        let receiver_inbox = root.path().join("receiver-inbox");
        let sender_lan = call(
            &sender,
            "lan.start",
            json!({
                "peer_name": "Short-code sender",
                "instance_id": "12121212121212121212121212121212",
                "inbox": sender_inbox,
                "disable_mdns": true,
                "disable_udp": true,
            }),
        )
        .await;
        let receiver_lan = call(
            &receiver,
            "lan.start",
            json!({
                "peer_name": "Short-code receiver",
                "instance_id": "34343434343434343434343434343434",
                "inbox": receiver_inbox,
                "disable_mdns": true,
                "disable_udp": true,
            }),
        )
        .await;

        let sources = root.path().join("sources");
        let photo = sources.join("photo.jpg");
        let album = sources.join("album");
        tokio::fs::create_dir_all(album.join("nested/empty"))
            .await
            .unwrap();
        tokio::fs::write(&photo, b"short-code photo").await.unwrap();
        tokio::fs::write(album.join("nested/note.txt"), b"short-code folder")
            .await
            .unwrap();
        let settings = json!({
            "rendezvous_url": rendezvous.url(),
            "transit_relay": transit_relay,
            "timeout_minutes": 1,
        });
        let created = call(
            &sender,
            "wormhole.create",
            json!({
                "session_id": sender_lan["session_id"],
                "paths": [photo, album],
                "settings": settings,
            }),
        )
        .await;
        let joined = call(
            &receiver,
            "wormhole.join.start",
            json!({
                "session_id": receiver_lan["session_id"],
                "code": created["code"],
                "settings": settings,
            }),
        )
        .await;
        let offer = wait_event(&receiver, "wormhole.offer").await;
        assert_eq!(offer["session_id"], joined["session_id"]);
        assert_eq!(offer["summary"]["device_name"], "Short-code sender");
        assert_eq!(offer["summary"]["item_count"], 2);
        assert_eq!(offer["summary"]["file_count"], 2);
        assert_eq!(offer["summary"]["directory_count"], 3);
        call(
            &receiver,
            "wormhole.accept",
            json!({ "session_id": joined["session_id"] }),
        )
        .await;

        let sender_events = tokio::time::timeout(Duration::from_secs(20), async {
            let mut ready_ids = HashSet::new();
            let mut sent_ids = HashSet::new();
            while ready_ids.len() != 2 || sent_ids.len() != 2 {
                let event = next_event(&sender).await;
                match event.event.as_str() {
                    "wormhole.ready" => {
                        assert_eq!(event.data["session_id"], created["session_id"]);
                        ready_ids.extend(
                            event.data["send_ids"]
                                .as_array()
                                .unwrap()
                                .iter()
                                .map(|value| value.as_str().unwrap().to_owned()),
                        );
                    }
                    "lan.sent" => {
                        assert_eq!(event.data["ok"], true, "{}", event.data);
                        sent_ids.insert(event.data["send_id"].as_str().unwrap().to_owned());
                    }
                    "wormhole.error" => panic!("sender short-code error: {}", event.data),
                    _ => {}
                }
            }
            assert_eq!(ready_ids, sent_ids);
        });
        let receiver_events = tokio::time::timeout(Duration::from_secs(20), async {
            let mut ready = false;
            let mut received = HashSet::new();
            while !ready || received.len() != 2 {
                let event = next_event(&receiver).await;
                match event.event.as_str() {
                    "wormhole.ready" => {
                        assert_eq!(event.data["session_id"], joined["session_id"]);
                        ready = true;
                    }
                    "lan.received" => {
                        received.insert(event.data["filename"].as_str().unwrap().to_owned());
                    }
                    "wormhole.error" => panic!("receiver short-code error: {}", event.data),
                    _ => {}
                }
            }
            assert_eq!(
                received,
                HashSet::from(["album".into(), "photo.jpg".into()])
            );
        });
        let (sender_events, receiver_events) = tokio::join!(sender_events, receiver_events);
        sender_events.expect("sender short-code events timed out");
        receiver_events.expect("receiver short-code events timed out");

        assert_eq!(
            tokio::fs::read(receiver_inbox.join("photo.jpg"))
                .await
                .unwrap(),
            b"short-code photo"
        );
        assert_eq!(
            tokio::fs::read(receiver_inbox.join("album/nested/note.txt"))
                .await
                .unwrap(),
            b"short-code folder"
        );
        assert!(
            tokio::fs::metadata(receiver_inbox.join("album/nested/empty"))
                .await
                .unwrap()
                .is_dir()
        );
        wait_wormhole_finished(&sender, created["session_id"].as_str().unwrap()).await;
        wait_wormhole_finished(&receiver, joined["session_id"].as_str().unwrap()).await;

        let (sender_close, receiver_close) = tokio::join!(sender.close(), receiver.close());
        sender_close.unwrap();
        receiver_close.unwrap();
        rendezvous.close().await;
    }
}
