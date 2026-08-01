use std::{
    fs,
    net::SocketAddr,
    path::{Path, PathBuf},
    time::Duration,
};

use rand::Rng;
use serde::{Deserialize, Serialize};
use tokio_util::sync::CancellationToken;

use super::{
    protocol::{PendingWormhole, WormholeProtocol, WormholeSettings},
    transit::{self, TransitAcceptor, TransitOffer},
    tunnel::UdpTunnel,
};
use crate::{CoreError, PeerEndpoint, Result, protocol::valid_instance_id};

const MAX_SELECTED_ITEMS: usize = 256;
const MAX_SUMMARY_ENTRIES: u64 = 1_000_000;
/// How long the sender waits for the receiver to complete the transit handshake before giving up.
const ACCEPT_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub(crate) struct TransferSummary {
    pub device_name: String,
    pub instance_id: String,
    pub item_count: u64,
    pub file_count: u64,
    pub directory_count: u64,
    pub total_bytes: u64,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub names: Vec<String>,
}

impl TransferSummary {
    fn validate(&self) -> Result<()> {
        if self.device_name.trim().is_empty()
            || self.device_name.len() > 255
            || !valid_instance_id(&self.instance_id)
            || self.item_count == 0
            || self.item_count > MAX_SELECTED_ITEMS as u64
            || self
                .file_count
                .checked_add(self.directory_count)
                .is_none_or(|count| count < self.item_count || count > MAX_SUMMARY_ENTRIES)
            || self.names.len() > 8
            || self
                .names
                .iter()
                .any(|name| name.trim().is_empty() || name.len() > 255)
        {
            return Err(CoreError::InvalidTransfer(
                "short-code transfer summary is invalid".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct SenderOffer {
    summary: TransferSummary,
    transit: TransitOffer,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct ReceiverAnswer {
    accepted: bool,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    error: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    quic_fingerprint: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    quic_secret: String,
}

pub(crate) struct SenderSession {
    pending: PendingWormhole,
    settings: WormholeSettings,
    summary: TransferSummary,
    cancellation: CancellationToken,
}

impl std::fmt::Debug for SenderSession {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SenderSession")
            .field("settings", &self.settings)
            .field("summary", &self.summary)
            .field("cancelled", &self.cancellation.is_cancelled())
            .finish_non_exhaustive()
    }
}

pub(crate) struct SenderConnection {
    pub peer: PeerEndpoint,
    pub shared_secret: String,
    pub tunnel: UdpTunnel,
}

impl SenderSession {
    pub async fn create(
        mut settings: WormholeSettings,
        summary: TransferSummary,
        cancellation: CancellationToken,
    ) -> Result<(String, Self)> {
        settings.normalize()?;
        summary.validate()?;
        let (code, pending) = tokio::select! {
            _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = WormholeProtocol::allocate(&settings) => result?,
        };
        Ok((
            code,
            Self {
                pending,
                settings,
                summary,
                cancellation,
            },
        ))
    }

    pub async fn connect(self) -> Result<SenderConnection> {
        let Self {
            pending,
            settings,
            summary,
            cancellation,
        } = self;
        let mut protocol = tokio::select! {
            _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = pending.establish() => result?,
        };
        let transit_key = protocol.transit_key()?;
        let acceptor = TransitAcceptor::prepare(
            transit_key,
            &settings.transit_relay,
            cancellation.child_token(),
        )
        .await?;
        send_message(
            &mut protocol,
            &SenderOffer {
                summary,
                transit: acceptor.offer().clone(),
            },
            &cancellation,
        )
        .await?;
        let answer: ReceiverAnswer = tokio::select! {
            _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = protocol.receive() => result?,
        };
        if !answer.accepted {
            return Err(CoreError::Protocol(if answer.error.is_empty() {
                "receiver rejected the transfer".into()
            } else {
                answer.error
            }));
        }
        if answer.quic_secret.len() < 32
            || answer.quic_secret.len() > 128
            || !valid_fingerprint(&answer.quic_fingerprint)
        {
            return Err(CoreError::Protocol(
                "receiver returned invalid temporary QUIC credentials".into(),
            ));
        }
        // Start listening for the receiver's transit handshake *before* tearing down the
        // rendezvous connection: the receiver dials as soon as it has read the answer, and
        // close_protocol can take seconds.
        let accept = tokio::spawn(acceptor.accept());
        close_protocol(protocol, "happy").await;
        let mut accept = accept;
        let accepted = tokio::select! {
            _ = cancellation.cancelled() => None,
            result = tokio::time::timeout(ACCEPT_TIMEOUT, &mut accept) => Some(result),
        };
        let stream = match accepted {
            None => {
                accept.abort();
                return Err(CoreError::Cancelled);
            }
            Some(Ok(Ok(stream))) => stream?,
            Some(Ok(Err(error))) => {
                return Err(CoreError::Protocol(format!(
                    "transit accept task failed: {error}"
                )));
            }
            Some(Err(_)) => {
                accept.abort();
                return Err(CoreError::Protocol(
                    "the receiver never completed the direct connection; check that both devices can reach the network and try again".into(),
                ));
            }
        };
        let tunnel =
            UdpTunnel::start_sender(stream, transit_key, cancellation.child_token()).await?;
        Ok(SenderConnection {
            peer: PeerEndpoint {
                address: tunnel.local_address(),
                certificate_fingerprint: answer.quic_fingerprint,
            },
            shared_secret: answer.quic_secret,
            tunnel,
        })
    }
}

pub(crate) struct ReceivedOffer {
    protocol: WormholeProtocol,
    offer: SenderOffer,
    transit_key: [u8; 32],
    cancellation: CancellationToken,
}

impl std::fmt::Debug for ReceivedOffer {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ReceivedOffer")
            .field("summary", &self.offer.summary)
            .field("transit", &self.offer.transit)
            .field("cancelled", &self.cancellation.is_cancelled())
            .finish_non_exhaustive()
    }
}

impl ReceivedOffer {
    pub async fn join(
        mut settings: WormholeSettings,
        code: &str,
        cancellation: CancellationToken,
    ) -> Result<Self> {
        settings.normalize()?;
        let mut protocol = tokio::select! {
            _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = WormholeProtocol::join(&settings, code) => result?,
        };
        let transit_key = protocol.transit_key()?;
        let offer: SenderOffer = tokio::select! {
            _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = protocol.receive() => result?,
        };
        offer.summary.validate()?;
        Ok(Self {
            protocol,
            offer,
            transit_key,
            cancellation,
        })
    }

    pub fn summary(&self) -> &TransferSummary {
        &self.offer.summary
    }

    pub async fn accept(
        mut self,
        upstream: SocketAddr,
        quic_fingerprint: String,
        quic_secret: String,
    ) -> Result<UdpTunnel> {
        if !upstream.ip().is_loopback()
            || !valid_fingerprint(&quic_fingerprint)
            || quic_secret.len() < 32
            || quic_secret.len() > 128
        {
            return Err(CoreError::InvalidRequest(
                "temporary QUIC endpoint is invalid".into(),
            ));
        }
        send_message(
            &mut self.protocol,
            &ReceiverAnswer {
                accepted: true,
                error: String::new(),
                quic_fingerprint,
                quic_secret,
            },
            &self.cancellation,
        )
        .await?;
        let stream = transit::connect(
            &self.offer.transit,
            self.transit_key,
            self.cancellation.child_token(),
        )
        .await?;
        close_protocol(self.protocol, "happy").await;
        UdpTunnel::start_receiver(
            stream,
            upstream,
            self.transit_key,
            self.cancellation.child_token(),
        )
        .await
    }

    pub async fn reject(mut self, reason: &str) -> Result<()> {
        send_message(
            &mut self.protocol,
            &ReceiverAnswer {
                accepted: false,
                error: reason.trim().chars().take(255).collect(),
                quic_fingerprint: String::new(),
                quic_secret: String::new(),
            },
            &self.cancellation,
        )
        .await?;
        close_protocol(self.protocol, "scary").await;
        Ok(())
    }
}

pub(crate) async fn summarize_paths(
    device_name: &str,
    instance_id: &str,
    paths: &[PathBuf],
    cancellation: CancellationToken,
) -> Result<TransferSummary> {
    if device_name.trim().is_empty()
        || !valid_instance_id(instance_id)
        || paths.is_empty()
        || paths.len() > MAX_SELECTED_ITEMS
    {
        return Err(CoreError::InvalidRequest(
            "short-code transfer paths or device identity are invalid".into(),
        ));
    }
    let device_name = device_name.to_owned();
    let instance_id = instance_id.to_owned();
    let paths = paths.to_vec();
    tokio::task::spawn_blocking(move || {
        summarize_paths_blocking(device_name, instance_id, paths, cancellation)
    })
    .await
    .map_err(|error| CoreError::Protocol(format!("summary task failed: {error}")))?
}

fn summarize_paths_blocking(
    device_name: String,
    instance_id: String,
    paths: Vec<PathBuf>,
    cancellation: CancellationToken,
) -> Result<TransferSummary> {
    let mut summary = TransferSummary {
        device_name,
        instance_id,
        item_count: paths.len() as u64,
        file_count: 0,
        directory_count: 0,
        total_bytes: 0,
        names: Vec::with_capacity(paths.len().min(8)),
    };
    let mut entries = 0_u64;
    for path in paths {
        check_cancelled(&cancellation)?;
        if summary.names.len() < 8 {
            summary.names.push(path_name(&path)?);
        }
        let mut pending = vec![path];
        while let Some(current) = pending.pop() {
            check_cancelled(&cancellation)?;
            let metadata = fs::symlink_metadata(&current)?;
            if metadata.file_type().is_symlink() {
                return Err(CoreError::InvalidTransfer(format!(
                    "symbolic links cannot be sent: {}",
                    current.display()
                )));
            }
            entries = entries
                .checked_add(1)
                .ok_or_else(|| CoreError::InvalidTransfer("summary entry count overflow".into()))?;
            if entries > MAX_SUMMARY_ENTRIES {
                return Err(CoreError::InvalidTransfer(
                    "selected folders contain too many entries".into(),
                ));
            }
            if metadata.is_file() {
                summary.file_count += 1;
                summary.total_bytes = summary
                    .total_bytes
                    .checked_add(metadata.len())
                    .ok_or_else(|| CoreError::InvalidTransfer("summary size overflow".into()))?;
            } else if metadata.is_dir() {
                summary.directory_count += 1;
                for entry in fs::read_dir(&current)? {
                    pending.push(entry?.path());
                }
            } else {
                return Err(CoreError::InvalidTransfer(format!(
                    "unsupported file type: {}",
                    current.display()
                )));
            }
        }
    }
    summary.validate()?;
    Ok(summary)
}

fn path_name(path: &Path) -> Result<String> {
    path.file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| {
            CoreError::InvalidTransfer(format!("invalid source path: {}", path.display()))
        })
}

fn check_cancelled(cancellation: &CancellationToken) -> Result<()> {
    if cancellation.is_cancelled() {
        Err(CoreError::Cancelled)
    } else {
        Ok(())
    }
}

fn valid_fingerprint(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn random_secret() -> String {
    let mut value = [0_u8; 32];
    rand::rng().fill(&mut value);
    hex::encode(value)
}

async fn send_message<T: Serialize>(
    protocol: &mut WormholeProtocol,
    value: &T,
    cancellation: &CancellationToken,
) -> Result<()> {
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        result = protocol.send(value) => result,
    }
}

async fn close_protocol(protocol: WormholeProtocol, mood: &str) {
    let _ = tokio::time::timeout(Duration::from_secs(3), protocol.close(mood)).await;
}

pub(crate) fn temporary_secret() -> String {
    random_secret()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn summary_counts_nested_files_directories_and_bytes() {
        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join("folder");
        fs::create_dir_all(folder.join("empty")).unwrap();
        fs::write(folder.join("first.txt"), b"first").unwrap();
        fs::write(folder.join("empty/second.bin"), b"second").unwrap();
        let standalone = root.path().join("standalone.dat");
        fs::write(&standalone, b"third").unwrap();

        let summary = summarize_paths(
            "Sender",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            &[folder, standalone],
            CancellationToken::new(),
        )
        .await
        .unwrap();
        assert_eq!(summary.item_count, 2);
        assert_eq!(summary.file_count, 3);
        assert_eq!(summary.directory_count, 2);
        assert_eq!(summary.total_bytes, 16);
        assert_eq!(summary.names, ["folder", "standalone.dat"]);
    }

    #[test]
    fn temporary_credentials_have_full_entropy_and_strict_format() {
        let first = temporary_secret();
        let second = temporary_secret();
        assert_eq!(first.len(), 64);
        assert!(valid_fingerprint(&first));
        assert_ne!(first, second);
    }
}
