use std::{
    collections::{HashMap, HashSet},
    fmt,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    sync::{Arc, Mutex as StdMutex},
    time::Duration,
};

use mdns_sd::{IfKind, ResolvedService, ServiceDaemon, ServiceEvent, ServiceInfo};
use serde::{Deserialize, Serialize};
use socket2::{Domain, Protocol, SockAddr, Socket, Type};
use tokio::{
    net::UdpSocket,
    sync::{Notify, mpsc, watch},
    task::{JoinHandle, JoinSet},
    time::{Instant, MissedTickBehavior},
};
use tokio_util::sync::CancellationToken;

use crate::{
    CoreError, LAN_DISCOVERY_PROTOCOL_VERSION, Result,
    hash::valid_blake3,
    protocol::valid_instance_id,
    transport::{PeerEndpoint, VerifiedPeer, probe_peer},
};

pub const UDP_DISCOVERY_PORT: u16 = 41_301;
const UDP_DISCOVERY_MAGIC: &str = "inkhole-lan-v1";
const MDNS_SERVICE_TYPE: &str = "_inkhole._udp.local.";
const MDNS_TRANSPORT: &str = "quic";
const MDNS_HASH: &str = "blake3";
const MAX_UDP_PACKET: usize = 2_048;
const MAX_PENDING_PROBES: usize = 16;
const MAX_DISCOVERED_PEERS: usize = 256;
const MAX_MDNS_EVENTS: usize = 128;
const MDNS_CLEANUP_TIMEOUT: Duration = Duration::from_secs(1);
const MAX_UDP_ERROR_STREAK: u32 = 50;
const UDP_ERROR_BACKOFF: Duration = Duration::from_millis(250);

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DiscoveredPeer {
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

pub type DiscoveryPeersCallback = Arc<dyn Fn(Vec<DiscoveredPeer>) + Send + Sync + 'static>;

#[derive(Debug, Clone, Copy)]
pub struct UdpDiscoveryTimings {
    pub announce_interval: Duration,
    pub refresh_burst_gap: Duration,
    pub refresh_burst: usize,
    pub probe_timeout: Duration,
    pub min_probe_interval: Duration,
    pub liveness_interval: Duration,
    pub prune_interval: Duration,
    pub stale_after: Duration,
    pub max_failures: u8,
}

impl Default for UdpDiscoveryTimings {
    fn default() -> Self {
        Self {
            announce_interval: Duration::from_secs(2),
            refresh_burst_gap: Duration::from_millis(150),
            refresh_burst: 5,
            probe_timeout: Duration::from_millis(1_500),
            min_probe_interval: Duration::from_secs(1),
            liveness_interval: Duration::from_secs(4),
            prune_interval: Duration::from_secs(1),
            stale_after: Duration::from_secs(12),
            max_failures: 3,
        }
    }
}

#[derive(Clone)]
pub struct UdpDiscoveryConfig {
    pub enable_udp: bool,
    pub enable_mdns: bool,
    pub bind_address: SocketAddr,
    pub targets: Vec<SocketAddr>,
    pub instance_id: String,
    pub quic_port: u16,
    pub certificate_fingerprint: String,
    pub shared_secret: String,
    pub timings: UdpDiscoveryTimings,
    pub on_peers: Option<DiscoveryPeersCallback>,
}

impl fmt::Debug for UdpDiscoveryConfig {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("UdpDiscoveryConfig")
            .field("enable_udp", &self.enable_udp)
            .field("enable_mdns", &self.enable_mdns)
            .field("bind_address", &self.bind_address)
            .field("targets", &self.targets)
            .field("instance_id", &self.instance_id)
            .field("quic_port", &self.quic_port)
            .field("certificate_fingerprint", &self.certificate_fingerprint)
            .field("has_shared_secret", &!self.shared_secret.is_empty())
            .field("timings", &self.timings)
            .field("has_peer_callback", &self.on_peers.is_some())
            .finish()
    }
}

pub struct UdpDiscovery {
    cancellation: CancellationToken,
    refresh: Arc<Notify>,
    snapshots: watch::Receiver<Arc<Vec<DiscoveredPeer>>>,
    local_address: SocketAddr,
    task: StdMutex<Option<JoinHandle<Result<()>>>>,
}

impl fmt::Debug for UdpDiscovery {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("UdpDiscovery")
            .field("local_address", &self.local_address)
            .field("cancelled", &self.cancellation.is_cancelled())
            .finish_non_exhaustive()
    }
}

impl UdpDiscovery {
    pub async fn start(mut config: UdpDiscoveryConfig) -> Result<Self> {
        validate_config(&mut config)?;
        let cancellation = CancellationToken::new();
        let mut startup_errors = Vec::new();
        let socket = if config.enable_udp {
            match bind_udp_socket(config.bind_address) {
                Ok(socket) => Some(Arc::new(socket)),
                Err(error) => {
                    startup_errors.push(format!("UDP: {error}"));
                    None
                }
            }
        } else {
            None
        };
        let local_address = match socket.as_ref() {
            Some(socket) => socket.local_addr()?,
            None => SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), 0),
        };
        let mdns = if config.enable_mdns {
            match start_mdns(&config, cancellation.child_token()) {
                Ok(runtime) => Some(runtime),
                Err(error) => {
                    startup_errors.push(format!("mDNS: {error}"));
                    None
                }
            }
        } else {
            None
        };
        if socket.is_none() && mdns.is_none() {
            return Err(CoreError::Protocol(format!(
                "no LAN discovery transport could start: {}",
                startup_errors.join("; ")
            )));
        }
        let announcement = encode_announcement(&config, false, false)?;
        let reply = encode_announcement(&config, true, false)?;
        let goodbye = encode_announcement(&config, false, true)?;
        let refresh = Arc::new(Notify::new());
        let (snapshots, receiver) = watch::channel(Arc::new(Vec::new()));
        let task = tokio::spawn(run_discovery(DiscoveryTask {
            socket,
            mdns,
            config,
            announcement,
            reply,
            goodbye,
            cancellation: cancellation.clone(),
            refresh: refresh.clone(),
            snapshots,
        }));
        Ok(Self {
            cancellation,
            refresh,
            snapshots: receiver,
            local_address,
            task: StdMutex::new(Some(task)),
        })
    }

    pub fn local_address(&self) -> SocketAddr {
        self.local_address
    }

    pub fn peers(&self) -> Vec<DiscoveredPeer> {
        self.snapshots.borrow().as_ref().clone()
    }

    pub fn refresh(&self) {
        if !self.cancellation.is_cancelled() {
            self.refresh.notify_one();
        }
    }

    pub fn close_immediately(&self) {
        self.cancellation.cancel();
        if let Ok(mut task) = self.task.lock()
            && let Some(task) = task.take()
        {
            task.abort();
        }
    }

    pub async fn close(&self) -> Result<()> {
        self.cancellation.cancel();
        let task = self
            .task
            .lock()
            .map_err(|_| CoreError::Protocol("discovery task lock was poisoned".into()))?
            .take();
        if let Some(task) = task {
            task.await.map_err(|error| {
                CoreError::Protocol(format!("discovery task failed: {error}"))
            })??;
        }
        Ok(())
    }
}

impl Drop for UdpDiscovery {
    fn drop(&mut self) {
        self.cancellation.cancel();
        if let Ok(task) = self.task.get_mut()
            && let Some(task) = task.take()
        {
            task.abort();
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct UdpAnnouncement {
    magic: String,
    version: u16,
    instance_id: String,
    port: u16,
    fingerprint: String,
    reply: bool,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    bye: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct CandidateKey {
    instance_id: String,
    address: SocketAddr,
    fingerprint: String,
}

#[derive(Debug, Clone)]
struct Candidate {
    key: CandidateKey,
    service_name: String,
}

#[derive(Debug)]
struct ProbeOutcome {
    candidate: Candidate,
    result: Result<VerifiedPeer>,
}

struct PeerRecord {
    peer: DiscoveredPeer,
    last_verified: Instant,
    failures: HashMap<SocketAddr, u8>,
    /// Set by an unauthenticated goodbye packet. A record in this state is dropped on
    /// the first failing probe instead of after `max_failures`, while a peer that still
    /// answers clears the flag — so a forged goodbye cannot evict anyone.
    farewelled: bool,
}

#[derive(Debug)]
enum MdnsDiscoveryEvent {
    Resolved(Box<ResolvedService>),
    Removed(String),
}

struct MdnsRuntime {
    daemon: ServiceDaemon,
    service_fullname: String,
    events: mpsc::Receiver<MdnsDiscoveryEvent>,
    forward_task: JoinHandle<()>,
}

struct DiscoveryTask {
    socket: Option<Arc<UdpSocket>>,
    mdns: Option<MdnsRuntime>,
    config: UdpDiscoveryConfig,
    announcement: Vec<u8>,
    reply: Vec<u8>,
    goodbye: Vec<u8>,
    cancellation: CancellationToken,
    refresh: Arc<Notify>,
    snapshots: watch::Sender<Arc<Vec<DiscoveredPeer>>>,
}

async fn run_discovery(task: DiscoveryTask) -> Result<()> {
    let DiscoveryTask {
        socket,
        mut mdns,
        config,
        announcement,
        reply,
        goodbye,
        cancellation,
        refresh,
        snapshots,
    } = task;
    let timings = config.timings;
    let mut records = HashMap::<String, PeerRecord>::new();
    let mut tracker = ProbeTracker::default();
    let mut udp_error_streak = 0_u32;
    let mut mdns_services = HashMap::<String, HashSet<CandidateKey>>::new();
    let mut mdns_event_stream_open = mdns.is_some();
    let mut receive_buffer = vec![0_u8; MAX_UDP_PACKET + 1];
    let mut periodic = tokio::time::interval(timings.announce_interval);
    let mut burst_tick = tokio::time::interval(timings.refresh_burst_gap);
    let mut liveness_tick = tokio::time::interval(timings.liveness_interval);
    let mut prune_tick = tokio::time::interval(timings.prune_interval);
    periodic.set_missed_tick_behavior(MissedTickBehavior::Skip);
    burst_tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    liveness_tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    prune_tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    periodic.tick().await;
    burst_tick.tick().await;
    liveness_tick.tick().await;
    prune_tick.tick().await;
    if let Some(socket) = socket.as_ref() {
        send_to_targets(socket, &announcement, &config.targets).await;
    }
    let mut burst_remaining = 0_usize;

    let run_result = loop {
        tokio::select! {
            _ = cancellation.cancelled() => break Ok(()),
            _ = periodic.tick(), if socket.is_some() => {
                if let Some(socket) = socket.as_ref() {
                    send_to_targets(socket, &announcement, &config.targets).await;
                }
            }
            _ = burst_tick.tick(), if socket.is_some() && burst_remaining > 0 => {
                if let Some(socket) = socket.as_ref() {
                    send_to_targets(socket, &announcement, &config.targets).await;
                }
                burst_remaining -= 1;
            }
            _ = refresh.notified() => {
                if socket.is_some() {
                    burst_remaining = timings.refresh_burst;
                }
                let candidates = records
                    .values()
                    .flat_map(record_candidates)
                    .collect::<Vec<_>>();
                for candidate in candidates {
                    tracker.last_attempt.remove(&candidate.key);
                    schedule_probe(
                        &mut tracker,
                        candidate,
                        &config.shared_secret,
                        &cancellation,
                        timings,
                    );
                }
            }
            received = receive_udp(socket.as_deref(), &mut receive_buffer), if socket.is_some() => {
                let (length, sender) = match received {
                    Ok(received) => {
                        udp_error_streak = 0;
                        received
                    }
                    // Only a socket that can never recover ends discovery; transient
                    // per-datagram errors (Windows ICMP fallout, interface churn) must
                    // not take the whole task down and freeze the peer list.
                    Err(error) if matches!(error.kind(), std::io::ErrorKind::NotConnected) => {
                        break Err(error.into());
                    }
                    Err(error) => {
                        udp_error_streak = udp_error_streak.saturating_add(1);
                        tracing::debug!(
                            %error,
                            streak = udp_error_streak,
                            "UDP discovery receive failed"
                        );
                        if udp_error_streak >= MAX_UDP_ERROR_STREAK {
                            udp_error_streak = 0;
                            tokio::time::sleep(UDP_ERROR_BACKOFF).await;
                        }
                        continue;
                    }
                };
                let Some(decoded) = decode_announcement(&receive_buffer[..length]) else {
                    continue;
                };
                if decoded.instance_id == config.instance_id {
                    continue;
                }
                if decoded.bye {
                    // A goodbye packet is unauthenticated, so treat it as a hint: shorten
                    // the peer's liveness window and re-probe immediately. A real
                    // shutdown fails the probe and expires; a forged one is refreshed.
                    for candidate in demote_goodbye(&mut records, &decoded, sender.ip()) {
                        tracker.last_attempt.remove(&candidate.key);
                        schedule_probe(
                            &mut tracker,
                            candidate,
                            &config.shared_secret,
                            &cancellation,
                            timings,
                        );
                    }
                    continue;
                }
                if !decoded.reply
                    && let Some(socket) = socket.as_ref()
                {
                    let _ = socket.send_to(&reply, sender).await;
                }
                schedule_probe(
                    &mut tracker,
                    Candidate {
                        key: CandidateKey {
                            instance_id: decoded.instance_id,
                            address: SocketAddr::new(sender.ip(), decoded.port),
                            fingerprint: decoded.fingerprint,
                        },
                        service_name: "udp-v4".into(),
                    },
                    &config.shared_secret,
                    &cancellation,
                    timings,
                );
            }
            event = receive_mdns_event(mdns.as_mut()), if mdns_event_stream_open => {
                let Some(event) = event else {
                    mdns_event_stream_open = false;
                    continue;
                };
                match event {
                    MdnsDiscoveryEvent::Resolved(service) => {
                        let service_name = service.get_fullname().to_owned();
                        let Some(candidates) = mdns_candidates(&service) else {
                            continue;
                        };
                        let mut service_candidates = HashSet::new();
                        for candidate in candidates {
                            if candidate.key.instance_id == config.instance_id {
                                continue;
                            }
                            service_candidates.insert(candidate.key.clone());
                            schedule_probe(
                                &mut tracker,
                                candidate,
                                &config.shared_secret,
                                &cancellation,
                                timings,
                            );
                        }
                        if service_candidates.is_empty() {
                            mdns_services.remove(&service_name);
                        } else {
                            mdns_services.insert(service_name, service_candidates);
                        }
                    }
                    MdnsDiscoveryEvent::Removed(service_name) => {
                        let mut candidates = mdns_services
                            .remove(&service_name)
                            .unwrap_or_default();
                            candidates.extend(
                                records
                                    .values()
                                    .filter(|record| record.peer.service_name == service_name)
                                    .flat_map(record_candidates)
                                    .map(|candidate| candidate.key),
                            );
                        for key in candidates {
                            tracker.last_attempt.remove(&key);
                            schedule_probe(
                                &mut tracker,
                                Candidate {
                                    key,
                                    service_name: service_name.clone(),
                                },
                                &config.shared_secret,
                                &cancellation,
                                timings,
                            );
                        }
                    }
                }
            }
            joined = tracker.probes.join_next_with_id(), if !tracker.probes.is_empty() => {
                match joined {
                    Some(Ok((id, outcome))) => {
                        tracker.owners.remove(&id);
                        tracker.pending.remove(&outcome.candidate.key);
                        if apply_probe_outcome(&mut records, outcome, timings.max_failures) {
                            publish_snapshot(&records, &snapshots, config.on_peers.as_ref());
                        }
                    }
                    Some(Err(error)) => {
                        // A panicked or aborted probe still has to release its pending
                        // slot, otherwise the candidate is never retried again.
                        if let Some(key) = tracker.owners.remove(&error.id()) {
                            tracker.pending.remove(&key);
                        }
                        tracing::debug!(%error, "peer probe task did not complete");
                    }
                    None => {}
                }
            }
            _ = liveness_tick.tick(), if !records.is_empty() => {
                let candidates = records
                    .values()
                    .flat_map(record_candidates)
                    .collect::<Vec<_>>();
                for candidate in candidates {
                    schedule_probe(
                        &mut tracker,
                        candidate,
                        &config.shared_secret,
                        &cancellation,
                        timings,
                    );
                }
            }
            _ = prune_tick.tick() => {
                let now = Instant::now();
                let before = records.len();
                records.retain(|_, record| now.duration_since(record.last_verified) < timings.stale_after);
                tracker.last_attempt.retain(|_, attempted| {
                    now.duration_since(*attempted) < timings.stale_after.saturating_mul(2)
                });
                if records.len() != before {
                    publish_snapshot(&records, &snapshots, config.on_peers.as_ref());
                }
            }
        }
    };

    if run_result.is_err() && !records.is_empty() {
        // The socket died: nobody is being verified any more, so the UI must not keep
        // showing a stale online list.
        records.clear();
        publish_snapshot(&records, &snapshots, config.on_peers.as_ref());
    }
    if let Some(socket) = socket.as_ref() {
        send_to_targets(socket, &goodbye, &config.targets).await;
    }
    tracker.probes.abort_all();
    while tracker.probes.join_next().await.is_some() {}
    if let Some(runtime) = mdns.take() {
        shutdown_mdns(runtime).await;
    }
    run_result
}

fn start_mdns(config: &UdpDiscoveryConfig, cancellation: CancellationToken) -> Result<MdnsRuntime> {
    let daemon =
        ServiceDaemon::new().map_err(|error| mdns_error("could not create mDNS daemon", error))?;
    if let Err(error) = daemon.disable_interface(IfKind::IPv6) {
        let _ = daemon.shutdown();
        return Err(mdns_error("could not disable IPv6 mDNS", error));
    }
    let receiver = match daemon.browse(MDNS_SERVICE_TYPE) {
        Ok(receiver) => receiver,
        Err(error) => {
            let _ = daemon.shutdown();
            return Err(mdns_error("could not browse mDNS services", error));
        }
    };
    let service = match mdns_service_info(config) {
        Ok(service) => service,
        Err(error) => {
            let _ = daemon.stop_browse(MDNS_SERVICE_TYPE);
            let _ = daemon.shutdown();
            return Err(error);
        }
    };
    let service_fullname = service.get_fullname().to_owned();
    if let Err(error) = daemon.register(service) {
        let _ = daemon.stop_browse(MDNS_SERVICE_TYPE);
        let _ = daemon.shutdown();
        return Err(mdns_error("could not register mDNS service", error));
    }

    let (sender, events) = mpsc::channel(MAX_MDNS_EVENTS);
    let forward_task = tokio::spawn(async move {
        loop {
            let event = tokio::select! {
                _ = cancellation.cancelled() => break,
                event = receiver.recv_async() => match event {
                    Ok(event) => event,
                    Err(_) => break,
                },
            };
            let event = match event {
                ServiceEvent::ServiceResolved(service) => {
                    Some(MdnsDiscoveryEvent::Resolved(service))
                }
                ServiceEvent::ServiceRemoved(_, fullname) => {
                    Some(MdnsDiscoveryEvent::Removed(fullname))
                }
                _ => None,
            };
            let Some(event) = event else {
                continue;
            };
            let sent = tokio::select! {
                _ = cancellation.cancelled() => false,
                result = sender.send(event) => result.is_ok(),
            };
            if !sent {
                break;
            }
        }
    });

    Ok(MdnsRuntime {
        daemon,
        service_fullname,
        events,
        forward_task,
    })
}

fn mdns_service_info(config: &UdpDiscoveryConfig) -> Result<ServiceInfo> {
    let properties = HashMap::from([
        (
            "version".to_owned(),
            LAN_DISCOVERY_PROTOCOL_VERSION.to_string(),
        ),
        ("instance_id".to_owned(), config.instance_id.clone()),
        (
            "fingerprint".to_owned(),
            config.certificate_fingerprint.clone(),
        ),
        ("transport".to_owned(), MDNS_TRANSPORT.to_owned()),
        ("hash".to_owned(), MDNS_HASH.to_owned()),
    ]);
    let hostname = format!("inkhole-{}.local.", config.instance_id);
    ServiceInfo::new(
        MDNS_SERVICE_TYPE,
        &config.instance_id,
        &hostname,
        "",
        config.quic_port,
        properties,
    )
    .map(ServiceInfo::enable_addr_auto)
    .map_err(|error| mdns_error("could not build mDNS service", error))
}

fn mdns_candidates(service: &ResolvedService) -> Option<Vec<Candidate>> {
    if !service.ty_domain.eq_ignore_ascii_case(MDNS_SERVICE_TYPE)
        || service.get_fullname().is_empty()
        || !service
            .get_fullname()
            .to_ascii_lowercase()
            .ends_with(MDNS_SERVICE_TYPE)
        || service.get_port() == 0
    {
        return None;
    }
    let version = service.get_property_val_str("version")?;
    let instance_id = service.get_property_val_str("instance_id")?;
    let fingerprint = service.get_property_val_str("fingerprint")?;
    let transport = service.get_property_val_str("transport")?;
    let hash = service.get_property_val_str("hash")?;
    if version != LAN_DISCOVERY_PROTOCOL_VERSION.to_string()
        || !valid_instance_id(instance_id)
        || !valid_blake3(fingerprint)
        || transport != MDNS_TRANSPORT
        || hash != MDNS_HASH
    {
        return None;
    }

    let mut addresses = service.get_addresses_v4().into_iter().collect::<Vec<_>>();
    addresses.sort_unstable();
    Some(
        addresses
            .into_iter()
            .filter(|address| {
                !address.is_unspecified()
                    && !address.is_multicast()
                    && *address != Ipv4Addr::BROADCAST
            })
            .map(|address| Candidate {
                key: CandidateKey {
                    instance_id: instance_id.to_owned(),
                    address: SocketAddr::new(IpAddr::V4(address), service.get_port()),
                    fingerprint: fingerprint.to_owned(),
                },
                service_name: service.get_fullname().to_owned(),
            })
            .collect(),
    )
}

async fn receive_udp(
    socket: Option<&UdpSocket>,
    buffer: &mut [u8],
) -> std::io::Result<(usize, SocketAddr)> {
    match socket {
        Some(socket) => socket.recv_from(buffer).await,
        None => std::future::pending().await,
    }
}

async fn receive_mdns_event(runtime: Option<&mut MdnsRuntime>) -> Option<MdnsDiscoveryEvent> {
    match runtime {
        Some(runtime) => runtime.events.recv().await,
        None => std::future::pending().await,
    }
}

async fn shutdown_mdns(runtime: MdnsRuntime) {
    if let Ok(receiver) = runtime.daemon.unregister(&runtime.service_fullname) {
        let _ = tokio::time::timeout(MDNS_CLEANUP_TIMEOUT, receiver.recv_async()).await;
    }
    if let Err(error) = runtime.daemon.stop_browse(MDNS_SERVICE_TYPE) {
        tracing::debug!(%error, "could not stop mDNS browse");
    }
    if let Ok(receiver) = runtime.daemon.shutdown() {
        let _ = tokio::time::timeout(MDNS_CLEANUP_TIMEOUT, receiver.recv_async()).await;
    }
    runtime.forward_task.abort();
    let _ = runtime.forward_task.await;
}

fn mdns_error(context: &str, error: mdns_sd::Error) -> CoreError {
    CoreError::Protocol(format!("{context}: {error}"))
}

/// The in-flight probe set plus the bookkeeping that keeps it from leaking: `pending`
/// suppresses duplicate probes, `owners` maps a task back to its candidate so an
/// aborted or panicked probe still releases its slot, and `last_attempt` rate-limits.
#[derive(Default)]
struct ProbeTracker {
    probes: JoinSet<ProbeOutcome>,
    pending: HashSet<CandidateKey>,
    owners: HashMap<tokio::task::Id, CandidateKey>,
    last_attempt: HashMap<CandidateKey, Instant>,
}

fn schedule_probe(
    tracker: &mut ProbeTracker,
    candidate: Candidate,
    shared_secret: &str,
    cancellation: &CancellationToken,
    timings: UdpDiscoveryTimings,
) {
    if tracker.probes.len() >= MAX_PENDING_PROBES || tracker.pending.contains(&candidate.key) {
        return;
    }
    let now = Instant::now();
    if tracker
        .last_attempt
        .get(&candidate.key)
        .is_some_and(|last| now.duration_since(*last) < timings.min_probe_interval)
    {
        return;
    }
    tracker.last_attempt.insert(candidate.key.clone(), now);
    tracker.pending.insert(candidate.key.clone());
    let probe_key = candidate.key.clone();
    let task_candidate = candidate.clone();
    let task_shared_secret = shared_secret.to_owned();
    let task_cancellation = cancellation.child_token();
    let handle = tracker.probes.spawn(async move {
        let endpoint = PeerEndpoint {
            address: task_candidate.key.address,
            certificate_fingerprint: task_candidate.key.fingerprint.clone(),
        };
        let result = tokio::select! {
            _ = task_cancellation.cancelled() => Err(CoreError::Cancelled),
            result = tokio::time::timeout(
                timings.probe_timeout,
                probe_peer(
                    &endpoint,
                    Some(&task_candidate.key.instance_id),
                    &task_shared_secret,
                    &task_cancellation,
                ),
            ) => match result {
                Ok(result) => result,
                Err(_) => Err(CoreError::Protocol("peer probe timed out".into())),
            },
        };
        ProbeOutcome {
            candidate: task_candidate,
            result,
        }
    });
    tracker.owners.insert(handle.id(), probe_key);
}

fn apply_probe_outcome(
    records: &mut HashMap<String, PeerRecord>,
    outcome: ProbeOutcome,
    max_failures: u8,
) -> bool {
    let ProbeOutcome { candidate, result } = outcome;
    match result {
        Ok(verified) => {
            let host = verified.address.ip().to_string();
            if let Some(record) = records.get_mut(&verified.instance_id) {
                if record.peer.fingerprint != verified.certificate_fingerprint
                    || record.peer.public_key != verified.public_key
                {
                    return false;
                }
                let before = record.peer.clone();
                if !record.peer.hosts.contains(&host) {
                    record.peer.hosts.push(host.clone());
                    record.peer.hosts.sort();
                }
                record.peer.host = host;
                record.peer.port = verified.address.port();
                record.peer.name = verified.name;
                record.peer.capabilities = verified.capabilities;
                record.last_verified = Instant::now();
                record.failures.remove(&verified.address);
                record.farewelled = false;
                return record.peer != before;
            }
            if records.len() >= MAX_DISCOVERED_PEERS {
                return false;
            }
            records.insert(
                verified.instance_id.clone(),
                PeerRecord {
                    peer: DiscoveredPeer {
                        instance_id: verified.instance_id,
                        name: verified.name,
                        host: host.clone(),
                        hosts: vec![host],
                        port: verified.address.port(),
                        capabilities: verified.capabilities,
                        fingerprint: verified.certificate_fingerprint,
                        public_key: verified.public_key,
                        service_name: candidate.service_name,
                    },
                    last_verified: Instant::now(),
                    failures: HashMap::new(),
                    farewelled: false,
                },
            );
            true
        }
        Err(CoreError::Cancelled) => false,
        Err(error) => {
            tracing::debug!(
                instance_id = %candidate.key.instance_id,
                address = %candidate.key.address,
                %error,
                "candidate peer probe failed"
            );
            let key = &candidate.key;
            let should_remove = records
                .get_mut(&key.instance_id)
                .filter(|record| {
                    record.peer.fingerprint == key.fingerprint
                        && record.peer.port == key.address.port()
                        && record.peer.hosts.contains(&key.address.ip().to_string())
                })
                .is_some_and(|record| {
                    if record.farewelled {
                        return true;
                    }
                    let failures = record.failures.entry(key.address).or_default();
                    *failures = failures.saturating_add(1);
                    record.peer.hosts.iter().all(|host| {
                        host.parse::<IpAddr>()
                            .ok()
                            .map(|ip| {
                                record
                                    .failures
                                    .get(&SocketAddr::new(ip, record.peer.port))
                                    .copied()
                                    .unwrap_or_default()
                                    >= max_failures
                            })
                            .unwrap_or(true)
                    })
                });
            if should_remove {
                records.remove(&key.instance_id);
            }
            should_remove
        }
    }
}

fn record_candidates(record: &PeerRecord) -> Vec<Candidate> {
    let mut hosts = record.peer.hosts.clone();
    if hosts.is_empty() {
        hosts.push(record.peer.host.clone());
    }
    hosts
        .into_iter()
        .filter_map(|host| {
            let address = host.parse::<IpAddr>().ok()?;
            Some(Candidate {
                key: CandidateKey {
                    instance_id: record.peer.instance_id.clone(),
                    address: SocketAddr::new(address, record.peer.port),
                    fingerprint: record.peer.fingerprint.clone(),
                },
                service_name: record.peer.service_name.clone(),
            })
        })
        .collect()
}

fn demote_goodbye(
    records: &mut HashMap<String, PeerRecord>,
    goodbye: &UdpAnnouncement,
    sender_ip: IpAddr,
) -> Vec<Candidate> {
    let sender_host = sender_ip.to_string();
    let Some(record) = records.get_mut(&goodbye.instance_id) else {
        return Vec::new();
    };
    if record.peer.fingerprint != goodbye.fingerprint
        || record.peer.port != goodbye.port
        || !record.peer.hosts.contains(&sender_host)
    {
        return Vec::new();
    }
    record.farewelled = true;
    record_candidates(record)
}

fn publish_snapshot(
    records: &HashMap<String, PeerRecord>,
    snapshots: &watch::Sender<Arc<Vec<DiscoveredPeer>>>,
    callback: Option<&DiscoveryPeersCallback>,
) {
    let mut peers = records
        .values()
        .map(|record| record.peer.clone())
        .collect::<Vec<_>>();
    peers.sort_by(|left, right| {
        left.name
            .to_lowercase()
            .cmp(&right.name.to_lowercase())
            .then_with(|| left.instance_id.cmp(&right.instance_id))
    });
    snapshots.send_replace(Arc::new(peers.clone()));
    if let Some(callback) = callback {
        callback(peers);
    }
}

async fn send_to_targets(socket: &UdpSocket, payload: &[u8], targets: &[SocketAddr]) {
    for target in targets {
        if let Err(error) = socket.send_to(payload, target).await {
            tracing::debug!(%target, %error, "UDP discovery send failed");
        }
    }
}

fn validate_config(config: &mut UdpDiscoveryConfig) -> Result<()> {
    config.instance_id = config.instance_id.trim().to_ascii_lowercase();
    config.certificate_fingerprint = config.certificate_fingerprint.trim().to_ascii_lowercase();
    if !config.enable_udp && !config.enable_mdns {
        return Err(CoreError::InvalidRequest(
            "at least one LAN discovery transport must be enabled".into(),
        ));
    }
    if config.enable_udp && !config.bind_address.is_ipv4() {
        return Err(CoreError::InvalidRequest(
            "UDP discovery currently requires an IPv4 bind address".into(),
        ));
    }
    if !valid_instance_id(&config.instance_id) {
        return Err(CoreError::InvalidIdentity(
            "invalid discovery instance id".into(),
        ));
    }
    if config.quic_port == 0 {
        return Err(CoreError::InvalidRequest(
            "invalid discovery QUIC port".into(),
        ));
    }
    if !valid_blake3(&config.certificate_fingerprint) {
        return Err(CoreError::InvalidIdentity(
            "invalid discovery certificate fingerprint".into(),
        ));
    }
    let timings = config.timings;
    if timings.announce_interval.is_zero()
        || timings.refresh_burst_gap.is_zero()
        || timings.probe_timeout.is_zero()
        || timings.min_probe_interval.is_zero()
        || timings.liveness_interval.is_zero()
        || timings.prune_interval.is_zero()
        || timings.stale_after.is_zero()
        || timings.refresh_burst == 0
        || timings.max_failures == 0
    {
        return Err(CoreError::InvalidRequest(
            "invalid LAN discovery timing configuration".into(),
        ));
    }
    if config.enable_udp {
        if config
            .targets
            .iter()
            .any(|target| !target.is_ipv4() || target.port() == 0 || target.ip().is_unspecified())
        {
            return Err(CoreError::InvalidRequest(
                "invalid IPv4 UDP discovery target".into(),
            ));
        }
        if config.targets.is_empty() {
            config.targets.push(SocketAddr::new(
                IpAddr::V4(Ipv4Addr::BROADCAST),
                UDP_DISCOVERY_PORT,
            ));
        }
        config.targets.sort_unstable();
        config.targets.dedup();
    }
    Ok(())
}

fn bind_udp_socket(address: SocketAddr) -> Result<UdpSocket> {
    let socket = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP))?;
    socket.set_reuse_address(true)?;
    socket.set_broadcast(true)?;
    socket.set_nonblocking(true)?;
    socket.bind(&SockAddr::from(address))?;
    let socket: std::net::UdpSocket = socket.into();
    Ok(UdpSocket::from_std(socket)?)
}

fn encode_announcement(config: &UdpDiscoveryConfig, reply: bool, bye: bool) -> Result<Vec<u8>> {
    let payload = serde_json::to_vec(&UdpAnnouncement {
        magic: UDP_DISCOVERY_MAGIC.into(),
        version: LAN_DISCOVERY_PROTOCOL_VERSION,
        instance_id: config.instance_id.clone(),
        port: config.quic_port,
        fingerprint: config.certificate_fingerprint.clone(),
        reply,
        bye,
    })?;
    if payload.len() > MAX_UDP_PACKET {
        return Err(CoreError::Protocol(
            "UDP discovery announcement is too large".into(),
        ));
    }
    Ok(payload)
}

fn decode_announcement(payload: &[u8]) -> Option<UdpAnnouncement> {
    if payload.is_empty() || payload.len() > MAX_UDP_PACKET {
        return None;
    }
    let decoded: UdpAnnouncement = serde_json::from_slice(payload).ok()?;
    if decoded.magic != UDP_DISCOVERY_MAGIC
        || decoded.version != LAN_DISCOVERY_PROTOCOL_VERSION
        || !valid_instance_id(&decoded.instance_id)
        || decoded.port == 0
        || !valid_blake3(&decoded.fingerprint)
    {
        return None;
    }
    Some(decoded)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{DeviceIdentity, QuicServer, QuicServerConfig};

    fn reserve_udp_port() -> u16 {
        let socket = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        socket.local_addr().unwrap().port()
    }

    fn test_timings() -> UdpDiscoveryTimings {
        UdpDiscoveryTimings {
            announce_interval: Duration::from_millis(100),
            refresh_burst_gap: Duration::from_millis(20),
            refresh_burst: 2,
            probe_timeout: Duration::from_millis(500),
            min_probe_interval: Duration::from_millis(40),
            liveness_interval: Duration::from_millis(100),
            prune_interval: Duration::from_millis(30),
            stale_after: Duration::from_millis(700),
            max_failures: 2,
        }
    }

    fn discovery_config(
        identity: &DeviceIdentity,
        quic_port: u16,
        discovery_port: u16,
        targets: Vec<SocketAddr>,
    ) -> UdpDiscoveryConfig {
        UdpDiscoveryConfig {
            enable_udp: true,
            enable_mdns: false,
            bind_address: SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), discovery_port),
            targets,
            instance_id: identity.instance_id().to_owned(),
            quic_port,
            certificate_fingerprint: identity.certificate_fingerprint().to_owned(),
            shared_secret: String::new(),
            timings: test_timings(),
            on_peers: None,
        }
    }

    async fn wait_for_peer_count(discovery: &UdpDiscovery, count: usize) {
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if discovery.peers().len() == count {
                    return;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("expected {count} peers, found {:?}", discovery.peers()));
    }

    fn test_mdns_properties(instance_id: &str, fingerprint: &str) -> HashMap<String, String> {
        HashMap::from([
            (
                "version".to_owned(),
                LAN_DISCOVERY_PROTOCOL_VERSION.to_string(),
            ),
            ("instance_id".to_owned(), instance_id.to_owned()),
            ("fingerprint".to_owned(), fingerprint.to_owned()),
            ("transport".to_owned(), MDNS_TRANSPORT.to_owned()),
            ("hash".to_owned(), MDNS_HASH.to_owned()),
        ])
    }

    fn resolved_mdns_service(
        instance_name: &str,
        address: &str,
        port: u16,
        properties: HashMap<String, String>,
    ) -> ResolvedService {
        ServiceInfo::new(
            MDNS_SERVICE_TYPE,
            instance_name,
            &format!("{instance_name}.local."),
            address,
            port,
            properties,
        )
        .unwrap()
        .as_resolved_service()
    }

    #[test]
    fn udp_v4_packet_rejects_old_versions_and_unbounded_payloads() {
        let identity = DeviceIdentity::generate(None, "Packet test").unwrap();
        let config = discovery_config(&identity, 42_000, 0, vec!["127.0.0.1:9".parse().unwrap()]);
        let payload = encode_announcement(&config, false, false).unwrap();
        let decoded = decode_announcement(&payload).unwrap();
        assert_eq!(decoded.version, LAN_DISCOVERY_PROTOCOL_VERSION);
        assert_eq!(decoded.instance_id, identity.instance_id());

        let mut old: serde_json::Value = serde_json::from_slice(&payload).unwrap();
        old["version"] = serde_json::json!(3);
        assert!(decode_announcement(&serde_json::to_vec(&old).unwrap()).is_none());
        assert!(decode_announcement(&vec![b'x'; MAX_UDP_PACKET + 1]).is_none());
    }

    #[test]
    fn mdns_txt_is_strictly_parsed_and_non_unicast_addresses_are_ignored() {
        let identity = DeviceIdentity::generate(None, "mDNS parser").unwrap();
        let properties =
            test_mdns_properties(identity.instance_id(), identity.certificate_fingerprint());
        let resolved = resolved_mdns_service("peer", "127.0.0.1", 42_000, properties.clone());
        let candidates = mdns_candidates(&resolved).unwrap();
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].key.instance_id, identity.instance_id());
        assert_eq!(
            candidates[0].key.address,
            "127.0.0.1:42000".parse().unwrap()
        );
        assert_eq!(candidates[0].service_name, resolved.get_fullname());

        for required in ["version", "instance_id", "fingerprint", "transport", "hash"] {
            let mut missing = properties.clone();
            missing.remove(required);
            let resolved = resolved_mdns_service("missing", "127.0.0.1", 42_000, missing);
            assert!(
                mdns_candidates(&resolved).is_none(),
                "accepted missing {required}"
            );
        }

        for (key, value) in [
            ("version", "04".to_owned()),
            ("instance_id", identity.instance_id().to_ascii_uppercase()),
            (
                "fingerprint",
                identity.certificate_fingerprint().to_ascii_uppercase(),
            ),
            ("transport", "tcp".to_owned()),
            ("hash", "sha256".to_owned()),
        ] {
            let mut invalid = properties.clone();
            invalid.insert(key.to_owned(), value);
            let resolved = resolved_mdns_service("invalid", "127.0.0.1", 42_000, invalid);
            assert!(
                mdns_candidates(&resolved).is_none(),
                "accepted invalid {key}"
            );
        }

        let zero_port = resolved_mdns_service("zero", "127.0.0.1", 0, properties.clone());
        assert!(mdns_candidates(&zero_port).is_none());
        let unspecified = resolved_mdns_service("unspecified", "0.0.0.0", 42_000, properties);
        assert!(mdns_candidates(&unspecified).unwrap().is_empty());
    }

    #[test]
    fn udp_and_mdns_candidates_share_the_same_deduplication_key() {
        let identity = DeviceIdentity::generate(None, "Dedup").unwrap();
        let properties =
            test_mdns_properties(identity.instance_id(), identity.certificate_fingerprint());
        let first = mdns_candidates(&resolved_mdns_service(
            "first",
            "127.0.0.1",
            42_000,
            properties.clone(),
        ))
        .unwrap()
        .remove(0);
        let second = mdns_candidates(&resolved_mdns_service(
            "second",
            "127.0.0.1",
            42_000,
            properties,
        ))
        .unwrap()
        .remove(0);
        assert_ne!(first.service_name, second.service_name);
        let candidates = HashSet::from([first.key, second.key]);
        assert_eq!(candidates.len(), 1);
    }

    #[test]
    fn discovery_requires_at_least_one_transport_but_mdns_does_not_bind_udp() {
        let identity = DeviceIdentity::generate(None, "Config").unwrap();
        let mut disabled = discovery_config(&identity, 42_000, 0, Vec::new());
        disabled.enable_udp = false;
        assert!(validate_config(&mut disabled).is_err());

        let mut mdns_only = discovery_config(&identity, 42_000, 0, Vec::new());
        mdns_only.enable_udp = false;
        mdns_only.enable_mdns = true;
        mdns_only.bind_address = "[::1]:41301".parse().unwrap();
        validate_config(&mut mdns_only).unwrap();
        assert!(mdns_only.targets.is_empty());

        let manual: SocketAddr = "192.0.2.10:41301".parse().unwrap();
        let mut udp = discovery_config(&identity, 42_000, 0, vec![manual, manual]);
        validate_config(&mut udp).unwrap();
        assert_eq!(udp.targets, vec![manual]);
        assert!(udp.targets.contains(&manual));

        let mut invalid =
            discovery_config(&identity, 42_000, 0, vec!["[::1]:41301".parse().unwrap()]);
        assert!(validate_config(&mut invalid).is_err());
    }

    #[tokio::test]
    async fn forged_mdns_candidates_never_enter_the_verified_registry() {
        let root = tempfile::tempdir().unwrap();
        let genuine = DeviceIdentity::generate(None, "Genuine mDNS").unwrap();
        let impostor = DeviceIdentity::generate(None, "Impostor mDNS").unwrap();
        let server = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: root.path().join("inbox"),
            inbox_category_roots: crate::InboxCategoryRoots::default(),
            identity: genuine.clone(),
            capabilities: vec!["quic-v2".into()],
            shared_secret: String::new(),
        })
        .await
        .unwrap();
        let cancellation = CancellationToken::new();
        let mut records = HashMap::new();

        let forged = resolved_mdns_service(
            "forged-fingerprint",
            "127.0.0.1",
            server.local_address().port(),
            test_mdns_properties(genuine.instance_id(), impostor.certificate_fingerprint()),
        );
        let candidate = mdns_candidates(&forged).unwrap().remove(0);
        let endpoint = PeerEndpoint {
            address: candidate.key.address,
            certificate_fingerprint: candidate.key.fingerprint.clone(),
        };
        let result = probe_peer(
            &endpoint,
            Some(&candidate.key.instance_id),
            "",
            &cancellation,
        )
        .await;
        // A certificate mismatch must not publish a snapshot when no verified
        // record exists for the forged candidate.
        assert!(!apply_probe_outcome(
            &mut records,
            ProbeOutcome { candidate, result },
            2,
        ));
        assert!(records.is_empty());

        let forged = resolved_mdns_service(
            "forged-instance",
            "127.0.0.1",
            server.local_address().port(),
            test_mdns_properties(impostor.instance_id(), genuine.certificate_fingerprint()),
        );
        let candidate = mdns_candidates(&forged).unwrap().remove(0);
        let endpoint = PeerEndpoint {
            address: candidate.key.address,
            certificate_fingerprint: candidate.key.fingerprint.clone(),
        };
        let result = probe_peer(
            &endpoint,
            Some(&candidate.key.instance_id),
            "",
            &cancellation,
        )
        .await;
        assert!(!apply_probe_outcome(
            &mut records,
            ProbeOutcome { candidate, result },
            2,
        ));
        assert!(records.is_empty());

        server.close().await.unwrap();
    }

    #[tokio::test]
    async fn two_udp_nodes_discover_verified_quic_peers_and_process_goodbye() {
        let root = tempfile::tempdir().unwrap();
        let identity_a = DeviceIdentity::generate(None, "Alpha").unwrap();
        let identity_b = DeviceIdentity::generate(None, "Beta").unwrap();
        let server_a = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: root.path().join("a"),
            inbox_category_roots: crate::InboxCategoryRoots::default(),
            identity: identity_a.clone(),
            capabilities: vec!["quic-v2".into(), "blake3".into()],
            shared_secret: String::new(),
        })
        .await
        .unwrap();
        let server_b = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: root.path().join("b"),
            inbox_category_roots: crate::InboxCategoryRoots::default(),
            identity: identity_b.clone(),
            capabilities: vec!["quic-v2".into()],
            shared_secret: String::new(),
        })
        .await
        .unwrap();
        let discovery_port_a = reserve_udp_port();
        let discovery_port_b = reserve_udp_port();
        let discovery_a = UdpDiscovery::start(discovery_config(
            &identity_a,
            server_a.local_address().port(),
            discovery_port_a,
            vec![SocketAddr::new(
                IpAddr::V4(Ipv4Addr::LOCALHOST),
                discovery_port_b,
            )],
        ))
        .await
        .unwrap();
        let discovery_b = UdpDiscovery::start(discovery_config(
            &identity_b,
            server_b.local_address().port(),
            discovery_port_b,
            vec![SocketAddr::new(
                IpAddr::V4(Ipv4Addr::LOCALHOST),
                discovery_port_a,
            )],
        ))
        .await
        .unwrap();

        wait_for_peer_count(&discovery_a, 1).await;
        wait_for_peer_count(&discovery_b, 1).await;
        assert_eq!(discovery_a.peers()[0].instance_id, identity_b.instance_id());
        assert_eq!(discovery_a.peers()[0].capabilities, vec!["quic-v2"]);
        assert_eq!(discovery_b.peers()[0].instance_id, identity_a.instance_id());

        // A goodbye is now only a hint: the peer is dropped once it also stops answering
        // probes, so the QUIC server has to go down with its discovery task.
        discovery_b.close().await.unwrap();
        server_b.close().await.unwrap();
        wait_for_peer_count(&discovery_a, 0).await;
        discovery_a.close().await.unwrap();
        server_a.close().await.unwrap();
    }

    #[tokio::test]
    async fn forged_udp_candidates_never_enter_the_verified_registry() {
        let root = tempfile::tempdir().unwrap();
        let observer = DeviceIdentity::generate(None, "Observer").unwrap();
        let genuine = DeviceIdentity::generate(None, "Genuine").unwrap();
        let impostor = DeviceIdentity::generate(None, "Impostor").unwrap();
        let server = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: root.path().join("inbox"),
            inbox_category_roots: crate::InboxCategoryRoots::default(),
            identity: genuine.clone(),
            capabilities: vec!["quic-v2".into()],
            shared_secret: String::new(),
        })
        .await
        .unwrap();
        let discovery = UdpDiscovery::start(discovery_config(
            &observer,
            42_001,
            0,
            vec!["127.0.0.1:9".parse().unwrap()],
        ))
        .await
        .unwrap();
        let sender = UdpSocket::bind("127.0.0.1:0").await.unwrap();

        let wrong_fingerprint = discovery_config(
            &impostor,
            server.local_address().port(),
            0,
            vec!["127.0.0.1:9".parse().unwrap()],
        );
        sender
            .send_to(
                &encode_announcement(&wrong_fingerprint, false, false).unwrap(),
                discovery.local_address(),
            )
            .await
            .unwrap();

        let wrong_instance = UdpDiscoveryConfig {
            certificate_fingerprint: genuine.certificate_fingerprint().to_owned(),
            ..wrong_fingerprint.clone()
        };
        sender
            .send_to(
                &encode_announcement(&wrong_instance, false, false).unwrap(),
                discovery.local_address(),
            )
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(650)).await;
        assert!(discovery.peers().is_empty());

        let genuine_announcement = discovery_config(
            &genuine,
            server.local_address().port(),
            0,
            vec!["127.0.0.1:9".parse().unwrap()],
        );
        sender
            .send_to(
                &encode_announcement(&genuine_announcement, false, false).unwrap(),
                discovery.local_address(),
            )
            .await
            .unwrap();
        wait_for_peer_count(&discovery, 1).await;
        assert_eq!(discovery.peers()[0].instance_id, genuine.instance_id());

        discovery.close().await.unwrap();
        server.close().await.unwrap();
    }

    #[test]
    fn a_goodbye_only_evicts_a_peer_that_also_stops_answering_probes() {
        let instance_id = "0123456789abcdef0123456789abcdef";
        let fingerprint = "00".repeat(32);
        let address: SocketAddr = "192.168.1.20:41302".parse().unwrap();
        let peer = DiscoveredPeer {
            instance_id: instance_id.into(),
            name: "farewell".into(),
            host: address.ip().to_string(),
            hosts: vec![address.ip().to_string()],
            port: address.port(),
            capabilities: Vec::new(),
            fingerprint: fingerprint.clone(),
            public_key: "public-key".into(),
            service_name: "udp-v4".into(),
        };
        let record = || PeerRecord {
            peer: peer.clone(),
            last_verified: Instant::now(),
            failures: HashMap::new(),
            farewelled: false,
        };
        let goodbye = UdpAnnouncement {
            magic: UDP_DISCOVERY_MAGIC.into(),
            version: LAN_DISCOVERY_PROTOCOL_VERSION,
            instance_id: instance_id.into(),
            port: address.port(),
            fingerprint: fingerprint.clone(),
            reply: false,
            bye: true,
        };
        let candidate = Candidate {
            key: CandidateKey {
                instance_id: instance_id.into(),
                address,
                fingerprint: fingerprint.clone(),
            },
            service_name: "udp-v4".into(),
        };

        // A forged goodbye followed by a peer that still answers must not evict it.
        let mut records = HashMap::from([(instance_id.to_owned(), record())]);
        assert_eq!(
            demote_goodbye(&mut records, &goodbye, address.ip()).len(),
            1
        );
        apply_probe_outcome(
            &mut records,
            ProbeOutcome {
                candidate: candidate.clone(),
                result: Ok(VerifiedPeer {
                    address,
                    instance_id: instance_id.into(),
                    name: "farewell".into(),
                    capabilities: Vec::new(),
                    public_key: "public-key".into(),
                    certificate_fingerprint: fingerprint.clone(),
                }),
            },
            3,
        );
        assert!(records.contains_key(instance_id));
        assert!(!records[instance_id].farewelled);

        // A genuine goodbye is confirmed by the first failing probe, well before
        // `max_failures` would have expired the peer.
        let mut records = HashMap::from([(instance_id.to_owned(), record())]);
        demote_goodbye(&mut records, &goodbye, address.ip());
        assert!(apply_probe_outcome(
            &mut records,
            ProbeOutcome {
                candidate,
                result: Err(CoreError::Protocol("peer is gone".into())),
            },
            3,
        ));
        assert!(records.is_empty());
    }

    #[test]
    fn liveness_candidates_include_all_verified_hosts() {
        let record = PeerRecord {
            peer: DiscoveredPeer {
                instance_id: "0123456789abcdef0123456789abcdef".into(),
                name: "multi-homed".into(),
                host: "192.168.1.20".into(),
                hosts: vec!["192.168.1.20".into(), "10.0.0.20".into()],
                port: 41_302,
                capabilities: Vec::new(),
                fingerprint: "00".repeat(32),
                public_key: "public-key".into(),
                service_name: "udp-v4".into(),
            },
            last_verified: Instant::now(),
            failures: HashMap::new(),
            farewelled: false,
        };
        let addresses = record_candidates(&record)
            .into_iter()
            .map(|candidate| candidate.key.address)
            .collect::<Vec<_>>();
        assert_eq!(
            addresses,
            vec![
                "192.168.1.20:41302".parse().unwrap(),
                "10.0.0.20:41302".parse().unwrap(),
            ]
        );
    }

    #[test]
    fn liveness_does_not_remove_a_peer_when_another_address_is_healthy() {
        let instance_id = "0123456789abcdef0123456789abcdef";
        let fingerprint = "00".repeat(32);
        let first: SocketAddr = "192.168.1.20:41302".parse().unwrap();
        let second: SocketAddr = "10.0.0.20:41302".parse().unwrap();
        let mut records = HashMap::from([(
            instance_id.to_owned(),
            PeerRecord {
                peer: DiscoveredPeer {
                    instance_id: instance_id.into(),
                    name: "multi-homed".into(),
                    host: first.ip().to_string(),
                    hosts: vec![first.ip().to_string(), second.ip().to_string()],
                    port: first.port(),
                    capabilities: Vec::new(),
                    fingerprint: fingerprint.clone(),
                    public_key: "public-key".into(),
                    service_name: "udp-v4".into(),
                },
                last_verified: Instant::now(),
                failures: HashMap::new(),
                farewelled: false,
            },
        )]);

        for _ in 0..3 {
            assert!(!apply_probe_outcome(
                &mut records,
                ProbeOutcome {
                    candidate: Candidate {
                        key: CandidateKey {
                            instance_id: instance_id.into(),
                            address: first,
                            fingerprint: fingerprint.clone(),
                        },
                        service_name: "udp-v4".into(),
                    },
                    result: Err(CoreError::Protocol("first address is offline".into())),
                },
                3,
            ));
            assert!(records.contains_key(instance_id));
        }

        assert!(apply_probe_outcome(
            &mut records,
            ProbeOutcome {
                candidate: Candidate {
                    key: CandidateKey {
                        instance_id: instance_id.into(),
                        address: second,
                        fingerprint: fingerprint.clone(),
                    },
                    service_name: "udp-v4".into(),
                },
                result: Ok(VerifiedPeer {
                    address: second,
                    instance_id: instance_id.into(),
                    name: "multi-homed".into(),
                    capabilities: Vec::new(),
                    public_key: "public-key".into(),
                    certificate_fingerprint: fingerprint,
                }),
            },
            3,
        ));
        assert!(records.contains_key(instance_id));
    }
}
