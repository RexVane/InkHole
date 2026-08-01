use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Arc, Weak},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize, de::DeserializeOwned};
use tokio::{
    io::{AsyncSeekExt, AsyncWriteExt},
    sync::{Mutex, OwnedMutexGuard},
};
use uuid::Uuid;

use crate::{
    CoreError, Result,
    hash::blake3_file_hex,
    inbox::InboxCategoryRoots,
    protocol::{
        FolderEntry, FolderEntryKind, FolderManifest, TransferKind, TransferOffer, TransferReceipt,
        safe_filename, validate_folder_relative_path,
    },
};

const STATE_DIRECTORY: &str = ".inkhole-transfers";
/// Resume state that nothing has touched for this long is abandoned: without it the
/// `.part`/`.json` sidecars accumulate forever in the inbox.
const STATE_RETENTION: Duration = Duration::from_secs(30 * 24 * 60 * 60);

#[derive(Clone)]
pub(crate) struct TransferStore {
    inner: Arc<StoreInner>,
}

struct StoreInner {
    inbox: PathBuf,
    category_roots: InboxCategoryRoots,
    transfer_locks: Mutex<HashMap<String, Weak<Mutex<()>>>>,
    commit_lock: Mutex<()>,
}

#[derive(Debug)]
pub(crate) struct PreparedTransfer {
    pub part_path: PathBuf,
    pub offset: u64,
}

#[derive(Debug)]
pub(crate) struct PreparedFolderTransfer {
    pub staging_path: PathBuf,
    pub file_index: usize,
    pub offset: u64,
    pub completed: u64,
}

#[derive(Debug)]
pub(crate) enum PrepareOutcome {
    Resume(PreparedTransfer),
    Complete(CompletedTransfer),
}

#[derive(Debug)]
pub(crate) enum PrepareFolderOutcome {
    Resume(PreparedFolderTransfer),
    Complete(CompletedTransfer),
}

#[derive(Debug, Clone)]
pub(crate) struct CompletedTransfer {
    pub receipt: TransferReceipt,
    pub destination: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct StoredOffer {
    transfer_id: String,
    filename: String,
    kind: TransferKind,
    size: u64,
    modified_ms: i64,
    blake3: String,
    sender_instance_id: String,
    sender_public_key: String,
}

impl From<&TransferOffer> for StoredOffer {
    fn from(offer: &TransferOffer) -> Self {
        Self {
            transfer_id: offer.transfer_id.clone(),
            filename: safe_filename(&offer.filename),
            kind: offer.kind,
            size: offer.size,
            modified_ms: offer.modified_ms,
            blake3: offer.blake3.clone(),
            sender_instance_id: offer.sender.instance_id.clone(),
            sender_public_key: offer.sender.public_key.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct CommitRecord {
    offer: StoredOffer,
    destination: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    manifest: Option<FolderManifest>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct DoneRecord {
    offer: StoredOffer,
    destination: String,
    receipt: TransferReceipt,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    manifest: Option<FolderManifest>,
}

struct StatePaths {
    part: PathBuf,
    folder_part: PathBuf,
    metadata: PathBuf,
    manifest: PathBuf,
    commit: PathBuf,
    done: PathBuf,
}

impl TransferStore {
    #[cfg(test)]
    pub async fn new(inbox: impl AsRef<Path>) -> Result<Self> {
        Self::with_category_roots(inbox, InboxCategoryRoots::default()).await
    }

    pub async fn with_category_roots(
        inbox: impl AsRef<Path>,
        category_roots: InboxCategoryRoots,
    ) -> Result<Self> {
        let inbox = inbox.as_ref().to_path_buf();
        for root in category_roots.all_roots(&inbox) {
            let state_directory = root.join(STATE_DIRECTORY);
            tokio::fs::create_dir_all(&state_directory).await?;
            prune_stale_state(&state_directory).await;
        }
        Ok(Self {
            inner: Arc::new(StoreInner {
                inbox,
                category_roots,
                transfer_locks: Mutex::new(HashMap::new()),
                commit_lock: Mutex::new(()),
            }),
        })
    }

    pub async fn lock_transfer(&self, transfer_id: &str) -> OwnedMutexGuard<()> {
        let lock = {
            let mut locks = self.inner.transfer_locks.lock().await;
            locks.retain(|_, lock| lock.strong_count() > 0);
            if let Some(lock) = locks.get(transfer_id).and_then(Weak::upgrade) {
                lock
            } else {
                let lock = Arc::new(Mutex::new(()));
                locks.insert(transfer_id.to_owned(), Arc::downgrade(&lock));
                lock
            }
        };
        lock.lock_owned().await
    }

    pub async fn prepare(&self, offer: &TransferOffer) -> Result<PrepareOutcome> {
        offer.validate()?;
        if offer.kind != TransferKind::File {
            return Err(CoreError::InvalidTransfer(
                "folder transfers are not enabled in this protocol revision".into(),
            ));
        }
        let expected = StoredOffer::from(offer);
        let paths = self.paths(offer);

        if let Some(done) = read_json_optional::<DoneRecord>(&paths.done).await? {
            ensure_same_offer(&expected, &done.offer)?;
            if let Some(completed) = self.validate_completed(done).await? {
                return Ok(PrepareOutcome::Complete(completed));
            }
            remove_if_exists(&paths.done).await?;
        }

        if let Some(commit) = read_json_optional::<CommitRecord>(&paths.commit).await? {
            ensure_same_offer(&expected, &commit.offer)?;
            if let Some(completed) = self.recover_commit(commit, &paths).await? {
                return Ok(PrepareOutcome::Complete(completed));
            }
        }

        match read_json_optional::<StoredOffer>(&paths.metadata).await? {
            Some(stored) => ensure_same_offer(&expected, &stored)?,
            None => {
                if tokio::fs::try_exists(&paths.part).await? {
                    remove_if_exists(&paths.part).await?;
                }
                atomic_write_json(&paths.metadata, &expected).await?;
            }
        }

        let offset = match tokio::fs::metadata(&paths.part).await {
            Ok(metadata) => metadata.len(),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                tokio::fs::File::create(&paths.part)
                    .await?
                    .sync_all()
                    .await?;
                0
            }
            Err(error) => return Err(error.into()),
        };
        if offset > offer.size {
            return Err(CoreError::InvalidTransfer(
                "partial transfer is larger than the offer".into(),
            ));
        }
        Ok(PrepareOutcome::Resume(PreparedTransfer {
            part_path: paths.part,
            offset,
        }))
    }

    pub async fn prepare_folder(
        &self,
        offer: &TransferOffer,
        manifest: &FolderManifest,
    ) -> Result<PrepareFolderOutcome> {
        offer.validate()?;
        manifest.validate_offer(offer)?;
        let expected = StoredOffer::from(offer);
        let paths = self.paths(offer);

        if let Some(done) = read_json_optional::<DoneRecord>(&paths.done).await? {
            ensure_same_offer(&expected, &done.offer)?;
            ensure_same_manifest(manifest, done.manifest.as_ref())?;
            if let Some(completed) = self.validate_completed_folder(done).await? {
                return Ok(PrepareFolderOutcome::Complete(completed));
            }
            remove_if_exists(&paths.done).await?;
        }

        if let Some(commit) = read_json_optional::<CommitRecord>(&paths.commit).await? {
            ensure_same_offer(&expected, &commit.offer)?;
            ensure_same_manifest(manifest, commit.manifest.as_ref())?;
            if let Some(completed) = self.recover_folder_commit(commit, &paths).await? {
                return Ok(PrepareFolderOutcome::Complete(completed));
            }
        }

        match read_json_optional::<StoredOffer>(&paths.metadata).await? {
            Some(stored) => {
                ensure_same_offer(&expected, &stored)?;
                let stored_manifest = read_json_optional::<FolderManifest>(&paths.manifest)
                    .await?
                    .ok_or_else(|| {
                        CoreError::InvalidTransfer("folder transfer manifest is missing".into())
                    })?;
                ensure_same_manifest(manifest, Some(&stored_manifest))?;
            }
            None => {
                remove_dir_all_if_exists(&paths.folder_part).await?;
                remove_if_exists(&paths.manifest).await?;
                atomic_write_json(&paths.metadata, &expected).await?;
                atomic_write_json(&paths.manifest, manifest).await?;
            }
        }
        ensure_directory(&paths.folder_part).await?;

        let mut completed = 0_u64;
        for (file_index, entry) in manifest
            .entries
            .iter()
            .filter(|entry| entry.kind == FolderEntryKind::File)
            .enumerate()
        {
            let path = folder_entry_path(&paths.folder_part, &entry.path)?;
            ensure_safe_parent_directories(&paths.folder_part, &entry.path).await?;
            let metadata = match tokio::fs::symlink_metadata(&path).await {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    return Ok(PrepareFolderOutcome::Resume(PreparedFolderTransfer {
                        staging_path: paths.folder_part,
                        file_index,
                        offset: 0,
                        completed,
                    }));
                }
                Err(error) => return Err(error.into()),
            };
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return Err(CoreError::InvalidTransfer(
                    "folder checkpoint contains a non-regular file".into(),
                ));
            }
            if metadata.len() > entry.size {
                return Err(CoreError::InvalidTransfer(
                    "folder checkpoint file is larger than declared".into(),
                ));
            }
            if metadata.len() < entry.size {
                return Ok(PrepareFolderOutcome::Resume(PreparedFolderTransfer {
                    staging_path: paths.folder_part,
                    file_index,
                    offset: metadata.len(),
                    completed,
                }));
            }
            if blake3_file_hex(&path).await? != entry.blake3 {
                remove_if_exists(&path).await?;
                return Ok(PrepareFolderOutcome::Resume(PreparedFolderTransfer {
                    staging_path: paths.folder_part,
                    file_index,
                    offset: 0,
                    completed,
                }));
            }
            completed = completed.checked_add(entry.size).ok_or_else(|| {
                CoreError::InvalidTransfer("folder checkpoint size overflow".into())
            })?;
        }
        Ok(PrepareFolderOutcome::Resume(PreparedFolderTransfer {
            staging_path: paths.folder_part,
            file_index: manifest
                .entries
                .iter()
                .filter(|entry| entry.kind == FolderEntryKind::File)
                .count(),
            offset: 0,
            completed,
        }))
    }

    pub async fn open_folder_file(
        &self,
        prepared: &PreparedFolderTransfer,
        entry: &FolderEntry,
        offset: u64,
    ) -> Result<tokio::fs::File> {
        if entry.kind != FolderEntryKind::File || offset > entry.size {
            return Err(CoreError::InvalidTransfer(
                "invalid folder file checkpoint".into(),
            ));
        }
        let path = folder_entry_path(&prepared.staging_path, &entry.path)?;
        ensure_safe_parent_directories(&prepared.staging_path, &entry.path).await?;
        match tokio::fs::symlink_metadata(&path).await {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
                return Err(CoreError::InvalidTransfer(
                    "folder checkpoint path is not a regular file".into(),
                ));
            }
            Ok(metadata) if offset > 0 && metadata.len() != offset => {
                return Err(CoreError::InvalidTransfer(
                    "folder checkpoint offset changed".into(),
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound && offset == 0 => {}
            Err(error) => return Err(error.into()),
        }
        let mut file = tokio::fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(offset == 0)
            .open(&path)
            .await?;
        file.seek(std::io::SeekFrom::Start(offset)).await?;
        Ok(file)
    }

    pub async fn verify_folder_file(
        &self,
        prepared: &PreparedFolderTransfer,
        entry: &FolderEntry,
    ) -> Result<()> {
        let path = folder_entry_path(&prepared.staging_path, &entry.path)?;
        ensure_safe_parent_directories(&prepared.staging_path, &entry.path).await?;
        let metadata = tokio::fs::symlink_metadata(&path).await?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.len() != entry.size
            || blake3_file_hex(&path).await? != entry.blake3
        {
            remove_if_exists(&path).await?;
            return Err(CoreError::DigestMismatch { path });
        }
        apply_modified_time(&path, entry.modified_ms)?;
        Ok(())
    }

    pub async fn finalize_folder(
        &self,
        offer: &TransferOffer,
        manifest: &FolderManifest,
        prepared: &PreparedFolderTransfer,
    ) -> Result<CompletedTransfer> {
        manifest.validate_offer(offer)?;
        create_manifest_directories(&prepared.staging_path, manifest).await?;
        validate_folder_contents(&prepared.staging_path, manifest).await?;
        apply_manifest_times(&prepared.staging_path, manifest)?;

        let _commit_guard = self.inner.commit_lock.lock().await;
        let paths = self.paths(offer);
        let stored_offer = StoredOffer::from(offer);
        let commit = match read_json_optional::<CommitRecord>(&paths.commit).await? {
            Some(record) => {
                ensure_same_offer(&stored_offer, &record.offer)?;
                ensure_same_manifest(manifest, record.manifest.as_ref())?;
                validate_destination_name(&record.destination)?;
                record
            }
            None => {
                let root = self.destination_root(offer);
                let destination = self
                    .unique_destination(root, &stored_offer.filename)
                    .await?;
                let record = CommitRecord {
                    offer: stored_offer.clone(),
                    destination,
                    manifest: Some(manifest.clone()),
                };
                atomic_write_json(&paths.commit, &record).await?;
                record
            }
        };
        let destination = self.destination_root(offer).join(&commit.destination);
        if !tokio::fs::try_exists(&destination).await? {
            tokio::fs::rename(&prepared.staging_path, &destination).await?;
        }
        validate_folder_contents(&destination, manifest).await?;
        apply_modified_time(&destination, offer.modified_ms)?;

        let receipt = TransferReceipt {
            transfer_id: offer.transfer_id.clone(),
            size: offer.size,
            blake3: offer.blake3.clone(),
        };
        let done = DoneRecord {
            offer: stored_offer,
            destination: commit.destination,
            receipt: receipt.clone(),
            manifest: Some(manifest.clone()),
        };
        atomic_write_json(&paths.done, &done).await?;
        remove_if_exists(&paths.metadata).await?;
        remove_if_exists(&paths.manifest).await?;
        remove_if_exists(&paths.commit).await?;
        Ok(CompletedTransfer {
            receipt,
            destination,
        })
    }

    pub async fn finalize(&self, offer: &TransferOffer) -> Result<CompletedTransfer> {
        let paths = self.paths(offer);
        let metadata = tokio::fs::metadata(&paths.part).await?;
        if metadata.len() != offer.size {
            return Err(CoreError::InvalidTransfer(format!(
                "received {} of {} bytes",
                metadata.len(),
                offer.size
            )));
        }
        let actual_digest = blake3_file_hex(&paths.part).await?;
        if actual_digest != offer.blake3 {
            remove_if_exists(&paths.part).await?;
            remove_if_exists(&paths.metadata).await?;
            remove_if_exists(&paths.commit).await?;
            return Err(CoreError::DigestMismatch {
                path: paths.part.clone(),
            });
        }

        let _commit_guard = self.inner.commit_lock.lock().await;
        let stored_offer = StoredOffer::from(offer);
        let commit = match read_json_optional::<CommitRecord>(&paths.commit).await? {
            Some(record) => {
                ensure_same_offer(&stored_offer, &record.offer)?;
                validate_destination_name(&record.destination)?;
                record
            }
            None => {
                let root = self.destination_root(offer);
                let destination = self
                    .unique_destination(root, &stored_offer.filename)
                    .await?;
                let record = CommitRecord {
                    offer: stored_offer.clone(),
                    destination,
                    manifest: None,
                };
                atomic_write_json(&paths.commit, &record).await?;
                record
            }
        };
        let destination = self.destination_root(offer).join(&commit.destination);
        if !tokio::fs::try_exists(&destination).await? {
            tokio::fs::rename(&paths.part, &destination).await?;
        }
        ensure_file_destination(&destination, offer.size, &offer.blake3).await?;
        apply_modified_time(&destination, offer.modified_ms)?;

        let receipt = TransferReceipt {
            transfer_id: offer.transfer_id.clone(),
            size: offer.size,
            blake3: offer.blake3.clone(),
        };
        let done = DoneRecord {
            offer: stored_offer,
            destination: commit.destination,
            receipt: receipt.clone(),
            manifest: None,
        };
        atomic_write_json(&paths.done, &done).await?;
        remove_if_exists(&paths.metadata).await?;
        remove_if_exists(&paths.commit).await?;
        Ok(CompletedTransfer {
            receipt,
            destination,
        })
    }

    async fn recover_commit(
        &self,
        commit: CommitRecord,
        paths: &StatePaths,
    ) -> Result<Option<CompletedTransfer>> {
        validate_destination_name(&commit.destination)?;
        let destination = self
            .destination_root_stored(&commit.offer)
            .join(&commit.destination);
        if !tokio::fs::try_exists(&destination).await? {
            return Ok(None);
        }
        ensure_file_destination(&destination, commit.offer.size, &commit.offer.blake3).await?;
        let receipt = TransferReceipt {
            transfer_id: commit.offer.transfer_id.clone(),
            size: commit.offer.size,
            blake3: commit.offer.blake3.clone(),
        };
        let done = DoneRecord {
            offer: commit.offer,
            destination: commit.destination,
            receipt: receipt.clone(),
            manifest: None,
        };
        atomic_write_json(&paths.done, &done).await?;
        remove_if_exists(&paths.part).await?;
        remove_if_exists(&paths.metadata).await?;
        remove_if_exists(&paths.commit).await?;
        Ok(Some(CompletedTransfer {
            receipt,
            destination,
        }))
    }

    async fn validate_completed(&self, done: DoneRecord) -> Result<Option<CompletedTransfer>> {
        validate_destination_name(&done.destination)?;
        let destination = self
            .destination_root_stored(&done.offer)
            .join(&done.destination);
        if !tokio::fs::try_exists(&destination).await? {
            return Ok(None);
        }
        ensure_file_destination(&destination, done.receipt.size, &done.receipt.blake3).await?;
        Ok(Some(CompletedTransfer {
            receipt: done.receipt,
            destination,
        }))
    }

    async fn recover_folder_commit(
        &self,
        commit: CommitRecord,
        paths: &StatePaths,
    ) -> Result<Option<CompletedTransfer>> {
        validate_destination_name(&commit.destination)?;
        let manifest = commit.manifest.clone().ok_or_else(|| {
            CoreError::InvalidTransfer("folder commit manifest is missing".into())
        })?;
        let destination = self
            .destination_root_stored(&commit.offer)
            .join(&commit.destination);
        if !tokio::fs::try_exists(&destination).await? {
            return Ok(None);
        }
        validate_folder_contents(&destination, &manifest).await?;
        let receipt = TransferReceipt {
            transfer_id: commit.offer.transfer_id.clone(),
            size: commit.offer.size,
            blake3: commit.offer.blake3.clone(),
        };
        let done = DoneRecord {
            offer: commit.offer,
            destination: commit.destination,
            receipt: receipt.clone(),
            manifest: Some(manifest),
        };
        atomic_write_json(&paths.done, &done).await?;
        remove_dir_all_if_exists(&paths.folder_part).await?;
        remove_if_exists(&paths.metadata).await?;
        remove_if_exists(&paths.manifest).await?;
        remove_if_exists(&paths.commit).await?;
        Ok(Some(CompletedTransfer {
            receipt,
            destination,
        }))
    }

    async fn validate_completed_folder(
        &self,
        done: DoneRecord,
    ) -> Result<Option<CompletedTransfer>> {
        validate_destination_name(&done.destination)?;
        let manifest = done.manifest.as_ref().ok_or_else(|| {
            CoreError::InvalidTransfer("completed folder manifest is missing".into())
        })?;
        let destination = self
            .destination_root_stored(&done.offer)
            .join(&done.destination);
        if !tokio::fs::try_exists(&destination).await? {
            return Ok(None);
        }
        validate_folder_contents(&destination, manifest).await?;
        Ok(Some(CompletedTransfer {
            receipt: done.receipt,
            destination,
        }))
    }

    async fn unique_destination(&self, root: &Path, filename: &str) -> Result<String> {
        if !tokio::fs::try_exists(root.join(filename)).await? {
            return Ok(filename.to_owned());
        }
        let (stem, extension) = split_extension(filename);
        for index in 2_u32..=u32::MAX {
            let candidate = format!("{stem} ({index}){extension}");
            if !tokio::fs::try_exists(root.join(&candidate)).await? {
                return Ok(candidate);
            }
        }
        Err(CoreError::InvalidTransfer(
            "cannot allocate a destination filename".into(),
        ))
    }

    fn destination_root(&self, offer: &TransferOffer) -> &Path {
        self.inner.category_roots.root_for(
            &self.inner.inbox,
            offer.kind,
            &safe_filename(&offer.filename),
        )
    }

    fn destination_root_stored(&self, offer: &StoredOffer) -> &Path {
        self.inner
            .category_roots
            .root_for(&self.inner.inbox, offer.kind, &offer.filename)
    }

    fn paths(&self, offer: &TransferOffer) -> StatePaths {
        let prefix = self
            .destination_root(offer)
            .join(STATE_DIRECTORY)
            .join(&offer.transfer_id);
        StatePaths {
            part: prefix.with_extension("part"),
            folder_part: prefix.with_extension("folder.part"),
            metadata: prefix.with_extension("json"),
            manifest: prefix.with_extension("manifest.json"),
            commit: prefix.with_extension("commit.json"),
            done: prefix.with_extension("done.json"),
        }
    }
}

fn ensure_same_manifest(expected: &FolderManifest, actual: Option<&FolderManifest>) -> Result<()> {
    if actual != Some(expected) {
        return Err(CoreError::InvalidTransfer(
            "transfer_id was reused with a different folder manifest".into(),
        ));
    }
    Ok(())
}

fn folder_entry_path(root: &Path, relative: &str) -> Result<PathBuf> {
    validate_folder_relative_path(relative)?;
    let mut path = root.to_path_buf();
    for component in relative.split('/') {
        path.push(component);
    }
    Ok(path)
}

async fn ensure_directory(path: &Path) -> Result<()> {
    match tokio::fs::symlink_metadata(path).await {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => Err(
            CoreError::InvalidTransfer("folder checkpoint root is not a directory".into()),
        ),
        Ok(_) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            match tokio::fs::create_dir(path).await {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    ensure_directory_exists(path).await
                }
                Err(error) => Err(error.into()),
            }
        }
        Err(error) => Err(error.into()),
    }
}

async fn ensure_directory_exists(path: &Path) -> Result<()> {
    let metadata = tokio::fs::symlink_metadata(path).await?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(CoreError::InvalidTransfer(
            "folder path contains a non-directory component".into(),
        ));
    }
    Ok(())
}

async fn ensure_safe_parent_directories(root: &Path, relative: &str) -> Result<()> {
    validate_folder_relative_path(relative)?;
    ensure_directory_exists(root).await?;
    let components = relative.split('/').collect::<Vec<_>>();
    let mut current = root.to_path_buf();
    for component in components.iter().take(components.len().saturating_sub(1)) {
        current.push(component);
        match tokio::fs::create_dir(&current).await {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                ensure_directory_exists(&current).await?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

async fn create_manifest_directories(root: &Path, manifest: &FolderManifest) -> Result<()> {
    ensure_directory_exists(root).await?;
    for entry in &manifest.entries {
        if entry.kind != FolderEntryKind::Directory {
            continue;
        }
        ensure_safe_parent_directories(root, &entry.path).await?;
        let path = folder_entry_path(root, &entry.path)?;
        match tokio::fs::create_dir(&path).await {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                ensure_directory_exists(&path).await?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn apply_manifest_times(root: &Path, manifest: &FolderManifest) -> Result<()> {
    for entry in manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == FolderEntryKind::File)
    {
        apply_modified_time(&folder_entry_path(root, &entry.path)?, entry.modified_ms)?;
    }
    let mut directories = manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == FolderEntryKind::Directory)
        .collect::<Vec<_>>();
    directories.sort_unstable_by_key(|entry| std::cmp::Reverse(entry.path.matches('/').count()));
    for entry in directories {
        apply_modified_time(&folder_entry_path(root, &entry.path)?, entry.modified_ms)?;
    }
    Ok(())
}

async fn validate_folder_contents(root: &Path, manifest: &FolderManifest) -> Result<()> {
    manifest.validate()?;
    ensure_directory_exists(root).await?;
    let expected = manifest
        .entries
        .iter()
        .map(|entry| (entry.path.as_str(), entry))
        .collect::<HashMap<_, _>>();
    let mut seen = HashMap::with_capacity(expected.len());
    let mut directories = vec![(root.to_path_buf(), String::new())];
    while let Some((directory, relative_parent)) = directories.pop() {
        let mut reader = tokio::fs::read_dir(&directory).await?;
        while let Some(item) = reader.next_entry().await? {
            let name = item.file_name().into_string().map_err(|_| {
                CoreError::InvalidTransfer("folder contains a non-UTF-8 filename".into())
            })?;
            let relative = if relative_parent.is_empty() {
                name
            } else {
                format!("{relative_parent}/{name}")
            };
            validate_folder_relative_path(&relative)?;
            let entry = expected.get(relative.as_str()).ok_or_else(|| {
                CoreError::InvalidTransfer("folder contains an unexpected entry".into())
            })?;
            if seen.insert(relative.clone(), ()).is_some() {
                return Err(CoreError::InvalidTransfer(
                    "folder contains a duplicate entry".into(),
                ));
            }
            let path = item.path();
            let metadata = tokio::fs::symlink_metadata(&path).await?;
            if metadata.file_type().is_symlink() {
                return Err(CoreError::InvalidTransfer(
                    "folder contains a symbolic link".into(),
                ));
            }
            match entry.kind {
                FolderEntryKind::Directory if metadata.is_dir() => {
                    directories.push((path, relative));
                }
                FolderEntryKind::File
                    if metadata.is_file()
                        && metadata.len() == entry.size
                        && blake3_file_hex(&path).await? == entry.blake3 => {}
                _ => {
                    return Err(CoreError::InvalidTransfer(
                        "folder entry does not match its manifest".into(),
                    ));
                }
            }
        }
    }
    if seen.len() != expected.len() {
        return Err(CoreError::InvalidTransfer(
            "folder is missing a manifest entry".into(),
        ));
    }
    Ok(())
}

fn ensure_same_offer(expected: &StoredOffer, actual: &StoredOffer) -> Result<()> {
    if expected != actual {
        return Err(CoreError::InvalidTransfer(
            "transfer_id was reused with different metadata".into(),
        ));
    }
    Ok(())
}

fn validate_destination_name(name: &str) -> Result<()> {
    if safe_filename(name) != name
        || Path::new(name).file_name().and_then(|item| item.to_str()) != Some(name)
    {
        return Err(CoreError::InvalidTransfer(
            "invalid destination in transfer state".into(),
        ));
    }
    Ok(())
}

async fn ensure_file_destination(path: &Path, size: u64, digest: &str) -> Result<()> {
    let metadata = tokio::fs::symlink_metadata(path).await?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(CoreError::InvalidTransfer(
            "file destination is not a regular file".into(),
        ));
    }
    if metadata.len() != size || blake3_file_hex(path).await?.as_str() != digest {
        return Err(CoreError::DigestMismatch {
            path: path.to_owned(),
        });
    }
    Ok(())
}

fn split_extension(filename: &str) -> (&str, &str) {
    match filename.rfind('.') {
        Some(index) if index > 0 => (&filename[..index], &filename[index..]),
        _ => (filename, ""),
    }
}

fn apply_modified_time(path: &Path, modified_ms: i64) -> Result<()> {
    let duration = Duration::from_millis(modified_ms as u64);
    let timestamp = UNIX_EPOCH
        .checked_add(duration)
        .ok_or_else(|| CoreError::InvalidTransfer("modified time is out of range".into()))?;
    filetime::set_file_mtime(path, filetime::FileTime::from_system_time(timestamp))?;
    Ok(())
}

async fn read_json_optional<T: DeserializeOwned>(path: &Path) -> Result<Option<T>> {
    match tokio::fs::read(path).await {
        Ok(data) => Ok(Some(serde_json::from_slice(&data)?)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

async fn atomic_write_json<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let data = serde_json::to_vec(value)?;
    let temporary = path.with_extension(format!("tmp-{}", Uuid::new_v4().simple()));
    let mut file = tokio::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)
        .await?;
    file.write_all(&data).await?;
    file.sync_all().await?;
    drop(file);
    // `rename` replaces the destination atomically on every supported platform,
    // including Windows, so no unlink window is opened here. Only if that fails do we
    // fall back to the unlink-then-rename sequence.
    if tokio::fs::rename(&temporary, path).await.is_err() {
        let _ = tokio::fs::remove_file(path).await;
        if let Err(error) = tokio::fs::rename(&temporary, path).await {
            let _ = tokio::fs::remove_file(&temporary).await;
            return Err(error.into());
        }
    }
    Ok(())
}

async fn prune_stale_state(directory: &Path) {
    let Ok(mut entries) = tokio::fs::read_dir(directory).await else {
        return;
    };
    let now = SystemTime::now();
    while let Ok(Some(entry)) = entries.next_entry().await {
        let Ok(metadata) = entry.metadata().await else {
            continue;
        };
        let touched = metadata.modified().or_else(|_| metadata.created());
        let stale = touched
            .ok()
            .and_then(|touched| now.duration_since(touched).ok())
            .is_some_and(|age| age > STATE_RETENTION);
        if !stale {
            continue;
        }
        let path = entry.path();
        let removed = if metadata.is_dir() {
            tokio::fs::remove_dir_all(&path).await
        } else {
            tokio::fs::remove_file(&path).await
        };
        if let Err(error) = removed {
            tracing::debug!(path = %path.display(), %error, "could not prune stale transfer state");
        }
    }
}

async fn remove_if_exists(path: &Path) -> Result<()> {
    match tokio::fs::remove_file(path).await {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

async fn remove_dir_all_if_exists(path: &Path) -> Result<()> {
    match tokio::fs::symlink_metadata(path).await {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            tokio::fs::remove_file(path).await?;
            Ok(())
        }
        Ok(metadata) if metadata.is_dir() => {
            tokio::fs::remove_dir_all(path).await?;
            Ok(())
        }
        Ok(_) => Err(CoreError::InvalidTransfer(
            "folder checkpoint root is not a directory".into(),
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

#[cfg(test)]
mod tests {
    use std::time::SystemTime;

    use tokio::io::AsyncWriteExt;

    use super::*;
    use crate::{
        DeviceIdentity,
        protocol::{FolderEntry, FolderEntryKind, FolderManifest, TransferKind, TransferOffer},
    };

    async fn signed_offer(source: &Path, identity: &DeviceIdentity) -> TransferOffer {
        let metadata = tokio::fs::metadata(source).await.unwrap();
        let modified_ms = metadata
            .modified()
            .unwrap_or(SystemTime::now())
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64;
        let mut offer = TransferOffer::new(
            Uuid::new_v4(),
            source.file_name().unwrap().to_string_lossy().into_owned(),
            metadata.len(),
            modified_ms,
            crate::hash::blake3_file(source).await.unwrap(),
            identity.summary(),
        );
        offer.signature = identity.sign_base64(&offer.signing_bytes().unwrap());
        offer
    }

    fn signed_folder_offer(
        filename: &str,
        manifest: &FolderManifest,
        identity: &DeviceIdentity,
    ) -> TransferOffer {
        let mut offer = TransferOffer::new(
            Uuid::new_v4(),
            filename.to_owned(),
            manifest.validate().unwrap(),
            1_700_000_000_000,
            manifest.digest().unwrap(),
            identity.summary(),
        );
        offer.kind = TransferKind::FolderV1;
        offer.signature = identity.sign_base64(&offer.signing_bytes().unwrap());
        offer
    }

    fn file_entry(path: &str, contents: &[u8]) -> FolderEntry {
        FolderEntry {
            path: path.to_owned(),
            kind: FolderEntryKind::File,
            size: contents.len() as u64,
            modified_ms: 1_700_000_000_000,
            blake3: blake3::hash(contents).to_hex().to_string(),
        }
    }

    fn directory_entry(path: &str) -> FolderEntry {
        FolderEntry {
            path: path.to_owned(),
            kind: FolderEntryKind::Directory,
            size: 0,
            modified_ms: 1_700_000_000_000,
            blake3: String::new(),
        }
    }

    #[tokio::test]
    async fn abandoned_resume_state_is_pruned_and_live_state_is_kept() {
        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let state = inbox.join(STATE_DIRECTORY);
        tokio::fs::create_dir_all(&state).await.unwrap();

        let abandoned = state.join("abandoned.part");
        let fresh = state.join("fresh.part");
        let stale_folder = state.join("abandoned.folder.part");
        tokio::fs::write(&abandoned, b"old").await.unwrap();
        tokio::fs::write(&fresh, b"new").await.unwrap();
        tokio::fs::create_dir_all(&stale_folder).await.unwrap();

        let expired = SystemTime::now() - STATE_RETENTION - Duration::from_secs(60);
        let expired = filetime::FileTime::from_system_time(expired);
        filetime::set_file_mtime(&abandoned, expired).unwrap();
        filetime::set_file_mtime(&stale_folder, expired).unwrap();

        TransferStore::new(&inbox).await.unwrap();
        assert!(!tokio::fs::try_exists(&abandoned).await.unwrap());
        assert!(!tokio::fs::try_exists(&stale_folder).await.unwrap());
        assert!(tokio::fs::try_exists(&fresh).await.unwrap());
    }

    #[tokio::test]
    async fn resumes_and_replays_completed_receipt() {
        let root = tempfile::tempdir().unwrap();
        let source = root.path().join("source.txt");
        tokio::fs::write(&source, b"hello resumable world")
            .await
            .unwrap();
        let identity = DeviceIdentity::generate(None, "Sender").unwrap();
        let offer = signed_offer(&source, &identity).await;
        let inbox = root.path().join("inbox");
        let store = TransferStore::new(&inbox).await.unwrap();
        let _guard = store.lock_transfer(&offer.transfer_id).await;

        let first = match store.prepare(&offer).await.unwrap() {
            PrepareOutcome::Resume(value) => value,
            PrepareOutcome::Complete(_) => panic!("new transfer was already complete"),
        };
        assert_eq!(first.offset, 0);
        let mut part = tokio::fs::OpenOptions::new()
            .append(true)
            .open(&first.part_path)
            .await
            .unwrap();
        part.write_all(b"hello ").await.unwrap();
        part.sync_all().await.unwrap();
        drop(part);

        let resumed = match store.prepare(&offer).await.unwrap() {
            PrepareOutcome::Resume(value) => value,
            PrepareOutcome::Complete(_) => panic!("partial transfer was complete"),
        };
        assert_eq!(resumed.offset, 6);
        let mut part = tokio::fs::OpenOptions::new()
            .append(true)
            .open(&resumed.part_path)
            .await
            .unwrap();
        part.write_all(b"resumable world").await.unwrap();
        part.sync_all().await.unwrap();
        drop(part);

        let completed = store.finalize(&offer).await.unwrap();
        assert_eq!(
            tokio::fs::read(&completed.destination).await.unwrap(),
            b"hello resumable world"
        );
        let replayed = match store.prepare(&offer).await.unwrap() {
            PrepareOutcome::Complete(value) => value,
            PrepareOutcome::Resume(_) => panic!("completed transfer did not replay its receipt"),
        };
        assert_eq!(completed.receipt, replayed.receipt);
        assert_eq!(completed.destination, replayed.destination);
    }

    #[tokio::test]
    async fn resumes_folder_at_the_first_partial_file_and_replays_receipt() {
        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let identity = DeviceIdentity::generate(None, "Sender").unwrap();
        let first_contents = b"alpha";
        let second_contents = b"bravo resumable";
        let manifest = FolderManifest::new(vec![
            directory_entry("docs"),
            file_entry("docs/a.txt", first_contents),
            file_entry("docs/b.txt", second_contents),
            directory_entry("empty"),
        ]);
        let offer = signed_folder_offer("project", &manifest, &identity);
        let store = TransferStore::new(&inbox).await.unwrap();
        let _guard = store.lock_transfer(&offer.transfer_id).await;

        let prepared = match store.prepare_folder(&offer, &manifest).await.unwrap() {
            PrepareFolderOutcome::Resume(value) => value,
            PrepareFolderOutcome::Complete(_) => panic!("new folder transfer was already complete"),
        };
        let mut first = store
            .open_folder_file(&prepared, &manifest.entries[1], 0)
            .await
            .unwrap();
        first.write_all(first_contents).await.unwrap();
        first.sync_all().await.unwrap();
        drop(first);
        store
            .verify_folder_file(&prepared, &manifest.entries[1])
            .await
            .unwrap();

        let partial_length = 6;
        let mut second = store
            .open_folder_file(&prepared, &manifest.entries[2], 0)
            .await
            .unwrap();
        second
            .write_all(&second_contents[..partial_length])
            .await
            .unwrap();
        second.sync_all().await.unwrap();
        drop(second);

        let resumed = match store.prepare_folder(&offer, &manifest).await.unwrap() {
            PrepareFolderOutcome::Resume(value) => value,
            PrepareFolderOutcome::Complete(_) => panic!("partial folder transfer was complete"),
        };
        assert_eq!(resumed.file_index, 1);
        assert_eq!(resumed.offset, partial_length as u64);
        assert_eq!(resumed.completed, first_contents.len() as u64);

        let mut second = store
            .open_folder_file(&resumed, &manifest.entries[2], resumed.offset)
            .await
            .unwrap();
        second
            .write_all(&second_contents[partial_length..])
            .await
            .unwrap();
        second.sync_all().await.unwrap();
        drop(second);
        store
            .verify_folder_file(&resumed, &manifest.entries[2])
            .await
            .unwrap();

        let completed = store
            .finalize_folder(&offer, &manifest, &resumed)
            .await
            .unwrap();
        assert_eq!(
            tokio::fs::read(completed.destination.join("docs/b.txt"))
                .await
                .unwrap(),
            second_contents
        );
        assert!(
            tokio::fs::metadata(completed.destination.join("empty"))
                .await
                .unwrap()
                .is_dir()
        );
        let replayed = match store.prepare_folder(&offer, &manifest).await.unwrap() {
            PrepareFolderOutcome::Complete(value) => value,
            PrepareFolderOutcome::Resume(_) => {
                panic!("completed folder transfer did not replay its receipt")
            }
        };
        assert_eq!(completed.receipt, replayed.receipt);
        assert_eq!(completed.destination, replayed.destination);
    }

    #[tokio::test]
    async fn removes_a_folder_file_with_the_wrong_blake3_digest() {
        let root = tempfile::tempdir().unwrap();
        let identity = DeviceIdentity::generate(None, "Sender").unwrap();
        let expected = b"expected";
        let manifest = FolderManifest::new(vec![file_entry("payload.bin", expected)]);
        let offer = signed_folder_offer("project", &manifest, &identity);
        let store = TransferStore::new(root.path().join("inbox")).await.unwrap();
        let prepared = match store.prepare_folder(&offer, &manifest).await.unwrap() {
            PrepareFolderOutcome::Resume(value) => value,
            PrepareFolderOutcome::Complete(_) => panic!("new folder transfer was already complete"),
        };
        let mut file = store
            .open_folder_file(&prepared, &manifest.entries[0], 0)
            .await
            .unwrap();
        file.write_all(b"tampered").await.unwrap();
        file.sync_all().await.unwrap();
        drop(file);

        assert!(matches!(
            store
                .verify_folder_file(&prepared, &manifest.entries[0])
                .await,
            Err(CoreError::DigestMismatch { .. })
        ));
        assert!(
            !tokio::fs::try_exists(prepared.staging_path.join("payload.bin"))
                .await
                .unwrap()
        );
        let resumed = match store.prepare_folder(&offer, &manifest).await.unwrap() {
            PrepareFolderOutcome::Resume(value) => value,
            PrepareFolderOutcome::Complete(_) => panic!("invalid folder transfer was committed"),
        };
        assert_eq!(resumed.file_index, 0);
        assert_eq!(resumed.offset, 0);
        assert_eq!(resumed.completed, 0);
    }

    #[tokio::test]
    async fn rejects_unexpected_entries_before_committing_a_folder() {
        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let identity = DeviceIdentity::generate(None, "Sender").unwrap();
        let contents = b"expected";
        let manifest = FolderManifest::new(vec![file_entry("payload.bin", contents)]);
        let offer = signed_folder_offer("project", &manifest, &identity);
        let store = TransferStore::new(&inbox).await.unwrap();
        let prepared = match store.prepare_folder(&offer, &manifest).await.unwrap() {
            PrepareFolderOutcome::Resume(value) => value,
            PrepareFolderOutcome::Complete(_) => panic!("new folder transfer was already complete"),
        };
        let mut file = store
            .open_folder_file(&prepared, &manifest.entries[0], 0)
            .await
            .unwrap();
        file.write_all(contents).await.unwrap();
        file.sync_all().await.unwrap();
        drop(file);
        store
            .verify_folder_file(&prepared, &manifest.entries[0])
            .await
            .unwrap();
        tokio::fs::write(prepared.staging_path.join("unexpected.txt"), b"unexpected")
            .await
            .unwrap();

        assert!(
            store
                .finalize_folder(&offer, &manifest, &prepared)
                .await
                .is_err()
        );
        assert!(!tokio::fs::try_exists(inbox.join("project")).await.unwrap());
        assert!(tokio::fs::try_exists(&prepared.staging_path).await.unwrap());
    }

    #[tokio::test]
    async fn category_roots_keep_checkpoints_and_destinations_on_the_same_root() {
        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let media = root.path().join("media");
        let folders = root.path().join("folders");
        let store = TransferStore::with_category_roots(
            &inbox,
            InboxCategoryRoots {
                media: Some(media.clone()),
                archive: Some(root.path().join("archives")),
                file: Some(root.path().join("files")),
                folder: Some(folders.clone()),
            },
        )
        .await
        .unwrap();
        let identity = DeviceIdentity::generate(None, "Sender").unwrap();
        let source = root.path().join("PHOTO.JPG");
        tokio::fs::write(&source, b"image").await.unwrap();
        let offer = signed_offer(&source, &identity).await;
        let prepared = match store.prepare(&offer).await.unwrap() {
            PrepareOutcome::Resume(value) => value,
            PrepareOutcome::Complete(_) => panic!("new transfer was already complete"),
        };
        assert!(prepared.part_path.starts_with(&media));
        let mut part = tokio::fs::OpenOptions::new()
            .append(true)
            .open(&prepared.part_path)
            .await
            .unwrap();
        part.write_all(b"image").await.unwrap();
        part.sync_all().await.unwrap();
        drop(part);
        let completed = store.finalize(&offer).await.unwrap();
        assert_eq!(completed.destination, media.join("PHOTO.JPG"));

        let manifest = FolderManifest::new(vec![directory_entry("empty")]);
        let folder_offer = signed_folder_offer("project", &manifest, &identity);
        let prepared = match store
            .prepare_folder(&folder_offer, &manifest)
            .await
            .unwrap()
        {
            PrepareFolderOutcome::Resume(value) => value,
            PrepareFolderOutcome::Complete(_) => panic!("new folder transfer was already complete"),
        };
        assert!(prepared.staging_path.starts_with(&folders));
        let completed = store
            .finalize_folder(&folder_offer, &manifest, &prepared)
            .await
            .unwrap();
        assert_eq!(completed.destination, folders.join("project"));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn rejects_a_symbolic_link_in_a_folder_checkpoint_parent() {
        use std::os::unix::fs::symlink;

        let root = tempfile::tempdir().unwrap();
        let outside = root.path().join("outside");
        tokio::fs::create_dir(&outside).await.unwrap();
        let identity = DeviceIdentity::generate(None, "Sender").unwrap();
        let manifest = FolderManifest::new(vec![
            directory_entry("nested"),
            file_entry("nested/payload.bin", b"payload"),
        ]);
        let offer = signed_folder_offer("project", &manifest, &identity);
        let store = TransferStore::new(root.path().join("inbox")).await.unwrap();
        let prepared = match store.prepare_folder(&offer, &manifest).await.unwrap() {
            PrepareFolderOutcome::Resume(value) => value,
            PrepareFolderOutcome::Complete(_) => panic!("new folder transfer was already complete"),
        };
        tokio::fs::write(outside.join("payload.bin"), b"tampered")
            .await
            .unwrap();
        // prepare_folder 已按 manifest 预建了 nested 真实目录,先移除才能放置符号链接。
        tokio::fs::remove_dir(prepared.staging_path.join("nested"))
            .await
            .unwrap();
        symlink(&outside, prepared.staging_path.join("nested")).unwrap();

        assert!(store.prepare_folder(&offer, &manifest).await.is_err());
        assert_eq!(
            tokio::fs::read(outside.join("payload.bin")).await.unwrap(),
            b"tampered"
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn rejects_a_symbolic_link_at_a_file_commit_destination() {
        use std::os::unix::fs::symlink;

        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let outside = root.path().join("outside.bin");
        tokio::fs::write(&outside, b"outside").await.unwrap();
        let source = root.path().join("payload.bin");
        tokio::fs::write(&source, b"payload").await.unwrap();
        let identity = DeviceIdentity::generate(None, "Sender").unwrap();
        let offer = signed_offer(&source, &identity).await;
        let store = TransferStore::new(&inbox).await.unwrap();
        let prepared = match store.prepare(&offer).await.unwrap() {
            PrepareOutcome::Resume(value) => value,
            PrepareOutcome::Complete(_) => panic!("new transfer was already complete"),
        };
        tokio::fs::write(&prepared.part_path, b"payload")
            .await
            .unwrap();
        let paths = store.paths(&offer);
        atomic_write_json(
            &paths.commit,
            &CommitRecord {
                offer: StoredOffer::from(&offer),
                destination: "payload.bin".into(),
                manifest: None,
            },
        )
        .await
        .unwrap();
        symlink(&outside, inbox.join("payload.bin")).unwrap();

        assert!(matches!(
            store.finalize(&offer).await,
            Err(CoreError::InvalidTransfer(message)) if message.contains("regular file")
        ));
        assert_eq!(tokio::fs::read(&outside).await.unwrap(), b"outside");
    }
}
