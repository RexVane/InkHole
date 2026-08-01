use std::{future::pending, net::Ipv4Addr, time::Duration};

use serde::{Deserialize, Serialize};
use subtle::ConstantTimeEq;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    task::JoinSet,
};
use tokio_util::sync::CancellationToken;

use super::protocol::derive_key;
use crate::{CoreError, Result};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
/// Outbound budget covering the TCP connect *and* the peer handshake. The sender may still be
/// tearing down its rendezvous connection when we dial, so this has to be comfortably longer than
/// the sender's close timeout or every transfer races into a false failure.
const HINT_TIMEOUT: Duration = Duration::from_secs(15);
/// Per-connection budget for an inbound handshake, so one dead peer cannot pin the acceptor.
const INBOUND_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_TRANSIT_HINTS: usize = 32;

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub(super) enum TransitHintKind {
    DirectTcpV1,
    RelayV1,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub(super) struct TransitHint {
    pub kind: TransitHintKind,
    pub host: String,
    pub port: u16,
    pub priority: u8,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub(super) struct TransitOffer {
    pub hints: Vec<TransitHint>,
}

pub(super) struct TransitAcceptor {
    listener: TcpListener,
    relay: Option<TcpStream>,
    offer: TransitOffer,
    transit_key: [u8; 32],
    cancellation: CancellationToken,
}

impl std::fmt::Debug for TransitAcceptor {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("TransitAcceptor")
            .field("offer", &self.offer)
            .field("has_relay", &self.relay.is_some())
            .field("cancelled", &self.cancellation.is_cancelled())
            .finish_non_exhaustive()
    }
}

impl TransitAcceptor {
    pub async fn prepare(
        transit_key: [u8; 32],
        relay_address: &str,
        cancellation: CancellationToken,
    ) -> Result<Self> {
        let listener = TcpListener::bind((Ipv4Addr::UNSPECIFIED, 0)).await?;
        let port = listener.local_addr()?.port();
        let mut addresses = tokio::task::spawn_blocking(local_ipv4_addresses)
            .await
            .map_err(|error| CoreError::Protocol(format!("enumerate local addresses: {error}")))?;
        addresses.push(Ipv4Addr::LOCALHOST);
        addresses.sort_unstable();
        addresses.dedup();
        let mut hints = addresses
            .into_iter()
            .take(MAX_TRANSIT_HINTS - 1)
            .map(|address| TransitHint {
                kind: TransitHintKind::DirectTcpV1,
                host: address.to_string(),
                port,
                priority: if address.is_loopback() { 2 } else { 0 },
            })
            .collect::<Vec<_>>();

        let relay_address = relay_address.trim();
        let relay = if relay_address.is_empty() {
            None
        } else {
            match connect_relay(relay_address, &transit_key, &cancellation).await {
                Ok(stream) => {
                    let (host, port) = split_host_port(relay_address)?;
                    hints.push(TransitHint {
                        kind: TransitHintKind::RelayV1,
                        host,
                        port,
                        priority: 3,
                    });
                    Some(stream)
                }
                Err(error) => {
                    tracing::debug!(%error, "wormhole transit relay reservation failed");
                    None
                }
            }
        };
        Ok(Self {
            listener,
            relay,
            offer: TransitOffer { hints },
            transit_key,
            cancellation,
        })
    }

    pub fn offer(&self) -> &TransitOffer {
        &self.offer
    }

    pub async fn accept(self) -> Result<TcpStream> {
        let Self {
            listener,
            relay,
            transit_key,
            cancellation,
            ..
        } = self;
        let direct_key = transit_key;
        let direct_cancel = cancellation.clone();
        // Inbound connections are handshaked concurrently: a peer that connects and then goes
        // silent must not block the connections behind it in the accept queue.
        let direct = async move {
            let mut handshakes = JoinSet::new();
            loop {
                tokio::select! {
                    accepted = listener.accept() => {
                        let (stream, address) = accepted?;
                        let key = direct_key;
                        let handshake_cancel = direct_cancel.child_token();
                        handshakes.spawn(async move {
                            tokio::time::timeout(
                                INBOUND_HANDSHAKE_TIMEOUT,
                                authenticate_incoming(stream, &key, &handshake_cancel),
                            )
                            .await
                            .map_err(|_| {
                                CoreError::Protocol(format!(
                                    "transit handshake from {address} timed out"
                                ))
                            })?
                        });
                    }
                    Some(finished) = handshakes.join_next() => match finished {
                        Ok(Ok(stream)) => return Ok(stream),
                        Ok(Err(error)) => {
                            tracing::debug!(%error, "rejected unauthenticated transit peer");
                        }
                        Err(error) => {
                            tracing::debug!(%error, "transit handshake task failed");
                        }
                    },
                }
            }
        };
        let relay_cancel = cancellation.clone();
        let relay_future = async move {
            match relay {
                Some(stream) => {
                    authenticate_relay_incoming(stream, &transit_key, &relay_cancel).await
                }
                None => pending::<Result<TcpStream>>().await,
            }
        };
        tokio::select! {
            _ = cancellation.cancelled() => Err(CoreError::Cancelled),
            result = direct => result,
            result = relay_future => result,
        }
    }
}

pub(super) async fn connect(
    offer: &TransitOffer,
    transit_key: [u8; 32],
    cancellation: CancellationToken,
) -> Result<TcpStream> {
    validate_offer(offer)?;
    let mut attempts = JoinSet::new();
    for hint in offer.hints.iter().cloned() {
        let key = transit_key;
        let attempt_cancellation = cancellation.child_token();
        attempts.spawn(async move { connect_hint(hint, key, attempt_cancellation).await });
    }
    let mut errors = Vec::new();
    loop {
        tokio::select! {
            _ = cancellation.cancelled() => {
                attempts.abort_all();
                return Err(CoreError::Cancelled);
            }
            result = attempts.join_next() => match result {
                Some(Ok(Ok(stream))) => {
                    attempts.abort_all();
                    return Ok(stream);
                }
                Some(Ok(Err(error))) => errors.push(error.to_string()),
                Some(Err(error)) => errors.push(format!("transit task failed: {error}")),
                None => {
                    let detail = errors.last().cloned().unwrap_or_else(|| "no transit hints".into());
                    return Err(CoreError::Protocol(format!(
                        "failed to establish direct or relayed transit: {detail}"
                    )));
                }
            }
        }
    }
}

async fn connect_hint(
    hint: TransitHint,
    transit_key: [u8; 32],
    cancellation: CancellationToken,
) -> Result<TcpStream> {
    let address = format_host_port(&hint.host, hint.port);
    let connect = async {
        let mut stream = TcpStream::connect(&address).await?;
        stream.set_nodelay(true)?;
        if hint.kind == TransitHintKind::RelayV1 {
            stream
                .write_all(&relay_handshake_header(&transit_key))
                .await?;
            read_ok(&mut stream).await?;
        }
        authenticate_outgoing(stream, &transit_key, &cancellation).await
    };
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        result = tokio::time::timeout(HINT_TIMEOUT, connect) => result
            .map_err(|_| CoreError::Protocol(format!("transit connection to {address} timed out")))?,
    }
}

async fn connect_relay(
    relay_address: &str,
    transit_key: &[u8; 32],
    cancellation: &CancellationToken,
) -> Result<TcpStream> {
    let connect = async {
        let mut stream = TcpStream::connect(relay_address).await?;
        stream.set_nodelay(true)?;
        stream
            .write_all(&relay_handshake_header(transit_key))
            .await?;
        Ok(stream)
    };
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        result = tokio::time::timeout(CONNECT_TIMEOUT, connect) => result
            .map_err(|_| CoreError::Protocol("transit relay reservation timed out".into()))?,
    }
}

async fn authenticate_relay_incoming(
    mut stream: TcpStream,
    transit_key: &[u8; 32],
    cancellation: &CancellationToken,
) -> Result<TcpStream> {
    read_ok(&mut stream).await?;
    authenticate_incoming(stream, transit_key, cancellation).await
}

async fn authenticate_incoming(
    mut stream: TcpStream,
    transit_key: &[u8; 32],
    cancellation: &CancellationToken,
) -> Result<TcpStream> {
    stream.set_nodelay(true)?;
    let sender = sender_handshake_header(transit_key)?;
    let receiver = receiver_handshake_header(transit_key)?;
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        result = async {
            stream.write_all(&sender).await?;
            read_exact_and_verify(&mut stream, &receiver, "receiver transit handshake").await?;
            stream.write_all(b"go\n").await?;
            Ok(stream)
        } => result,
    }
}

async fn authenticate_outgoing(
    mut stream: TcpStream,
    transit_key: &[u8; 32],
    cancellation: &CancellationToken,
) -> Result<TcpStream> {
    let sender = sender_handshake_header(transit_key)?;
    let receiver = receiver_handshake_header(transit_key)?;
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        result = async {
            read_exact_and_verify(&mut stream, &sender, "sender transit handshake").await?;
            stream.write_all(&receiver).await?;
            read_exact_and_verify(&mut stream, b"go\n", "transit go acknowledgement").await?;
            Ok(stream)
        } => result,
    }
}

async fn read_ok(stream: &mut TcpStream) -> Result<()> {
    read_exact_and_verify(stream, b"ok\n", "transit relay acknowledgement").await
}

async fn read_exact_and_verify(stream: &mut TcpStream, expected: &[u8], label: &str) -> Result<()> {
    let mut received = vec![0_u8; expected.len()];
    stream.read_exact(&mut received).await?;
    if received.ct_eq(expected).unwrap_u8() != 1 {
        return Err(CoreError::Protocol(format!("invalid {label}")));
    }
    Ok(())
}

fn sender_handshake_header(transit_key: &[u8; 32]) -> Result<Vec<u8>> {
    handshake_header(transit_key, b"transit_sender", "transit sender")
}

fn receiver_handshake_header(transit_key: &[u8; 32]) -> Result<Vec<u8>> {
    handshake_header(transit_key, b"transit_receiver", "transit receiver")
}

fn handshake_header(transit_key: &[u8; 32], purpose: &[u8], role: &str) -> Result<Vec<u8>> {
    Ok(format!(
        "{role} {} ready\n\n",
        hex::encode(derive_key(transit_key, purpose)?)
    )
    .into_bytes())
}

fn relay_handshake_header(transit_key: &[u8; 32]) -> Vec<u8> {
    let token =
        derive_key(transit_key, b"transit_relay_token").expect("fixed HKDF output length is valid");
    format!(
        "please relay {} for side {}\n",
        hex::encode(token),
        hex::encode(rand::random::<[u8; 8]>())
    )
    .into_bytes()
}

fn validate_offer(offer: &TransitOffer) -> Result<()> {
    if offer.hints.is_empty() || offer.hints.len() > MAX_TRANSIT_HINTS {
        return Err(CoreError::Protocol(
            "transit offer has an invalid hint count".into(),
        ));
    }
    for hint in &offer.hints {
        if hint.host.is_empty()
            || hint.host.len() > 255
            || hint.port == 0
            || hint.host.chars().any(char::is_whitespace)
        {
            return Err(CoreError::Protocol(
                "transit offer contains an invalid hint".into(),
            ));
        }
    }
    Ok(())
}

fn local_ipv4_addresses() -> Vec<Ipv4Addr> {
    if_addrs::get_if_addrs()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|interface| match interface.ip() {
            std::net::IpAddr::V4(address)
                if !address.is_unspecified()
                    && !address.is_multicast()
                    && !address.is_broadcast()
                    && !address.is_loopback() =>
            {
                Some(address)
            }
            _ => None,
        })
        .collect()
}

fn split_host_port(value: &str) -> Result<(String, u16)> {
    let (host, port) = value
        .rsplit_once(':')
        .ok_or_else(|| CoreError::InvalidRequest("transit relay must use host:port".into()))?;
    let host = host.trim_matches(['[', ']']);
    let port = port
        .parse::<u16>()
        .map_err(|_| CoreError::InvalidRequest("transit relay port is invalid".into()))?;
    if host.is_empty() || port == 0 {
        return Err(CoreError::InvalidRequest("transit relay is invalid".into()));
    }
    Ok((host.to_owned(), port))
}

fn format_host_port(host: &str, port: u16) -> String {
    if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn direct_transit_authenticates_and_carries_a_bidirectional_stream() {
        let key = [0x3c; 32];
        let cancellation = CancellationToken::new();
        let acceptor = TransitAcceptor::prepare(key, "", cancellation.clone())
            .await
            .unwrap();
        let offer = acceptor.offer().clone();
        let (sender, receiver) = tokio::join!(
            acceptor.accept(),
            connect(&offer, key, cancellation.clone())
        );
        let mut sender = sender.unwrap();
        let mut receiver = receiver.unwrap();

        sender.write_all(b"sender payload").await.unwrap();
        let mut from_sender = [0_u8; 14];
        receiver.read_exact(&mut from_sender).await.unwrap();
        assert_eq!(&from_sender, b"sender payload");

        receiver.write_all(b"receiver payload").await.unwrap();
        let mut from_receiver = [0_u8; 16];
        sender.read_exact(&mut from_receiver).await.unwrap();
        assert_eq!(&from_receiver, b"receiver payload");
    }

    #[test]
    fn rejects_unbounded_or_malformed_transit_hints() {
        assert!(validate_offer(&TransitOffer { hints: Vec::new() }).is_err());
        assert!(
            validate_offer(&TransitOffer {
                hints: vec![TransitHint {
                    kind: TransitHintKind::DirectTcpV1,
                    host: "bad host".into(),
                    port: 443,
                    priority: 0,
                }],
            })
            .is_err()
        );
    }
}
