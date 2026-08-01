use std::{
    collections::HashMap,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::PathBuf,
    sync::{Arc, Mutex as StdMutex},
    time::Duration,
};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::{
    sync::{Mutex, Notify, RwLock, mpsc},
    task::{JoinHandle, JoinSet},
    time::Instant,
};
use tokio_util::sync::CancellationToken;

use super::{
    channel::SshChannelStream,
    client::{IncomingSshChannel, SshClient, SshProfile},
    protocol::{
        ChannelMode, PairingIdentity, exchange_pairing, generate_pair_code, pair_code_port,
        read_channel_mode, receive_data_answer, receive_data_hello, send_data_answer,
        send_data_hello, verify_data_hello, write_channel_mode,
    },
};
use crate::{
    CoreError, DeviceIdentity, InboxCategoryRoots, PeerEndpoint, QuicServer, QuicServerConfig,
    Result, TransferEventCallback,
    wormhole::{UdpTunnel, temporary_secret},
};

const PAIRING_LIFETIME: Duration = Duration::from_secs(10 * 60);
const CHANNEL_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(30);
const CLIENT_RECONNECT_WAIT: Duration = Duration::from_secs(45);
const MAX_RECONNECT_BACKOFF: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub(crate) struct SshPeer {
    pub id: String,
    pub name: String,
    pub instance_id: String,
    pub remote_port: u16,
    pub noise_public: String,
    pub end_to_end: bool,
}

impl Default for SshPeer {
    fn default() -> Self {
        Self {
            id: String::new(),
            name: String::new(),
            instance_id: String::new(),
            remote_port: 0,
            noise_public: String::new(),
            end_to_end: true,
        }
    }
}

impl SshPeer {
    fn from_identity(identity: PairingIdentity) -> Result<Self> {
        identity.validate()?;
        Ok(Self {
            id: identity.instance_id.clone(),
            name: identity.name,
            instance_id: identity.instance_id,
            remote_port: identity.remote_port,
            noise_public: identity.public_key,
            end_to_end: true,
        })
    }

    fn identity(&self) -> PairingIdentity {
        PairingIdentity {
            name: self.name.clone(),
            instance_id: self.instance_id.clone(),
            remote_port: self.remote_port,
            public_key: self.noise_public.clone(),
        }
    }

    fn normalize(mut self, local_instance_id: &str) -> Result<Self> {
        self.id = self.id.trim().to_owned();
        self.name = self.name.trim().to_owned();
        self.instance_id = self.instance_id.trim().to_ascii_lowercase();
        self.noise_public = self.noise_public.trim().to_owned();
        if self.id.is_empty() {
            self.id.clone_from(&self.instance_id);
        }
        self.end_to_end = true;
        self.identity().validate()?;
        if self.instance_id == local_instance_id {
            return Err(CoreError::InvalidRequest(
                "SSH relay peer cannot be the local device".into(),
            ));
        }
        Ok(self)
    }
}

#[derive(Debug, Clone)]
pub(crate) struct SshRelayEvent {
    pub event: &'static str,
    pub data: Value,
}

pub(crate) type SshRelayEventCallback = Arc<dyn Fn(SshRelayEvent) + Send + Sync + 'static>;

#[derive(Clone)]
pub(crate) struct SshRelayConfig {
    pub lan_session_id: String,
    pub profile: SshProfile,
    pub requested_port: u16,
    pub identity: DeviceIdentity,
    pub inbox: PathBuf,
    pub inbox_category_roots: InboxCategoryRoots,
    pub capabilities: Vec<String>,
    pub peers: Vec<SshPeer>,
    pub cancellation: CancellationToken,
    pub on_event: Option<SshRelayEventCallback>,
    pub on_transfer: Option<TransferEventCallback>,
}

impl std::fmt::Debug for SshRelayConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SshRelayConfig")
            .field("lan_session_id", &self.lan_session_id)
            .field("profile", &self.profile)
            .field("requested_port", &self.requested_port)
            .field("identity", &self.identity)
            .field("inbox", &self.inbox)
            .field("inbox_category_roots", &self.inbox_category_roots)
            .field("capabilities", &self.capabilities)
            .field("peers", &self.peers)
            .field("cancelled", &self.cancellation.is_cancelled())
            .field("has_event_callback", &self.on_event.is_some())
            .field("has_transfer_callback", &self.on_transfer.is_some())
            .finish()
    }
}

struct ActivePairing {
    code: String,
    expires_at: Instant,
    in_use: bool,
}

pub(crate) struct SshRelaySession {
    lan_session_id: String,
    profile: SshProfile,
    identity: DeviceIdentity,
    inbox: PathBuf,
    inbox_category_roots: InboxCategoryRoots,
    capabilities: Vec<String>,
    remote_port: u16,
    peers: RwLock<HashMap<String, SshPeer>>,
    client: RwLock<Option<Arc<SshClient>>>,
    client_changed: Notify,
    pairing: Mutex<Option<ActivePairing>>,
    cancellation: CancellationToken,
    supervisor: StdMutex<Option<JoinHandle<()>>>,
    on_event: Option<SshRelayEventCallback>,
    on_transfer: Option<TransferEventCallback>,
}

impl std::fmt::Debug for SshRelaySession {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SshRelaySession")
            .field("lan_session_id", &self.lan_session_id)
            .field("profile", &self.profile)
            .field("identity", &self.identity)
            .field("remote_port", &self.remote_port)
            .field("cancelled", &self.cancellation.is_cancelled())
            .finish_non_exhaustive()
    }
}

pub(crate) struct SshRelayStart {
    pub session: Arc<SshRelaySession>,
    pub connected: bool,
    pub initial_error: Option<String>,
}

pub(crate) struct SshSenderConnection {
    pub peer: PeerEndpoint,
    pub shared_secret: String,
    pub tunnel: UdpTunnel,
}

impl SshRelaySession {
    pub(crate) async fn start(config: SshRelayConfig) -> Result<SshRelayStart> {
        if config.lan_session_id.trim().is_empty() {
            return Err(CoreError::InvalidRequest(
                "lan session id is required".into(),
            ));
        }
        config.profile.validate_relay()?;
        let mut peers = HashMap::with_capacity(config.peers.len());
        for peer in config.peers {
            let peer = peer.normalize(config.identity.instance_id())?;
            peers.insert(peer.instance_id.clone(), peer);
        }
        let cancellation = config.cancellation.child_token();
        let initial_connection = SshClient::connect_reverse(
            config.profile.clone(),
            config.requested_port,
            &cancellation,
        )
        .await;
        let (connection, remote_port, initial_error) = match initial_connection {
            Ok((client, incoming)) => {
                let port = client.remote_port();
                (Some((client, incoming)), port, None)
            }
            Err(error) if config.requested_port != 0 => {
                (None, config.requested_port, Some(error.to_string()))
            }
            Err(error) => return Err(error),
        };
        let connected = connection.is_some();
        let session = Arc::new(Self {
            lan_session_id: config.lan_session_id,
            profile: config.profile,
            identity: config.identity,
            inbox: config.inbox,
            inbox_category_roots: config.inbox_category_roots,
            capabilities: config.capabilities,
            remote_port,
            peers: RwLock::new(peers),
            client: RwLock::new(connection.as_ref().map(|(client, _)| client.clone())),
            client_changed: Notify::new(),
            pairing: Mutex::new(None),
            cancellation,
            supervisor: StdMutex::new(None),
            on_event: config.on_event,
            on_transfer: config.on_transfer,
        });
        let run_session = session.clone();
        let handle = tokio::spawn(async move {
            run_session.run(connection).await;
        });
        *session
            .supervisor
            .lock()
            .unwrap_or_else(|lock| lock.into_inner()) = Some(handle);
        Ok(SshRelayStart {
            session,
            connected,
            initial_error,
        })
    }

    pub(crate) fn lan_session_id(&self) -> &str {
        &self.lan_session_id
    }

    pub(crate) fn remote_port(&self) -> u16 {
        self.remote_port
    }

    pub(crate) async fn is_connected(&self) -> bool {
        self.client
            .read()
            .await
            .as_ref()
            .is_some_and(|client| client.is_connected())
    }

    pub(crate) async fn peer_list(&self) -> Vec<SshPeer> {
        let mut peers = self
            .peers
            .read()
            .await
            .values()
            .cloned()
            .collect::<Vec<_>>();
        peers.sort_by(|left, right| {
            left.name
                .cmp(&right.name)
                .then_with(|| left.instance_id.cmp(&right.instance_id))
        });
        peers
    }

    pub(crate) async fn has_peer(&self, instance_id: &str) -> bool {
        self.peers.read().await.contains_key(instance_id)
    }

    pub(crate) async fn create_pairing(&self) -> Result<String> {
        if !self.is_connected().await {
            return Err(CoreError::Protocol("SSH relay is reconnecting".into()));
        }
        let mut pairing = self.pairing.lock().await;
        // An exchange that is already running would still call add_peer with the
        // old code, so a new code must not silently replace it.
        if pairing
            .as_ref()
            .is_some_and(|active| active.in_use && active.expires_at > Instant::now())
        {
            return Err(CoreError::Protocol(
                "SSH pairing is already in progress".into(),
            ));
        }
        let code = generate_pair_code(self.remote_port)?;
        *pairing = Some(ActivePairing {
            code: code.clone(),
            expires_at: Instant::now() + PAIRING_LIFETIME,
            in_use: false,
        });
        Ok(code)
    }

    pub(crate) async fn join_pairing(&self, code: &str) -> Result<SshPeer> {
        let remote_port = pair_code_port(code)?;
        let client = self.connected_client().await?;
        let channel = client.open_direct(remote_port, &self.cancellation).await?;
        let mut stream = SshChannelStream::new(channel);
        let local = PairingIdentity::from_device(&self.identity, self.remote_port);
        let peer = timeout_handshake(async {
            write_channel_mode(&mut stream, ChannelMode::Pair, &self.cancellation).await?;
            exchange_pairing(&mut stream, code, false, local, &self.cancellation).await
        })
        .await?;
        let peer = self.add_peer(SshPeer::from_identity(peer)?).await?;
        self.emit("ssh.paired", json!({ "peer": peer }));
        Ok(peer)
    }

    pub(crate) async fn connect_sender(
        &self,
        instance_id: &str,
        cancellation: CancellationToken,
    ) -> Result<SshSenderConnection> {
        let peer = self
            .peers
            .read()
            .await
            .get(instance_id)
            .cloned()
            .ok_or_else(|| CoreError::InvalidRequest("unknown SSH relay peer".into()))?;
        let client = self.connected_client_with(&cancellation).await?;
        let channel = client.open_direct(peer.remote_port, &cancellation).await?;
        let mut stream = SshChannelStream::new(channel);
        let peer_identity = peer.identity();
        let authentication = timeout_handshake(async {
            write_channel_mode(&mut stream, ChannelMode::Data, &cancellation).await?;
            let nonce =
                send_data_hello(&mut stream, &self.identity, &peer_identity, &cancellation).await?;
            receive_data_answer(
                &mut stream,
                &self.identity,
                &peer_identity,
                &nonce,
                &cancellation,
            )
            .await
        })
        .await?;
        let (reader, writer) = stream.into_parts();
        let tunnel = UdpTunnel::start_sender_parts(
            reader,
            writer,
            authentication.tunnel_key,
            cancellation.child_token(),
        )
        .await?;
        Ok(SshSenderConnection {
            peer: PeerEndpoint {
                address: tunnel.local_address(),
                certificate_fingerprint: authentication.quic_fingerprint,
            },
            shared_secret: authentication.quic_secret,
            tunnel,
        })
    }

    pub(crate) async fn close(&self) -> Result<()> {
        self.cancellation.cancel();
        self.pairing.lock().await.take();
        if let Some(client) = self.client.write().await.take() {
            client.close().await?;
        }
        self.client_changed.notify_waiters();
        let handle = self
            .supervisor
            .lock()
            .unwrap_or_else(|lock| lock.into_inner())
            .take();
        if let Some(mut handle) = handle
            && tokio::time::timeout(Duration::from_secs(5), &mut handle)
                .await
                .is_err()
        {
            handle.abort();
            let _ = handle.await;
        }
        Ok(())
    }

    pub(crate) fn close_immediately(&self) {
        self.cancellation.cancel();
        if let Ok(mut handle) = self.supervisor.lock()
            && let Some(handle) = handle.take()
        {
            handle.abort();
        }
    }

    async fn run(
        self: Arc<Self>,
        mut current: Option<(Arc<SshClient>, mpsc::Receiver<IncomingSshChannel>)>,
    ) {
        let mut backoff = Duration::from_secs(1);
        let mut channel_tasks = JoinSet::new();
        loop {
            if self.cancellation.is_cancelled() {
                break;
            }
            let Some((client, mut incoming)) = current.take() else {
                tokio::select! {
                    _ = self.cancellation.cancelled() => break,
                    _ = tokio::time::sleep(backoff) => {}
                }
                match SshClient::connect_reverse(
                    self.profile.clone(),
                    self.remote_port,
                    &self.cancellation,
                )
                .await
                {
                    Ok((client, incoming)) if client.remote_port() == self.remote_port => {
                        *self.client.write().await = Some(client.clone());
                        self.client_changed.notify_waiters();
                        self.emit("ssh.connected", json!({ "remote_port": self.remote_port }));
                        backoff = Duration::from_secs(1);
                        current = Some((client, incoming));
                        continue;
                    }
                    Ok((client, _)) => {
                        let _ = client.close().await;
                        self.emit(
                            "ssh.reconnect.error",
                            json!({ "error": "SSH relay returned a different reverse port" }),
                        );
                    }
                    Err(error) if !self.cancellation.is_cancelled() => {
                        self.emit("ssh.reconnect.error", json!({ "error": error.to_string() }));
                    }
                    Err(_) => break,
                }
                backoff = (backoff * 2).min(MAX_RECONNECT_BACKOFF);
                continue;
            };

            let mut connected = client.subscribe_connected();
            let disconnected = loop {
                tokio::select! {
                    _ = self.cancellation.cancelled() => break false,
                    changed = connected.changed() => {
                        if changed.is_err() || !*connected.borrow() {
                            break true;
                        }
                    }
                    received = incoming.recv() => match received {
                        Some(channel) => {
                            let session = self.clone();
                            channel_tasks.spawn(async move { session.handle_incoming(channel).await });
                        }
                        None => break true,
                    },
                    result = channel_tasks.join_next(), if !channel_tasks.is_empty() => {
                        report_channel_result(&self, result);
                    }
                }
            };
            if !disconnected {
                break;
            }
            let mut slot = self.client.write().await;
            if slot
                .as_ref()
                .is_some_and(|active| Arc::ptr_eq(active, &client))
            {
                slot.take();
            }
            drop(slot);
            self.client_changed.notify_waiters();
            let _ = client.close().await;
            self.emit(
                "ssh.disconnected",
                json!({ "error": "SSH relay connection was lost" }),
            );
        }
        self.cancellation.cancel();
        channel_tasks.abort_all();
        while let Some(result) = channel_tasks.join_next().await {
            report_channel_result(&self, Some(result));
        }
        if let Some((client, _)) = current {
            let _ = client.close().await;
        }
    }

    async fn handle_incoming(self: Arc<Self>, incoming: IncomingSshChannel) -> Result<()> {
        if incoming.connected_port != self.remote_port {
            return Err(CoreError::Protocol(
                "SSH relay forwarded an unexpected port".into(),
            ));
        }
        let mut stream = SshChannelStream::new(incoming.channel);
        let mode = timeout_handshake(read_channel_mode(&mut stream, &self.cancellation)).await?;
        match mode {
            ChannelMode::Pair => self.handle_incoming_pair(&mut stream).await,
            ChannelMode::Data => self.handle_incoming_data(stream).await,
        }
    }

    async fn handle_incoming_pair<S>(&self, stream: &mut S) -> Result<()>
    where
        S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    {
        let code = self.begin_pairing_attempt().await?;
        let local = PairingIdentity::from_device(&self.identity, self.remote_port);
        let result = timeout_handshake(exchange_pairing(
            stream,
            &code,
            true,
            local,
            &self.cancellation,
        ))
        .await;
        self.finish_pairing_attempt(&code, result.is_ok()).await;
        let peer = self.add_peer(SshPeer::from_identity(result?)?).await?;
        self.emit("ssh.paired", json!({ "peer": peer }));
        Ok(())
    }

    async fn handle_incoming_data(&self, mut stream: SshChannelStream) -> Result<()> {
        let hello = timeout_handshake(receive_data_hello(&mut stream, &self.cancellation)).await?;
        let peer = self
            .peers
            .read()
            .await
            .get(hello.sender_instance_id())
            .cloned()
            .ok_or_else(|| CoreError::Protocol("SSH relay data peer is not paired".into()))?;
        let peer_identity = peer.identity();
        let hello = verify_data_hello(hello, &self.identity, &peer_identity)?;
        let secret = temporary_secret();
        let server = QuicServer::bind_with_event_handler(
            QuicServerConfig {
                bind_address: SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0),
                inbox: self.inbox.clone(),
                inbox_category_roots: self.inbox_category_roots.clone(),
                identity: self.identity.clone(),
                capabilities: self.capabilities.clone(),
                shared_secret: secret.clone(),
                on_inbound_peer: None,
            },
            self.on_transfer.clone(),
        )
        .await?;
        let tunnel_key = timeout_handshake(send_data_answer(
            &mut stream,
            &self.identity,
            &peer_identity,
            &hello,
            server.certificate_fingerprint().to_owned(),
            secret,
            &self.cancellation,
        ))
        .await?;
        let (reader, writer) = stream.into_parts();
        let tunnel = UdpTunnel::start_receiver_parts(
            reader,
            writer,
            server.local_address(),
            tunnel_key,
            self.cancellation.child_token(),
        )
        .await?;
        let tunnel_result = tunnel.wait().await;
        let server_result = server.close().await;
        tunnel_result.and(server_result)
    }

    async fn add_peer(&self, peer: SshPeer) -> Result<SshPeer> {
        let peer = peer.normalize(self.identity.instance_id())?;
        self.peers
            .write()
            .await
            .insert(peer.instance_id.clone(), peer.clone());
        Ok(peer)
    }

    async fn begin_pairing_attempt(&self) -> Result<String> {
        let mut pairing = self.pairing.lock().await;
        let active = pairing
            .as_mut()
            .filter(|active| active.expires_at > Instant::now())
            .ok_or_else(|| CoreError::Protocol("SSH pairing code is unavailable".into()))?;
        if active.in_use {
            return Err(CoreError::Protocol(
                "SSH pairing is already in progress".into(),
            ));
        }
        active.in_use = true;
        Ok(active.code.clone())
    }

    async fn finish_pairing_attempt(&self, code: &str, success: bool) {
        let mut pairing = self.pairing.lock().await;
        if pairing.as_ref().is_some_and(|active| active.code == code) {
            if success {
                pairing.take();
            } else if let Some(active) = pairing.as_mut() {
                active.in_use = false;
            }
        }
    }

    async fn connected_client(&self) -> Result<Arc<SshClient>> {
        self.connected_client_with(&self.cancellation).await
    }

    async fn connected_client_with(
        &self,
        cancellation: &CancellationToken,
    ) -> Result<Arc<SshClient>> {
        tokio::time::timeout(CLIENT_RECONNECT_WAIT, async {
            loop {
                let notified = self.client_changed.notified();
                if let Some(client) = self.client.read().await.clone()
                    && client.is_connected()
                {
                    return Ok(client);
                }
                tokio::select! {
                    _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
                    _ = self.cancellation.cancelled() => return Err(CoreError::Cancelled),
                    _ = notified => {}
                }
            }
        })
        .await
        .map_err(|_| CoreError::Protocol("SSH relay reconnect timed out".into()))?
    }

    fn emit(&self, event: &'static str, data: Value) {
        if !self.cancellation.is_cancelled()
            && let Some(callback) = &self.on_event
        {
            callback(SshRelayEvent { event, data });
        }
    }
}

impl Drop for SshRelaySession {
    fn drop(&mut self) {
        self.cancellation.cancel();
        if let Ok(handle) = self.supervisor.get_mut()
            && let Some(handle) = handle.take()
        {
            handle.abort();
        }
    }
}

async fn timeout_handshake<F, T>(future: F) -> Result<T>
where
    F: Future<Output = Result<T>>,
{
    tokio::time::timeout(CHANNEL_HANDSHAKE_TIMEOUT, future)
        .await
        .map_err(|_| CoreError::Protocol("SSH relay handshake timed out".into()))?
}

fn report_channel_result(
    session: &SshRelaySession,
    result: Option<std::result::Result<Result<()>, tokio::task::JoinError>>,
) {
    let error = match result {
        Some(Ok(Err(error))) if !matches!(error, CoreError::Cancelled) => Some(error.to_string()),
        Some(Err(error)) if !error.is_cancelled() => {
            Some(format!("SSH relay channel task failed: {error}"))
        }
        _ => None,
    };
    if let Some(error) = error {
        session.emit("ssh.channel.error", json!({ "error": error }));
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use tokio::sync::mpsc;

    use super::*;
    use crate::{SendFileOptions, send_file, ssh::test_support::MockSshRelay};

    fn relay_config(
        profile: SshProfile,
        lan_session_id: &str,
        requested_port: u16,
        identity: DeviceIdentity,
        inbox: PathBuf,
        events: mpsc::UnboundedSender<&'static str>,
    ) -> SshRelayConfig {
        SshRelayConfig {
            lan_session_id: lan_session_id.into(),
            profile,
            requested_port,
            identity,
            inbox,
            inbox_category_roots: InboxCategoryRoots::default(),
            capabilities: vec!["blake3".into(), "folder-v1".into(), "quic-v2".into()],
            peers: Vec::new(),
            cancellation: CancellationToken::new(),
            on_event: Some(Arc::new(move |event| {
                let _ = events.send(event.event);
            })),
            on_transfer: None,
        }
    }

    async fn wait_for_event(events: &mut mpsc::UnboundedReceiver<&'static str>, expected: &str) {
        tokio::time::timeout(Duration::from_secs(15), async {
            loop {
                let event = events.recv().await.expect("SSH event channel closed");
                if event == expected {
                    break;
                }
            }
        })
        .await
        .unwrap();
    }

    async fn wait_until(mut condition: impl FnMut() -> bool) {
        tokio::time::timeout(Duration::from_secs(5), async {
            while !condition() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
    }

    async fn send_over_relay(
        session: &SshRelaySession,
        identity: &DeviceIdentity,
        receiver_instance_id: &str,
        source: &Path,
    ) -> crate::protocol::TransferReceipt {
        let cancellation = CancellationToken::new();
        let connection = session
            .connect_sender(receiver_instance_id, cancellation.clone())
            .await
            .unwrap();
        let send_result = send_file(
            identity,
            &connection.peer,
            source,
            SendFileOptions {
                shared_secret: connection.shared_secret.clone(),
                cancellation,
                ..SendFileOptions::default()
            },
        )
        .await;
        let close_result = connection.tunnel.close().await;
        match (send_result, close_result) {
            (Ok(receipt), Ok(())) => receipt,
            (Err(error), close) => {
                panic!(
                    "send {} over SSH relay failed: {error}; tunnel close: {close:?}",
                    source.display()
                )
            }
            (Ok(_), Err(error)) => panic!("SSH relay tunnel close failed: {error}"),
        }
    }

    #[test]
    fn saved_peer_is_always_end_to_end_and_rejects_self() {
        let identity =
            DeviceIdentity::generate(Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), "Local").unwrap();
        let remote =
            DeviceIdentity::generate(Some("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"), "Remote").unwrap();
        let mut peer =
            SshPeer::from_identity(PairingIdentity::from_device(&remote, 32000)).unwrap();
        peer.end_to_end = false;
        assert!(peer.normalize(identity.instance_id()).unwrap().end_to_end);

        let local = SshPeer::from_identity(PairingIdentity::from_device(&identity, 32001)).unwrap();
        assert!(local.normalize(identity.instance_id()).is_err());
    }

    #[tokio::test]
    async fn pairs_reconnects_and_transfers_files_and_folders_over_ssh_quic() {
        let relay = MockSshRelay::start().await;
        let root = tempfile::tempdir().unwrap();
        let inbox_a = root.path().join("inbox-a");
        let inbox_b = root.path().join("inbox-b");
        let identity_a =
            DeviceIdentity::generate(Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), "Device A").unwrap();
        let identity_b =
            DeviceIdentity::generate(Some("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"), "Device B").unwrap();
        let (events_tx, mut events_rx) = mpsc::unbounded_channel();
        let start_a = SshRelaySession::start(relay_config(
            relay.profile(),
            "lan-a",
            0,
            identity_a.clone(),
            inbox_a.clone(),
            events_tx.clone(),
        ))
        .await
        .unwrap();
        assert!(start_a.connected);
        let fixed_port = 45_000;
        let start_b = SshRelaySession::start(relay_config(
            relay.profile(),
            "lan-b",
            fixed_port,
            identity_b.clone(),
            inbox_b,
            events_tx,
        ))
        .await
        .unwrap();
        assert!(start_b.connected);
        assert_eq!(start_b.session.remote_port(), fixed_port);

        let code = start_a.session.create_pairing().await.unwrap();
        let peer_a = start_b.session.join_pairing(&code).await.unwrap();
        assert_eq!(peer_a.instance_id, identity_a.instance_id());
        tokio::time::timeout(Duration::from_secs(5), async {
            while !start_a.session.has_peer(identity_b.instance_id()).await {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        assert!(start_b.session.has_peer(identity_a.instance_id()).await);

        let source = root.path().join("payload.bin");
        let payload = (0..256_000_u32)
            .flat_map(u32::to_le_bytes)
            .collect::<Vec<_>>();
        tokio::fs::write(&source, &payload).await.unwrap();
        let receipt = send_over_relay(
            &start_b.session,
            &identity_b,
            identity_a.instance_id(),
            &source,
        )
        .await;
        assert_eq!(receipt.blake3, blake3::hash(&payload).to_hex().to_string());
        assert_eq!(
            tokio::fs::read(inbox_a.join("payload.bin")).await.unwrap(),
            payload
        );

        assert!(relay.disconnect_port(fixed_port).await);
        wait_for_event(&mut events_rx, "ssh.disconnected").await;
        wait_for_event(&mut events_rx, "ssh.connected").await;
        assert!(start_b.session.is_connected().await);

        let folder = root.path().join("project");
        tokio::fs::create_dir_all(folder.join("empty/deep"))
            .await
            .unwrap();
        tokio::fs::create_dir_all(folder.join("src")).await.unwrap();
        tokio::fs::write(folder.join("README.md"), b"SSH QUIC folder")
            .await
            .unwrap();
        tokio::fs::write(folder.join("src/main.rs"), b"fn main() {}")
            .await
            .unwrap();
        let folder_receipt = send_over_relay(
            &start_b.session,
            &identity_b,
            identity_a.instance_id(),
            &folder,
        )
        .await;
        assert_eq!(folder_receipt.blake3.len(), 64);
        assert_eq!(
            tokio::fs::read(inbox_a.join("project/README.md"))
                .await
                .unwrap(),
            b"SSH QUIC folder"
        );
        assert!(
            tokio::fs::metadata(inbox_a.join("project/empty/deep"))
                .await
                .unwrap()
                .is_dir()
        );

        let connection = start_b
            .session
            .connect_sender(identity_a.instance_id(), CancellationToken::new())
            .await
            .unwrap();
        let cancelled = CancellationToken::new();
        cancelled.cancel();
        let cancelled_result = send_file(
            &identity_b,
            &connection.peer,
            &source,
            SendFileOptions {
                shared_secret: connection.shared_secret.clone(),
                cancellation: cancelled,
                ..SendFileOptions::default()
            },
        )
        .await;
        assert!(matches!(cancelled_result, Err(CoreError::Cancelled)));
        connection.tunnel.close().await.unwrap();

        start_b.session.close().await.unwrap();
        start_a.session.close().await.unwrap();
        wait_until(|| relay.active_ports().is_empty()).await;
        relay.close().await;
    }
}
