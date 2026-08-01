use std::path::PathBuf;

#[derive(Debug, thiserror::Error)]
pub enum CoreError {
    #[error("invalid request: {0}")]
    InvalidRequest(String),
    #[error("invalid identity: {0}")]
    InvalidIdentity(String),
    #[error("invalid transfer: {0}")]
    InvalidTransfer(String),
    #[error("peer certificate fingerprint mismatch")]
    FingerprintMismatch,
    #[error("transfer was cancelled")]
    Cancelled,
    #[error("transfer is already active: {0}")]
    TransferBusy(String),
    #[error("BLAKE3 mismatch for {path}")]
    DigestMismatch { path: PathBuf },
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("QUIC connection error: {0}")]
    QuinnConnection(#[from] quinn::ConnectionError),
    #[error("QUIC connect error: {0}")]
    QuinnConnect(#[from] quinn::ConnectError),
    #[error("QUIC write error: {0}")]
    QuinnWrite(#[from] quinn::WriteError),
    #[error("QUIC read error: {0}")]
    QuinnRead(#[from] quinn::ReadError),
    #[error("QUIC configuration error: {0}")]
    QuinnConfig(String),
    #[error("cryptographic error: {0}")]
    Crypto(String),
    #[error("protocol error: {0}")]
    Protocol(String),
}

pub type Result<T> = std::result::Result<T, CoreError>;
