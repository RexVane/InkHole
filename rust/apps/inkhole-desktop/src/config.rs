use std::{
    collections::BTreeMap,
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

pub const CONFIG_SCHEMA_VERSION: u16 = 2;
pub const MAXIMUM_MANUAL_PEERS: usize = 32;
pub const MAXIMUM_RECENT_FILES: usize = 50;

const CONFIG_FILENAME: &str = "desktop-v2.json";
const IDENTITY_DIRECTORY: &str = "identities";
const CREDENTIAL_SERVICE: &str = "com.rexvane.inkhole.v2";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct DesktopConfig {
    pub schema_version: u16,
    pub peer_name: String,
    pub instance_id: String,
    pub inbox: String,
    pub inbox_auto_classify: bool,
    pub inbox_category_dirs: BTreeMap<String, String>,
    pub listen_port: u16,
    pub encryption_enabled: bool,
    pub manual_peers: Vec<ManualPeerConfig>,
    pub recent_files: Vec<String>,
    pub cross_network: CrossNetworkConfig,
    pub show_pet: bool,
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self {
            schema_version: CONFIG_SCHEMA_VERSION,
            peer_name: default_peer_name(),
            instance_id: Uuid::new_v4().simple().to_string(),
            inbox: default_inbox().to_string_lossy().into_owned(),
            inbox_auto_classify: false,
            inbox_category_dirs: empty_category_dirs(),
            listen_port: 0,
            encryption_enabled: false,
            manual_peers: Vec::new(),
            recent_files: Vec::new(),
            cross_network: CrossNetworkConfig::default(),
            show_pet: true,
        }
    }
}

impl DesktopConfig {
    pub fn normalize(&mut self) -> Result<()> {
        if self.schema_version != CONFIG_SCHEMA_VERSION {
            bail!(
                "unsupported desktop configuration schema {}; expected {}",
                self.schema_version,
                CONFIG_SCHEMA_VERSION
            );
        }
        self.peer_name = self.peer_name.trim().chars().take(40).collect();
        if self.peer_name.is_empty() {
            self.peer_name = default_peer_name();
        }
        self.instance_id = self.instance_id.trim().to_ascii_lowercase();
        if !valid_instance_id(&self.instance_id) {
            self.instance_id = Uuid::new_v4().simple().to_string();
        }
        self.inbox = normalize_path(&self.inbox)
            .unwrap_or_else(default_inbox)
            .to_string_lossy()
            .into_owned();
        self.inbox_category_dirs = normalize_category_dirs(&self.inbox_category_dirs);
        self.manual_peers.truncate(MAXIMUM_MANUAL_PEERS);
        for peer in &mut self.manual_peers {
            peer.normalize();
        }
        // 端口允许为 0(未指定):发现路径只用 host,端口仅作为直连提示保存。
        self.manual_peers.retain(|peer| !peer.host.is_empty());
        self.recent_files.truncate(MAXIMUM_RECENT_FILES);
        self.recent_files.retain(|path| !path.trim().is_empty());
        self.cross_network.normalize();
        Ok(())
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct ManualPeerConfig {
    pub name: String,
    pub host: String,
    pub port: u16,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub instance_id: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub fingerprint: String,
}

impl ManualPeerConfig {
    fn normalize(&mut self) {
        self.name = self.name.trim().chars().take(40).collect();
        self.host = self.host.trim().to_owned();
        self.instance_id = self.instance_id.trim().to_ascii_lowercase();
        if !self.instance_id.is_empty() && !valid_instance_id(&self.instance_id) {
            self.instance_id.clear();
        }
        self.fingerprint = self.fingerprint.trim().to_ascii_lowercase();
        if !self.fingerprint.is_empty() && !valid_fingerprint(&self.fingerprint) {
            self.fingerprint.clear();
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct WormholeConfig {
    #[serde(skip_serializing_if = "String::is_empty")]
    pub rendezvous_url: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub transit_relay: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct SshProfileConfig {
    pub id: String,
    pub host: String,
    pub port: u16,
    pub user: String,
    pub private_key_mode: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub private_key_path: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub private_key_label: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub host_key_sha256: String,
}

impl Default for SshProfileConfig {
    fn default() -> Self {
        Self {
            id: new_profile_id(),
            host: String::new(),
            port: 22,
            user: String::new(),
            private_key_mode: "file".into(),
            private_key_path: String::new(),
            private_key_label: String::new(),
            host_key_sha256: String::new(),
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct SshPeerConfig {
    pub id: String,
    pub name: String,
    pub instance_id: String,
    pub remote_port: u16,
    pub noise_public: String,
    pub end_to_end: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct SshConfig {
    pub enabled: bool,
    pub profile: SshProfileConfig,
    pub remote_port: u16,
    pub peers: Vec<SshPeerConfig>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct CrossNetworkConfig {
    pub wormhole: WormholeConfig,
    pub ssh: SshConfig,
}

impl CrossNetworkConfig {
    fn normalize(&mut self) {
        self.wormhole.rendezvous_url = self.wormhole.rendezvous_url.trim().to_owned();
        self.wormhole.transit_relay = self.wormhole.transit_relay.trim().to_owned();
        self.ssh.profile.host = self.ssh.profile.host.trim().to_owned();
        self.ssh.profile.user = self.ssh.profile.user.trim().to_owned();
        self.ssh.profile.private_key_path = self.ssh.profile.private_key_path.trim().to_owned();
        self.ssh.profile.private_key_label = self.ssh.profile.private_key_label.trim().to_owned();
        self.ssh.profile.host_key_sha256 = self.ssh.profile.host_key_sha256.trim().to_owned();
        if self.ssh.profile.id.is_empty() {
            self.ssh.profile.id = new_profile_id();
        }
        if self.ssh.profile.port == 0 {
            self.ssh.profile.port = 22;
        }
        if self.ssh.profile.private_key_mode != "paste" {
            self.ssh.profile.private_key_mode = "file".into();
        }
    }
}

#[derive(Debug, Clone)]
pub struct ConfigStore {
    root: PathBuf,
}

impl ConfigStore {
    pub fn discover() -> Result<Self> {
        let base =
            dirs::config_dir().context("operating system config directory is unavailable")?;
        Ok(Self::new(base.join("InkHole")))
    }

    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    pub fn config_path(&self) -> PathBuf {
        self.root.join(CONFIG_FILENAME)
    }

    pub fn identity_path(&self, instance_id: &str) -> PathBuf {
        self.root
            .join(IDENTITY_DIRECTORY)
            .join(format!("{instance_id}.json"))
    }

    pub fn load_or_create(&self) -> Result<DesktopConfig> {
        let path = self.config_path();
        let existing = match fs::read(&path) {
            Ok(data) => Some(data),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => {
                return Err(error).with_context(|| format!("failed to read {}", path.display()));
            }
        };
        let mut config = match &existing {
            Some(data) => serde_json::from_slice::<DesktopConfig>(data)
                .with_context(|| format!("invalid V2 desktop configuration: {}", path.display()))?,
            None => DesktopConfig::default(),
        };
        config.normalize()?;
        // 只有归一化后的内容确实和磁盘不同才回写，避免每次启动都无谓改写配置文件。
        let normalized = serde_json::to_vec_pretty(&config)?;
        if existing.as_deref() != Some(normalized.as_slice())
            && let Err(error) = self.save(&config)
        {
            // 配置目录只读或磁盘满时不应该让程序起不来：本次运行用内存里的配置继续。
            tracing::warn!(%error, "无法写回归一化后的桌面配置，本次运行仅使用内存配置");
        }
        Ok(config)
    }

    pub fn save(&self, config: &DesktopConfig) -> Result<()> {
        fs::create_dir_all(&self.root)
            .with_context(|| format!("failed to create {}", self.root.display()))?;
        let path = self.config_path();
        let temporary = self
            .root
            .join(format!(".desktop-v2-{}.tmp", Uuid::new_v4().simple()));
        let data = serde_json::to_vec_pretty(config)?;
        let mut options = OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&temporary)
            .with_context(|| format!("failed to create {}", temporary.display()))?;
        let result = (|| -> Result<()> {
            file.write_all(&data)?;
            file.sync_all()?;
            drop(file);
            replace_file(&temporary, &path)?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result.with_context(|| format!("failed to save {}", path.display()))
    }

    pub fn ensure_identity_parent(&self) -> Result<()> {
        let directory = self.root.join(IDENTITY_DIRECTORY);
        fs::create_dir_all(&directory)
            .with_context(|| format!("failed to create {}", directory.display()))
    }

    pub fn secret_entry(&self, instance_id: &str) -> Result<keyring::Entry> {
        keyring::Entry::new(
            CREDENTIAL_SERVICE,
            &format!("transfer-secret:{instance_id}"),
        )
        .context("system credential store is unavailable")
    }

    pub fn ssh_entry(&self, profile_id: &str, kind: &str) -> Result<keyring::Entry> {
        keyring::Entry::new(CREDENTIAL_SERVICE, &format!("ssh:{profile_id}:{kind}"))
            .context("system credential store is unavailable")
    }
}

fn replace_file(source: &Path, destination: &Path) -> std::io::Result<()> {
    match fs::rename(source, destination) {
        Ok(()) => Ok(()),
        Err(error)
            if destination.exists()
                && matches!(
                    error.kind(),
                    std::io::ErrorKind::AlreadyExists | std::io::ErrorKind::PermissionDenied
                ) =>
        {
            fs::remove_file(destination)?;
            fs::rename(source, destination)
        }
        Err(error) => Err(error),
    }
}

fn default_peer_name() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .ok()
        .map(|value| value.trim().chars().take(40).collect::<String>())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "InkHole Device".into())
}

fn default_inbox() -> PathBuf {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    #[cfg(target_os = "windows")]
    return home.join("Desktop").join("inkhole");
    #[cfg(target_os = "macos")]
    return home.join("Documents").join("inkhole");
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    return home.join("InkHole").join("Inbox");
}

fn normalize_path(value: &str) -> Option<PathBuf> {
    let value = value.trim();
    if value.is_empty() {
        return None;
    }
    /*
    if value == "~" || value.starts_with("~/") || value.starts_with("~\") {
    */
    let windows_home = value.starts_with('~') && value.as_bytes().get(1) == Some(&92);
    if value == "~" || value.starts_with("~/") || windows_home {
        return dirs::home_dir().map(|home| home.join(value[1..].trim_start_matches(['/', '\\'])));
    }
    let path = PathBuf::from(value);
    if path.is_absolute() {
        Some(path)
    } else {
        std::env::current_dir()
            .ok()
            .map(|directory| directory.join(path))
    }
}

fn empty_category_dirs() -> BTreeMap<String, String> {
    ["media", "archive", "file", "folder"]
        .into_iter()
        .map(|category| (category.to_owned(), String::new()))
        .collect()
}

fn normalize_category_dirs(source: &BTreeMap<String, String>) -> BTreeMap<String, String> {
    empty_category_dirs()
        .into_keys()
        .map(|category| {
            let value = source
                .get(&category)
                .map(|value| value.trim().to_owned())
                .unwrap_or_default();
            (category, value)
        })
        .collect()
}

fn new_profile_id() -> String {
    Uuid::new_v4().simple().to_string()[..24].to_owned()
}

fn valid_instance_id(value: &str) -> bool {
    value.len() == 32 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn valid_fingerprint(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn creates_only_the_v2_configuration() {
        let root = tempfile::tempdir().unwrap();
        let store = ConfigStore::new(root.path().join("InkHole"));
        let config = store.load_or_create().unwrap();

        assert_eq!(config.schema_version, CONFIG_SCHEMA_VERSION);
        assert!(store.config_path().is_file());
        assert!(!root.path().join("InkHole").join("desktop.json").exists());
        assert!(!root.path().join("InkHole").join("config.json").exists());
    }

    #[test]
    fn rejects_an_unknown_schema_instead_of_migrating_it() {
        let root = tempfile::tempdir().unwrap();
        let store = ConfigStore::new(root.path().join("InkHole"));
        fs::create_dir_all(root.path().join("InkHole")).unwrap();
        fs::write(
            store.config_path(),
            br#"{"schema_version":1,"peer_name":"old"}"#,
        )
        .unwrap();

        let error = store.load_or_create().unwrap_err();
        assert!(
            error
                .to_string()
                .contains("unsupported desktop configuration schema")
        );
    }

    #[test]
    fn normalizes_manual_peers_and_category_keys() {
        let mut config = DesktopConfig {
            inbox_category_dirs: BTreeMap::from([
                ("media".into(), "  media-dir  ".into()),
                ("unknown".into(), "ignored".into()),
            ]),
            manual_peers: vec![ManualPeerConfig {
                name: " receiver ".into(),
                host: " 192.0.2.1 ".into(),
                port: 4433,
                instance_id: "INVALID".into(),
                fingerprint: "INVALID".into(),
            }],
            ..DesktopConfig::default()
        };

        config.normalize().unwrap();

        assert_eq!(config.inbox_category_dirs.len(), 4);
        assert_eq!(config.inbox_category_dirs["media"], "media-dir");
        assert_eq!(config.manual_peers[0].host, "192.0.2.1");
        assert!(config.manual_peers[0].instance_id.is_empty());
        assert!(config.manual_peers[0].fingerprint.is_empty());
    }

    #[test]
    fn preserves_disabled_ssh_peer_end_to_end_setting() {
        let mut config = DesktopConfig::default();
        config.cross_network.ssh.peers.push(SshPeerConfig {
            instance_id: "peer-1".into(),
            remote_port: 2201,
            noise_public: "noise-key".into(),
            end_to_end: false,
            ..SshPeerConfig::default()
        });

        config.normalize().unwrap();

        assert!(!config.cross_network.ssh.peers[0].end_to_end);
    }
}
