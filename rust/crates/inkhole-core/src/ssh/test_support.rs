use std::{
    collections::HashMap,
    net::{Ipv4Addr, SocketAddr},
    sync::{
        Arc, Mutex as StdMutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use russh::{
    Channel, ChannelOpenFailure, Disconnect,
    keys::{
        Algorithm, HashAlg, PrivateKey, PublicKey,
        signature::rand_core::{Infallible, TryCryptoRng, TryRng},
        ssh_key::LineEnding,
    },
    server::{self, Msg, Server as _, Session},
};
use tokio::{
    net::{TcpListener, TcpStream},
    sync::oneshot,
    task::{JoinHandle, JoinSet},
};
use tokio_util::sync::CancellationToken;

use super::client::SshProfile;

struct KeyRng;

impl TryRng for KeyRng {
    type Error = Infallible;

    fn try_next_u32(&mut self) -> Result<u32, Self::Error> {
        Ok(rand::random())
    }

    fn try_next_u64(&mut self) -> Result<u64, Self::Error> {
        Ok(rand::random())
    }

    fn try_fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), Self::Error> {
        for chunk in destination.chunks_mut(size_of::<u64>()) {
            let random = rand::random::<u64>().to_le_bytes();
            chunk.copy_from_slice(&random[..chunk.len()]);
        }
        Ok(())
    }
}

impl TryCryptoRng for KeyRng {}

struct ForwardTarget {
    owner: u64,
    handle: server::Handle,
    cancellation: CancellationToken,
}

#[derive(Default)]
struct RelayState {
    forwards: HashMap<u16, ForwardTarget>,
}

impl RelayState {
    fn remove_owner(&mut self, owner: u64) {
        self.forwards.retain(|_, target| {
            let keep = target.owner != owner;
            if !keep {
                target.cancellation.cancel();
            }
            keep
        });
    }
}

struct RelayServer {
    state: Arc<StdMutex<RelayState>>,
    accepted_key: PublicKey,
    next_connection: Arc<AtomicU64>,
}

impl server::Server for RelayServer {
    type Handler = RelayHandler;

    fn new_client(&mut self, _peer_addr: Option<SocketAddr>) -> Self::Handler {
        RelayHandler {
            id: self.next_connection.fetch_add(1, Ordering::Relaxed),
            state: self.state.clone(),
            accepted_key: self.accepted_key.clone(),
        }
    }
}

struct RelayHandler {
    id: u64,
    state: Arc<StdMutex<RelayState>>,
    accepted_key: PublicKey,
}

impl server::Handler for RelayHandler {
    type Error = russh::Error;

    async fn auth_publickey(
        &mut self,
        user: &str,
        public_key: &PublicKey,
    ) -> Result<server::Auth, Self::Error> {
        Ok(
            if user == "inkhole-test" && public_key == &self.accepted_key {
                server::Auth::Accept
            } else {
                server::Auth::reject()
            },
        )
    }

    async fn tcpip_forward(
        &mut self,
        address: &str,
        port: &mut u32,
        session: &mut Session,
    ) -> Result<bool, Self::Error> {
        if address != "127.0.0.1" {
            return Ok(false);
        }
        let Ok(requested_port) = u16::try_from(*port) else {
            return Ok(false);
        };
        if requested_port != 0
            && self
                .state
                .lock()
                .unwrap_or_else(|lock| lock.into_inner())
                .forwards
                .contains_key(&requested_port)
        {
            return Ok(false);
        }
        let listener = match TcpListener::bind((Ipv4Addr::LOCALHOST, requested_port)).await {
            Ok(listener) => listener,
            Err(_) => return Ok(false),
        };
        let selected_port = listener.local_addr()?.port();
        let handle = session.handle();
        let cancellation = CancellationToken::new();
        {
            let mut state = self.state.lock().unwrap_or_else(|lock| lock.into_inner());
            if state.forwards.contains_key(&selected_port) {
                return Ok(false);
            }
            state.forwards.insert(
                selected_port,
                ForwardTarget {
                    owner: self.id,
                    handle: handle.clone(),
                    cancellation: cancellation.clone(),
                },
            );
        }
        tokio::spawn(accept_forwarded_connections(
            listener,
            selected_port,
            handle,
            cancellation,
        ));
        if *port == 0 {
            *port = u32::from(selected_port);
        }
        Ok(true)
    }

    async fn cancel_tcpip_forward(
        &mut self,
        address: &str,
        port: u32,
        _session: &mut Session,
    ) -> Result<bool, Self::Error> {
        let Ok(port) = u16::try_from(port) else {
            return Ok(false);
        };
        if address != "127.0.0.1" {
            return Ok(false);
        }
        let removed = {
            let mut state = self.state.lock().unwrap_or_else(|lock| lock.into_inner());
            if state
                .forwards
                .get(&port)
                .is_some_and(|target| target.owner == self.id)
            {
                state.forwards.remove(&port)
            } else {
                None
            }
        };
        if let Some(target) = &removed {
            target.cancellation.cancel();
        }
        Ok(removed.is_some())
    }

    #[allow(clippy::too_many_arguments)]
    async fn channel_open_direct_tcpip(
        &mut self,
        channel: Channel<Msg>,
        host_to_connect: &str,
        port_to_connect: u32,
        _originator_address: &str,
        _originator_port: u32,
        reply: server::ChannelOpenHandle,
        _session: &mut Session,
    ) -> Result<(), Self::Error> {
        let target = u16::try_from(port_to_connect).ok().and_then(|port| {
            self.state
                .lock()
                .unwrap_or_else(|lock| lock.into_inner())
                .forwards
                .get(&port)
                .map(|target| target.cancellation.child_token())
        });
        let Some(cancellation) = target.filter(|_| host_to_connect == "127.0.0.1") else {
            reply.reject(ChannelOpenFailure::ConnectFailed).await;
            return Ok(());
        };
        let stream = TcpStream::connect((Ipv4Addr::LOCALHOST, port_to_connect as u16)).await;
        let Ok(stream) = stream else {
            reply.reject(ChannelOpenFailure::ConnectFailed).await;
            return Ok(());
        };
        reply.accept().await;
        tokio::spawn(bridge_tcp_channel(stream, channel, cancellation));
        Ok(())
    }
}

async fn accept_forwarded_connections(
    listener: TcpListener,
    connected_port: u16,
    handle: server::Handle,
    cancellation: CancellationToken,
) {
    let mut connections = JoinSet::new();
    loop {
        tokio::select! {
            _ = cancellation.cancelled() => break,
            accepted = listener.accept() => {
                let Ok((stream, originator)) = accepted else { break };
                let handle = handle.clone();
                let connection_cancellation = cancellation.child_token();
                connections.spawn(async move {
                    let channel = handle
                        .channel_open_forwarded_tcpip(
                            "127.0.0.1",
                            u32::from(connected_port),
                            originator.ip().to_string(),
                            u32::from(originator.port()),
                        )
                        .await;
                    if let Ok(channel) = channel {
                        bridge_tcp_channel(stream, channel, connection_cancellation).await;
                    }
                });
            }
            result = connections.join_next(), if !connections.is_empty() => {
                let _ = result;
            }
        }
    }
    connections.abort_all();
    while connections.join_next().await.is_some() {}
}

async fn bridge_tcp_channel(
    mut stream: TcpStream,
    channel: Channel<Msg>,
    cancellation: CancellationToken,
) {
    let mut channel = channel.into_stream();
    tokio::select! {
        _ = cancellation.cancelled() => {}
        _ = tokio::io::copy_bidirectional(&mut stream, &mut channel) => {}
    }
}

impl Drop for RelayHandler {
    fn drop(&mut self) {
        self.state
            .lock()
            .unwrap_or_else(|lock| lock.into_inner())
            .remove_owner(self.id);
    }
}

pub(crate) struct MockSshRelay {
    address: SocketAddr,
    private_key: String,
    host_fingerprint: String,
    state: Arc<StdMutex<RelayState>>,
    shutdown: Option<server::RunningServerHandle>,
    task: Option<JoinHandle<std::io::Result<()>>>,
}

impl MockSshRelay {
    pub(crate) async fn start() -> Self {
        let host_key = PrivateKey::random(&mut KeyRng, Algorithm::Ed25519).unwrap();
        let client_key = PrivateKey::random(&mut KeyRng, Algorithm::Ed25519).unwrap();
        let host_fingerprint = host_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string();
        let private_key = client_key.to_openssh(LineEnding::LF).unwrap().to_string();
        let accepted_key = client_key.public_key().clone();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let state = Arc::new(StdMutex::new(RelayState::default()));
        let server_state = state.clone();
        let config = Arc::new(server::Config {
            keys: vec![host_key],
            auth_rejection_time: Duration::ZERO,
            auth_rejection_time_initial: Some(Duration::ZERO),
            inactivity_timeout: None,
            window_size: 16 * 1024 * 1024,
            channel_buffer_size: 4_096,
            event_buffer_size: 4_096,
            nodelay: true,
            ..server::Config::default()
        });
        let (started_tx, started_rx) = oneshot::channel();
        let task = tokio::spawn(async move {
            let mut server = RelayServer {
                state: server_state,
                accepted_key,
                next_connection: Arc::new(AtomicU64::new(1)),
            };
            let running = server.run_on_socket(config, &listener);
            let _ = started_tx.send(running.handle());
            running.await
        });
        let shutdown = started_rx.await.unwrap();
        Self {
            address,
            private_key,
            host_fingerprint,
            state,
            shutdown: Some(shutdown),
            task: Some(task),
        }
    }

    pub(crate) fn profile(&self) -> SshProfile {
        SshProfile {
            id: "test-relay".into(),
            host: self.address.ip().to_string(),
            port: self.address.port(),
            user: "inkhole-test".into(),
            private_key: self.private_key.clone(),
            private_key_label: "test key".into(),
            passphrase: String::new(),
            host_key_sha256: self.host_fingerprint.clone(),
        }
    }

    pub(crate) fn active_ports(&self) -> Vec<u16> {
        let mut ports = self
            .state
            .lock()
            .unwrap_or_else(|lock| lock.into_inner())
            .forwards
            .keys()
            .copied()
            .collect::<Vec<_>>();
        ports.sort_unstable();
        ports
    }

    pub(crate) async fn disconnect_port(&self, port: u16) -> bool {
        let handle = self
            .state
            .lock()
            .unwrap_or_else(|lock| lock.into_inner())
            .forwards
            .get(&port)
            .map(|target| target.handle.clone());
        let Some(handle) = handle else {
            return false;
        };
        handle
            .disconnect(
                Disconnect::ConnectionLost,
                "test disconnect".into(),
                "en".into(),
            )
            .await
            .is_ok()
    }

    pub(crate) async fn close(mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            shutdown.shutdown("test complete".into());
        }
        if let Some(task) = self.task.take() {
            task.await.unwrap().unwrap();
        }
    }
}

impl Drop for MockSshRelay {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            shutdown.shutdown("test dropped".into());
        }
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}
