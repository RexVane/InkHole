use std::path::Path;

use tokio::io::AsyncReadExt;
use tokio_util::sync::CancellationToken;

use crate::{CoreError, Result};

pub async fn blake3_file(path: impl AsRef<Path>) -> Result<blake3::Hash> {
    blake3_file_inner(path.as_ref(), None).await
}

pub async fn blake3_file_cancellable(
    path: impl AsRef<Path>,
    cancellation: &CancellationToken,
) -> Result<blake3::Hash> {
    blake3_file_inner(path.as_ref(), Some(cancellation)).await
}

async fn blake3_file_inner(
    path: &Path,
    cancellation: Option<&CancellationToken>,
) -> Result<blake3::Hash> {
    let mut file = tokio::fs::File::open(path).await?;
    let mut hasher = blake3::Hasher::new();
    let mut buffer = vec![0_u8; 256 * 1024];
    loop {
        let read = if let Some(cancellation) = cancellation {
            tokio::select! {
                _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
                result = file.read(&mut buffer) => result?,
            }
        } else {
            file.read(&mut buffer).await?
        };
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hasher.finalize())
}

pub async fn blake3_file_hex(path: impl AsRef<Path>) -> Result<String> {
    Ok(blake3_file(path).await?.to_hex().to_string())
}

pub fn valid_blake3(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_lowercase_blake3_hex() {
        assert!(valid_blake3(&"a".repeat(64)));
        assert!(!valid_blake3(&"A".repeat(64)));
        assert!(!valid_blake3(&"a".repeat(63)));
    }
}
