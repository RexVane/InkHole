use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::protocol::TransferKind;

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct InboxCategoryRoots {
    pub media: Option<PathBuf>,
    pub archive: Option<PathBuf>,
    pub file: Option<PathBuf>,
    pub folder: Option<PathBuf>,
}

impl InboxCategoryRoots {
    pub(crate) fn root_for<'a>(
        &'a self,
        primary: &'a Path,
        kind: TransferKind,
        filename: &str,
    ) -> &'a Path {
        let configured = if kind == TransferKind::FolderV1 {
            self.folder.as_deref()
        } else {
            match file_category(filename) {
                FileCategory::Media => self.media.as_deref(),
                FileCategory::Archive => self.archive.as_deref(),
                FileCategory::File => self.file.as_deref(),
            }
        };
        configured.unwrap_or(primary)
    }

    pub(crate) fn all_roots(&self, primary: &Path) -> Vec<PathBuf> {
        let mut roots = vec![primary.to_path_buf()];
        for root in [
            self.media.as_ref(),
            self.archive.as_ref(),
            self.file.as_ref(),
            self.folder.as_ref(),
        ]
        .into_iter()
        .flatten()
        {
            if !roots.iter().any(|existing| same_path(existing, root)) {
                roots.push(root.clone());
            }
        }
        roots
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FileCategory {
    Media,
    Archive,
    File,
}

fn file_category(filename: &str) -> FileCategory {
    let extension = Path::new(filename)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if matches!(
        extension.as_str(),
        "jpg"
            | "jpeg"
            | "png"
            | "gif"
            | "webp"
            | "bmp"
            | "tif"
            | "tiff"
            | "heic"
            | "heif"
            | "svg"
            | "mp4"
            | "mov"
            | "m4v"
            | "mkv"
            | "avi"
            | "webm"
            | "wmv"
            | "flv"
            | "mpeg"
            | "mpg"
            | "3gp"
            | "m2ts"
            | "mts"
    ) {
        FileCategory::Media
    } else if matches!(
        extension.as_str(),
        "zip"
            | "rar"
            | "7z"
            | "tar"
            | "gz"
            | "bz2"
            | "xz"
            | "tgz"
            | "tbz"
            | "tbz2"
            | "txz"
            | "zst"
            | "lz"
            | "lz4"
            | "cab"
            | "iso"
            | "dmg"
    ) {
        FileCategory::Archive
    } else {
        FileCategory::File
    }
}

#[cfg(windows)]
fn same_path(left: &Path, right: &Path) -> bool {
    left.to_string_lossy()
        .eq_ignore_ascii_case(&right.to_string_lossy())
}

#[cfg(not(windows))]
fn same_path(left: &Path, right: &Path) -> bool {
    left == right
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routes_known_extensions_and_folders_without_case_sensitivity() {
        let primary = Path::new("primary");
        let roots = InboxCategoryRoots {
            media: Some("media".into()),
            archive: Some("archive".into()),
            file: Some("files".into()),
            folder: Some("folders".into()),
        };
        assert_eq!(
            roots.root_for(primary, TransferKind::File, "PHOTO.JPEG"),
            Path::new("media")
        );
        assert_eq!(
            roots.root_for(primary, TransferKind::File, "backup.TAR"),
            Path::new("archive")
        );
        assert_eq!(
            roots.root_for(primary, TransferKind::File, "notes.txt"),
            Path::new("files")
        );
        assert_eq!(
            roots.root_for(primary, TransferKind::FolderV1, "photo.jpg"),
            Path::new("folders")
        );
        // `.ts` is far more often TypeScript than an MPEG transport stream; the
        // unambiguous container extensions stay in the media bucket.
        assert_eq!(
            roots.root_for(primary, TransferKind::File, "main.ts"),
            Path::new("files")
        );
        assert_eq!(
            roots.root_for(primary, TransferKind::File, "clip.m2ts"),
            Path::new("media")
        );
    }
}
