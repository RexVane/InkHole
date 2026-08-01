use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use tokio_util::sync::CancellationToken;

use crate::{
    CoreError, Result,
    protocol::{
        FolderEntry, FolderEntryKind, FolderManifest, MAX_FOLDER_ENTRIES,
        validate_folder_relative_path,
    },
};

const HASH_BUFFER_SIZE: usize = 1024 * 1024;

pub(crate) struct FolderSource {
    pub manifest: FolderManifest,
    pub files: Vec<PathBuf>,
}

struct ScannedEntry {
    wire: FolderEntry,
    source: Option<PathBuf>,
}

pub(crate) async fn scan_folder(
    root: impl AsRef<Path>,
    cancellation: &CancellationToken,
) -> Result<FolderSource> {
    let root = root.as_ref().to_path_buf();
    let cancellation = cancellation.clone();
    tokio::task::spawn_blocking(move || scan_folder_blocking(&root, &cancellation))
        .await
        .map_err(|error| CoreError::Protocol(format!("folder scan task failed: {error}")))?
}

fn scan_folder_blocking(root: &Path, cancellation: &CancellationToken) -> Result<FolderSource> {
    check_cancelled(cancellation)?;
    let root_metadata = fs::symlink_metadata(root)?;
    if root_metadata.file_type().is_symlink() || !root_metadata.is_dir() {
        return Err(CoreError::InvalidTransfer(
            "source path is not a regular directory".into(),
        ));
    }

    let mut scanned = Vec::new();
    let mut directories = vec![(root.to_path_buf(), String::new())];
    while let Some((directory, relative_parent)) = directories.pop() {
        check_cancelled(cancellation)?;
        for entry in fs::read_dir(&directory)? {
            check_cancelled(cancellation)?;
            let entry = entry?;
            let name = entry.file_name().into_string().map_err(|_| {
                CoreError::InvalidTransfer("folder contains a non-UTF-8 filename".into())
            })?;
            let relative = if relative_parent.is_empty() {
                name
            } else {
                format!("{relative_parent}/{name}")
            };
            validate_folder_relative_path(&relative)?;
            let source = entry.path();
            let metadata = fs::symlink_metadata(&source)?;
            let file_type = metadata.file_type();
            if file_type.is_symlink() {
                return Err(CoreError::InvalidTransfer(format!(
                    "folder contains a symbolic link: {relative}"
                )));
            }
            if file_type.is_dir() {
                scanned.push(ScannedEntry {
                    wire: FolderEntry {
                        path: relative.clone(),
                        kind: FolderEntryKind::Directory,
                        size: 0,
                        modified_ms: modified_ms(&metadata),
                        blake3: String::new(),
                    },
                    source: None,
                });
                directories.push((source, relative));
            } else if file_type.is_file() {
                let (digest, bytes_read) = hash_file(&source, cancellation)?;
                let current_metadata = fs::symlink_metadata(&source)?;
                if current_metadata.file_type().is_symlink()
                    || !current_metadata.is_file()
                    || current_metadata.len() != metadata.len()
                    || bytes_read != metadata.len()
                {
                    return Err(CoreError::InvalidTransfer(format!(
                        "source file changed while scanning: {relative}"
                    )));
                }
                scanned.push(ScannedEntry {
                    wire: FolderEntry {
                        path: relative,
                        kind: FolderEntryKind::File,
                        size: metadata.len(),
                        modified_ms: modified_ms(&metadata),
                        blake3: digest.to_hex().to_string(),
                    },
                    source: Some(source),
                });
            } else {
                return Err(CoreError::InvalidTransfer(format!(
                    "folder contains a special file: {relative}"
                )));
            }
            if scanned.len() > MAX_FOLDER_ENTRIES {
                return Err(CoreError::InvalidTransfer(
                    "folder contains too many entries".into(),
                ));
            }
        }
    }

    scanned.sort_unstable_by(|left, right| left.wire.path.cmp(&right.wire.path));
    let mut entries = Vec::with_capacity(scanned.len());
    let mut files = Vec::new();
    for entry in scanned {
        entries.push(entry.wire);
        if let Some(source) = entry.source {
            files.push(source);
        }
    }
    let manifest = FolderManifest::new(entries);
    manifest.validate()?;
    Ok(FolderSource { manifest, files })
}

fn hash_file(path: &Path, cancellation: &CancellationToken) -> Result<(blake3::Hash, u64)> {
    let mut file = fs::File::open(path)?;
    let mut hasher = blake3::Hasher::new();
    let mut buffer = vec![0_u8; HASH_BUFFER_SIZE];
    let mut total = 0_u64;
    loop {
        check_cancelled(cancellation)?;
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        total = total
            .checked_add(read as u64)
            .ok_or_else(|| CoreError::InvalidTransfer("folder size overflow".into()))?;
    }
    Ok((hasher.finalize(), total))
}

fn check_cancelled(cancellation: &CancellationToken) -> Result<()> {
    if cancellation.is_cancelled() {
        Err(CoreError::Cancelled)
    } else {
        Ok(())
    }
}

fn modified_ms(metadata: &fs::Metadata) -> i64 {
    metadata
        .modified()
        .unwrap_or(SystemTime::now())
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(i64::MAX as u128) as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn scans_nested_files_and_empty_directories_deterministically() {
        let root = tempfile::tempdir().unwrap();
        let source = root.path().join("folder");
        tokio::fs::create_dir_all(source.join("empty"))
            .await
            .unwrap();
        tokio::fs::create_dir_all(source.join("nested"))
            .await
            .unwrap();
        tokio::fs::write(source.join("z.txt"), b"z").await.unwrap();
        tokio::fs::write(source.join("nested/a.txt"), b"alpha")
            .await
            .unwrap();

        let scanned = scan_folder(&source, &CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(
            scanned
                .manifest
                .entries
                .iter()
                .map(|entry| entry.path.as_str())
                .collect::<Vec<_>>(),
            ["empty", "nested", "nested/a.txt", "z.txt"]
        );
        assert_eq!(scanned.files.len(), 2);
        assert_eq!(scanned.manifest.validate().unwrap(), 6);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn rejects_symbolic_links() {
        use std::os::unix::fs::symlink;

        let root = tempfile::tempdir().unwrap();
        let source = root.path().join("folder");
        tokio::fs::create_dir(&source).await.unwrap();
        tokio::fs::write(root.path().join("outside"), b"outside")
            .await
            .unwrap();
        symlink(root.path().join("outside"), source.join("link")).unwrap();
        assert!(
            scan_folder(&source, &CancellationToken::new())
                .await
                .is_err()
        );
    }
}
