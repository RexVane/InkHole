use std::collections::HashSet;

use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64};
use bip39::Language;
use crypto_secretbox::{
    Key, Nonce, XSalsa20Poly1305,
    aead::{Aead, KeyInit},
};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use hkdf::Hkdf;
use rand::Rng;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use sha2::Sha256;
use spake2::{Ed25519Group, Identity, Password, Spake2};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio_util::sync::CancellationToken;

use crate::{CoreError, DeviceIdentity, Result, protocol::valid_instance_id};

const PAIRING_APP_ID: &[u8] = b"com.rexvane.inkhole/ssh-pair-v2";
const MAX_RELAY_FRAME: usize = 64 * 1024;
const PAIR_CODE_WORDS: usize = 4;
const NONCE_BYTES: usize = 32;
const PAIR_CHANNEL_MAGIC: &[u8; 4] = b"IKP2";
const DATA_CHANNEL_MAGIC: &[u8; 4] = b"IKD2";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ChannelMode {
    Pair,
    Data,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct PairingIdentity {
    pub name: String,
    pub instance_id: String,
    pub remote_port: u16,
    pub public_key: String,
}

impl PairingIdentity {
    pub(crate) fn from_device(identity: &DeviceIdentity, remote_port: u16) -> Self {
        Self {
            name: identity.peer_name().to_owned(),
            instance_id: identity.instance_id().to_owned(),
            remote_port,
            public_key: identity.public_key_base64(),
        }
    }

    pub(crate) fn validate(&self) -> Result<()> {
        if self.name.trim().is_empty()
            || self.name.len() > 255
            || !valid_instance_id(&self.instance_id)
            || self.remote_port == 0
        {
            return Err(protocol_error("invalid SSH relay peer identity"));
        }
        verifying_key(&self.public_key)?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum PairRole {
    Creator,
    Joiner,
}

impl PairRole {
    fn opposite(self) -> Self {
        match self {
            Self::Creator => Self::Joiner,
            Self::Joiner => Self::Creator,
        }
    }
}

#[derive(Serialize, Deserialize)]
struct PakeFrame {
    pake: String,
}

#[derive(Serialize, Deserialize)]
struct CipherFrame {
    nonce: String,
    ciphertext: String,
}

#[derive(Serialize, Deserialize)]
struct PairPayload {
    role: PairRole,
    identity: PairingIdentity,
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct DataHello {
    sender_instance_id: String,
    receiver_instance_id: String,
    nonce: String,
    signature: String,
}

impl DataHello {
    pub(crate) fn sender_instance_id(&self) -> &str {
        &self.sender_instance_id
    }
}

#[derive(Debug)]
pub(crate) struct VerifiedDataHello {
    pub sender_instance_id: String,
    nonce: [u8; NONCE_BYTES],
}

#[derive(Debug, Serialize, Deserialize)]
struct DataAnswer {
    sender_instance_id: String,
    receiver_instance_id: String,
    client_nonce: String,
    server_nonce: String,
    quic_fingerprint: String,
    quic_secret: String,
    signature: String,
}

#[derive(Debug)]
pub(crate) struct SenderAuthentication {
    pub quic_fingerprint: String,
    pub quic_secret: String,
    pub tunnel_key: [u8; 32],
}

pub(crate) async fn write_channel_mode<S>(
    stream: &mut S,
    mode: ChannelMode,
    cancellation: &CancellationToken,
) -> Result<()>
where
    S: AsyncWrite + Unpin,
{
    let magic = match mode {
        ChannelMode::Pair => PAIR_CHANNEL_MAGIC,
        ChannelMode::Data => DATA_CHANNEL_MAGIC,
    };
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        result = async {
            stream.write_all(magic).await?;
            stream.flush().await?;
            Ok(())
        } => result,
    }
}

pub(crate) async fn read_channel_mode<S>(
    stream: &mut S,
    cancellation: &CancellationToken,
) -> Result<ChannelMode>
where
    S: AsyncRead + Unpin,
{
    let mut magic = [0_u8; 4];
    tokio::select! {
        _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = stream.read_exact(&mut magic) => {
            result?;
        }
    }
    match &magic {
        PAIR_CHANNEL_MAGIC => Ok(ChannelMode::Pair),
        DATA_CHANNEL_MAGIC => Ok(ChannelMode::Data),
        _ => Err(protocol_error("unknown SSH relay channel mode")),
    }
}

pub(crate) fn generate_pair_code(remote_port: u16) -> Result<String> {
    if remote_port == 0 {
        return Err(CoreError::InvalidRequest(
            "SSH relay remote port is unavailable".into(),
        ));
    }
    let words = Language::English.word_list();
    let mut rng = rand::rng();
    // Resampling guards against a draw that normalize_pair_code would reject,
    // such as four identical words.
    for _ in 0..64 {
        let suffix = (0..PAIR_CODE_WORDS)
            .map(|_| words[rng.random_range(0..words.len())])
            .collect::<Vec<_>>()
            .join("-");
        let code = format!("{remote_port}-{suffix}");
        if normalize_pair_code(&code).is_ok() {
            return Ok(code);
        }
    }
    Err(CoreError::Crypto(
        "could not generate an SSH pairing code".into(),
    ))
}

pub(crate) fn pair_code_port(code: &str) -> Result<u16> {
    let code = normalize_pair_code(code)?;
    code.split('-')
        .next()
        .and_then(|port| port.parse::<u16>().ok())
        .filter(|port| *port != 0)
        .ok_or_else(|| CoreError::InvalidRequest("SSH pairing code has an invalid port".into()))
}

pub(crate) async fn exchange_pairing<S>(
    stream: &mut S,
    code: &str,
    role: bool,
    local: PairingIdentity,
    cancellation: &CancellationToken,
) -> Result<PairingIdentity>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    local.validate()?;
    let role = if role {
        PairRole::Creator
    } else {
        PairRole::Joiner
    };
    let code = normalize_pair_code(code)?;
    let key = establish_pairing_key(stream, &code, cancellation).await?;
    let outbound = encrypt_pair_payload(
        &key,
        &PairPayload {
            role,
            identity: local,
        },
    )?;
    cancellable_write(stream, &outbound, cancellation).await?;
    let inbound: CipherFrame = cancellable_read(stream, cancellation).await?;
    let peer: PairPayload = decrypt_pair_payload(&key, &inbound)?;
    if peer.role != role.opposite() {
        return Err(protocol_error("SSH pairing role reflection detected"));
    }
    peer.identity.validate()?;
    Ok(peer.identity)
}

pub(crate) async fn send_data_hello<S>(
    stream: &mut S,
    local: &DeviceIdentity,
    peer: &PairingIdentity,
    cancellation: &CancellationToken,
) -> Result<[u8; NONCE_BYTES]>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    peer.validate()?;
    let nonce = random_nonce();
    let message = client_auth_message(local.instance_id(), &peer.instance_id, &nonce);
    let hello = DataHello {
        sender_instance_id: local.instance_id().to_owned(),
        receiver_instance_id: peer.instance_id.clone(),
        nonce: hex::encode(nonce),
        signature: local.sign_base64(&message),
    };
    cancellable_write(stream, &hello, cancellation).await?;
    Ok(nonce)
}

pub(crate) async fn receive_data_hello<S>(
    stream: &mut S,
    cancellation: &CancellationToken,
) -> Result<DataHello>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    cancellable_read(stream, cancellation).await
}

pub(crate) fn verify_data_hello(
    hello: DataHello,
    local: &DeviceIdentity,
    peer: &PairingIdentity,
) -> Result<VerifiedDataHello> {
    if hello.sender_instance_id != peer.instance_id
        || hello.receiver_instance_id != local.instance_id()
    {
        return Err(protocol_error(
            "SSH relay data channel targets the wrong peer",
        ));
    }
    let nonce = decode_nonce(&hello.nonce)?;
    let message = client_auth_message(
        &hello.sender_instance_id,
        &hello.receiver_instance_id,
        &nonce,
    );
    verify_signature(&peer.public_key, &message, &hello.signature)?;
    Ok(VerifiedDataHello {
        sender_instance_id: hello.sender_instance_id,
        nonce,
    })
}

pub(crate) async fn send_data_answer<S>(
    stream: &mut S,
    local: &DeviceIdentity,
    peer: &PairingIdentity,
    hello: &VerifiedDataHello,
    quic_fingerprint: String,
    quic_secret: String,
    cancellation: &CancellationToken,
) -> Result<[u8; 32]>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    if hello.sender_instance_id != peer.instance_id
        || !valid_fingerprint(&quic_fingerprint)
        || quic_secret.len() < 32
        || quic_secret.len() > 128
    {
        return Err(protocol_error("invalid SSH relay QUIC credentials"));
    }
    let server_nonce = random_nonce();
    let message = server_auth_message(
        &peer.instance_id,
        local.instance_id(),
        &hello.nonce,
        &server_nonce,
        &quic_fingerprint,
        &quic_secret,
    );
    let answer = DataAnswer {
        sender_instance_id: peer.instance_id.clone(),
        receiver_instance_id: local.instance_id().to_owned(),
        client_nonce: hex::encode(hello.nonce),
        server_nonce: hex::encode(server_nonce),
        quic_fingerprint,
        quic_secret,
        signature: local.sign_base64(&message),
    };
    cancellable_write(stream, &answer, cancellation).await?;
    derive_tunnel_key(
        &hello.nonce,
        &server_nonce,
        &peer.public_key,
        &local.public_key_base64(),
    )
}

pub(crate) async fn receive_data_answer<S>(
    stream: &mut S,
    local: &DeviceIdentity,
    peer: &PairingIdentity,
    client_nonce: &[u8; NONCE_BYTES],
    cancellation: &CancellationToken,
) -> Result<SenderAuthentication>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let answer: DataAnswer = cancellable_read(stream, cancellation).await?;
    if answer.sender_instance_id != local.instance_id()
        || answer.receiver_instance_id != peer.instance_id
        || decode_nonce(&answer.client_nonce)? != *client_nonce
        || !valid_fingerprint(&answer.quic_fingerprint)
        || answer.quic_secret.len() < 32
        || answer.quic_secret.len() > 128
    {
        return Err(protocol_error("invalid SSH relay data answer"));
    }
    let server_nonce = decode_nonce(&answer.server_nonce)?;
    let message = server_auth_message(
        local.instance_id(),
        &peer.instance_id,
        client_nonce,
        &server_nonce,
        &answer.quic_fingerprint,
        &answer.quic_secret,
    );
    verify_signature(&peer.public_key, &message, &answer.signature)?;
    let tunnel_key = derive_tunnel_key(
        client_nonce,
        &server_nonce,
        &local.public_key_base64(),
        &peer.public_key,
    )?;
    Ok(SenderAuthentication {
        quic_fingerprint: answer.quic_fingerprint,
        quic_secret: answer.quic_secret,
        tunnel_key,
    })
}

async fn establish_pairing_key<S>(
    stream: &mut S,
    code: &str,
    cancellation: &CancellationToken,
) -> Result<[u8; 32]>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let (spake, outbound) = Spake2::<Ed25519Group>::start_symmetric(
        &Password::new(code.as_bytes()),
        &Identity::new(PAIRING_APP_ID),
    );
    cancellable_write(
        stream,
        &PakeFrame {
            pake: hex::encode(outbound),
        },
        cancellation,
    )
    .await?;
    let peer: PakeFrame = cancellable_read(stream, cancellation).await?;
    let peer =
        hex::decode(peer.pake).map_err(|_| protocol_error("SSH pairing PAKE value is invalid"))?;
    let shared = spake
        .finish(&peer)
        .map_err(|error| CoreError::Crypto(format!("SSH pairing SPAKE2 failed: {error}")))?;
    let mut key = [0_u8; 32];
    Hkdf::<Sha256>::new(None, &shared)
        .expand(b"inkhole/ssh-pair-v2/control", &mut key)
        .map_err(|_| CoreError::Crypto("SSH pairing key derivation failed".into()))?;
    Ok(key)
}

fn encrypt_pair_payload(key: &[u8; 32], payload: &PairPayload) -> Result<CipherFrame> {
    let nonce = random_nonce_bytes::<24>();
    let plaintext = serde_json::to_vec(payload)?;
    let ciphertext = XSalsa20Poly1305::new(Key::from_slice(key))
        .encrypt(Nonce::from_slice(&nonce), plaintext.as_ref())
        .map_err(|_| CoreError::Crypto("encrypt SSH pairing identity failed".into()))?;
    Ok(CipherFrame {
        nonce: hex::encode(nonce),
        ciphertext: hex::encode(ciphertext),
    })
}

fn decrypt_pair_payload(key: &[u8; 32], frame: &CipherFrame) -> Result<PairPayload> {
    let nonce: [u8; 24] = hex::decode(&frame.nonce)
        .map_err(|_| protocol_error("SSH pairing nonce is invalid"))?
        .try_into()
        .map_err(|_| protocol_error("SSH pairing nonce is invalid"))?;
    let ciphertext = hex::decode(&frame.ciphertext)
        .map_err(|_| protocol_error("SSH pairing ciphertext is invalid"))?;
    let plaintext = XSalsa20Poly1305::new(Key::from_slice(key))
        .decrypt(Nonce::from_slice(&nonce), ciphertext.as_ref())
        .map_err(|_| CoreError::Crypto("decrypt SSH pairing identity failed".into()))?;
    serde_json::from_slice(&plaintext).map_err(Into::into)
}

async fn cancellable_write<S, T>(
    stream: &mut S,
    value: &T,
    cancellation: &CancellationToken,
) -> Result<()>
where
    S: AsyncWrite + Unpin,
    T: Serialize,
{
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        result = write_frame(stream, value) => result,
    }
}

async fn cancellable_read<S, T>(stream: &mut S, cancellation: &CancellationToken) -> Result<T>
where
    S: AsyncRead + Unpin,
    T: DeserializeOwned,
{
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        result = read_frame(stream) => result,
    }
}

async fn write_frame<S: AsyncWrite + Unpin, T: Serialize>(stream: &mut S, value: &T) -> Result<()> {
    let payload = serde_json::to_vec(value)?;
    if payload.is_empty() || payload.len() > MAX_RELAY_FRAME {
        return Err(CoreError::InvalidRequest(
            "SSH relay control frame is invalid".into(),
        ));
    }
    stream.write_u32(payload.len() as u32).await?;
    stream.write_all(&payload).await?;
    stream.flush().await?;
    Ok(())
}

async fn read_frame<S: AsyncRead + Unpin, T: DeserializeOwned>(stream: &mut S) -> Result<T> {
    let size = stream.read_u32().await? as usize;
    if size == 0 || size > MAX_RELAY_FRAME {
        return Err(protocol_error("SSH relay control frame is invalid"));
    }
    let mut payload = vec![0_u8; size];
    stream.read_exact(&mut payload).await?;
    serde_json::from_slice(&payload).map_err(Into::into)
}

fn normalize_pair_code(raw: &str) -> Result<String> {
    let code = raw.trim().to_ascii_lowercase();
    let components = code.split('-').collect::<Vec<_>>();
    if components.len() != PAIR_CODE_WORDS + 1
        || components[0]
            .parse::<u16>()
            .ok()
            .filter(|port| *port != 0)
            .is_none()
        || components[1..].iter().any(|word| {
            word.is_empty()
                || !word.bytes().all(|byte| byte.is_ascii_lowercase())
                || !Language::English.word_list().contains(word)
        })
        || components[1..].iter().collect::<HashSet<_>>().len() < 2
    {
        return Err(CoreError::InvalidRequest(
            "SSH pairing code is invalid".into(),
        ));
    }
    Ok(code)
}

fn verifying_key(encoded: &str) -> Result<VerifyingKey> {
    let bytes: [u8; 32] = BASE64
        .decode(encoded)
        .map_err(|_| protocol_error("SSH relay peer public key is invalid"))?
        .try_into()
        .map_err(|_| protocol_error("SSH relay peer public key is invalid"))?;
    VerifyingKey::from_bytes(&bytes)
        .map_err(|_| protocol_error("SSH relay peer public key is invalid"))
}

fn verify_signature(public_key: &str, message: &[u8], encoded: &str) -> Result<()> {
    let signature: [u8; 64] = BASE64
        .decode(encoded)
        .map_err(|_| protocol_error("SSH relay signature is invalid"))?
        .try_into()
        .map_err(|_| protocol_error("SSH relay signature is invalid"))?;
    verifying_key(public_key)?
        .verify(message, &Signature::from_bytes(&signature))
        .map_err(|_| protocol_error("SSH relay signature verification failed"))
}

fn client_auth_message(sender: &str, receiver: &str, nonce: &[u8; NONCE_BYTES]) -> Vec<u8> {
    authentication_message(
        b"inkhole/ssh-relay-v2/client",
        &[sender.as_bytes(), receiver.as_bytes(), nonce],
    )
}

fn server_auth_message(
    sender: &str,
    receiver: &str,
    client_nonce: &[u8; NONCE_BYTES],
    server_nonce: &[u8; NONCE_BYTES],
    fingerprint: &str,
    secret: &str,
) -> Vec<u8> {
    authentication_message(
        b"inkhole/ssh-relay-v2/server",
        &[
            sender.as_bytes(),
            receiver.as_bytes(),
            client_nonce,
            server_nonce,
            fingerprint.as_bytes(),
            secret.as_bytes(),
        ],
    )
}

fn authentication_message(domain: &[u8], fields: &[&[u8]]) -> Vec<u8> {
    let mut message = Vec::with_capacity(
        domain.len() + fields.iter().map(|field| field.len() + 4).sum::<usize>(),
    );
    message.extend_from_slice(domain);
    for field in fields {
        message.extend_from_slice(&(field.len() as u32).to_be_bytes());
        message.extend_from_slice(field);
    }
    message
}

fn derive_tunnel_key(
    client_nonce: &[u8; NONCE_BYTES],
    server_nonce: &[u8; NONCE_BYTES],
    client_public: &str,
    server_public: &str,
) -> Result<[u8; 32]> {
    let mut input = authentication_message(
        b"inkhole/ssh-relay-v2/tunnel",
        &[
            client_nonce,
            server_nonce,
            client_public.as_bytes(),
            server_public.as_bytes(),
        ],
    );
    let mut key = [0_u8; 32];
    Hkdf::<Sha256>::new(None, &input)
        .expand(b"record-key", &mut key)
        .map_err(|_| CoreError::Crypto("SSH relay tunnel key derivation failed".into()))?;
    input.fill(0);
    Ok(key)
}

fn decode_nonce(value: &str) -> Result<[u8; NONCE_BYTES]> {
    hex::decode(value)
        .map_err(|_| protocol_error("SSH relay nonce is invalid"))?
        .try_into()
        .map_err(|_| protocol_error("SSH relay nonce is invalid"))
}

fn random_nonce() -> [u8; NONCE_BYTES] {
    random_nonce_bytes()
}

fn random_nonce_bytes<const N: usize>() -> [u8; N] {
    let mut value = [0_u8; N];
    rand::rng().fill(&mut value);
    value
}

fn valid_fingerprint(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn protocol_error(message: impl Into<String>) -> CoreError {
    CoreError::Protocol(message.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity(instance_id: &str, name: &str, port: u16) -> (DeviceIdentity, PairingIdentity) {
        let device = DeviceIdentity::generate(Some(instance_id), name).unwrap();
        let pairing = PairingIdentity::from_device(&device, port);
        (device, pairing)
    }

    #[test]
    fn generated_pair_codes_always_normalize() {
        for _ in 0..256 {
            let code = generate_pair_code(32001).unwrap();
            assert_eq!(normalize_pair_code(&code).unwrap(), code);
        }
    }

    #[tokio::test]
    async fn pairing_code_uses_pake_and_exchanges_identities() {
        let (_, creator) = identity("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Creator", 32001);
        let (_, joiner) = identity("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "Joiner", 32002);
        let code = generate_pair_code(creator.remote_port).unwrap();
        assert_eq!(pair_code_port(&code).unwrap(), creator.remote_port);
        let (mut left, mut right) = tokio::io::duplex(MAX_RELAY_FRAME * 2);
        let cancellation = CancellationToken::new();
        let (creator_result, joiner_result) = tokio::join!(
            exchange_pairing(&mut left, &code, true, creator.clone(), &cancellation),
            exchange_pairing(&mut right, &code, false, joiner.clone(), &cancellation),
        );
        assert_eq!(creator_result.unwrap(), joiner);
        assert_eq!(joiner_result.unwrap(), creator);
    }

    #[tokio::test]
    async fn pairing_rejects_the_wrong_code() {
        let (_, creator) = identity("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Creator", 32001);
        let (_, joiner) = identity("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "Joiner", 32002);
        let creator_code = generate_pair_code(creator.remote_port).unwrap();
        let joiner_code = generate_pair_code(creator.remote_port).unwrap();
        let (mut left, mut right) = tokio::io::duplex(MAX_RELAY_FRAME * 2);
        let cancellation = CancellationToken::new();
        let (creator_result, joiner_result) = tokio::join!(
            exchange_pairing(&mut left, &creator_code, true, creator, &cancellation,),
            exchange_pairing(&mut right, &joiner_code, false, joiner, &cancellation),
        );
        assert!(creator_result.is_err());
        assert!(joiner_result.is_err());
    }

    #[tokio::test]
    async fn data_channel_mutually_authenticates_temporary_quic_credentials() {
        let (sender, sender_peer) = identity("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Sender", 32001);
        let (receiver, receiver_peer) =
            identity("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "Receiver", 32002);
        let (mut left, mut right) = tokio::io::duplex(MAX_RELAY_FRAME * 2);
        let cancellation = CancellationToken::new();
        let fingerprint = "c".repeat(64);
        let secret = "d".repeat(64);
        let sender_task = async {
            let nonce = send_data_hello(&mut left, &sender, &receiver_peer, &cancellation).await?;
            receive_data_answer(&mut left, &sender, &receiver_peer, &nonce, &cancellation).await
        };
        let receiver_task = async {
            let hello = receive_data_hello(&mut right, &cancellation).await?;
            let hello = verify_data_hello(hello, &receiver, &sender_peer)?;
            send_data_answer(
                &mut right,
                &receiver,
                &sender_peer,
                &hello,
                fingerprint.clone(),
                secret.clone(),
                &cancellation,
            )
            .await
        };
        let (sender_result, receiver_key) = tokio::join!(sender_task, receiver_task);
        let sender_result = sender_result.unwrap();
        assert_eq!(sender_result.quic_fingerprint, "c".repeat(64));
        assert_eq!(sender_result.quic_secret, "d".repeat(64));
        assert_eq!(sender_result.tunnel_key, receiver_key.unwrap());
    }
}
