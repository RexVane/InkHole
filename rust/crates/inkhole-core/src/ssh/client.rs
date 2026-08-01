use std::{
    sync::{
        Arc, Mutex as StdMutex,
        atomic::{AtomicBool, AtomicU32, Ordering},
    },
    time::Duration,
};

use russh::{
    Channel, ChannelOpenFailure, Disconnect,
    client::{self, Config, Handler, Msg},
    keys::{HashAlg, PrivateKeyWithHashAlg, decode_secret_key, ssh_key},
};
use serde::{Deserialize, Serialize};
use subtle::ConstantTimeEq;
use tokio::sync::{Mutex, mpsc, watch};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    net::TcpStream,
};
use tokio_util::sync::CancellationToken;

use crate::{CoreError, Result};

const SSH_CONNECT_TIMEOUT: Duration = Duration::from_secs(20);
const SSH_OPERATION_TIMEOUT: Duration = Duration::from_secs(20);
const SSH_KEEPALIVE_INTERVAL: Duration = Duration::from_secs(30);
const INCOMING_CHANNEL_CAPACITY: usize = 64;
const MAX_PRIVATE_KEY_BYTES: usize = 1024 * 1024;

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub(crate) struct SshProfile {
    pub id: String,
    pub host: String,
    pub port: u16,
    pub user: String,
    pub private_key: String,
    pub private_key_label: String,
    pub passphrase: String,
    pub host_key_sha256: String,
}

impl SshProfile {
    pub(crate) fn validate_relay(&self) -> Result<()> {
        let profile = self.clone().normalize(true)?;
        parse_private_key(&profile).map(|_| ())
    }

    fn normalize(mut self, require_fingerprint: bool) -> Result<Self> {
        self.id = self.id.trim().to_owned();
        self.host = self.host.trim().to_owned();
        self.user = self.user.trim().to_owned();
        self.private_key_label = self.private_key_label.trim().to_owned();
        self.host_key_sha256 = self.host_key_sha256.trim().to_owned();
        if self.port == 0 {
            self.port = 22;
        }
        if self.host.is_empty()
            || self.host.len() > 255
            || self.user.is_empty()
            || self.user.len() > 255
            || self.private_key.is_empty()
            || self.private_key.len() > MAX_PRIVATE_KEY_BYTES
            || self.host.bytes().any(invalid_text_byte)
            || self.user.bytes().any(invalid_text_byte)
        {
            return Err(CoreError::InvalidRequest(
                "SSH host, user and a valid private key are required".into(),
            ));
        }
        if require_fingerprint && !valid_host_fingerprint(&self.host_key_sha256) {
            return Err(CoreError::InvalidRequest(
                "confirm the SSH host fingerprint before enabling the relay".into(),
            ));
        }
        Ok(self)
    }
}

fn invalid_text_byte(byte: u8) -> bool {
    matches!(byte, b'\r' | b'\n' | 0)
}

pub(crate) struct IncomingSshChannel {
    pub channel: Channel<Msg>,
    pub connected_address: String,
    pub connected_port: u16,
    pub originator_address: String,
    pub originator_port: u16,
}

impl std::fmt::Debug for IncomingSshChannel {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("IncomingSshChannel")
            .field("channel", &self.channel)
            .field("connected_address", &self.connected_address)
            .field("connected_port", &self.connected_port)
            .field("originator_address", &self.originator_address)
            .field("originator_port", &self.originator_port)
            .finish()
    }
}

/// Tracks which reverse-forwarded port inbound channels may use.
///
/// The expectation is published before the forwarding request is sent, so an
/// inbound channel arriving right after the server's reply is never rejected by a
/// stale value. A dynamically assigned port is unknown until the reply arrives,
/// so `pending` accepts any port for the duration of that request.
#[derive(Default)]
struct ForwardState {
    port: AtomicU32,
    pending: AtomicBool,
}

impl ForwardState {
    fn new(requested_port: u16) -> Self {
        Self {
            port: AtomicU32::new(u32::from(requested_port)),
            pending: AtomicBool::new(requested_port == 0),
        }
    }

    fn accepts(&self, connected_port: u32) -> bool {
        match self.port.load(Ordering::Acquire) {
            0 => self.pending.load(Ordering::Acquire) && connected_port != 0,
            expected => connected_port == expected,
        }
    }

    fn confirm(&self, remote_port: u16) {
        self.port.store(u32::from(remote_port), Ordering::Release);
        self.pending.store(false, Ordering::Release);
    }

    fn abandon(&self) {
        self.pending.store(false, Ordering::Release);
    }
}

struct SshHandler {
    expected_fingerprint: Option<String>,
    observed_fingerprint: Arc<StdMutex<Option<String>>>,
    forward: Arc<ForwardState>,
    incoming: mpsc::Sender<IncomingSshChannel>,
    connected: watch::Sender<bool>,
}

impl Handler for SshHandler {
    type Error = russh::Error;

    async fn check_server_key(
        &mut self,
        server_public_key: &ssh_key::PublicKey,
    ) -> std::result::Result<bool, Self::Error> {
        let fingerprint = server_public_key.fingerprint(HashAlg::Sha256).to_string();
        *self
            .observed_fingerprint
            .lock()
            .unwrap_or_else(|lock| lock.into_inner()) = Some(fingerprint.clone());
        Ok(self
            .expected_fingerprint
            .as_ref()
            .is_none_or(|expected| bool::from(expected.as_bytes().ct_eq(fingerprint.as_bytes()))))
    }

    #[allow(clippy::too_many_arguments)]
    async fn server_channel_open_forwarded_tcpip(
        &mut self,
        channel: Channel<Msg>,
        connected_address: &str,
        connected_port: u32,
        originator_address: &str,
        originator_port: u32,
        reply: client::ChannelOpenHandle,
        _session: &mut client::Session,
    ) -> std::result::Result<(), Self::Error> {
        let valid_port = self.forward.accepts(connected_port)
            && u16::try_from(connected_port).is_ok()
            && u16::try_from(originator_port).is_ok();
        if !valid_port {
            reply
                .reject(ChannelOpenFailure::AdministrativelyProhibited)
                .await;
            return Ok(());
        }
        let incoming = IncomingSshChannel {
            channel,
            connected_address: connected_address.to_owned(),
            connected_port: connected_port as u16,
            originator_address: originator_address.to_owned(),
            originator_port: originator_port as u16,
        };
        match self.incoming.try_send(incoming) {
            Ok(()) => reply.accept().await,
            Err(_) => reply.reject(ChannelOpenFailure::ResourceShortage).await,
        }
        Ok(())
    }

    async fn disconnected(
        &mut self,
        reason: client::DisconnectReason<Self::Error>,
    ) -> std::result::Result<(), Self::Error> {
        let _ = self.connected.send(false);
        match reason {
            client::DisconnectReason::ReceivedDisconnect(_) => Ok(()),
            client::DisconnectReason::Error(error) => Err(error),
        }
    }
}

pub(crate) struct SshClient {
    handle: Mutex<Option<Arc<client::Handle<SshHandler>>>>,
    remote_port: u16,
    connected: watch::Receiver<bool>,
}

impl std::fmt::Debug for SshClient {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SshClient")
            .field("remote_port", &self.remote_port)
            .field("connected", &*self.connected.borrow())
            .finish_non_exhaustive()
    }
}

impl SshClient {
    pub(crate) async fn connect_reverse(
        profile: SshProfile,
        requested_port: u16,
        cancellation: &CancellationToken,
    ) -> Result<(Arc<Self>, mpsc::Receiver<IncomingSshChannel>)> {
        let profile = profile.normalize(true)?;
        let (incoming_tx, incoming_rx) = mpsc::channel(INCOMING_CHANNEL_CAPACITY);
        let forward = Arc::new(ForwardState::new(requested_port));
        let (connected_tx, connected_rx) = watch::channel(true);
        let (handle, _) = connect_authenticated(
            &profile,
            Some(profile.host_key_sha256.clone()),
            forward.clone(),
            incoming_tx,
            connected_tx,
            cancellation,
        )
        .await?;
        let assigned_port = cancellable_timeout(
            cancellation,
            SSH_OPERATION_TIMEOUT,
            handle.tcpip_forward("127.0.0.1", u32::from(requested_port)),
            "request SSH reverse forwarding",
        )
        .await
        .inspect_err(|_| forward.abandon())?;
        let remote_port = if requested_port == 0 {
            u16::try_from(assigned_port)
                .ok()
                .filter(|port| *port != 0)
                .ok_or_else(|| {
                    forward.abandon();
                    CoreError::Protocol("SSH reverse forwarding returned no port".into())
                })?
        } else {
            requested_port
        };
        forward.confirm(remote_port);
        Ok((
            Arc::new(Self {
                handle: Mutex::new(Some(Arc::new(handle))),
                remote_port,
                connected: connected_rx,
            }),
            incoming_rx,
        ))
    }

    pub(crate) fn remote_port(&self) -> u16 {
        self.remote_port
    }

    pub(crate) fn is_connected(&self) -> bool {
        *self.connected.borrow()
    }

    pub(crate) fn subscribe_connected(&self) -> watch::Receiver<bool> {
        self.connected.clone()
    }

    pub(crate) async fn open_direct(
        &self,
        remote_port: u16,
        cancellation: &CancellationToken,
    ) -> Result<Channel<Msg>> {
        if remote_port == 0 || !self.is_connected() {
            return Err(CoreError::Protocol("SSH relay is reconnecting".into()));
        }
        // The handle is cloned out of the lock so a slow channel open cannot block
        // close() for the whole operation timeout.
        let handle = self
            .handle
            .lock()
            .await
            .as_ref()
            .cloned()
            .ok_or_else(|| CoreError::Protocol("SSH relay is closed".into()))?;
        cancellable_timeout(
            cancellation,
            SSH_OPERATION_TIMEOUT,
            handle.channel_open_direct_tcpip("127.0.0.1", u32::from(remote_port), "127.0.0.1", 0),
            "open SSH relay channel",
        )
        .await
    }

    pub(crate) async fn close(&self) -> Result<()> {
        let Some(handle) = self.handle.lock().await.take() else {
            return Ok(());
        };
        let _ = tokio::time::timeout(
            Duration::from_secs(3),
            handle.cancel_tcpip_forward("127.0.0.1", u32::from(self.remote_port)),
        )
        .await;
        let _ = tokio::time::timeout(
            Duration::from_secs(3),
            handle.disconnect(Disconnect::ByApplication, "InkHole relay closed", "en"),
        )
        .await;
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub(crate) struct SshCheckResult {
    pub fingerprint: String,
    pub server_version: String,
    pub confirmed: bool,
}

pub(crate) async fn check_ssh(
    profile: SshProfile,
    cancellation: &CancellationToken,
) -> Result<SshCheckResult> {
    let profile = profile.normalize(false)?;
    let server_version = probe_ssh_server_version(&profile, cancellation).await?;
    let expected = profile.host_key_sha256.clone();
    let (incoming_tx, _incoming_rx) = mpsc::channel(1);
    let (connected_tx, _connected_rx) = watch::channel(true);
    let (handle, fingerprint) = connect_authenticated(
        &profile,
        None,
        Arc::new(ForwardState::default()),
        incoming_tx,
        connected_tx,
        cancellation,
    )
    .await?;
    let confirmed =
        !expected.is_empty() && bool::from(expected.as_bytes().ct_eq(fingerprint.as_bytes()));
    let _ = tokio::time::timeout(
        Duration::from_secs(3),
        handle.disconnect(Disconnect::ByApplication, "SSH check complete", "en"),
    )
    .await;
    Ok(SshCheckResult {
        fingerprint,
        server_version,
        confirmed,
    })
}

async fn probe_ssh_server_version(
    profile: &SshProfile,
    cancellation: &CancellationToken,
) -> Result<String> {
    let stream = cancellable_timeout(
        cancellation,
        SSH_CONNECT_TIMEOUT,
        TcpStream::connect((profile.host.as_str(), profile.port)),
        "connect SSH server for identification",
    )
    .await?;
    let mut reader = BufReader::new(stream);
    let mut line = Vec::with_capacity(256);
    for _ in 0..32 {
        line.clear();
        let bytes = cancellable_timeout(
            cancellation,
            SSH_OPERATION_TIMEOUT,
            reader.read_until(b'\n', &mut line),
            "read SSH server identification",
        )
        .await?;
        if bytes == 0 {
            break;
        }
        if line.len() > 255 {
            return Err(CoreError::Protocol(
                "SSH server identification line is too long".into(),
            ));
        }
        if line.starts_with(b"SSH-") {
            let version = String::from_utf8(line.clone())
                .map_err(|_| CoreError::Protocol("SSH server identification is not UTF-8".into()))?
                .trim()
                .to_owned();
            if version.starts_with("SSH-1.99-") || version.starts_with("SSH-2.0-") {
                return Ok(version);
            }
            return Err(CoreError::Protocol(format!(
                "unsupported SSH server identification: {version}"
            )));
        }
    }
    Err(CoreError::Protocol(
        "SSH server did not provide an identification string".into(),
    ))
}

async fn connect_authenticated(
    profile: &SshProfile,
    expected_fingerprint: Option<String>,
    forward: Arc<ForwardState>,
    incoming: mpsc::Sender<IncomingSshChannel>,
    connected: watch::Sender<bool>,
    cancellation: &CancellationToken,
) -> Result<(client::Handle<SshHandler>, String)> {
    let fingerprint_is_pinned = expected_fingerprint.is_some();
    let key = parse_private_key(profile)?;
    let observed_fingerprint = Arc::new(StdMutex::new(None));
    let handler = SshHandler {
        expected_fingerprint,
        observed_fingerprint: observed_fingerprint.clone(),
        forward,
        incoming,
        connected,
    };
    let config = Arc::new(Config {
        nodelay: true,
        keepalive_interval: Some(SSH_KEEPALIVE_INTERVAL),
        keepalive_max: 2,
        inactivity_timeout: Some(Duration::from_secs(120)),
        ..Config::default()
    });
    let address = (profile.host.as_str(), profile.port);
    let mut handle = cancellable_timeout(
        cancellation,
        SSH_CONNECT_TIMEOUT,
        client::connect(config, address, handler),
        "connect SSH server",
    )
    .await
    .map_err(|error| {
        let observed = observed_fingerprint
            .lock()
            .unwrap_or_else(|lock| lock.into_inner())
            .clone();
        if fingerprint_is_pinned
            && let Some(observed) = observed
            && !profile.host_key_sha256.is_empty()
            && observed != profile.host_key_sha256
        {
            CoreError::Protocol(format!("SSH host fingerprint changed: {observed}"))
        } else {
            error
        }
    })?;
    let hash = handle
        .best_supported_rsa_hash()
        .await
        .map_err(ssh_error)?
        .flatten();
    let authentication = cancellable_timeout(
        cancellation,
        SSH_OPERATION_TIMEOUT,
        handle.authenticate_publickey(
            profile.user.clone(),
            PrivateKeyWithHashAlg::new(Arc::new(key), hash),
        ),
        "authenticate SSH public key",
    )
    .await?;
    if !authentication.success() {
        return Err(CoreError::Protocol(
            "SSH public-key authentication failed".into(),
        ));
    }
    let fingerprint = observed_fingerprint
        .lock()
        .unwrap_or_else(|lock| lock.into_inner())
        .clone()
        .ok_or_else(|| CoreError::Protocol("SSH server returned no host key".into()))?;
    Ok((handle, fingerprint))
}

async fn cancellable_timeout<F, T, E>(
    cancellation: &CancellationToken,
    timeout: Duration,
    future: F,
    operation: &str,
) -> Result<T>
where
    F: Future<Output = std::result::Result<T, E>>,
    E: std::fmt::Display,
{
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        result = tokio::time::timeout(timeout, future) => match result {
            Ok(Ok(value)) => Ok(value),
            Ok(Err(error)) => Err(CoreError::Protocol(format!("{operation}: {error}"))),
            Err(_) => Err(CoreError::Protocol(format!("{operation} timed out"))),
        },
    }
}

fn ssh_error(error: russh::Error) -> CoreError {
    CoreError::Protocol(format!("SSH error: {error}"))
}

fn parse_private_key(profile: &SshProfile) -> Result<russh::keys::PrivateKey> {
    decode_secret_key(
        &profile.private_key,
        (!profile.passphrase.is_empty()).then_some(profile.passphrase.as_str()),
    )
    .map_err(|error| CoreError::InvalidRequest(format!("invalid SSH private key: {error}")))
}

fn valid_host_fingerprint(value: &str) -> bool {
    let Some(encoded) = value.strip_prefix("SHA256:") else {
        return false;
    };
    (32..=64).contains(&encoded.len())
        && encoded
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/' | b'_' | b'-'))
}

#[cfg(test)]
mod tests {
    use russh::{ChannelMsg, ChannelReadHalf};

    use super::*;
    use crate::ssh::test_support::MockSshRelay;

    async fn read_channel(mut reader: ChannelReadHalf, expected: usize) -> Vec<u8> {
        let mut received = Vec::with_capacity(expected);
        while received.len() < expected {
            match reader.wait().await {
                Some(ChannelMsg::Data { data }) => received.extend_from_slice(&data),
                Some(ChannelMsg::Eof | ChannelMsg::Close) | None => {
                    panic!(
                        "SSH channel closed after {} of {expected} bytes",
                        received.len()
                    )
                }
                Some(_) => {}
            }
        }
        assert_eq!(received.len(), expected);
        received
    }

    fn profile() -> SshProfile {
        SshProfile {
            host: " relay.example ".into(),
            port: 22,
            user: " user ".into(),
            private_key: "key".into(),
            host_key_sha256: "SHA256:ldyiXa1JQakitNU5tErauu8DvWQ1dZ7aXu+rm7KQuog".into(),
            ..SshProfile::default()
        }
    }

    #[test]
    fn profile_requires_a_pinned_sha256_fingerprint_for_relay() {
        let mut unpinned = profile();
        unpinned.host_key_sha256.clear();
        assert!(unpinned.normalize(true).is_err());
        assert!(profile().normalize(true).is_ok());
    }

    #[test]
    fn profile_rejects_control_characters_and_oversized_keys() {
        let mut invalid_host = profile();
        invalid_host.host = "relay.example\nother".into();
        assert!(invalid_host.normalize(true).is_err());

        let mut oversized = profile();
        oversized.private_key = "x".repeat(MAX_PRIVATE_KEY_BYTES + 1);
        assert!(oversized.normalize(true).is_err());
    }

    #[tokio::test]
    async fn reverse_forward_handles_dynamic_and_fixed_server_replies() {
        let relay = MockSshRelay::start().await;
        let cancellation = CancellationToken::new();

        let check = check_ssh(relay.profile(), &cancellation).await.unwrap();
        assert!(check.confirmed);
        assert!(check.fingerprint.starts_with("SHA256:"));

        let (dynamic, _dynamic_incoming) =
            SshClient::connect_reverse(relay.profile(), 0, &cancellation)
                .await
                .unwrap();
        let fixed_port = dynamic.remote_port() + 1;
        let (fixed, _fixed_incoming) =
            SshClient::connect_reverse(relay.profile(), fixed_port, &cancellation)
                .await
                .unwrap();
        assert_eq!(fixed.remote_port(), fixed_port);
        assert_eq!(
            relay.active_ports(),
            vec![dynamic.remote_port(), fixed_port]
        );

        fixed.close().await.unwrap();
        dynamic.close().await.unwrap();
        assert!(relay.active_ports().is_empty());
        relay.close().await;
    }

    #[tokio::test]
    async fn reverse_forward_preserves_bidirectional_byte_streams() {
        let relay = MockSshRelay::start().await;
        let cancellation = CancellationToken::new();
        let (receiver, mut incoming) =
            SshClient::connect_reverse(relay.profile(), 0, &cancellation)
                .await
                .unwrap();
        let (sender, _sender_incoming) =
            SshClient::connect_reverse(relay.profile(), 45_001, &cancellation)
                .await
                .unwrap();
        let direct = sender
            .open_direct(receiver.remote_port(), &cancellation)
            .await
            .unwrap();
        let forwarded = incoming.recv().await.unwrap().channel;
        let outbound = (0..800_000_u32)
            .flat_map(u32::to_be_bytes)
            .collect::<Vec<_>>();
        let response = (800_000..1_600_000_u32)
            .flat_map(u32::to_be_bytes)
            .collect::<Vec<_>>();
        let direct_outbound = outbound.clone();
        let direct_response_len = response.len();
        let direct_task = tokio::spawn(async move {
            let (reader, writer) = direct.split();
            let write = async move {
                writer.data_bytes(direct_outbound).await.unwrap();
            };
            let read = read_channel(reader, direct_response_len);
            let ((), received) = tokio::join!(write, read);
            received
        });
        let forwarded_outbound_len = outbound.len();
        let forwarded_response = response.clone();
        let (reader, writer) = forwarded.split();
        let read = read_channel(reader, forwarded_outbound_len);
        let write = async move {
            writer.data_bytes(forwarded_response).await.unwrap();
        };
        let (received, ()) =
            tokio::time::timeout(Duration::from_secs(15), async { tokio::join!(read, write) })
                .await
                .expect("forwarded SSH stream stalled");
        assert_eq!(received, outbound);
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(15), direct_task)
                .await
                .expect("direct SSH stream stalled")
                .unwrap(),
            response
        );

        sender.close().await.unwrap();
        receiver.close().await.unwrap();
        relay.close().await;
    }
}
