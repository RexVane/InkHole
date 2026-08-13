use std::fmt;

use bip39::Language;
use crypto_secretbox::{
    Key, Nonce, XSalsa20Poly1305,
    aead::{Aead, KeyInit},
};
use hkdf::Hkdf;
use rand::Rng;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::json;
use sha2::{Digest, Sha256};
use spake2::{Ed25519Group, Identity, Password, Spake2};
use tokio::task::JoinHandle;
use tokio_util::sync::{CancellationToken, DropGuard};

use super::rendezvous::{MailboxMessage, RendezvousClient};
use crate::{CoreError, Result};

pub(super) const WORMHOLE_APP_ID: &str = "com.rexvane.inkhole/transport-v2";
pub(super) const DEFAULT_RENDEZVOUS_URL: &str = "wss://relay.magic-wormhole.io/v1";
pub(super) const DEFAULT_TRANSIT_RELAY: &str = "transit.magic-wormhole.io:4001";

const WORMHOLE_PROTOCOL_VERSION: u16 = 2;
const MAX_CODE_LENGTH: usize = 160;

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub(super) enum PeerRole {
    Sender,
    Receiver,
}

impl PeerRole {
    fn opposite(self) -> Self {
        match self {
            Self::Sender => Self::Receiver,
            Self::Receiver => Self::Sender,
        }
    }
}

#[derive(Debug, Clone)]
pub(crate) struct WormholeSettings {
    pub rendezvous_url: String,
    pub transit_relay: String,
}

impl Default for WormholeSettings {
    fn default() -> Self {
        Self {
            rendezvous_url: DEFAULT_RENDEZVOUS_URL.into(),
            transit_relay: DEFAULT_TRANSIT_RELAY.into(),
        }
    }
}

impl WormholeSettings {
    pub fn normalize(&mut self) -> Result<()> {
        self.rendezvous_url = self.rendezvous_url.trim().to_owned();
        if self.rendezvous_url.is_empty() {
            self.rendezvous_url = DEFAULT_RENDEZVOUS_URL.into();
        }
        if !matches!(
            self.rendezvous_url.split_once("://"),
            Some(("ws" | "wss", _))
        ) {
            return Err(CoreError::InvalidRequest(
                "rendezvous URL must use ws:// or wss://".into(),
            ));
        }
        self.transit_relay = self.transit_relay.trim().to_owned();
        if self.transit_relay.is_empty() {
            self.transit_relay = DEFAULT_TRANSIT_RELAY.into();
        }
        if !valid_host_port(&self.transit_relay) {
            return Err(CoreError::InvalidRequest(
                "transit relay must use host:port".into(),
            ));
        }
        Ok(())
    }
}

pub(super) struct WormholeProtocol {
    rendezvous: RendezvousClient,
    shared_key: [u8; 32],
    send_phase: u32,
    receive_phase: u32,
}

pub(super) struct PendingWormhole {
    keepalive: JoinHandle<Result<RendezvousClient>>,
    stop: CancellationToken,
    /// Stops the keepalive pump if the pending wormhole is dropped without ever establishing.
    _guard: DropGuard,
    code: String,
    role: PeerRole,
}

impl fmt::Debug for WormholeProtocol {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("WormholeProtocol")
            .field("send_phase", &self.send_phase)
            .field("receive_phase", &self.receive_phase)
            .finish_non_exhaustive()
    }
}

impl WormholeProtocol {
    pub async fn allocate(settings: &WormholeSettings) -> Result<(String, PendingWormhole)> {
        let side_id = random_hex(10);
        let mut rendezvous =
            RendezvousClient::connect(&settings.rendezvous_url, side_id.clone(), WORMHOLE_APP_ID)
                .await?;
        let nameplate = rendezvous.create_mailbox().await?;
        let code = generate_code(&nameplate);
        // Nothing polls the websocket between handing the code to the user and the peer entering
        // it, so keep a pump running or the server drops us on a missed ping.
        let stop = CancellationToken::new();
        let keepalive = tokio::spawn(rendezvous.pump(stop.clone()));
        Ok((
            code.clone(),
            PendingWormhole {
                keepalive,
                stop: stop.clone(),
                _guard: stop.drop_guard(),
                code,
                role: PeerRole::Sender,
            },
        ))
    }

    pub async fn join(settings: &WormholeSettings, code: &str) -> Result<Self> {
        let code = normalize_code(code)?;
        let nameplate = code
            .split_once('-')
            .map(|(nameplate, _)| nameplate)
            .ok_or_else(|| CoreError::InvalidRequest("short code is incomplete".into()))?;
        let side_id = random_hex(10);
        let mut rendezvous =
            RendezvousClient::connect(&settings.rendezvous_url, side_id.clone(), WORMHOLE_APP_ID)
                .await?;
        rendezvous.attach_mailbox(nameplate).await?;
        Self::establish(rendezvous, &code, PeerRole::Receiver).await
    }

    async fn establish(
        mut rendezvous: RendezvousClient,
        code: &str,
        role: PeerRole,
    ) -> Result<Self> {
        tracing::debug!(?role, "wormhole: establishing (PAKE exchange)");
        let (spake, outbound) = Spake2::<Ed25519Group>::start_symmetric(
            &Password::new(code.as_bytes()),
            &Identity::new(WORMHOLE_APP_ID.as_bytes()),
        );
        let encoded_pake = hex::encode(serde_json::to_vec(&json!({
            "pake_v1": hex::encode(outbound),
        }))?);
        rendezvous.add_message("pake", &encoded_pake).await?;
        let peer_pake = rendezvous.next_peer_message().await?;
        if peer_pake.phase != "pake" {
            return Err(protocol_error(format!(
                "expected PAKE phase, received {}",
                peer_pake.phase
            )));
        }
        let pake_json = hex::decode(&peer_pake.body)
            .map_err(|_| protocol_error("peer PAKE envelope is not hexadecimal"))?;
        let pake_value: serde_json::Value = serde_json::from_slice(&pake_json)?;
        let peer_outbound = pake_value
            .get("pake_v1")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| protocol_error("peer PAKE envelope is invalid"))?;
        let peer_outbound = hex::decode(peer_outbound)
            .map_err(|_| protocol_error("peer PAKE value is not hexadecimal"))?;
        let key = spake
            .finish(&peer_outbound)
            .map_err(|error| CoreError::Crypto(format!("SPAKE2 failed: {error}")))?;
        let shared_key: [u8; 32] = key
            .try_into()
            .map_err(|_| CoreError::Crypto("SPAKE2 returned an invalid key length".into()))?;
        let mut protocol = Self {
            rendezvous,
            shared_key,
            send_phase: 0,
            receive_phase: 0,
        };
        protocol
            .send_phase(
                "version",
                &VersionEnvelope {
                    inkhole_v2: VersionInfo {
                        protocol: WORMHOLE_PROTOCOL_VERSION,
                        role,
                    },
                },
            )
            .await?;
        let peer: VersionEnvelope = protocol.receive_phase("version").await?;
        if peer.inkhole_v2.protocol != WORMHOLE_PROTOCOL_VERSION
            || peer.inkhole_v2.role != role.opposite()
        {
            return Err(protocol_error(
                "peer does not support InkHole short-code V2",
            ));
        }
        tracing::debug!(?role, "wormhole: PAKE + version exchange complete");
        Ok(protocol)
    }

    pub async fn send<T: Serialize>(&mut self, value: &T) -> Result<()> {
        let phase = self.send_phase.to_string();
        self.send_phase = self
            .send_phase
            .checked_add(1)
            .ok_or_else(|| protocol_error("outgoing phase counter exhausted"))?;
        self.send_phase(&phase, value).await
    }

    pub async fn receive<T: DeserializeOwned>(&mut self) -> Result<T> {
        let phase = self.receive_phase.to_string();
        self.receive_phase = self
            .receive_phase
            .checked_add(1)
            .ok_or_else(|| protocol_error("incoming phase counter exhausted"))?;
        self.receive_phase(&phase).await
    }

    async fn send_phase<T: Serialize>(&mut self, phase: &str, value: &T) -> Result<()> {
        let plaintext = serde_json::to_vec(value)?;
        let key = derive_phase_key(&self.shared_key, &self.rendezvous.side_id, phase)?;
        let cipher = XSalsa20Poly1305::new(Key::from_slice(&key));
        let mut nonce_bytes = [0_u8; 24];
        rand::rng().fill(&mut nonce_bytes);
        let ciphertext = cipher
            .encrypt(Nonce::from_slice(&nonce_bytes), plaintext.as_ref())
            .map_err(|_| CoreError::Crypto("encrypt wormhole control message failed".into()))?;
        let mut envelope = Vec::with_capacity(nonce_bytes.len() + ciphertext.len());
        envelope.extend_from_slice(&nonce_bytes);
        envelope.extend_from_slice(&ciphertext);
        self.rendezvous
            .add_message(phase, &hex::encode(envelope))
            .await
    }

    async fn receive_phase<T: DeserializeOwned>(&mut self, expected_phase: &str) -> Result<T> {
        let MailboxMessage { side, phase, body } = self.rendezvous.next_peer_message().await?;
        if phase != expected_phase {
            return Err(protocol_error(format!(
                "expected wormhole phase {expected_phase}, received {phase}"
            )));
        }
        let envelope = hex::decode(body)
            .map_err(|_| protocol_error("encrypted wormhole message is not hexadecimal"))?;
        if envelope.len() < 24 + 16 {
            return Err(protocol_error("encrypted wormhole message is truncated"));
        }
        let (nonce, ciphertext) = envelope.split_at(24);
        let key = derive_phase_key(&self.shared_key, &side, &phase)?;
        let cipher = XSalsa20Poly1305::new(Key::from_slice(&key));
        let plaintext = cipher
            .decrypt(Nonce::from_slice(nonce), ciphertext)
            .map_err(|_| CoreError::Crypto("decrypt wormhole control message failed".into()))?;
        serde_json::from_slice(&plaintext).map_err(Into::into)
    }

    pub fn transit_key(&self) -> Result<[u8; 32]> {
        derive_key(
            &self.shared_key,
            format!("{WORMHOLE_APP_ID}/transit-key").as_bytes(),
        )
    }

    pub async fn close(self, mood: &str) -> Result<()> {
        self.rendezvous.close(mood).await
    }
}

impl PendingWormhole {
    pub async fn establish(self) -> Result<WormholeProtocol> {
        let Self {
            keepalive,
            stop,
            code,
            role,
            ..
        } = self;
        stop.cancel();
        let rendezvous = keepalive
            .await
            .map_err(|error| protocol_error(format!("rendezvous keepalive failed: {error}")))??;
        WormholeProtocol::establish(rendezvous, &code, role).await
    }
}

#[derive(Debug, Deserialize, Serialize)]
struct VersionEnvelope {
    inkhole_v2: VersionInfo,
}

#[derive(Debug, Deserialize, Serialize)]
struct VersionInfo {
    protocol: u16,
    role: PeerRole,
}

fn derive_phase_key(shared_key: &[u8; 32], side: &str, phase: &str) -> Result<[u8; 32]> {
    let side_hash = Sha256::digest(side.as_bytes());
    let phase_hash = Sha256::digest(phase.as_bytes());
    let mut purpose = Vec::with_capacity(15 + side_hash.len() + phase_hash.len());
    purpose.extend_from_slice(b"wormhole:phase:");
    purpose.extend_from_slice(&side_hash);
    purpose.extend_from_slice(&phase_hash);
    derive_key(shared_key, &purpose)
}

pub(super) fn derive_key(input: &[u8], purpose: &[u8]) -> Result<[u8; 32]> {
    let mut output = [0_u8; 32];
    Hkdf::<Sha256>::new(None, input)
        .expand(purpose, &mut output)
        .map_err(|_| CoreError::Crypto("HKDF output length is invalid".into()))?;
    Ok(output)
}

fn generate_code(nameplate: &str) -> String {
    let words = Language::English.word_list();
    let mut rng = rand::rng();
    let first = words[rng.random_range(0..words.len())];
    let second = words[rng.random_range(0..words.len())];
    format!("{nameplate}-{first}-{second}")
}

fn normalize_code(raw: &str) -> Result<String> {
    let code = raw.trim().to_ascii_lowercase();
    if code.is_empty()
        || code.len() > MAX_CODE_LENGTH
        || !code
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
    {
        return Err(CoreError::InvalidRequest("short code is invalid".into()));
    }
    let mut components = code.split('-');
    let nameplate = components.next().unwrap_or_default();
    if nameplate.is_empty()
        || !nameplate.bytes().all(|byte| byte.is_ascii_digit())
        || components.clone().count() < 2
        || components.any(str::is_empty)
    {
        return Err(CoreError::InvalidRequest("short code is incomplete".into()));
    }
    Ok(code)
}

fn random_hex(bytes: usize) -> String {
    let mut value = vec![0_u8; bytes];
    rand::rng().fill(value.as_mut_slice());
    hex::encode(value)
}

fn valid_host_port(value: &str) -> bool {
    value.rsplit_once(':').is_some_and(|(host, port)| {
        !host.is_empty() && port.parse::<u16>().is_ok_and(|port| port > 0)
    })
}

fn protocol_error(message: impl Into<String>) -> CoreError {
    CoreError::Protocol(message.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wormhole::test_support::MockRendezvous;

    #[test]
    fn validates_and_normalizes_short_codes() {
        assert_eq!(
            normalize_code(" 12-ABANDON-Ability ").unwrap(),
            "12-abandon-ability"
        );
        assert!(normalize_code("word-word").is_err());
        assert!(normalize_code("12-one").is_err());
        assert!(normalize_code("12-one-").is_err());
        assert!(normalize_code("12-one two").is_err());
    }

    #[test]
    fn phase_encryption_matches_on_both_sides_and_binds_context() {
        let shared = [0x5a; 32];
        let first = derive_phase_key(&shared, "side-a", "0").unwrap();
        assert_eq!(first, derive_phase_key(&shared, "side-a", "0").unwrap());
        assert_ne!(first, derive_phase_key(&shared, "side-b", "0").unwrap());
        assert_ne!(first, derive_phase_key(&shared, "side-a", "1").unwrap());
        assert_ne!(first, derive_key(&shared, b"transit").unwrap());
    }

    #[test]
    fn settings_reject_ambiguous_network_endpoints() {
        let mut valid = WormholeSettings::default();
        valid.normalize().unwrap();
        let mut invalid_url = WormholeSettings {
            rendezvous_url: "https://example.test".into(),
            ..WormholeSettings::default()
        };
        assert!(invalid_url.normalize().is_err());
        let mut invalid_relay = WormholeSettings {
            transit_relay: "example.test".into(),
            ..WormholeSettings::default()
        };
        assert!(invalid_relay.normalize().is_err());
    }

    #[tokio::test]
    async fn local_rendezvous_establishes_pake_and_encrypted_phases() {
        let rendezvous = MockRendezvous::start().await;
        let settings = WormholeSettings {
            rendezvous_url: rendezvous.url().into(),
            transit_relay: "127.0.0.1:1".into(),
        };
        let (code, pending) = WormholeProtocol::allocate(&settings).await.unwrap();
        let (sender, receiver) = tokio::join!(
            pending.establish(),
            WormholeProtocol::join(&settings, &code)
        );
        let mut sender = sender.unwrap();
        let mut receiver = receiver.unwrap();

        let (sender_result, receiver_result) = tokio::join!(
            async {
                sender.send(&json!({ "from": "sender" })).await?;
                sender.receive::<serde_json::Value>().await
            },
            async {
                receiver.send(&json!({ "from": "receiver" })).await?;
                receiver.receive::<serde_json::Value>().await
            },
        );
        assert_eq!(sender_result.unwrap(), json!({ "from": "receiver" }));
        assert_eq!(receiver_result.unwrap(), json!({ "from": "sender" }));
        let (sender_close, receiver_close) =
            tokio::join!(sender.close("happy"), receiver.close("happy"));
        sender_close.unwrap();
        receiver_close.unwrap();
        rendezvous.close().await;
    }
}
