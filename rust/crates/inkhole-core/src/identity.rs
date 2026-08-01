use std::{path::Path, sync::Arc};

use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64};
use ed25519_dalek::{Signer, SigningKey};
use rcgen::{CertificateParams, DistinguishedName, DnType, KeyPair};
use serde::{Deserialize, Serialize};
use tokio::io::AsyncWriteExt;
use uuid::Uuid;

use crate::{
    CoreError, Result,
    protocol::{DeviceSummary, valid_instance_id},
};

const IDENTITY_FORMAT_VERSION: u16 = 1;
const PRIVATE_IDENTITY_FORMAT_VERSION: u16 = 1;

#[derive(Debug, Serialize, Deserialize)]
struct IdentityDocument {
    version: u16,
    instance_id: String,
    peer_name: String,
    tls_certificate: String,
    tls_private_key: String,
    signing_private_key: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct PrivateIdentityDocument {
    version: u16,
    tls_certificate: String,
    tls_private_key: String,
    signing_private_key: String,
}

#[derive(Clone)]
pub struct DeviceIdentity {
    inner: Arc<IdentityInner>,
}

impl std::fmt::Debug for DeviceIdentity {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("DeviceIdentity")
            .field("instance_id", &self.instance_id())
            .field("peer_name", &self.peer_name())
            .field("certificate_fingerprint", &self.certificate_fingerprint())
            .finish_non_exhaustive()
    }
}

struct IdentityInner {
    instance_id: String,
    peer_name: String,
    tls_certificate: Vec<u8>,
    tls_private_key: Vec<u8>,
    signing_key: SigningKey,
    certificate_fingerprint: String,
}

impl DeviceIdentity {
    pub async fn load_or_create(
        path: impl AsRef<Path>,
        requested_instance_id: Option<&str>,
        peer_name: &str,
    ) -> Result<Self> {
        let path = path.as_ref();
        let peer_name = peer_name.trim();
        if peer_name.is_empty() || peer_name.len() > 255 {
            return Err(CoreError::InvalidIdentity("invalid peer name".into()));
        }
        if let Some(instance_id) = requested_instance_id
            && !valid_instance_id(instance_id)
        {
            return Err(CoreError::InvalidIdentity("invalid instance id".into()));
        }

        match tokio::fs::read(path).await {
            Ok(data) => {
                let identity = Self::from_document(serde_json::from_slice(&data)?)?;
                if let Some(expected) = requested_instance_id
                    && identity.instance_id() != expected
                {
                    return Err(CoreError::InvalidIdentity(
                        "stored identity belongs to another instance id".into(),
                    ));
                }
                return Ok(identity);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }

        let identity = Self::generate(requested_instance_id, peer_name)?;
        identity.persist(path).await?;
        Ok(identity)
    }

    pub fn generate(requested_instance_id: Option<&str>, peer_name: &str) -> Result<Self> {
        let (instance_id, peer_name) = validate_metadata(requested_instance_id, peer_name)?;

        let mut certificate_params = CertificateParams::new(vec!["inkhole.local".to_owned()])
            .map_err(|error| CoreError::Crypto(error.to_string()))?;
        let mut distinguished_name = DistinguishedName::new();
        distinguished_name.push(DnType::CommonName, &peer_name);
        certificate_params.distinguished_name = distinguished_name;
        let tls_key = KeyPair::generate().map_err(|error| CoreError::Crypto(error.to_string()))?;
        let certificate = certificate_params
            .self_signed(&tls_key)
            .map_err(|error| CoreError::Crypto(error.to_string()))?;

        let signing_seed: [u8; 32] = rand::random();
        let signing_key = SigningKey::from_bytes(&signing_seed);
        let tls_certificate = certificate.der().to_vec();
        let certificate_fingerprint = blake3::hash(&tls_certificate).to_hex().to_string();
        Ok(Self {
            inner: Arc::new(IdentityInner {
                instance_id,
                peer_name,
                tls_certificate,
                tls_private_key: tls_key.serialize_der(),
                signing_key,
                certificate_fingerprint,
            }),
        })
    }

    pub fn from_private_export(
        encoded: &str,
        requested_instance_id: Option<&str>,
        peer_name: &str,
    ) -> Result<Self> {
        let (instance_id, peer_name) = validate_metadata(requested_instance_id, peer_name)?;
        let document: PrivateIdentityDocument = serde_json::from_slice(
            &BASE64
                .decode(encoded.trim())
                .map_err(|_| CoreError::InvalidIdentity("invalid private identity".into()))?,
        )?;
        if document.version != PRIVATE_IDENTITY_FORMAT_VERSION {
            return Err(CoreError::InvalidIdentity(
                "unsupported private identity version".into(),
            ));
        }
        Self::from_key_material(
            instance_id,
            peer_name,
            document.tls_certificate,
            document.tls_private_key,
            document.signing_private_key,
        )
    }

    pub fn export_private(&self) -> Result<String> {
        let document = PrivateIdentityDocument {
            version: PRIVATE_IDENTITY_FORMAT_VERSION,
            tls_certificate: BASE64.encode(self.tls_certificate_der()),
            tls_private_key: BASE64.encode(self.tls_private_key_der()),
            signing_private_key: BASE64.encode(self.inner.signing_key.to_bytes()),
        };
        Ok(BASE64.encode(serde_json::to_vec(&document)?))
    }

    fn from_document(document: IdentityDocument) -> Result<Self> {
        if document.version != IDENTITY_FORMAT_VERSION
            || !valid_instance_id(&document.instance_id)
            || document.peer_name.trim().is_empty()
        {
            return Err(CoreError::InvalidIdentity(
                "invalid identity document".into(),
            ));
        }
        Self::from_key_material(
            document.instance_id,
            document.peer_name,
            document.tls_certificate,
            document.tls_private_key,
            document.signing_private_key,
        )
    }

    fn from_key_material(
        instance_id: String,
        peer_name: String,
        tls_certificate: String,
        tls_private_key: String,
        signing_private_key: String,
    ) -> Result<Self> {
        let tls_certificate = decode_nonempty(&tls_certificate, "TLS certificate")?;
        let tls_private_key = decode_nonempty(&tls_private_key, "TLS private key")?;
        let signing_seed: [u8; 32] = BASE64
            .decode(signing_private_key)
            .map_err(|_| CoreError::InvalidIdentity("invalid signing private key".into()))?
            .try_into()
            .map_err(|_| CoreError::InvalidIdentity("invalid signing private key".into()))?;
        let certificate_fingerprint = blake3::hash(&tls_certificate).to_hex().to_string();
        Ok(Self {
            inner: Arc::new(IdentityInner {
                instance_id,
                peer_name,
                tls_certificate,
                tls_private_key,
                signing_key: SigningKey::from_bytes(&signing_seed),
                certificate_fingerprint,
            }),
        })
    }

    async fn persist(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            tokio::fs::create_dir_all(parent).await?;
        }
        let document = IdentityDocument {
            version: IDENTITY_FORMAT_VERSION,
            instance_id: self.instance_id().to_owned(),
            peer_name: self.peer_name().to_owned(),
            tls_certificate: BASE64.encode(self.tls_certificate_der()),
            tls_private_key: BASE64.encode(self.tls_private_key_der()),
            signing_private_key: BASE64.encode(self.inner.signing_key.to_bytes()),
        };
        let data = serde_json::to_vec_pretty(&document)?;
        let temp_path = path.with_extension(format!("tmp-{}", Uuid::new_v4().simple()));
        let mut options = tokio::fs::OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            options.mode(0o600);
        }
        let mut file = options.open(&temp_path).await?;
        file.write_all(&data).await?;
        file.sync_all().await?;
        drop(file);
        match tokio::fs::rename(&temp_path, path).await {
            Ok(()) => Ok(()),
            Err(error) => {
                let _ = tokio::fs::remove_file(&temp_path).await;
                Err(error.into())
            }
        }
    }

    pub fn instance_id(&self) -> &str {
        &self.inner.instance_id
    }

    pub fn peer_name(&self) -> &str {
        &self.inner.peer_name
    }

    pub fn certificate_fingerprint(&self) -> &str {
        &self.inner.certificate_fingerprint
    }

    pub fn tls_certificate_der(&self) -> &[u8] {
        &self.inner.tls_certificate
    }

    pub fn tls_private_key_der(&self) -> &[u8] {
        &self.inner.tls_private_key
    }

    pub fn public_key_base64(&self) -> String {
        BASE64.encode(self.inner.signing_key.verifying_key().to_bytes())
    }

    pub fn summary(&self) -> DeviceSummary {
        DeviceSummary {
            instance_id: self.instance_id().to_owned(),
            name: self.peer_name().to_owned(),
            public_key: self.public_key_base64(),
        }
    }

    pub fn sign_base64(&self, message: &[u8]) -> String {
        BASE64.encode(self.inner.signing_key.sign(message).to_bytes())
    }
}

fn validate_metadata(
    requested_instance_id: Option<&str>,
    peer_name: &str,
) -> Result<(String, String)> {
    let instance_id = requested_instance_id
        .map(str::to_owned)
        .unwrap_or_else(|| Uuid::new_v4().simple().to_string());
    if !valid_instance_id(&instance_id) {
        return Err(CoreError::InvalidIdentity("invalid instance id".into()));
    }
    let peer_name = peer_name.trim();
    if peer_name.is_empty() || peer_name.len() > 255 {
        return Err(CoreError::InvalidIdentity("invalid peer name".into()));
    }
    Ok((instance_id, peer_name.to_owned()))
}

fn decode_nonempty(encoded: &str, label: &str) -> Result<Vec<u8>> {
    let decoded = BASE64
        .decode(encoded)
        .map_err(|_| CoreError::InvalidIdentity(format!("invalid {label}")))?;
    if decoded.is_empty() {
        return Err(CoreError::InvalidIdentity(format!("empty {label}")));
    }
    Ok(decoded)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn identity_survives_restart() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("identity.json");
        let first = DeviceIdentity::load_or_create(&path, None, "Desktop")
            .await
            .unwrap();
        let second =
            DeviceIdentity::load_or_create(&path, Some(first.instance_id()), "Ignored on reload")
                .await
                .unwrap();
        assert_eq!(first.instance_id(), second.instance_id());
        assert_eq!(
            first.certificate_fingerprint(),
            second.certificate_fingerprint()
        );
        assert_eq!(first.public_key_base64(), second.public_key_base64());
    }

    #[tokio::test]
    async fn rejects_identity_instance_mismatch() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("identity.json");
        DeviceIdentity::load_or_create(&path, Some("0123456789abcdef0123456789abcdef"), "Desktop")
            .await
            .unwrap();
        let error = DeviceIdentity::load_or_create(
            &path,
            Some("fedcba9876543210fedcba9876543210"),
            "Desktop",
        )
        .await
        .unwrap_err();
        assert!(error.to_string().contains("another instance"));
    }

    #[test]
    fn private_export_keeps_keys_when_device_metadata_changes() {
        let first =
            DeviceIdentity::generate(Some("0123456789abcdef0123456789abcdef"), "Old name").unwrap();
        let exported = first.export_private().unwrap();
        let imported = DeviceIdentity::from_private_export(
            &exported,
            Some("fedcba9876543210fedcba9876543210"),
            "New name",
        )
        .unwrap();

        assert_eq!(
            first.certificate_fingerprint(),
            imported.certificate_fingerprint()
        );
        assert_eq!(first.public_key_base64(), imported.public_key_base64());
        assert_eq!(imported.instance_id(), "fedcba9876543210fedcba9876543210");
        assert_eq!(imported.peer_name(), "New name");
    }
}
