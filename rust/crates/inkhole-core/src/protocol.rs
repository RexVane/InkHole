use std::{collections::HashSet, path::Path};

use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::Value;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use unicode_normalization::UnicodeNormalization;
use uuid::Uuid;

use crate::{
    CoreError, LAN_DISCOVERY_PROTOCOL_VERSION, QUIC_PROTOCOL_VERSION, Result, hash::valid_blake3,
};

pub const MAX_CONTROL_FRAME: usize = 64 * 1024;
pub const MAX_FOLDER_MANIFEST_FRAME: usize = 32 * 1024 * 1024;
pub const MAX_FILE_SIZE: u64 = 1_u64 << 40;
pub const MAX_FOLDER_ENTRIES: usize = 100_000;
pub const AUTH_HELLO_TYPE: &str = "auth_hello";
pub const AUTH_CHALLENGE_TYPE: &str = "auth_challenge";
pub const AUTHENTICATED_REQUEST_TYPE: &str = "authenticated_request";
pub const AUTH_REJECTED_TYPE: &str = "auth_rejected";
pub const PEER_PROBE_REQUEST_TYPE: &str = "peer_probe";
pub const PEER_PROBE_RESPONSE_TYPE: &str = "peer_probe_response";
const AUTH_KEY_CONTEXT: &str = "com.rexvane.inkhole.quic.shared-secret.v2";
const AUTH_REQUEST_DOMAIN: &[u8] = b"inkhole/quic/authenticated-request/v2\0";
const OFFER_SIGNATURE_DOMAIN: &[u8] = b"inkhole/quic/offer/v2\0";
const PROBE_SIGNATURE_DOMAIN: &[u8] = b"inkhole/quic/peer-probe/v2\0";
const FOLDER_MANIFEST_DOMAIN: &[u8] = b"inkhole/quic/folder-manifest/v1\0";
const AUTH_NONCE_BYTES: usize = 32;
const PROBE_NONCE_BYTES: usize = 32;
const MAX_CAPABILITIES: usize = 64;
const MAX_IDENTITY_FIELD: usize = 255;
const MAX_FOLDER_PATH_BYTES: usize = 4096;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DeviceSummary {
    pub instance_id: String,
    pub name: String,
    pub public_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AuthenticationHello {
    #[serde(rename = "type")]
    pub frame_type: String,
    pub protocol: u16,
    pub nonce: String,
}

impl AuthenticationHello {
    pub fn new() -> Self {
        let nonce: [u8; AUTH_NONCE_BYTES] = rand::random();
        Self {
            frame_type: AUTH_HELLO_TYPE.to_owned(),
            protocol: QUIC_PROTOCOL_VERSION,
            nonce: hex::encode(nonce),
        }
    }

    pub fn validate(&self) -> Result<()> {
        if self.frame_type != AUTH_HELLO_TYPE
            || self.protocol != QUIC_PROTOCOL_VERSION
            || !valid_hex_nonce(&self.nonce, AUTH_NONCE_BYTES)
        {
            return Err(authentication_failed());
        }
        Ok(())
    }
}

impl Default for AuthenticationHello {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AuthenticationChallenge {
    #[serde(rename = "type")]
    pub frame_type: String,
    pub protocol: u16,
    pub client_nonce: String,
    pub nonce: String,
}

impl AuthenticationChallenge {
    pub fn new(hello: &AuthenticationHello) -> Result<Self> {
        hello.validate()?;
        let nonce: [u8; AUTH_NONCE_BYTES] = rand::random();
        Ok(Self {
            frame_type: AUTH_CHALLENGE_TYPE.to_owned(),
            protocol: QUIC_PROTOCOL_VERSION,
            client_nonce: hello.nonce.clone(),
            nonce: hex::encode(nonce),
        })
    }

    pub fn validate_for(&self, hello: &AuthenticationHello) -> Result<()> {
        hello.validate()?;
        if self.frame_type != AUTH_CHALLENGE_TYPE {
            return Err(authentication_failed());
        }
        if self.protocol != QUIC_PROTOCOL_VERSION {
            return Err(CoreError::Protocol(format!(
                "unsupported QUIC authentication protocol version {}",
                self.protocol
            )));
        }
        if self.client_nonce != hello.nonce
            || !valid_hex_nonce(&self.client_nonce, AUTH_NONCE_BYTES)
            || !valid_hex_nonce(&self.nonce, AUTH_NONCE_BYTES)
        {
            return Err(authentication_failed());
        }
        Ok(())
    }

    fn validate(&self) -> Result<()> {
        if self.frame_type != AUTH_CHALLENGE_TYPE
            || self.protocol != QUIC_PROTOCOL_VERSION
            || !valid_hex_nonce(&self.client_nonce, AUTH_NONCE_BYTES)
            || !valid_hex_nonce(&self.nonce, AUTH_NONCE_BYTES)
        {
            return Err(authentication_failed());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AuthenticatedRequest {
    #[serde(rename = "type")]
    pub frame_type: String,
    pub protocol: u16,
    pub client_nonce: String,
    pub challenge: String,
    pub payload: Value,
    pub mac: String,
}

#[derive(Debug, Serialize)]
struct AuthenticatableRequest<'a> {
    protocol: u16,
    client_nonce: &'a str,
    challenge: &'a str,
    payload: &'a Value,
}

impl AuthenticatedRequest {
    pub fn new<T>(
        challenge: &AuthenticationChallenge,
        payload: &T,
        shared_secret: &str,
    ) -> Result<Self>
    where
        T: Serialize,
    {
        challenge.validate()?;
        let payload = serde_json::to_value(payload)?;
        let mac = authentication_mac(
            challenge.protocol,
            &challenge.client_nonce,
            &challenge.nonce,
            &payload,
            shared_secret,
        )?;
        Ok(Self {
            frame_type: AUTHENTICATED_REQUEST_TYPE.to_owned(),
            protocol: challenge.protocol,
            client_nonce: challenge.client_nonce.clone(),
            challenge: challenge.nonce.clone(),
            payload,
            mac: hex::encode(mac),
        })
    }

    pub fn authenticate(
        self,
        expected_challenge: &AuthenticationChallenge,
        shared_secret: &str,
    ) -> Result<Value> {
        expected_challenge.validate()?;
        if self.frame_type != AUTHENTICATED_REQUEST_TYPE
            || self.protocol != expected_challenge.protocol
            || self.client_nonce != expected_challenge.client_nonce
            || self.challenge != expected_challenge.nonce
        {
            return Err(authentication_failed());
        }
        let received: [u8; 32] = hex::decode(&self.mac)
            .map_err(|_| authentication_failed())?
            .try_into()
            .map_err(|_| authentication_failed())?;
        let expected = authentication_mac(
            self.protocol,
            &self.client_nonce,
            &self.challenge,
            &self.payload,
            shared_secret,
        )?;
        if !constant_time_eq(&received, &expected) {
            return Err(authentication_failed());
        }
        Ok(self.payload)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AuthenticationRejected {
    #[serde(rename = "type")]
    pub frame_type: String,
    pub reason: String,
}

impl AuthenticationRejected {
    pub fn new() -> Self {
        Self {
            frame_type: AUTH_REJECTED_TYPE.to_owned(),
            reason: "shared secret authentication failed".to_owned(),
        }
    }
}

impl Default for AuthenticationRejected {
    fn default() -> Self {
        Self::new()
    }
}

fn authentication_mac(
    protocol: u16,
    client_nonce: &str,
    challenge: &str,
    payload: &Value,
    shared_secret: &str,
) -> Result<[u8; 32]> {
    let signable = AuthenticatableRequest {
        protocol,
        client_nonce,
        challenge,
        payload,
    };
    let encoded = serde_json::to_vec(&signable)?;
    let key = blake3::derive_key(AUTH_KEY_CONTEXT, shared_secret.as_bytes());
    let mut hasher = blake3::Hasher::new_keyed(&key);
    hasher.update(AUTH_REQUEST_DOMAIN);
    hasher.update(&encoded);
    Ok(*hasher.finalize().as_bytes())
}

fn authentication_failed() -> CoreError {
    CoreError::Protocol("shared secret authentication failed".into())
}

fn constant_time_eq(left: &[u8; 32], right: &[u8; 32]) -> bool {
    left.iter()
        .zip(right)
        .fold(0_u8, |difference, (left, right)| {
            difference | (left ^ right)
        })
        == 0
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PeerProbeRequest {
    #[serde(rename = "type")]
    pub frame_type: String,
    pub protocol: u16,
    pub nonce: String,
}

impl PeerProbeRequest {
    pub fn new() -> Self {
        let nonce: [u8; PROBE_NONCE_BYTES] = rand::random();
        Self {
            frame_type: PEER_PROBE_REQUEST_TYPE.to_owned(),
            protocol: LAN_DISCOVERY_PROTOCOL_VERSION,
            nonce: hex::encode(nonce),
        }
    }

    pub fn validate(&self) -> Result<()> {
        if self.frame_type != PEER_PROBE_REQUEST_TYPE {
            return Err(CoreError::InvalidIdentity(
                "invalid peer probe frame type".into(),
            ));
        }
        if self.protocol != LAN_DISCOVERY_PROTOCOL_VERSION {
            return Err(CoreError::InvalidIdentity(format!(
                "unsupported peer probe protocol version {}",
                self.protocol
            )));
        }
        if !valid_probe_nonce(&self.nonce) {
            return Err(CoreError::InvalidIdentity(
                "invalid peer probe nonce".into(),
            ));
        }
        Ok(())
    }
}

impl Default for PeerProbeRequest {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PeerProbeResponse {
    #[serde(rename = "type")]
    pub frame_type: String,
    pub protocol: u16,
    pub nonce: String,
    pub instance_id: String,
    pub peer_name: String,
    pub capabilities: Vec<String>,
    pub public_key: String,
    pub certificate_fingerprint: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub signature: String,
}

#[derive(Debug, Serialize)]
struct SignablePeerProbeResponse<'a> {
    protocol: u16,
    nonce: &'a str,
    instance_id: &'a str,
    peer_name: &'a str,
    capabilities: &'a [String],
    public_key: &'a str,
    certificate_fingerprint: &'a str,
}

impl PeerProbeResponse {
    pub(crate) fn unsigned(
        request: &PeerProbeRequest,
        identity: DeviceSummary,
        certificate_fingerprint: String,
        capabilities: Vec<String>,
    ) -> Result<Self> {
        request.validate()?;
        Ok(Self {
            frame_type: PEER_PROBE_RESPONSE_TYPE.to_owned(),
            protocol: request.protocol,
            nonce: request.nonce.clone(),
            instance_id: identity.instance_id,
            peer_name: identity.name,
            capabilities: normalize_capabilities(capabilities)?,
            public_key: identity.public_key,
            certificate_fingerprint,
            signature: String::new(),
        })
    }

    pub fn signing_bytes(&self) -> Result<Vec<u8>> {
        let signable = SignablePeerProbeResponse {
            protocol: self.protocol,
            nonce: &self.nonce,
            instance_id: &self.instance_id,
            peer_name: &self.peer_name,
            capabilities: &self.capabilities,
            public_key: &self.public_key,
            certificate_fingerprint: &self.certificate_fingerprint,
        };
        let encoded = serde_json::to_vec(&signable)?;
        let mut message = Vec::with_capacity(PROBE_SIGNATURE_DOMAIN.len() + encoded.len());
        message.extend_from_slice(PROBE_SIGNATURE_DOMAIN);
        message.extend_from_slice(&encoded);
        Ok(message)
    }

    pub fn validate(
        &self,
        request: &PeerProbeRequest,
        expected_instance_id: Option<&str>,
        expected_fingerprint: &str,
    ) -> Result<()> {
        request.validate()?;
        if self.frame_type != PEER_PROBE_RESPONSE_TYPE {
            return Err(CoreError::InvalidIdentity(
                "invalid peer probe response type".into(),
            ));
        }
        if self.protocol != request.protocol || self.nonce != request.nonce {
            return Err(CoreError::InvalidIdentity(
                "peer probe challenge mismatch".into(),
            ));
        }
        if !valid_instance_id(&self.instance_id) {
            return Err(CoreError::InvalidIdentity(
                "invalid peer instance id".into(),
            ));
        }
        if let Some(expected) = expected_instance_id
            && (!valid_instance_id(expected) || self.instance_id != expected)
        {
            return Err(CoreError::InvalidIdentity(
                "peer instance id mismatch".into(),
            ));
        }
        if self.peer_name.trim() != self.peer_name
            || self.peer_name.is_empty()
            || self.peer_name.len() > MAX_IDENTITY_FIELD
        {
            return Err(CoreError::InvalidIdentity("invalid peer name".into()));
        }
        if normalize_capabilities(self.capabilities.clone())? != self.capabilities {
            return Err(CoreError::InvalidIdentity(
                "peer capabilities are not canonical".into(),
            ));
        }
        if !valid_blake3(&self.certificate_fingerprint)
            || !valid_blake3(expected_fingerprint)
            || self.certificate_fingerprint != expected_fingerprint
        {
            return Err(CoreError::FingerprintMismatch);
        }

        let public_key: [u8; 32] = BASE64
            .decode(&self.public_key)
            .map_err(|_| CoreError::InvalidIdentity("invalid peer public key".into()))?
            .try_into()
            .map_err(|_| CoreError::InvalidIdentity("invalid peer public key".into()))?;
        let signature: [u8; 64] = BASE64
            .decode(&self.signature)
            .map_err(|_| CoreError::InvalidIdentity("invalid peer probe signature".into()))?
            .try_into()
            .map_err(|_| CoreError::InvalidIdentity("invalid peer probe signature".into()))?;
        let verifying_key = VerifyingKey::from_bytes(&public_key)
            .map_err(|_| CoreError::InvalidIdentity("invalid peer public key".into()))?;
        verifying_key
            .verify(&self.signing_bytes()?, &Signature::from_bytes(&signature))
            .map_err(|_| {
                CoreError::InvalidIdentity("peer probe signature verification failed".into())
            })?;
        Ok(())
    }
}

pub(crate) fn normalize_capabilities(capabilities: Vec<String>) -> Result<Vec<String>> {
    if capabilities.len() > MAX_CAPABILITIES {
        return Err(CoreError::InvalidIdentity(
            "too many peer capabilities".into(),
        ));
    }
    let mut normalized = Vec::with_capacity(capabilities.len());
    for capability in capabilities {
        let capability = capability.trim();
        if capability.is_empty() || capability.len() > MAX_IDENTITY_FIELD {
            return Err(CoreError::InvalidIdentity("invalid peer capability".into()));
        }
        normalized.push(capability.to_owned());
    }
    normalized.sort();
    normalized.dedup();
    Ok(normalized)
}

fn valid_probe_nonce(value: &str) -> bool {
    valid_hex_nonce(value, PROBE_NONCE_BYTES)
}

fn valid_hex_nonce(value: &str, bytes: usize) -> bool {
    value.len() == bytes * 2
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum TransferKind {
    File,
    FolderV1,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FolderEntryKind {
    File,
    Directory,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FolderEntry {
    pub path: String,
    pub kind: FolderEntryKind,
    pub size: u64,
    pub modified_ms: i64,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub blake3: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FolderManifest {
    pub version: u16,
    pub entries: Vec<FolderEntry>,
}

impl FolderManifest {
    pub fn new(entries: Vec<FolderEntry>) -> Self {
        Self {
            version: 1,
            entries,
        }
    }

    pub fn validate(&self) -> Result<u64> {
        if self.version != 1 {
            return Err(CoreError::InvalidTransfer(format!(
                "unsupported folder manifest version {}",
                self.version
            )));
        }
        if self.entries.len() > MAX_FOLDER_ENTRIES {
            return Err(CoreError::InvalidTransfer(
                "folder contains too many entries".into(),
            ));
        }
        let mut previous_path: Option<&str> = None;
        let mut canonical_paths = HashSet::with_capacity(self.entries.len());
        let mut file_paths = HashSet::new();
        let mut total_size = 0_u64;
        for entry in &self.entries {
            validate_folder_relative_path(&entry.path)?;
            if previous_path.is_some_and(|previous| previous >= entry.path.as_str()) {
                return Err(CoreError::InvalidTransfer(
                    "folder manifest paths are not strictly sorted".into(),
                ));
            }
            previous_path = Some(&entry.path);

            let canonical = canonical_folder_path(&entry.path);
            if let Some((parent, _)) = canonical.rsplit_once('/')
                && !canonical_paths.contains(parent)
            {
                return Err(CoreError::InvalidTransfer(
                    "folder manifest is missing a parent directory".into(),
                ));
            }
            if !canonical_paths.insert(canonical.clone()) {
                return Err(CoreError::InvalidTransfer(
                    "folder contains a cross-platform duplicate path".into(),
                ));
            }
            for ancestor in folder_ancestors(&canonical) {
                if file_paths.contains(ancestor) {
                    return Err(CoreError::InvalidTransfer(
                        "folder entry is nested below a file".into(),
                    ));
                }
            }

            if entry.modified_ms < 0 {
                return Err(CoreError::InvalidTransfer(
                    "folder entry modified_ms cannot be negative".into(),
                ));
            }
            match entry.kind {
                FolderEntryKind::File => {
                    if entry.size > MAX_FILE_SIZE || !valid_blake3(&entry.blake3) {
                        return Err(CoreError::InvalidTransfer(
                            "invalid folder file metadata".into(),
                        ));
                    }
                    total_size = total_size
                        .checked_add(entry.size)
                        .ok_or_else(|| CoreError::InvalidTransfer("folder size overflow".into()))?;
                    if total_size > MAX_FILE_SIZE {
                        return Err(CoreError::InvalidTransfer("folder is too large".into()));
                    }
                    file_paths.insert(canonical);
                }
                FolderEntryKind::Directory => {
                    if entry.size != 0 || !entry.blake3.is_empty() {
                        return Err(CoreError::InvalidTransfer(
                            "invalid folder directory metadata".into(),
                        ));
                    }
                }
            }
        }
        Ok(total_size)
    }

    pub fn digest(&self) -> Result<blake3::Hash> {
        self.validate()?;
        let encoded = serde_json::to_vec(self)?;
        let mut hasher = blake3::Hasher::new();
        hasher.update(FOLDER_MANIFEST_DOMAIN);
        hasher.update(&encoded);
        Ok(hasher.finalize())
    }

    pub fn validate_offer(&self, offer: &TransferOffer) -> Result<()> {
        if offer.kind != TransferKind::FolderV1
            || self.validate()? != offer.size
            || self.digest()?.to_hex().as_str() != offer.blake3
        {
            return Err(CoreError::InvalidTransfer(
                "folder manifest does not match its offer".into(),
            ));
        }
        Ok(())
    }
}

pub fn validate_folder_relative_path(path: &str) -> Result<()> {
    if path.is_empty()
        || path.len() > MAX_FOLDER_PATH_BYTES
        || path.starts_with('/')
        || path.ends_with('/')
        || path.contains(['\\', '\0'])
    {
        return Err(CoreError::InvalidTransfer(
            "invalid folder entry path".into(),
        ));
    }
    for component in path.split('/') {
        if component.is_empty()
            || component == "."
            || component == ".."
            || component.ends_with(['.', ' '])
            || component
                .chars()
                .any(|character| character < '\u{20}' || "<>:\"|?*".contains(character))
            || windows_reserved_name(component)
        {
            return Err(CoreError::InvalidTransfer(
                "invalid folder entry path".into(),
            ));
        }
    }
    Ok(())
}

fn canonical_folder_path(path: &str) -> String {
    path.nfc().flat_map(char::to_lowercase).collect()
}

fn folder_ancestors(path: &str) -> impl Iterator<Item = &str> {
    path.match_indices('/').map(|(index, _)| &path[..index])
}

fn windows_reserved_name(component: &str) -> bool {
    let stem = component.split('.').next().unwrap_or(component);
    let uppercase = stem.to_ascii_uppercase();
    matches!(uppercase.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || uppercase
            .strip_prefix("COM")
            .or_else(|| uppercase.strip_prefix("LPT"))
            .is_some_and(|suffix| {
                matches!(suffix, "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9")
            })
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TransferOffer {
    pub protocol: u16,
    pub transfer_id: String,
    pub filename: String,
    pub kind: TransferKind,
    pub size: u64,
    pub modified_ms: i64,
    pub blake3: String,
    pub sender: DeviceSummary,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub signature: String,
}

#[derive(Debug, Serialize)]
struct SignableOffer<'a> {
    protocol: u16,
    transfer_id: &'a str,
    filename: &'a str,
    kind: TransferKind,
    size: u64,
    modified_ms: i64,
    blake3: &'a str,
    sender: &'a DeviceSummary,
}

impl TransferOffer {
    pub fn new(
        transfer_id: Uuid,
        filename: String,
        size: u64,
        modified_ms: i64,
        digest: blake3::Hash,
        sender: DeviceSummary,
    ) -> Self {
        Self {
            protocol: QUIC_PROTOCOL_VERSION,
            transfer_id: transfer_id.hyphenated().to_string(),
            filename,
            kind: TransferKind::File,
            size,
            modified_ms,
            blake3: digest.to_hex().to_string(),
            sender,
            signature: String::new(),
        }
    }

    pub fn signing_bytes(&self) -> Result<Vec<u8>> {
        let signable = SignableOffer {
            protocol: self.protocol,
            transfer_id: &self.transfer_id,
            filename: &self.filename,
            kind: self.kind,
            size: self.size,
            modified_ms: self.modified_ms,
            blake3: &self.blake3,
            sender: &self.sender,
        };
        let encoded = serde_json::to_vec(&signable)?;
        let mut message = Vec::with_capacity(OFFER_SIGNATURE_DOMAIN.len() + encoded.len());
        message.extend_from_slice(OFFER_SIGNATURE_DOMAIN);
        message.extend_from_slice(&encoded);
        Ok(message)
    }

    pub fn validate(&self) -> Result<()> {
        if self.protocol != QUIC_PROTOCOL_VERSION {
            return Err(CoreError::InvalidTransfer(format!(
                "unsupported QUIC protocol version {}",
                self.protocol
            )));
        }
        Uuid::parse_str(&self.transfer_id)
            .map_err(|_| CoreError::InvalidTransfer("invalid transfer_id".into()))?;
        if self.filename.len() > 4096 {
            return Err(CoreError::InvalidTransfer("filename is too long".into()));
        }
        if self.size > MAX_FILE_SIZE {
            return Err(CoreError::InvalidTransfer("file is too large".into()));
        }
        if self.modified_ms < 0 {
            return Err(CoreError::InvalidTransfer(
                "modified_ms cannot be negative".into(),
            ));
        }
        if !valid_blake3(&self.blake3) {
            return Err(CoreError::InvalidTransfer("invalid BLAKE3 digest".into()));
        }
        if !valid_instance_id(&self.sender.instance_id) {
            return Err(CoreError::InvalidTransfer(
                "invalid sender instance id".into(),
            ));
        }
        if self.sender.name.trim().is_empty() || self.sender.name.len() > 255 {
            return Err(CoreError::InvalidTransfer("invalid sender name".into()));
        }

        let public_key: [u8; 32] = BASE64
            .decode(&self.sender.public_key)
            .map_err(|_| CoreError::InvalidTransfer("invalid sender public key".into()))?
            .try_into()
            .map_err(|_| CoreError::InvalidTransfer("invalid sender public key".into()))?;
        let signature: [u8; 64] = BASE64
            .decode(&self.signature)
            .map_err(|_| CoreError::InvalidTransfer("invalid offer signature".into()))?
            .try_into()
            .map_err(|_| CoreError::InvalidTransfer("invalid offer signature".into()))?;
        let verifying_key = VerifyingKey::from_bytes(&public_key)
            .map_err(|_| CoreError::InvalidTransfer("invalid sender public key".into()))?;
        verifying_key
            .verify(&self.signing_bytes()?, &Signature::from_bytes(&signature))
            .map_err(|_| {
                CoreError::InvalidTransfer("offer signature verification failed".into())
            })?;
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum TransferResponse {
    Resume {
        offset: u64,
    },
    FolderResume {
        file_index: u32,
        offset: u64,
        completed: u64,
    },
    Complete {
        receipt: TransferReceipt,
    },
    Rejected {
        reason: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TransferReceipt {
    pub transfer_id: String,
    pub size: u64,
    pub blake3: String,
}

pub fn valid_instance_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub fn safe_filename(raw: &str) -> String {
    let basename = raw.rsplit(['/', '\\']).next().unwrap_or(raw);
    let mut safe = String::with_capacity(basename.len());
    for character in basename.chars() {
        if character < '\u{20}' || "<>:\"|?*".contains(character) {
            safe.push('_');
        } else {
            safe.push(character);
        }
    }
    let trimmed = safe.trim_end_matches(['.', ' ']);
    if trimmed.is_empty() || trimmed == "." || trimmed == ".." {
        "unknown".to_owned()
    } else if windows_reserved_name(trimmed) {
        format!("_{trimmed}")
    } else {
        trimmed.to_owned()
    }
}

pub fn source_filename(path: &Path) -> Result<String> {
    path.file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| CoreError::InvalidTransfer("source path has no UTF-8 filename".into()))
}

pub async fn write_frame<W, T>(writer: &mut W, value: &T) -> Result<()>
where
    W: AsyncWrite + Unpin,
    T: Serialize,
{
    let payload = serde_json::to_vec(value)?;
    if payload.len() > MAX_CONTROL_FRAME {
        return Err(CoreError::Protocol("control frame is too large".into()));
    }
    writer.write_u32(payload.len() as u32).await?;
    writer.write_all(&payload).await?;
    writer.flush().await?;
    Ok(())
}

pub async fn read_frame<R, T>(reader: &mut R) -> Result<T>
where
    R: AsyncRead + Unpin,
    T: DeserializeOwned,
{
    let length = reader.read_u32().await? as usize;
    if length == 0 || length > MAX_CONTROL_FRAME {
        return Err(CoreError::Protocol("invalid control frame length".into()));
    }
    let mut payload = vec![0_u8; length];
    reader.read_exact(&mut payload).await?;
    Ok(serde_json::from_slice(&payload)?)
}

pub async fn write_folder_manifest<W>(writer: &mut W, manifest: &FolderManifest) -> Result<()>
where
    W: AsyncWrite + Unpin,
{
    let payload = serde_json::to_vec(manifest)?;
    if payload.len() > MAX_FOLDER_MANIFEST_FRAME {
        return Err(CoreError::InvalidTransfer(
            "folder manifest is too large".into(),
        ));
    }
    writer.write_u32(payload.len() as u32).await?;
    writer.write_all(&payload).await?;
    writer.flush().await?;
    Ok(())
}

pub async fn read_folder_manifest<R>(reader: &mut R) -> Result<FolderManifest>
where
    R: AsyncRead + Unpin,
{
    let length = reader.read_u32().await? as usize;
    if length == 0 || length > MAX_FOLDER_MANIFEST_FRAME {
        return Err(CoreError::InvalidTransfer(
            "invalid folder manifest length".into(),
        ));
    }
    let mut payload = vec![0_u8; length];
    reader.read_exact(&mut payload).await?;
    let manifest: FolderManifest = serde_json::from_slice(&payload)?;
    manifest.validate()?;
    Ok(manifest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::DeviceIdentity;

    fn signed_probe(identity: &DeviceIdentity, request: &PeerProbeRequest) -> PeerProbeResponse {
        let mut response = PeerProbeResponse::unsigned(
            request,
            identity.summary(),
            identity.certificate_fingerprint().to_owned(),
            vec!["quic-v1".into(), "blake3".into(), "quic-v1".into()],
        )
        .unwrap();
        response.signature = identity.sign_base64(&response.signing_bytes().unwrap());
        response
    }

    #[test]
    fn filename_cleanup_matches_existing_clients() {
        assert_eq!(safe_filename("../../bad:name?.txt "), "bad_name_.txt");
        assert_eq!(safe_filename(".."), "unknown");
        assert_eq!(safe_filename("folder\\photo.jpg"), "photo.jpg");
        // Windows device names are rejected inside folder manifests, so a single file
        // must not be able to smuggle one through either.
        assert_eq!(safe_filename("CON"), "_CON");
        assert_eq!(safe_filename("nul.txt"), "_nul.txt");
        assert_eq!(safe_filename("lpt9.log"), "_lpt9.log");
        assert_eq!(safe_filename("console.log"), "console.log");
    }

    #[test]
    fn authenticated_requests_require_the_secret_and_a_fresh_challenge() {
        let hello = AuthenticationHello::new();
        let challenge = AuthenticationChallenge::new(&hello).unwrap();
        let payload = PeerProbeRequest::new();
        let request = AuthenticatedRequest::new(&challenge, &payload, "room secret").unwrap();

        let decoded = request
            .clone()
            .authenticate(&challenge, "room secret")
            .unwrap();
        assert_eq!(
            serde_json::from_value::<PeerProbeRequest>(decoded).unwrap(),
            payload
        );
        assert!(
            request
                .clone()
                .authenticate(&challenge, "wrong secret")
                .is_err()
        );
        assert!(
            request
                .clone()
                .authenticate(
                    &AuthenticationChallenge::new(&AuthenticationHello::new()).unwrap(),
                    "room secret",
                )
                .is_err()
        );

        let mut tampered = request;
        tampered.payload["nonce"] = Value::String(hex::encode([0_u8; PROBE_NONCE_BYTES]));
        assert!(tampered.authenticate(&challenge, "room secret").is_err());
    }

    #[test]
    fn folder_manifests_reject_traversal_collisions_and_invalid_parents() {
        let digest = blake3::hash(b"payload").to_hex().to_string();
        let file = |path: &str| FolderEntry {
            path: path.into(),
            kind: FolderEntryKind::File,
            size: 7,
            modified_ms: 1,
            blake3: digest.clone(),
        };
        for path in ["../outside", "/absolute", "a\\outside", "CON", "a//b"] {
            assert!(FolderManifest::new(vec![file(path)]).validate().is_err());
        }

        let collision = FolderManifest::new(vec![file("A.txt"), file("a.txt")]);
        assert!(collision.validate().is_err());
        let missing_parent = FolderManifest::new(vec![file("nested/file.txt")]);
        assert!(missing_parent.validate().is_err());
        let file_parent = FolderManifest::new(vec![file("nested"), file("nested/file.txt")]);
        assert!(file_parent.validate().is_err());
    }

    #[test]
    fn folder_manifest_digest_covers_every_entry() {
        let mut manifest = FolderManifest::new(vec![FolderEntry {
            path: "payload.txt".into(),
            kind: FolderEntryKind::File,
            size: 7,
            modified_ms: 1,
            blake3: blake3::hash(b"payload").to_hex().to_string(),
        }]);
        let original = manifest.digest().unwrap();
        manifest.entries[0].modified_ms = 2;
        assert_ne!(manifest.digest().unwrap(), original);
    }

    #[tokio::test]
    async fn bounded_json_frame_round_trip() {
        let (mut left, mut right) = tokio::io::duplex(4096);
        let expected = TransferResponse::Resume { offset: 42 };
        let write = tokio::spawn(async move { write_frame(&mut left, &expected).await });
        let actual: TransferResponse = read_frame(&mut right).await.unwrap();
        write.await.unwrap().unwrap();
        assert_eq!(actual, TransferResponse::Resume { offset: 42 });
    }

    #[test]
    fn signed_peer_probe_rejects_identity_signature_and_nonce_mismatches() {
        let identity = DeviceIdentity::generate(None, "Receiver").unwrap();
        let request = PeerProbeRequest::new();
        let response = signed_probe(&identity, &request);
        response
            .validate(
                &request,
                Some(identity.instance_id()),
                identity.certificate_fingerprint(),
            )
            .unwrap();
        assert_eq!(response.capabilities, vec!["blake3", "quic-v1"]);

        let wrong_identity = DeviceIdentity::generate(None, "Other").unwrap();
        let wrong_identity_response = signed_probe(&wrong_identity, &request);
        assert!(
            wrong_identity_response
                .validate(
                    &request,
                    Some(identity.instance_id()),
                    wrong_identity.certificate_fingerprint(),
                )
                .is_err()
        );

        let mut tampered = response.clone();
        tampered.peer_name = "Tampered".into();
        assert!(
            tampered
                .validate(
                    &request,
                    Some(identity.instance_id()),
                    identity.certificate_fingerprint(),
                )
                .is_err()
        );

        assert!(
            response
                .validate(
                    &PeerProbeRequest::new(),
                    Some(identity.instance_id()),
                    identity.certificate_fingerprint(),
                )
                .is_err()
        );
    }
}
