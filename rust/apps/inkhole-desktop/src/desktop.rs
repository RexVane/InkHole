use std::{
    collections::{HashMap, HashSet},
    fs,
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, UdpSocket},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use anyhow::{Context, Result, anyhow, bail};
use inkhole_core::{DeviceIdentity, InboxCategoryRoots, JsonService, LanPeer, ServiceEvent};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tauri::{
    AppHandle, Emitter, Manager, PhysicalPosition, Position,
    menu::{MenuBuilder, MenuItemBuilder},
};
use tauri_plugin_autostart::ManagerExt as _;
use tauri_plugin_dialog::{DialogExt as _, FilePath};
use tauri_plugin_opener::OpenerExt as _;
use tokio::sync::{Mutex, oneshot};
use uuid::Uuid;

use crate::config::{
    ConfigStore, DesktopConfig, MAXIMUM_RECENT_FILES, ManualPeerConfig, SshPeerConfig,
};

const MAXIMUM_SEND_PATHS: usize = 128;
const REPOSITORY_URL: &str = "https://github.com/RexVane/InkHole";
const RELEASES_URL: &str = "https://github.com/RexVane/InkHole/releases";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct PeerView {
    pub instance_id: String,
    pub name: String,
    pub host: String,
    pub port: u16,
    pub fingerprint: String,
    pub transport: String,
    pub routes: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub capabilities: Vec<String>,
}

impl From<LanPeer> for PeerView {
    fn from(peer: LanPeer) -> Self {
        let routes = match peer.service_name.as_str() {
            "ssh" => vec!["SSH/QUIC".into()],
            "lan+ssh" => vec!["局域网".into(), "SSH/QUIC".into()],
            _ => vec!["局域网".into()],
        };
        let transport = routes.join(" + ");
        Self {
            instance_id: peer.instance_id,
            name: peer.name,
            host: peer.host,
            port: peer.port,
            fingerprint: peer.fingerprint,
            transport,
            routes,
            capabilities: peer.capabilities,
        }
    }
}

#[derive(Debug)]
struct SendBatch {
    total: usize,
    settled: usize,
    succeeded: usize,
    core_send_ids: Vec<String>,
}

#[derive(Debug)]
struct PendingOneTime {
    batch_id: String,
    total: usize,
}

#[derive(Debug)]
struct StartedOneTime {
    batch_id: String,
    total: usize,
    orphan_sent: Vec<Value>,
}

#[derive(Debug)]
struct RuntimeState {
    config: DesktopConfig,
    session_id: Option<String>,
    ssh_session_id: Option<String>,
    ssh_connected: bool,
    /// 核心侧 ssh.* 事件不带 session_id，只能在桌面侧用代际区分：
    /// 每次作废旧会话都递增，事件泵只接受当前有效代际的事件。
    ssh_generation: u64,
    ssh_live: bool,
    actual_port: u16,
    peers: Vec<PeerView>,
    selected: String,
    batches: HashMap<String, SendBatch>,
    core_to_batch: HashMap<String, String>,
    orphan_sent: HashMap<String, Value>,
    pending_one_time: HashMap<String, PendingOneTime>,
}

impl RuntimeState {
    fn new(config: DesktopConfig) -> Self {
        Self {
            config,
            session_id: None,
            ssh_session_id: None,
            ssh_connected: false,
            ssh_generation: 0,
            ssh_live: false,
            actual_port: 0,
            peers: Vec::new(),
            selected: String::new(),
            batches: HashMap::new(),
            core_to_batch: HashMap::new(),
            orphan_sent: HashMap::new(),
            pending_one_time: HashMap::new(),
        }
    }

    /// 作废当前 SSH 会话代际并返回新代际；此后到期的 ssh.* 事件一律丢弃。
    fn invalidate_ssh(&mut self) -> u64 {
        self.ssh_generation += 1;
        self.ssh_live = false;
        self.ssh_connected = false;
        self.ssh_generation
    }

    fn start_one_time(
        &mut self,
        session_id: &str,
        send_ids: Vec<String>,
    ) -> Result<StartedOneTime> {
        let pending = self
            .pending_one_time
            .remove(session_id)
            .context("one-time transfer is no longer pending")?;
        if send_ids.len() != pending.total
            || send_ids.iter().any(String::is_empty)
            || send_ids.iter().collect::<HashSet<_>>().len() != send_ids.len()
        {
            bail!("core returned invalid one-time send identifiers");
        }

        let mut orphan_sent = Vec::new();
        for send_id in &send_ids {
            self.core_to_batch
                .insert(send_id.clone(), pending.batch_id.clone());
            if let Some(event) = self.orphan_sent.remove(send_id) {
                orphan_sent.push(event);
            }
        }
        self.batches.insert(
            pending.batch_id.clone(),
            SendBatch {
                total: pending.total,
                settled: 0,
                succeeded: 0,
                core_send_ids: send_ids,
            },
        );
        Ok(StartedOneTime {
            batch_id: pending.batch_id,
            total: pending.total,
            orphan_sent,
        })
    }
}

pub struct DesktopState {
    store: ConfigStore,
    service: JsonService,
    runtime: Mutex<RuntimeState>,
    lifecycle: Mutex<()>,
    pet_motion: AtomicU64,
}

impl DesktopState {
    pub fn new(store: ConfigStore, config: DesktopConfig) -> Self {
        Self {
            store,
            service: JsonService::new(),
            runtime: Mutex::new(RuntimeState::new(config)),
            lifecycle: Mutex::new(()),
            pet_motion: AtomicU64::new(0),
        }
    }

    pub fn spawn_event_pump(self: &Arc<Self>, app: AppHandle) {
        let state = Arc::clone(self);
        tauri::async_runtime::spawn(async move {
            loop {
                match state.service.poll_event(Some(Duration::from_secs(1))).await {
                    Some(encoded) => match serde_json::from_str::<ServiceEvent>(&encoded) {
                        Ok(event) => state.handle_core_event(&app, event).await,
                        Err(error) => {
                            let _ = app.emit("status", format!("核心事件解析失败: {error}"));
                        }
                    },
                    None => tokio::time::sleep(Duration::from_millis(25)).await,
                }
            }
        });
    }

    pub async fn dispatch(&self, app: &AppHandle, method: &str, args: Vec<Value>) -> Result<Value> {
        match method {
            "Start" => {
                self.start(app).await?;
                Ok(Value::Null)
            }
            "Stop" => {
                self.stop(app).await?;
                Ok(Value::Null)
            }
            "Close" => {
                self.stop(app).await?;
                app.exit(0);
                Ok(Value::Null)
            }
            "GetConfig" => self.get_config().await,
            "SaveConfig" => self.save_config(app, &args).await,
            "SaveSettings" => self.save_settings(app, &args).await,
            "SaveInboxClassification" => self.save_inbox_classification(app, &args).await,
            "ManualPeers" => self.manual_peers().await,
            "SaveManualPeers" => self.save_manual_peers(app, &args).await,
            "CrossNetworkConfig" => self.cross_network_config().await,
            "SaveWormholeConfig" => self.save_wormhole_config(&args).await,
            "SaveSSHConfig" => self.save_ssh_config(app, &args).await,
            "RemoveSSHPeer" => self.remove_ssh_peer(app, &args).await,
            "CheckSSH" => self.check_ssh(&args).await,
            "CreateSSHPairing" => self.create_ssh_pairing().await,
            "JoinSSHPairing" => self.join_ssh_pairing(&args).await,
            "CreateOneTime" => self.create_one_time(&args).await,
            "JoinOneTime" => self.join_one_time(&args).await,
            "AcceptOneTime" => self.accept_one_time(&args).await,
            "RejectOneTime" => self.reject_one_time(&args).await,
            "CancelTransportSession" => self.cancel_transport_session(&args).await,
            "Peers" => serde_json::to_value(self.peers().await?).map_err(Into::into),
            "RefreshDiscovery" => {
                self.refresh_discovery(app).await?;
                Ok(Value::Null)
            }
            "SelectPeer" => self.select_peer(app, &args).await,
            "GetSelected" => Ok(Value::String(self.runtime.lock().await.selected.clone())),
            "SendPaths" => self.send_paths(app, &args, false).await,
            "SendToSelected" => self.send_paths(app, &args, true).await,
            "CancelSend" => self.cancel_send(&args).await,
            "RecentFiles" => serde_json::to_value(self.recent_files().await).map_err(Into::into),
            "ClearRecent" => self.clear_recent(app).await,
            "ChooseFiles" => serde_json::to_value(choose_files(app).await?).map_err(Into::into),
            "ChooseFolder" | "ChooseInbox" => Ok(Value::String(choose_folder(app, None).await?)),
            "ChooseInboxCategory" => {
                let current: String = argument(&args, 1, "current")?;
                let inbox: String = argument(&args, 2, "inbox")?;
                let initial = if current.trim().is_empty() {
                    inbox
                } else {
                    current
                };
                Ok(Value::String(choose_folder(app, Some(initial)).await?))
            }
            "OpenPath" => {
                let path: String = argument(&args, 0, "path")?;
                open_path(app, &path)?;
                Ok(Value::Null)
            }
            "OpenInbox" => self.open_inbox(app).await,
            "OpenRepository" => {
                open_url(app, REPOSITORY_URL)?;
                Ok(Value::Null)
            }
            "OpenReleases" => {
                open_url(app, RELEASES_URL)?;
                Ok(Value::Null)
            }
            "CheckForUpdate" => check_for_update().await,
            "DownloadUpdate" => {
                let url: String = argument(&args, 0, "url")?;
                download_and_launch_update(app, &url).await
            }
            "AutostartEnabled" => Ok(Value::Bool(app.autolaunch().is_enabled()?)),
            "SetAutostart" => self.set_autostart(app, &args),
            "ShowMain" => {
                show_main(app)?;
                Ok(Value::Null)
            }
            "SetPetVisible" => self.set_pet_visible(app, &args).await,
            "DisablePet" => self.disable_pet(app).await,
            "GetPetScreenArea" => get_pet_screen_area(app),
            "MovePetTo" => self.move_pet_to(app, &args).await,
            "CancelPetMotion" => {
                self.pet_motion.fetch_add(1, Ordering::SeqCst);
                Ok(Value::Null)
            }
            "DragPet" => {
                self.pet_motion.fetch_add(1, Ordering::SeqCst);
                let window = app
                    .get_webview_window("pet")
                    .context("pet window is unavailable")?;
                window.start_dragging()?;
                wait_for_drag_release(&window).await;
                Ok(Value::Null)
            }
            "OpenPetMenu" => self.open_pet_menu(app, &args).await,
            _ => bail!("unknown desktop method: {method}"),
        }
    }
}

impl DesktopState {
    async fn start(&self, app: &AppHandle) -> Result<()> {
        let _lifecycle = self.lifecycle.lock().await;
        if self.runtime.lock().await.session_id.is_some() {
            return Ok(());
        }

        let config = self.runtime.lock().await.config.clone();
        fs::create_dir_all(&config.inbox)
            .with_context(|| format!("无法创建收件箱 {}", config.inbox))?;
        self.store.ensure_identity_parent()?;
        let identity = DeviceIdentity::load_or_create(
            self.store.identity_path(&config.instance_id),
            Some(&config.instance_id),
            &config.peer_name,
        )
        .await?;
        let secret = self.active_secret(&config)?;
        let inbox_category_roots = inbox_category_roots(&config);
        let discovery_targets = config
            .manual_peers
            .iter()
            .filter(|peer| !peer.host.is_empty())
            .map(|peer| {
                if peer.port == 0 {
                    peer.host.clone()
                } else if peer.host.contains(':') && !peer.host.starts_with('[') {
                    format!("[{}]:{}", peer.host, peer.port)
                } else {
                    format!("{}:{}", peer.host, peer.port)
                }
            })
            .collect::<Vec<_>>();
        let start = call_core(
            &self.service,
            "lan.start",
            json!({
                "peer_name": config.peer_name,
                "instance_id": config.instance_id,
                "identity_private": identity.export_private()?,
                "secret": secret,
                "inbox": config.inbox,
                "inbox_category_roots": inbox_category_roots,
                "listen_port": config.listen_port,
                "capabilities": ["quic-v2", "blake3", "folder-v1"],
                "discovery_targets": discovery_targets,
            }),
        )
        .await?;
        let session_id = string_field(&start, "session_id")?;
        let actual_port = u16_field(&start, "port")?;
        {
            let mut runtime = self.runtime.lock().await;
            runtime.session_id = Some(session_id);
            runtime.actual_port = actual_port;
        }
        self.refresh_discovery(app).await?;
        let _ = app.emit("status", format!("墨洞已开启 · {}", config.peer_name));
        if config.cross_network.ssh.enabled
            && let Err(error) = self.start_ssh(app).await
        {
            let _ = app.emit(
                "transport",
                json!({
                    "event": "ssh.config.error",
                    "error": error.to_string(),
                }),
            );
        }
        Ok(())
    }

    async fn stop(&self, app: &AppHandle) -> Result<()> {
        let _lifecycle = self.lifecycle.lock().await;
        let (session_id, ssh_session_id) = {
            let mut runtime = self.runtime.lock().await;
            runtime.actual_port = 0;
            runtime.invalidate_ssh();
            runtime.peers.clear();
            runtime.batches.clear();
            runtime.core_to_batch.clear();
            runtime.orphan_sent.clear();
            runtime.pending_one_time.clear();
            (runtime.session_id.take(), runtime.ssh_session_id.take())
        };
        if let Some(ssh_session_id) = ssh_session_id {
            let _ = call_core(
                &self.service,
                "session.cancel",
                json!({ "session_id": ssh_session_id }),
            )
            .await;
        }
        if let Some(session_id) = session_id {
            // 本地状态已经清空，会话标识不会再有第二个持有者：这里必须尽力让核心侧也停掉，
            // 否则核心会话既停不掉又失去引用，端口和监听线程会永久泄漏。
            let params = json!({ "session_id": session_id });
            let mut outcome = call_core(&self.service, "lan.stop", params.clone()).await;
            if let Err(error) = &outcome {
                tracing::warn!(%error, %session_id, "lan.stop 失败，重试一次");
                outcome = call_core(&self.service, "lan.stop", params).await;
            }
            if let Err(error) = outcome {
                tracing::error!(%error, %session_id, "lan.stop 重试后仍失败，核心会话可能残留");
                let _ = app.emit("status", format!("停止墨洞时核心未能正常清理: {error}"));
            }
        }
        let _ = app.emit("peers", Vec::<PeerView>::new());
        Ok(())
    }

    async fn get_config(&self) -> Result<Value> {
        let runtime = self.runtime.lock().await;
        let (has_secret, warning) = self.secret_state(&runtime.config);
        Ok(json!({
            "peerName": runtime.config.peer_name,
            "instanceId": runtime.config.instance_id,
            "inbox": runtime.config.inbox,
            "inboxAutoClassify": runtime.config.inbox_auto_classify,
            "inboxCategoryDirs": runtime.config.inbox_category_dirs,
            "hasSecret": has_secret,
            "encryptionEnabled": runtime.config.encryption_enabled,
            "port": runtime.config.listen_port,
            "actualPort": runtime.actual_port,
            "running": runtime.session_id.is_some(),
            "showPet": runtime.config.show_pet,
            "version": env!("CARGO_PKG_VERSION"),
            "warning": warning,
            "addresses": local_addresses(),
        }))
    }

    async fn save_config(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let peer_name: String = argument(args, 0, "peerName")?;
        let inbox: String = argument(args, 1, "inbox")?;
        let secret: String = argument(args, 2, "secret")?;
        let clear_secret: bool = argument(args, 3, "clearSecret")?;
        let port: u16 = argument(args, 4, "port")?;
        let show_pet: bool = argument(args, 5, "showPet")?;
        let encryption_enabled: bool = argument(args, 6, "encryptionEnabled")?;

        let peer_name = peer_name.trim();
        if peer_name.is_empty() {
            bail!("设备名称不能为空");
        }
        if peer_name.chars().count() > 40 {
            bail!("设备名称不能超过 40 个字符");
        }
        let inbox = inbox.trim();
        if inbox.is_empty() {
            bail!("收件箱目录不能为空");
        }

        let (instance_id, restart) = {
            let runtime = self.runtime.lock().await;
            (
                runtime.config.instance_id.clone(),
                runtime.session_id.is_some()
                    && (runtime.config.peer_name != peer_name
                        || runtime.config.inbox != inbox
                        || runtime.config.listen_port != port
                        || runtime.config.encryption_enabled != encryption_enabled
                        || !secret.is_empty()
                        || clear_secret),
            )
        };
        let entry = self.store.secret_entry(&instance_id)?;
        if clear_secret {
            match entry.delete_credential() {
                Ok(()) | Err(keyring::Error::NoEntry) => {}
                Err(error) => return Err(error).context("清除传输口令失败"),
            }
        } else if !secret.is_empty() {
            entry.set_password(&secret).context("保存传输口令失败")?;
        }
        if encryption_enabled {
            let available = if secret.is_empty() {
                entry.get_password().is_ok_and(|value| !value.is_empty())
            } else {
                true
            };
            if !available {
                bail!("启用加密前请先设置端到端口令");
            }
        }

        {
            let mut runtime = self.runtime.lock().await;
            runtime.config.peer_name = peer_name.to_owned();
            runtime.config.inbox = inbox.to_owned();
            runtime.config.listen_port = port;
            runtime.config.show_pet = show_pet;
            runtime.config.encryption_enabled = encryption_enabled && !clear_secret;
            runtime.config.normalize()?;
            self.store.save(&runtime.config)?;
        }
        set_pet_window_visible(app, show_pet)?;
        if restart {
            self.stop(app).await?;
            self.start(app).await?;
        }
        Ok(Value::Null)
    }

    async fn save_settings(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let mut input: DesktopSettingsInput = argument(args, 0, "settings")?;
        input.peer_name = input.peer_name.trim().to_owned();
        input.inbox = input.inbox.trim().to_owned();
        input.ssh.host = input.ssh.host.trim().to_owned();
        input.ssh.user = input.ssh.user.trim().to_owned();
        input.ssh.private_key_path = input.ssh.private_key_path.trim().to_owned();
        input.ssh.host_key_sha256 = input.ssh.host_key_sha256.trim().to_owned();
        if input.peer_name.is_empty() {
            bail!("设备名称不能为空");
        }
        if input.peer_name.chars().count() > 40 {
            bail!("设备名称不能超过 40 个字符");
        }
        if input.inbox.is_empty() {
            bail!("收件箱目录不能为空");
        }
        if input.ssh.port == 0 {
            input.ssh.port = 22;
        }
        if input.ssh.private_key_mode != "paste" {
            input.ssh.private_key_mode = "file".into();
        }
        input.ssh.peers = normalize_ssh_peers(input.ssh.peers)?;

        let current = self.runtime.lock().await.config.clone();
        let transfer_secret = self.store.secret_entry(&current.instance_id)?;
        let profile_id = current.cross_network.ssh.profile.id.clone();
        let private_key = self.store.ssh_entry(&profile_id, "private-key")?;
        let passphrase = self.store.ssh_entry(&profile_id, "passphrase")?;
        let pasted_key_mode = input.ssh.private_key_mode == "paste";

        if input.encryption_enabled {
            let has_secret = if input.clear_secret {
                false
            } else if !input.secret.is_empty() {
                true
            } else {
                credential_value(&transfer_secret, "读取端到端口令")?
                    .is_some_and(|value| !value.is_empty())
            };
            if !has_secret {
                bail!("启用加密前请先设置端到端口令");
            }
        }
        if input.ssh.enabled {
            if input.ssh.host.is_empty() || input.ssh.user.is_empty() {
                bail!("SSH 主机和用户名不能为空");
            }
            if input.ssh.host_key_sha256.is_empty() {
                bail!("请先检测并确认 SSH 主机指纹");
            }
            if pasted_key_mode {
                let has_key = !input.ssh.pasted_key.is_empty()
                    || credential_value(&private_key, "读取 SSH 私钥")?
                        .is_some_and(|value| !value.is_empty());
                if !has_key {
                    bail!("请粘贴 SSH 私钥");
                }
            } else if input.ssh.private_key_path.is_empty() {
                bail!("请选择 SSH 私钥文件");
            }
        }

        let transfer_change = input.clear_secret || !input.secret.is_empty();
        let inspect_private_key = !input.ssh.pasted_key.is_empty() || !pasted_key_mode;
        let passphrase_change = input.ssh.clear_passphrase || !input.ssh.passphrase.is_empty();
        let old_transfer = transfer_change
            .then(|| credential_value(&transfer_secret, "读取旧端到端口令"))
            .transpose()?;
        let old_private_key = inspect_private_key
            .then(|| credential_value(&private_key, "读取旧 SSH 私钥"))
            .transpose()?;
        let private_key_change = !input.ssh.pasted_key.is_empty()
            || (!pasted_key_mode && old_private_key.as_ref().is_some_and(Option::is_some));
        let old_passphrase = passphrase_change
            .then(|| credential_value(&passphrase, "读取旧 SSH 私钥口令"))
            .transpose()?;

        let mut candidate = current.clone();
        candidate.peer_name = input.peer_name.clone();
        candidate.inbox = input.inbox.clone();
        candidate.listen_port = input.port;
        candidate.show_pet = input.show_pet;
        candidate.encryption_enabled = input.encryption_enabled && !input.clear_secret;
        candidate.inbox_auto_classify = input.auto_classify;
        candidate.inbox_category_dirs = input.category_dirs.clone().into_iter().collect();
        candidate.manual_peers = input.manual_peers.clone();
        candidate.cross_network.wormhole.rendezvous_url = input.wormhole_rendezvous.clone();
        candidate.cross_network.wormhole.transit_relay = input.wormhole_relay.clone();
        candidate.cross_network.ssh.enabled = input.ssh.enabled;
        candidate.cross_network.ssh.profile.host = input.ssh.host.clone();
        candidate.cross_network.ssh.profile.port = input.ssh.port;
        candidate.cross_network.ssh.profile.user = input.ssh.user.clone();
        candidate.cross_network.ssh.profile.private_key_mode = input.ssh.private_key_mode.clone();
        candidate.cross_network.ssh.profile.private_key_path = input.ssh.private_key_path.clone();
        candidate.cross_network.ssh.profile.private_key_label = if pasted_key_mode {
            "已存入系统安全存储".into()
        } else {
            String::new()
        };
        candidate.cross_network.ssh.profile.host_key_sha256 = input.ssh.host_key_sha256.clone();
        candidate.cross_network.ssh.peers = input.ssh.peers.clone();
        candidate.normalize()?;

        let old_autostart = app.autolaunch().is_enabled()?;
        let autostart_changed = old_autostart != input.autostart;
        if autostart_changed {
            let actual = match apply_autostart(app, input.autostart) {
                Ok(actual) => actual,
                Err(error) => {
                    restore_autostart(app, old_autostart);
                    return Err(error);
                }
            };
            if actual != input.autostart {
                restore_autostart(app, old_autostart);
                bail!("系统未能应用开机自启动设置");
            }
        }

        let lan_restart = current.peer_name != candidate.peer_name
            || current.inbox != candidate.inbox
            || current.listen_port != candidate.listen_port
            || current.encryption_enabled != candidate.encryption_enabled
            || current.inbox_auto_classify != candidate.inbox_auto_classify
            || current.inbox_category_dirs != candidate.inbox_category_dirs
            || current.manual_peers != candidate.manual_peers
            || transfer_change;
        let ssh_restart = current.cross_network.ssh != candidate.cross_network.ssh
            || private_key_change
            || passphrase_change;
        let pet_changed = current.show_pet != candidate.show_pet;

        let credential_update = (|| -> Result<()> {
            if transfer_change {
                let value = (!input.clear_secret).then_some(input.secret.as_str());
                write_credential(&transfer_secret, value, "保存端到端口令")?;
            }
            if private_key_change {
                let value = pasted_key_mode.then_some(input.ssh.pasted_key.as_str());
                write_credential(&private_key, value, "保存 SSH 私钥")?;
            }
            if passphrase_change {
                let value = (!input.ssh.clear_passphrase).then_some(input.ssh.passphrase.as_str());
                write_credential(&passphrase, value, "保存 SSH 私钥口令")?;
            }
            Ok(())
        })();
        if let Err(error) = credential_update {
            restore_credential(&transfer_secret, old_transfer.as_ref(), "端到端口令");
            restore_credential(&private_key, old_private_key.as_ref(), "SSH 私钥");
            restore_credential(&passphrase, old_passphrase.as_ref(), "SSH 私钥口令");
            if autostart_changed {
                restore_autostart(app, old_autostart);
            }
            return Err(error);
        }

        let running = {
            let mut runtime = self.runtime.lock().await;
            if runtime.config != current {
                drop(runtime);
                restore_credential(&transfer_secret, old_transfer.as_ref(), "端到端口令");
                restore_credential(&private_key, old_private_key.as_ref(), "SSH 私钥");
                restore_credential(&passphrase, old_passphrase.as_ref(), "SSH 私钥口令");
                if autostart_changed {
                    restore_autostart(app, old_autostart);
                }
                bail!("设置已被其他操作修改，请重新打开设置后再保存");
            }
            let running = runtime.session_id.is_some();
            if let Err(error) = self.store.save(&candidate) {
                drop(runtime);
                restore_credential(&transfer_secret, old_transfer.as_ref(), "端到端口令");
                restore_credential(&private_key, old_private_key.as_ref(), "SSH 私钥");
                restore_credential(&passphrase, old_passphrase.as_ref(), "SSH 私钥口令");
                if autostart_changed {
                    restore_autostart(app, old_autostart);
                }
                return Err(error);
            }
            runtime.config = candidate;
            running
        };

        if pet_changed {
            set_pet_window_visible(app, input.show_pet)?;
        }
        if running && lan_restart {
            self.stop(app).await?;
            self.start(app).await?;
        } else if running && ssh_restart {
            self.restart_ssh(app).await?;
        }
        Ok(Value::Bool(input.autostart))
    }

    async fn save_inbox_classification(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let enabled: bool = argument(args, 0, "enabled")?;
        let category_dirs: HashMap<String, String> =
            optional_argument(args, 1, "categoryDirs")?.unwrap_or_default();
        let restart = {
            let mut runtime = self.runtime.lock().await;
            let before_enabled = runtime.config.inbox_auto_classify;
            let before_dirs = runtime.config.inbox_category_dirs.clone();
            runtime.config.inbox_auto_classify = enabled;
            runtime.config.inbox_category_dirs = category_dirs.into_iter().collect();
            runtime.config.normalize()?;
            self.store.save(&runtime.config)?;
            runtime.session_id.is_some()
                && (before_enabled != runtime.config.inbox_auto_classify
                    || before_dirs != runtime.config.inbox_category_dirs)
        };
        if restart {
            self.stop(app).await?;
            self.start(app).await?;
        }
        Ok(Value::Null)
    }

    async fn manual_peers(&self) -> Result<Value> {
        serde_json::to_value(&self.runtime.lock().await.config.manual_peers).map_err(Into::into)
    }

    async fn save_manual_peers(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let peers: Vec<ManualPeerConfig> = optional_argument(args, 0, "peers")?.unwrap_or_default();
        let restart = {
            let mut runtime = self.runtime.lock().await;
            let before = runtime.config.manual_peers.clone();
            runtime.config.manual_peers = peers;
            runtime.config.normalize()?;
            self.store.save(&runtime.config)?;
            runtime.session_id.is_some() && before != runtime.config.manual_peers
        };
        if restart {
            self.stop(app).await?;
            self.start(app).await?;
        }
        Ok(Value::Null)
    }
}

impl DesktopState {
    async fn cross_network_config(&self) -> Result<Value> {
        let config = self.runtime.lock().await.config.cross_network.clone();
        let profile_id = config.ssh.profile.id.clone();
        let has_pasted_key = self
            .store
            .ssh_entry(&profile_id, "private-key")
            .is_ok_and(|entry| entry.get_password().is_ok_and(|value| !value.is_empty()));
        let has_passphrase = self
            .store
            .ssh_entry(&profile_id, "passphrase")
            .is_ok_and(|entry| entry.get_password().is_ok_and(|value| !value.is_empty()));
        let mut value = serde_json::to_value(&config)?;
        if let Some(ssh) = value.get_mut("ssh").and_then(Value::as_object_mut) {
            ssh.insert(
                "connected".into(),
                Value::Bool(self.runtime.lock().await.ssh_connected),
            );
            ssh.insert("hasPastedKey".into(), Value::Bool(has_pasted_key));
            ssh.insert("hasPassphrase".into(), Value::Bool(has_passphrase));
        }
        Ok(value)
    }

    async fn save_wormhole_config(&self, args: &[Value]) -> Result<Value> {
        let rendezvous_url: String = argument(args, 0, "rendezvousURL")?;
        let transit_relay: String = argument(args, 1, "transitRelay")?;
        let mut runtime = self.runtime.lock().await;
        runtime.config.cross_network.wormhole.rendezvous_url = rendezvous_url;
        runtime.config.cross_network.wormhole.transit_relay = transit_relay;
        runtime.config.normalize()?;
        self.store.save(&runtime.config)?;
        Ok(Value::Null)
    }

    async fn create_one_time(&self, args: &[Value]) -> Result<Value> {
        let paths: Vec<String> = optional_argument(args, 0, "paths")?.unwrap_or_default();
        let paths = normalize_send_paths(paths)?;
        let total = paths.len();
        let (lan_session_id, settings) = {
            let runtime = self.runtime.lock().await;
            (
                runtime.session_id.clone().context("墨洞未开启")?,
                wormhole_settings(&runtime.config),
            )
        };
        let result = call_core(
            &self.service,
            "wormhole.create",
            json!({
                "session_id": &lan_session_id,
                "paths": paths,
                "settings": settings,
            }),
        )
        .await?;
        let session_id = string_field(&result, "session_id")?;
        let mut runtime = self.runtime.lock().await;
        if runtime.session_id.as_deref() != Some(lan_session_id.as_str()) {
            drop(runtime);
            let _ = call_core(
                &self.service,
                "session.cancel",
                json!({ "session_id": session_id }),
            )
            .await;
            bail!("墨洞已停止");
        }
        runtime.pending_one_time.insert(
            session_id,
            PendingOneTime {
                batch_id: format!("send-{}", Uuid::new_v4().simple()),
                total,
            },
        );
        Ok(result)
    }

    async fn join_one_time(&self, args: &[Value]) -> Result<Value> {
        let code: String = argument(args, 0, "code")?;
        let code = code.trim();
        if code.is_empty() {
            bail!("请输入接收短码");
        }
        let (lan_session_id, settings) = {
            let runtime = self.runtime.lock().await;
            (
                runtime.session_id.clone().context("墨洞未开启")?,
                wormhole_settings(&runtime.config),
            )
        };
        let result = call_core(
            &self.service,
            "wormhole.join.start",
            json!({
                "session_id": &lan_session_id,
                "code": code,
                "settings": settings,
            }),
        )
        .await?;
        let session_id = string_field(&result, "session_id")?;
        if self.runtime.lock().await.session_id.as_deref() != Some(lan_session_id.as_str()) {
            let _ = call_core(
                &self.service,
                "session.cancel",
                json!({ "session_id": session_id }),
            )
            .await;
            bail!("墨洞已停止");
        }
        Ok(result)
    }

    async fn accept_one_time(&self, args: &[Value]) -> Result<Value> {
        self.one_time_command(args, "wormhole.accept", false).await
    }

    async fn reject_one_time(&self, args: &[Value]) -> Result<Value> {
        self.one_time_command(args, "wormhole.reject", false).await
    }

    async fn cancel_transport_session(&self, args: &[Value]) -> Result<Value> {
        self.one_time_command(args, "session.cancel", true).await
    }

    async fn one_time_command(
        &self,
        args: &[Value],
        method: &str,
        remove_pending: bool,
    ) -> Result<Value> {
        let session_id: String = argument(args, 0, "sessionID")?;
        let session_id = session_id.trim();
        if session_id.is_empty() {
            bail!("短码会话标识不能为空");
        }
        if remove_pending {
            self.runtime
                .lock()
                .await
                .pending_one_time
                .remove(session_id);
        }
        call_core(&self.service, method, json!({ "session_id": session_id })).await?;
        Ok(Value::Null)
    }

    async fn save_ssh_config(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let mut input: SshSettingsInput = argument(args, 0, "input")?;
        input.host = input.host.trim().to_owned();
        input.user = input.user.trim().to_owned();
        input.private_key_path = input.private_key_path.trim().to_owned();
        input.host_key_sha256 = input.host_key_sha256.trim().to_owned();
        if input.port == 0 {
            input.port = 22;
        }
        if input.private_key_mode != "paste" {
            input.private_key_mode = "file".into();
        }
        let pasted_key_mode = input.private_key_mode == "paste";
        input.peers = normalize_ssh_peers(input.peers)?;

        let profile_id = self
            .runtime
            .lock()
            .await
            .config
            .cross_network
            .ssh
            .profile
            .id
            .clone();
        let private_key = self.store.ssh_entry(&profile_id, "private-key")?;
        let passphrase = self.store.ssh_entry(&profile_id, "passphrase")?;
        if input.enabled {
            if input.host.is_empty() || input.user.is_empty() {
                bail!("SSH 主机和用户名不能为空");
            }
            if input.host_key_sha256.is_empty() {
                bail!("请先检测并确认 SSH 主机指纹");
            }
            if pasted_key_mode {
                let has_key = !input.pasted_key.is_empty()
                    || private_key
                        .get_password()
                        .is_ok_and(|value| !value.is_empty());
                if !has_key {
                    bail!("请粘贴 SSH 私钥");
                }
            } else if input.private_key_path.is_empty() {
                bail!("请选择 SSH 私钥文件");
            }
        }
        if !input.pasted_key.is_empty() {
            private_key
                .set_password(&input.pasted_key)
                .context("保存 SSH 私钥失败")?;
        }
        if input.clear_passphrase {
            match passphrase.delete_credential() {
                Ok(()) | Err(keyring::Error::NoEntry) => {}
                Err(error) => return Err(error).context("清除 SSH 私钥口令失败"),
            }
        } else if !input.passphrase.is_empty() {
            passphrase
                .set_password(&input.passphrase)
                .context("保存 SSH 私钥口令失败")?;
        }

        let mut runtime = self.runtime.lock().await;
        let restart = runtime.session_id.is_some();
        runtime.config.cross_network.ssh.enabled = input.enabled;
        runtime.config.cross_network.ssh.profile.host = input.host;
        runtime.config.cross_network.ssh.profile.port = input.port;
        runtime.config.cross_network.ssh.profile.user = input.user;
        runtime.config.cross_network.ssh.profile.private_key_mode = input.private_key_mode;
        runtime.config.cross_network.ssh.profile.private_key_path = input.private_key_path;
        runtime.config.cross_network.ssh.profile.private_key_label = if pasted_key_mode {
            "已存入系统安全存储".into()
        } else {
            String::new()
        };
        runtime.config.cross_network.ssh.profile.host_key_sha256 = input.host_key_sha256;
        runtime.config.cross_network.ssh.peers = input.peers;
        runtime.config.normalize()?;
        self.store.save(&runtime.config)?;
        drop(runtime);
        if restart {
            self.restart_ssh(app).await?;
        }
        Ok(Value::Null)
    }

    async fn remove_ssh_peer(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let instance_id: String = argument(args, 0, "instanceID")?;
        let mut runtime = self.runtime.lock().await;
        runtime
            .config
            .cross_network
            .ssh
            .peers
            .retain(|peer| peer.instance_id != instance_id);
        self.store.save(&runtime.config)?;
        let restart = runtime.session_id.is_some() && runtime.config.cross_network.ssh.enabled;
        drop(runtime);
        if restart {
            self.restart_ssh(app).await?;
        }
        Ok(Value::Null)
    }

    async fn check_ssh(&self, args: &[Value]) -> Result<Value> {
        let input: SshSettingsInput = argument(args, 0, "input")?;
        let profile_id = self
            .runtime
            .lock()
            .await
            .config
            .cross_network
            .ssh
            .profile
            .id
            .clone();
        let profile = self.ssh_profile_from_input(&profile_id, &input)?;
        call_core(&self.service, "ssh.check", json!({ "profile": profile })).await
    }

    async fn create_ssh_pairing(&self) -> Result<Value> {
        let session_id = self
            .runtime
            .lock()
            .await
            .ssh_session_id
            .clone()
            .context("SSH 中继尚未连接")?;
        call_core(
            &self.service,
            "ssh.pair.create",
            json!({ "session_id": session_id }),
        )
        .await
    }

    async fn join_ssh_pairing(&self, args: &[Value]) -> Result<Value> {
        let code: String = argument(args, 0, "code")?;
        let code = code.trim();
        if code.is_empty() {
            bail!("请输入 SSH 配对码");
        }
        let session_id = self
            .runtime
            .lock()
            .await
            .ssh_session_id
            .clone()
            .context("SSH 中继尚未连接")?;
        let result = call_core(
            &self.service,
            "ssh.pair.join",
            json!({ "session_id": session_id, "code": code }),
        )
        .await?;
        if let Some(peer) = result.get("peer") {
            let peer: SshPeerConfig = serde_json::from_value(peer.clone())?;
            self.remember_ssh_peer(peer).await?;
        }
        Ok(result)
    }

    fn ssh_profile_from_input(&self, profile_id: &str, input: &SshSettingsInput) -> Result<Value> {
        let private_key_entry = self.store.ssh_entry(profile_id, "private-key")?;
        let private_key =
            if input.private_key_mode == "paste" && !input.pasted_key.trim().is_empty() {
                input.pasted_key.clone()
            } else if input.private_key_mode != "paste" && !input.private_key_path.is_empty() {
                match fs::read_to_string(&input.private_key_path) {
                    Ok(value) => value,
                    Err(error) => {
                        // 文件被移动/删除时回退系统安全存储里已保存的私钥,
                        // 避免一次失效路径卡死整个保存/启动链。
                        match private_key_entry.get_password() {
                            Ok(stored) if !stored.trim().is_empty() => stored,
                            _ => {
                                return Err(error).with_context(|| {
                                    format!("无法读取 SSH 私钥文件: {}", input.private_key_path)
                                });
                            }
                        }
                    }
                }
            } else {
                private_key_entry
                    .get_password()
                    .context("无法读取已保存的 SSH 私钥")?
            };
        if private_key.trim().is_empty() {
            bail!("请提供 SSH 私钥");
        }
        let passphrase = if !input.passphrase.is_empty() {
            input.passphrase.clone()
        } else if input.clear_passphrase {
            String::new()
        } else {
            self.store
                .ssh_entry(profile_id, "passphrase")?
                .get_password()
                .unwrap_or_default()
        };
        Ok(json!({
            "id": profile_id,
            "host": input.host.trim(),
            "port": input.port,
            "user": input.user.trim(),
            "private_key": private_key,
            "private_key_label": input.private_key_path,
            "passphrase": passphrase,
            "host_key_sha256": input.host_key_sha256.trim(),
        }))
    }

    async fn start_ssh(&self, app: &AppHandle) -> Result<()> {
        let (generation, lan_session_id, input, remote_port, peers) = {
            let mut runtime = self.runtime.lock().await;
            let generation = runtime.invalidate_ssh();
            let profile = &runtime.config.cross_network.ssh.profile;
            (
                generation,
                runtime.session_id.clone().context("墨洞未开启")?,
                SshSettingsInput {
                    enabled: runtime.config.cross_network.ssh.enabled,
                    host: profile.host.clone(),
                    port: profile.port,
                    user: profile.user.clone(),
                    private_key_mode: profile.private_key_mode.clone(),
                    private_key_path: profile.private_key_path.clone(),
                    pasted_key: String::new(),
                    passphrase: String::new(),
                    clear_passphrase: false,
                    host_key_sha256: profile.host_key_sha256.clone(),
                    peers: runtime.config.cross_network.ssh.peers.clone(),
                },
                runtime.config.cross_network.ssh.remote_port,
                runtime.config.cross_network.ssh.peers.clone(),
            )
        };
        if !input.enabled {
            return Ok(());
        }
        let profile_id = self
            .runtime
            .lock()
            .await
            .config
            .cross_network
            .ssh
            .profile
            .id
            .clone();
        let profile = self.ssh_profile_from_input(&profile_id, &input)?;
        let result = call_core(
            &self.service,
            "ssh.listen",
            json!({
                "session_id": lan_session_id,
                "profile": profile,
                "remote_port": remote_port,
                "peers": peers,
            }),
        )
        .await?;
        let ssh_session_id = string_field(&result, "session_id")?;
        let saved_peers = result
            .get("peers")
            .cloned()
            .map(serde_json::from_value::<Vec<SshPeerConfig>>)
            .transpose()?
            .unwrap_or_default();
        {
            let mut runtime = self.runtime.lock().await;
            if runtime.ssh_generation != generation {
                // 期间发生了新的 restart/stop，这个会话已经过时：清理掉，避免核心侧泄漏。
                drop(runtime);
                let _ = call_core(
                    &self.service,
                    "session.cancel",
                    json!({ "session_id": ssh_session_id }),
                )
                .await;
                return Ok(());
            }
            runtime.ssh_session_id = Some(ssh_session_id);
            runtime.ssh_live = true;
            runtime.ssh_connected = result
                .get("connected")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if let Some(port) = result.get("remote_port").and_then(Value::as_u64) {
                runtime.config.cross_network.ssh.remote_port = u16::try_from(port).unwrap_or(0);
            }
            if !saved_peers.is_empty() || result.get("peers").is_some() {
                runtime.config.cross_network.ssh.peers = saved_peers;
            }
            runtime.config.normalize()?;
            self.store.save(&runtime.config)?;
        }
        let _ = app.emit("transport", json!({ "event": "ssh.ready", "data": result }));
        self.refresh_discovery(app).await?;
        Ok(())
    }

    async fn restart_ssh(&self, app: &AppHandle) -> Result<()> {
        let (old_session, enabled, running) = {
            let mut runtime = self.runtime.lock().await;
            runtime.invalidate_ssh();
            (
                runtime.ssh_session_id.take(),
                runtime.config.cross_network.ssh.enabled,
                runtime.session_id.is_some(),
            )
        };
        if let Some(old_session) = old_session {
            let _ = call_core(
                &self.service,
                "session.cancel",
                json!({ "session_id": old_session }),
            )
            .await;
        }
        if enabled && running {
            self.start_ssh(app).await?;
        } else {
            self.refresh_discovery(app).await?;
        }
        Ok(())
    }

    async fn remember_ssh_peer(&self, peer: SshPeerConfig) -> Result<()> {
        let mut runtime = self.runtime.lock().await;
        runtime
            .config
            .cross_network
            .ssh
            .peers
            .retain(|value| value.instance_id != peer.instance_id);
        runtime.config.cross_network.ssh.peers.push(peer);
        runtime.config.normalize()?;
        self.store.save(&runtime.config)?;
        Ok(())
    }

    async fn peers(&self) -> Result<Vec<PeerView>> {
        let session_id = self
            .runtime
            .lock()
            .await
            .session_id
            .clone()
            .context("墨洞未开启")?;
        let response = call_core(
            &self.service,
            "lan.peers",
            json!({ "session_id": session_id }),
        )
        .await?;
        let peers = decode_peers(&response)?;
        self.runtime.lock().await.peers = peers.clone();
        Ok(peers)
    }

    async fn refresh_discovery(&self, app: &AppHandle) -> Result<()> {
        let Some(session_id) = self.runtime.lock().await.session_id.clone() else {
            return Ok(());
        };
        let response = call_core(
            &self.service,
            "lan.refresh",
            json!({ "session_id": session_id }),
        )
        .await?;
        let peers = decode_peers(&response)?;
        self.runtime.lock().await.peers = peers.clone();
        let _ = app.emit("peers", peers);
        Ok(())
    }

    async fn select_peer(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let instance_id: String = argument(args, 0, "instanceID")?;
        let instance_id = instance_id.trim().to_ascii_lowercase();
        if !instance_id.is_empty()
            && (instance_id.len() != 32
                || !instance_id.bytes().all(|byte| byte.is_ascii_hexdigit()))
        {
            bail!("目标设备标识无效");
        }
        self.runtime.lock().await.selected = instance_id.clone();
        let _ = app.emit("selection", json!({ "instanceId": instance_id }));
        Ok(Value::Null)
    }

    async fn send_paths(
        &self,
        app: &AppHandle,
        args: &[Value],
        selected_only: bool,
    ) -> Result<Value> {
        let (requested_instance_id, paths_index) = if selected_only {
            (String::new(), 0)
        } else {
            (argument(args, 0, "instanceID")?, 1)
        };
        let paths: Vec<String> = optional_argument(args, paths_index, "paths")?.unwrap_or_default();
        let paths = normalize_send_paths(paths)?;
        let (session_id, target, batch_id) = {
            let mut runtime = self.runtime.lock().await;
            let session_id = runtime.session_id.clone().context("墨洞未开启")?;
            let mut instance_id = requested_instance_id.trim().to_ascii_lowercase();
            if instance_id.is_empty() {
                instance_id.clone_from(&runtime.selected);
            }
            if instance_id.is_empty() && runtime.peers.len() == 1 {
                instance_id.clone_from(&runtime.peers[0].instance_id);
                runtime.selected.clone_from(&instance_id);
            }
            let target = runtime
                .peers
                .iter()
                .find(|peer| peer.instance_id == instance_id)
                .cloned()
                .context("目标设备不在线")?;
            let batch_id = format!("send-{}", Uuid::new_v4().simple());
            runtime.batches.insert(
                batch_id.clone(),
                SendBatch {
                    total: paths.len(),
                    settled: 0,
                    succeeded: 0,
                    core_send_ids: Vec::new(),
                },
            );
            (session_id, target, batch_id)
        };
        let _ = app.emit(
            "transfer-start",
            json!({ "sendId": batch_id, "total": paths.len() }),
        );

        for path in paths {
            let params = json!({
                "session_id": &session_id,
                "path": &path,
                "instance_id": &target.instance_id,
                "host": &target.host,
                "port": target.port,
                "fingerprint": &target.fingerprint,
            });
            match call_core(&self.service, "lan.send", params).await {
                Ok(response) => {
                    let core_send_id = string_field(&response, "send_id")?;
                    let orphan = {
                        let mut runtime = self.runtime.lock().await;
                        runtime
                            .core_to_batch
                            .insert(core_send_id.clone(), batch_id.clone());
                        if let Some(batch) = runtime.batches.get_mut(&batch_id) {
                            batch.core_send_ids.push(core_send_id.clone());
                        }
                        runtime.orphan_sent.remove(&core_send_id)
                    };
                    if let Some(data) = orphan {
                        self.handle_sent(app, data).await;
                    }
                }
                Err(error) => {
                    let payload = json!({
                        "sendId": batch_id,
                        "path": path,
                        "name": path_basename(&path),
                        "ok": false,
                        "error": error.to_string(),
                    });
                    let _ = app.emit("sent", payload);
                    self.settle_batch(app, &batch_id, false).await;
                }
            }
        }
        Ok(Value::String(batch_id))
    }

    async fn cancel_send(&self, args: &[Value]) -> Result<Value> {
        let batch_id: String = argument(args, 0, "sendID")?;
        let (session_id, core_send_ids) = {
            let runtime = self.runtime.lock().await;
            let Some(batch) = runtime.batches.get(&batch_id) else {
                return Ok(Value::Bool(false));
            };
            (
                runtime.session_id.clone().context("墨洞未开启")?,
                batch.core_send_ids.clone(),
            )
        };
        let mut cancelled = false;
        // 批次里任一取消失败都不能中断循环，否则后面的传输会继续跑而用户以为已经停了。
        let mut failures = Vec::new();
        for core_send_id in core_send_ids {
            let response = call_core(
                &self.service,
                "lan.send.cancel",
                json!({
                    "session_id": session_id,
                    "send_id": core_send_id,
                }),
            )
            .await;
            match response {
                Ok(response) => {
                    cancelled |= response
                        .get("cancelled")
                        .and_then(Value::as_bool)
                        .unwrap_or(false);
                }
                Err(error) => failures.push(format!("{core_send_id}: {error}")),
            }
        }
        if !failures.is_empty() {
            bail!("部分传输取消失败（{}）", failures.join("；"));
        }
        Ok(Value::Bool(cancelled))
    }
}

impl DesktopState {
    async fn recent_files(&self) -> Vec<String> {
        self.runtime
            .lock()
            .await
            .config
            .recent_files
            .iter()
            .filter(|path| Path::new(path).exists())
            .cloned()
            .collect()
    }

    async fn clear_recent(&self, app: &AppHandle) -> Result<Value> {
        {
            let mut runtime = self.runtime.lock().await;
            runtime.config.recent_files.clear();
            self.store.save(&runtime.config)?;
        }
        let _ = app.emit("recent", Vec::<String>::new());
        Ok(Value::Null)
    }

    async fn open_inbox(&self, app: &AppHandle) -> Result<Value> {
        let inbox = self.runtime.lock().await.config.inbox.clone();
        fs::create_dir_all(&inbox).with_context(|| format!("无法创建收件箱 {inbox}"))?;
        open_path(app, &inbox)?;
        Ok(Value::Null)
    }

    fn set_autostart(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let enabled: bool = argument(args, 0, "enabled")?;
        Ok(Value::Bool(apply_autostart(app, enabled)?))
    }

    async fn set_pet_visible(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let visible: bool = argument(args, 0, "visible")?;
        set_pet_window_visible(app, visible)?;
        Ok(Value::Null)
    }

    async fn disable_pet(&self, app: &AppHandle) -> Result<Value> {
        {
            let mut runtime = self.runtime.lock().await;
            runtime.config.show_pet = false;
            self.store.save(&runtime.config)?;
        }
        set_pet_window_visible(app, false)?;
        Ok(Value::Null)
    }

    async fn move_pet_to(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let target_x: i32 = argument(args, 0, "x")?;
        let target_y: i32 = argument(args, 1, "y")?;
        let duration_ms: u64 = argument(args, 2, "durationMS")?;
        let window = app
            .get_webview_window("pet")
            .context("pet window is unavailable")?;
        let start = window.outer_position()?;
        let generation = self.pet_motion.fetch_add(1, Ordering::SeqCst) + 1;
        let duration_ms = duration_ms.clamp(1, 2_000);
        let started = tokio::time::Instant::now();
        let duration = Duration::from_millis(duration_ms);
        loop {
            if self.pet_motion.load(Ordering::SeqCst) != generation {
                return Ok(Value::Null);
            }
            let progress = (started.elapsed().as_secs_f64() / duration.as_secs_f64()).min(1.0);
            let eased = 1.0 - (1.0 - progress).powi(3);
            let x = start.x as f64 + (target_x - start.x) as f64 * eased;
            let y = start.y as f64 + (target_y - start.y) as f64 * eased;
            window.set_position(Position::Physical(PhysicalPosition::new(
                x.round() as i32,
                y.round() as i32,
            )))?;
            if progress >= 1.0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(12)).await;
        }
        Ok(Value::Null)
    }

    async fn open_pet_menu(&self, app: &AppHandle, args: &[Value]) -> Result<Value> {
        let x: i32 = argument(args, 0, "x")?;
        let y: i32 = argument(args, 1, "y")?;
        let (peers, selected) = {
            let runtime = self.runtime.lock().await;
            (runtime.peers.clone(), runtime.selected.clone())
        };
        let mut menu = MenuBuilder::new(app);
        if peers.is_empty() {
            menu = menu.text("pet-empty", "（等待发现设备…）");
        } else {
            for peer in peers {
                let marker = if peer.instance_id == selected {
                    "●"
                } else {
                    "○"
                };
                let suffix = peer.instance_id.chars().take(8).collect::<String>();
                let label = format!("{marker} {}-{suffix}", peer.name);
                let item =
                    MenuItemBuilder::with_id(format!("pet-select:{}", peer.instance_id), label)
                        .build(app)?;
                menu = menu.item(&item);
            }
        }
        let menu = menu
            .separator()
            .text("pet-main", "打开主界面")
            .text("pet-inbox", "打开收件箱")
            .separator()
            .text("pet-disable", "关闭桌宠")
            .text("app-quit", "退出程序")
            .build()?;
        app.get_webview_window("pet")
            .context("pet window is unavailable")?
            .popup_menu_at(&menu, Position::Physical(PhysicalPosition::new(x, y)))?;
        Ok(Value::Null)
    }

    async fn handle_core_event(&self, app: &AppHandle, event: ServiceEvent) {
        if event.event.starts_with("wormhole.") {
            self.handle_wormhole_event(app, event).await;
            return;
        }
        if event.event.starts_with("ssh.") {
            self.handle_ssh_event(app, event).await;
            return;
        }
        let event_session = event
            .data
            .get("session_id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let current_session = self.runtime.lock().await.session_id.clone();
        if current_session.as_deref() != Some(event_session) {
            return;
        }
        match event.event.as_str() {
            "lan.peers" => {
                if let Ok(peers) = decode_peers(&event.data) {
                    self.runtime.lock().await.peers = peers.clone();
                    let _ = app.emit("peers", peers);
                }
            }
            "lan.status" => {
                if let Some(message) = event.data.get("message").and_then(Value::as_str) {
                    let _ = app.emit("status", message);
                }
            }
            "lan.progress" => {
                let kind = event
                    .data
                    .get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or("send");
                let mut payload = json!({
                    "kind": kind,
                    "filename": event.data.get("filename").cloned().unwrap_or(Value::Null),
                    "done": event.data.get("done").cloned().unwrap_or(Value::from(0)),
                    "total": event.data.get("total").cloned().unwrap_or(Value::from(0)),
                    "transferId": event.data.get("transfer_id").cloned().unwrap_or(Value::Null),
                });
                if kind == "send"
                    && let Some(core_send_id) = event.data.get("send_id").and_then(Value::as_str)
                {
                    let batch_id = self
                        .runtime
                        .lock()
                        .await
                        .core_to_batch
                        .get(core_send_id)
                        .cloned()
                        .unwrap_or_else(|| core_send_id.to_owned());
                    payload["sendId"] = Value::String(batch_id);
                }
                let _ = app.emit("progress", payload);
            }
            "lan.sent" => self.handle_sent(app, event.data).await,
            "lan.received" => self.handle_received(app, event.data).await,
            _ => {}
        }
    }

    async fn handle_ssh_event(&self, app: &AppHandle, event: ServiceEvent) {
        // 核心事件不带会话标识，丢弃属于已作废代际（重启中或已停止）的 ssh.* 事件，
        // 否则旧会话的 connected/disconnected 会覆盖新会话的状态。
        if !self.runtime.lock().await.ssh_live {
            return;
        }
        match event.event.as_str() {
            "ssh.connected" => {
                self.runtime.lock().await.ssh_connected = true;
                let _ = app.emit("status", "SSH 中继已连接");
                let _ = self.refresh_discovery(app).await;
            }
            "ssh.disconnected" => {
                self.runtime.lock().await.ssh_connected = false;
                let _ = app.emit("status", "SSH 中继暂时不可用，正在自动重连");
                let _ = self.refresh_discovery(app).await;
            }
            "ssh.reconnect.error" | "ssh.channel.error" => {
                if let Some(error) = event.data.get("error").and_then(Value::as_str) {
                    let _ = app.emit("status", format!("SSH 中继：{error}"));
                }
            }
            "ssh.paired" => {
                if let Some(value) = event.data.get("peer").cloned()
                    && let Ok(peer) = serde_json::from_value::<SshPeerConfig>(value)
                {
                    let _ = self.remember_ssh_peer(peer).await;
                    let _ = self.refresh_discovery(app).await;
                }
            }
            _ => {}
        }
        let _ = app.emit(
            "transport",
            json!({ "event": event.event, "data": event.data }),
        );
    }

    async fn handle_wormhole_event(&self, app: &AppHandle, event: ServiceEvent) {
        let session_id = event
            .data
            .get("session_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        let mut orphan_sent = Vec::new();
        if event.event == "wormhole.ready"
            && event.data.get("role").and_then(Value::as_str) == Some("sender")
        {
            let started = match string_array_field(&event.data, "send_ids") {
                Ok(send_ids) => self
                    .runtime
                    .lock()
                    .await
                    .start_one_time(&session_id, send_ids),
                Err(error) => Err(error),
            };
            match started {
                Ok(started) => {
                    let _ = app.emit(
                        "transfer-start",
                        json!({ "sendId": started.batch_id, "total": started.total }),
                    );
                    orphan_sent = started.orphan_sent;
                }
                Err(error) => {
                    self.runtime
                        .lock()
                        .await
                        .pending_one_time
                        .remove(&session_id);
                    let _ = call_core(
                        &self.service,
                        "session.cancel",
                        json!({ "session_id": session_id }),
                    )
                    .await;
                    let _ = app.emit(
                        "transport",
                        json!({
                            "event": "wormhole.error",
                            "data": {
                                "session_id": session_id,
                                "error": error.to_string(),
                            },
                        }),
                    );
                    return;
                }
            }
        } else if event.event == "wormhole.error" {
            self.runtime
                .lock()
                .await
                .pending_one_time
                .remove(&session_id);
        }

        let _ = app.emit(
            "transport",
            json!({ "event": event.event, "data": event.data }),
        );
        for event in orphan_sent {
            self.handle_sent(app, event).await;
        }
    }
}

impl DesktopState {
    async fn handle_sent(&self, app: &AppHandle, data: Value) {
        let core_send_id = data
            .get("send_id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let batch_id = {
            let mut runtime = self.runtime.lock().await;
            let batch_id = runtime.core_to_batch.remove(core_send_id);
            if batch_id.is_none() && !core_send_id.is_empty() {
                runtime
                    .orphan_sent
                    .insert(core_send_id.to_owned(), data.clone());
            }
            batch_id
        };
        let Some(batch_id) = batch_id else {
            return;
        };
        let path = data.get("path").and_then(Value::as_str).unwrap_or_default();
        let ok = data.get("ok").and_then(Value::as_bool).unwrap_or(false);
        let payload = json!({
            "sendId": batch_id,
            "path": path,
            "name": path_basename(path),
            "ok": ok,
            "error": data.get("error").cloned().unwrap_or(Value::Null),
            "transferId": data.get("transfer_id").cloned().unwrap_or(Value::Null),
        });
        let _ = app.emit("sent", payload);
        self.settle_batch(app, &batch_id, ok).await;
    }

    async fn settle_batch(&self, app: &AppHandle, batch_id: &str, succeeded: bool) {
        let finished = {
            let mut runtime = self.runtime.lock().await;
            let Some(batch) = runtime.batches.get_mut(batch_id) else {
                return;
            };
            batch.settled += 1;
            if succeeded {
                batch.succeeded += 1;
            }
            if batch.settled == batch.total {
                let payload = (batch.succeeded, batch.total);
                runtime.batches.remove(batch_id);
                Some(payload)
            } else {
                None
            }
        };
        if let Some((succeeded, total)) = finished {
            let _ = app.emit(
                "transfer-finished",
                json!({
                    "sendId": batch_id,
                    "succeeded": succeeded,
                    "total": total,
                }),
            );
        }
    }

    async fn handle_received(&self, app: &AppHandle, data: Value) {
        let Some(path) = data.get("path").and_then(Value::as_str) else {
            return;
        };
        let recent = {
            let mut runtime = self.runtime.lock().await;
            runtime.config.recent_files.retain(|value| value != path);
            runtime.config.recent_files.insert(0, path.to_owned());
            runtime.config.recent_files.truncate(MAXIMUM_RECENT_FILES);
            let recent = runtime.config.recent_files.clone();
            if let Err(error) = self.store.save(&runtime.config) {
                let _ = app.emit("status", format!("保存接收记录失败: {error}"));
            }
            recent
        };
        let payload = json!({
            "path": path,
            "name": data
                .get("filename")
                .cloned()
                .unwrap_or_else(|| Value::String(path_basename(path))),
            "size": data.get("size").cloned().unwrap_or(Value::from(0)),
            "blake3": data.get("blake3").cloned().unwrap_or(Value::Null),
            "senderInstanceId": data
                .get("sender_instance_id")
                .cloned()
                .unwrap_or(Value::Null),
            "senderName": data.get("sender_name").cloned().unwrap_or(Value::Null),
        });
        let _ = app.emit("received", payload);
        let _ = app.emit("recent", recent);
    }

    fn active_secret(&self, config: &DesktopConfig) -> Result<String> {
        if !config.encryption_enabled {
            return Ok(String::new());
        }
        let secret = self
            .store
            .secret_entry(&config.instance_id)?
            .get_password()
            .context("无法读取端到端口令；为避免明文传输，墨洞未启动")?;
        if secret.is_empty() {
            bail!("已启用端到端加密，但没有已保存的口令");
        }
        Ok(secret)
    }

    fn secret_state(&self, config: &DesktopConfig) -> (bool, String) {
        match self.store.secret_entry(&config.instance_id) {
            Ok(entry) => match entry.get_password() {
                Ok(value) => (!value.is_empty(), String::new()),
                Err(keyring::Error::NoEntry) => (false, String::new()),
                Err(error) => (false, format!("系统凭据存储不可用: {error}")),
            },
            Err(error) => (false, error.to_string()),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct DesktopSettingsInput {
    peer_name: String,
    inbox: String,
    #[serde(default)]
    secret: String,
    #[serde(default)]
    clear_secret: bool,
    port: u16,
    show_pet: bool,
    autostart: bool,
    encryption_enabled: bool,
    auto_classify: bool,
    #[serde(default)]
    category_dirs: HashMap<String, String>,
    #[serde(default)]
    manual_peers: Vec<ManualPeerConfig>,
    wormhole_rendezvous: String,
    wormhole_relay: String,
    ssh: SshSettingsInput,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SshSettingsInput {
    enabled: bool,
    host: String,
    port: u16,
    user: String,
    private_key_mode: String,
    private_key_path: String,
    #[serde(default)]
    pasted_key: String,
    #[serde(default)]
    passphrase: String,
    #[serde(default)]
    clear_passphrase: bool,
    // 前端沿用 Wails 生成的绑定名 hostKeySHA256，rename_all 的 camelCase
    // 会推导成 hostKeySha256，必须显式指定才能与既有构建产物兼容。
    #[serde(rename = "hostKeySHA256")]
    host_key_sha256: String,
    #[serde(default)]
    peers: Vec<SshPeerConfig>,
}

fn credential_value(entry: &keyring::Entry, action: &str) -> Result<Option<String>> {
    match entry.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(error) => Err(error).with_context(|| action.to_owned()),
    }
}

fn write_credential(entry: &keyring::Entry, value: Option<&str>, action: &str) -> Result<()> {
    match value {
        Some(value) => entry.set_password(value).with_context(|| action.to_owned()),
        None => match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => Err(error).with_context(|| action.to_owned()),
        },
    }
}

fn restore_credential(entry: &keyring::Entry, previous: Option<&Option<String>>, label: &str) {
    let Some(previous) = previous else {
        return;
    };
    if let Err(error) = write_credential(entry, previous.as_deref(), "恢复旧凭据") {
        tracing::error!(%error, credential = label, "failed to roll back credential update");
    }
}

fn apply_autostart(app: &AppHandle, enabled: bool) -> Result<bool> {
    if enabled {
        app.autolaunch().enable()?;
    } else {
        app.autolaunch().disable()?;
    }
    Ok(app.autolaunch().is_enabled()?)
}

fn restore_autostart(app: &AppHandle, enabled: bool) {
    if let Err(error) = apply_autostart(app, enabled) {
        tracing::error!(%error, enabled, "failed to roll back autostart update");
    }
}

fn normalize_ssh_peers(values: Vec<SshPeerConfig>) -> Result<Vec<SshPeerConfig>> {
    let mut peers = Vec::with_capacity(values.len());
    let mut seen = HashSet::with_capacity(values.len());
    for mut peer in values {
        peer.id = peer.id.trim().to_owned();
        peer.name = peer.name.trim().to_owned();
        peer.instance_id = peer.instance_id.trim().to_owned();
        peer.noise_public = peer.noise_public.trim().to_owned();
        if peer.instance_id.is_empty() || peer.remote_port == 0 || peer.noise_public.is_empty() {
            bail!("SSH 配对设备配置无效");
        }
        if seen.insert(peer.instance_id.clone()) {
            peers.push(peer);
        }
    }
    Ok(peers)
}

fn wormhole_settings(config: &DesktopConfig) -> Value {
    json!({
        "rendezvous_url": config.cross_network.wormhole.rendezvous_url,
        "transit_relay": config.cross_network.wormhole.transit_relay,
        "timeout_minutes": 10,
    })
}

async fn call_core(service: &JsonService, method: &str, params: Value) -> Result<Value> {
    let id = Uuid::new_v4().simple().to_string();
    let request = serde_json::to_string(&json!({
        "id": id,
        "method": method,
        "params": params,
    }))?;
    let response = service.call_json(&request).await;
    let response: CoreResponse = serde_json::from_str(&response)?;
    if response.id != id {
        bail!("核心响应标识不匹配");
    }
    if !response.ok {
        bail!(response.error.unwrap_or_else(|| "核心调用失败".into()));
    }
    Ok(response.result.unwrap_or(Value::Null))
}

#[derive(Debug, Deserialize)]
struct CoreResponse {
    id: String,
    ok: bool,
    #[serde(default)]
    result: Option<Value>,
    #[serde(default)]
    error: Option<String>,
}

fn decode_peers(value: &Value) -> Result<Vec<PeerView>> {
    let mut peers = serde_json::from_value::<Vec<LanPeer>>(
        value
            .get("peers")
            .cloned()
            .context("核心设备列表缺少 peers")?,
    )?
    .into_iter()
    .map(PeerView::from)
    .collect::<Vec<_>>();
    peers.sort_by(|left, right| {
        left.name
            .cmp(&right.name)
            .then_with(|| left.instance_id.cmp(&right.instance_id))
    });
    Ok(peers)
}

fn argument<T: DeserializeOwned>(args: &[Value], index: usize, name: &str) -> Result<T> {
    let value = args
        .get(index)
        .cloned()
        .ok_or_else(|| anyhow!("missing argument: {name}"))?;
    serde_json::from_value(value).with_context(|| format!("invalid argument: {name}"))
}

fn optional_argument<T: DeserializeOwned>(
    args: &[Value],
    index: usize,
    name: &str,
) -> Result<Option<T>> {
    match args.get(index) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => serde_json::from_value(value.clone())
            .map(Some)
            .with_context(|| format!("invalid argument: {name}")),
    }
}

fn string_field(value: &Value, field: &str) -> Result<String> {
    value
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| anyhow!("核心响应缺少 {field}"))
}

fn string_array_field(value: &Value, field: &str) -> Result<Vec<String>> {
    value
        .get(field)
        .and_then(Value::as_array)
        .with_context(|| format!("核心响应缺少 {field}"))?
        .iter()
        .map(|value| {
            value
                .as_str()
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
                .with_context(|| format!("核心响应的 {field} 无效"))
        })
        .collect()
}

fn u16_field(value: &Value, field: &str) -> Result<u16> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .and_then(|value| u16::try_from(value).ok())
        .ok_or_else(|| anyhow!("核心响应缺少 {field}"))
}

fn normalize_send_paths(paths: Vec<String>) -> Result<Vec<String>> {
    if paths.is_empty() {
        bail!("没有选择要发送的内容");
    }
    if paths.len() > MAXIMUM_SEND_PATHS {
        bail!("单次最多发送 {MAXIMUM_SEND_PATHS} 项");
    }

    let mut normalized = Vec::with_capacity(paths.len());
    let mut seen = HashSet::with_capacity(paths.len());
    for value in paths {
        let value = value.trim();
        if value.is_empty() {
            continue;
        }
        let path = fs::canonicalize(value)
            .with_context(|| format!("无法读取 {}", path_basename(value)))?;
        let metadata =
            fs::metadata(&path).with_context(|| format!("无法读取 {}", path.display()))?;
        if !metadata.is_file() && !metadata.is_dir() {
            bail!("不支持发送 {}", path.display());
        }
        let path = path
            .into_os_string()
            .into_string()
            .map_err(|_| anyhow!("路径不是有效的 Unicode 文本"))?;
        if seen.insert(path.clone()) {
            normalized.push(path);
        }
    }
    if normalized.is_empty() {
        bail!("没有可发送的内容");
    }
    Ok(normalized)
}

async fn choose_files(app: &AppHandle) -> Result<Vec<String>> {
    let (sender, receiver) = oneshot::channel();
    app.dialog()
        .file()
        .set_title("选择要发送的文件")
        .pick_files(move |paths| {
            let _ = sender.send(paths);
        });
    receiver
        .await
        .context("文件选择窗口意外关闭")?
        .unwrap_or_default()
        .into_iter()
        .map(file_path_string)
        .collect()
}

async fn choose_folder(app: &AppHandle, initial: Option<String>) -> Result<String> {
    let mut dialog = app.dialog().file().set_title("选择文件夹");
    if let Some(initial) = initial.filter(|value| !value.trim().is_empty()) {
        dialog = dialog.set_directory(initial);
    }
    let (sender, receiver) = oneshot::channel();
    dialog.pick_folder(move |path| {
        let _ = sender.send(path);
    });
    receiver
        .await
        .context("文件夹选择窗口意外关闭")?
        .map(file_path_string)
        .transpose()
        .map(Option::unwrap_or_default)
}

fn file_path_string(path: FilePath) -> Result<String> {
    match path {
        FilePath::Path(path) => path
            .into_os_string()
            .into_string()
            .map_err(|_| anyhow!("选择的路径不是有效的 Unicode 文本")),
        FilePath::Url(url) => url
            .to_file_path()
            .map_err(|()| anyhow!("选择的地址不是本地文件路径"))?
            .into_os_string()
            .into_string()
            .map_err(|_| anyhow!("选择的路径不是有效的 Unicode 文本")),
    }
}

fn open_path(app: &AppHandle, value: &str) -> Result<()> {
    let path =
        fs::canonicalize(value).with_context(|| format!("无法打开 {}", path_basename(value)))?;
    let path = path
        .into_os_string()
        .into_string()
        .map_err(|_| anyhow!("路径不是有效的 Unicode 文本"))?;
    app.opener()
        .open_path(path, None::<&str>)
        .context("无法调用系统程序打开路径")
}

/// 查询 GitHub 最新 Release 与当前版本比较。走系统代理(reqwest 默认读环境),
/// 8 秒超时;失败返回可读中文错误由前端 toast 呈现。
async fn check_for_update() -> Result<Value> {
    #[derive(serde::Deserialize)]
    struct LatestRelease {
        tag_name: String,
    }
    let current = env!("CARGO_PKG_VERSION");
    let client = reqwest::Client::builder()
        .user_agent(concat!("InkHole/", env!("CARGO_PKG_VERSION")))
        .timeout(Duration::from_secs(8))
        .build()
        .context("初始化更新检查客户端失败")?;
    let release: LatestRelease = client
        .get("https://api.github.com/repos/RexVane/InkHole/releases/latest")
        .send()
        .await
        .context("无法连接 GitHub 检查更新")?
        .error_for_status()
        .context("GitHub 更新接口返回错误")?
        .json()
        .await
        .context("解析 GitHub 更新信息失败")?;
    let latest = release.tag_name.trim().trim_start_matches('v').to_owned();
    let tag = release.tag_name.trim();
    let download_url = platform_update_asset_url(tag);
    Ok(json!({
        "current": current,
        "latest": latest,
        "available": version_is_newer(&latest, current),
        "downloadUrl": download_url,
    }))
}

/// 当前平台应用内更新要下载的资产地址(Windows 裸安装器 / macOS dmg);
/// 其余平台返回空串,前端回退到打开发布页。
fn platform_update_asset_url(tag: &str) -> String {
    let base = format!("https://github.com/RexVane/InkHole/releases/download/{tag}");
    if cfg!(target_os = "windows") {
        format!("{base}/InkHole-{tag}-setup.exe")
    } else if cfg!(target_os = "macos") {
        format!("{base}/InkHole-{tag}-macos.dmg")
    } else {
        String::new()
    }
}

/// 应用内更新:流式下载到临时目录并按平台拉起安装。
/// Windows 运行 NSIS 安装器后退出应用;macOS 打开 dmg 由用户完成替换。
async fn download_and_launch_update(app: &AppHandle, url: &str) -> Result<Value> {
    if !url.starts_with("https://github.com/RexVane/InkHole/releases/download/") {
        bail!("更新地址不在允许范围");
    }
    let file_name = url.rsplit('/').next().unwrap_or("InkHole-update.bin");
    if file_name.is_empty()
        || !file_name.starts_with("InkHole-")
        || file_name.contains(['/', '\\', '?', '#'])
    {
        bail!("invalid update file name");
    }
    let target_dir = std::env::temp_dir().join("inkhole-update");
    tokio::fs::create_dir_all(&target_dir)
        .await
        .context("无法创建更新缓存目录")?;
    let target_path = target_dir.join(file_name);

    let client = reqwest::Client::builder()
        .user_agent(concat!("InkHole/", env!("CARGO_PKG_VERSION")))
        .build()
        .context("初始化下载客户端失败")?;
    let checksum_url = format!("{url}.sha256");
    let checksum_text = client
        .get(&checksum_url)
        .send()
        .await
        .context("failed to download update checksum")?
        .error_for_status()
        .context("update checksum request failed")?
        .text()
        .await
        .context("failed to read update checksum")?;
    let expected_checksum = checksum_text
        .split_whitespace()
        .next()
        .filter(|value| value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .map(str::to_ascii_lowercase)
        .context("invalid update checksum format")?;

    let response = client
        .get(url)
        .send()
        .await
        .context("无法连接更新下载服务器")?
        .error_for_status()
        .context("更新下载服务器返回错误")?;
    let total = response.content_length().unwrap_or(0);
    const MAX_UPDATE_SIZE: u64 = 250 * 1024 * 1024;
    if total > MAX_UPDATE_SIZE {
        bail!("更新包大小异常");
    }
    let mut file = tokio::fs::File::create(&target_path)
        .await
        .context("无法写入更新缓存文件")?;
    let mut stream = response;
    let mut hasher = Sha256::new();
    let mut done: u64 = 0;
    let mut last_percent: i64 = -1;
    while let Some(chunk) = stream.chunk().await.context("下载更新数据中断")? {
        done += chunk.len() as u64;
        if done > MAX_UPDATE_SIZE {
            bail!("更新包大小异常");
        }
        hasher.update(&chunk);
        tokio::io::AsyncWriteExt::write_all(&mut file, &chunk)
            .await
            .context("写入更新缓存失败")?;
        if total > 0 {
            let percent = (done * 100 / total) as i64;
            if percent != last_percent {
                last_percent = percent;
                let _ = app.emit("update-progress", json!({ "percent": percent }));
            }
        }
    }
    tokio::io::AsyncWriteExt::flush(&mut file)
        .await
        .context("写入更新缓存失败")?;
    drop(file);
    let actual_checksum = hex::encode(hasher.finalize());
    if actual_checksum != expected_checksum {
        let _ = tokio::fs::remove_file(&target_path).await;
        bail!("update SHA-256 verification failed");
    }

    #[cfg(target_os = "windows")]
    {
        std::process::Command::new(&target_path)
            .spawn()
            .context("无法启动更新安装程序")?;
        // 给安装器让路:主动走既有退出流程(Stop 清理由 ExitRequested 处理)。
        let app = app.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(600)).await;
            app.exit(0);
        });
        Ok(json!({ "launched": true, "restart": true }))
    }
    #[cfg(target_os = "macos")]
    {
        app.opener()
            .open_path(target_path.to_string_lossy().to_string(), None::<&str>)
            .context("无法打开更新镜像")?;
        return Ok(json!({ "launched": true, "restart": false }));
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        let _ = target_path;
        bail!("当前平台暂不支持应用内更新");
    }
}

/// 按点分数字逐段比较,latest 严格大于 current 才算有新版。
fn version_is_newer(latest: &str, current: &str) -> bool {
    let parse = |value: &str| {
        value
            .split('.')
            .map(|part| {
                part.trim()
                    .chars()
                    .take_while(char::is_ascii_digit)
                    .collect::<String>()
                    .parse::<u64>()
                    .unwrap_or(0)
            })
            .collect::<Vec<_>>()
    };
    parse(latest) > parse(current)
}

fn open_url(app: &AppHandle, url: &str) -> Result<()> {
    app.opener()
        .open_url(url, None::<&str>)
        .context("无法调用系统浏览器")
}

fn show_main(app: &AppHandle) -> Result<()> {
    let window = app.get_webview_window("main").context("主窗口尚未就绪")?;
    window.show()?;
    window.unminimize()?;
    window.set_focus()?;
    Ok(())
}

const DRAG_POLL_INTERVAL: Duration = Duration::from_millis(30);
const DRAG_MAXIMUM_DURATION: Duration = Duration::from_secs(10);
/// Windows/macOS 之外的平台以“位置连续静止”近似鼠标释放，约 240ms。
#[cfg(not(any(windows, target_os = "macos")))]
const DRAG_IDLE_POLLS: u32 = 8;

/// 前端依赖旧 Wails 语义：DragPet 在鼠标释放后才返回，返回后立刻读取窗口位置吸附边缘。
/// `start_dragging` 会立即返回，所以这里轮询等待拖拽真正结束。
async fn wait_for_drag_release(window: &tauri::WebviewWindow) {
    let deadline = tokio::time::Instant::now() + DRAG_MAXIMUM_DURATION;
    #[cfg(not(any(windows, target_os = "macos")))]
    let mut last: Option<PhysicalPosition<i32>> = None;
    #[cfg(not(any(windows, target_os = "macos")))]
    let mut idle = 0_u32;
    loop {
        tokio::time::sleep(DRAG_POLL_INTERVAL).await;
        if tokio::time::Instant::now() >= deadline {
            return;
        }
        #[cfg(any(windows, target_os = "macos"))]
        {
            let _ = window;
            if !primary_mouse_button_down() {
                return;
            }
        }
        #[cfg(not(any(windows, target_os = "macos")))]
        {
            let Ok(position) = window.outer_position() else {
                return;
            };
            if last == Some(position) {
                idle += 1;
                if idle >= DRAG_IDLE_POLLS {
                    return;
                }
            } else {
                idle = 0;
                last = Some(position);
            }
        }
    }
}

#[cfg(windows)]
fn primary_mouse_button_down() -> bool {
    #[link(name = "user32")]
    unsafe extern "system" {
        fn GetAsyncKeyState(key: i32) -> i16;
    }
    const VK_LBUTTON: i32 = 0x01;
    // 返回值最高位为 1 表示该键当前处于按下状态。
    unsafe { GetAsyncKeyState(VK_LBUTTON) as u16 & 0x8000 != 0 }
}

#[cfg(target_os = "macos")]
fn primary_mouse_button_down() -> bool {
    #[link(name = "CoreGraphics", kind = "framework")]
    unsafe extern "C" {
        fn CGEventSourceButtonState(state_id: i32, button: u32) -> bool;
    }
    // kCGEventSourceStateCombinedSessionState = 0,kCGMouseButtonLeft = 0:
    // 与 Windows 的 GetAsyncKeyState 等价的全局左键按压状态。
    unsafe { CGEventSourceButtonState(0, 0) }
}

fn set_pet_window_visible(app: &AppHandle, visible: bool) -> Result<()> {
    let window = app.get_webview_window("pet").context("桌宠窗口尚未就绪")?;
    if visible {
        window.show()?;
    } else {
        window.hide()?;
    }
    Ok(())
}

fn get_pet_screen_area(app: &AppHandle) -> Result<Value> {
    let window = app.get_webview_window("pet").context("桌宠窗口尚未就绪")?;
    let monitor = window
        .current_monitor()?
        .or_else(|| window.primary_monitor().ok().flatten())
        .context("无法获取桌宠所在屏幕")?;
    let position = monitor.position();
    let size = monitor.size();
    if size.width == 0 || size.height == 0 {
        bail!("桌宠所在屏幕范围无效");
    }
    Ok(json!({
        "X": position.x,
        "Y": position.y,
        "Width": size.width,
        "Height": size.height,
    }))
}

fn local_addresses() -> Vec<String> {
    let probes = [
        (
            SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), 0),
            SocketAddr::new(IpAddr::V4(Ipv4Addr::new(192, 0, 2, 1)), 9),
        ),
        (
            SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), 0),
            SocketAddr::new(
                IpAddr::V6(Ipv6Addr::new(0x2001, 0xdb8, 0, 0, 0, 0, 0, 1)),
                9,
            ),
        ),
    ];
    let mut addresses = probes
        .into_iter()
        .filter_map(|(bind, target)| {
            let socket = UdpSocket::bind(bind).ok()?;
            socket.connect(target).ok()?;
            let address = socket.local_addr().ok()?.ip();
            (!address.is_unspecified() && !address.is_loopback()).then(|| address.to_string())
        })
        .collect::<Vec<_>>();
    addresses.sort();
    addresses.dedup();
    addresses
}

fn inbox_category_roots(config: &DesktopConfig) -> InboxCategoryRoots {
    if !config.inbox_auto_classify {
        return InboxCategoryRoots::default();
    }
    let primary = Path::new(&config.inbox);
    let root = |category: &str, default_name: &str| {
        config
            .inbox_category_dirs
            .get(category)
            .filter(|path| !path.trim().is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| primary.join(default_name))
    };
    InboxCategoryRoots {
        media: Some(root("media", "图片和视频")),
        archive: Some(root("archive", "压缩包")),
        file: Some(root("file", "文件")),
        folder: Some(root("folder", "文件夹")),
    }
}

fn path_basename(value: &str) -> String {
    let trimmed = value.trim_end_matches(['/', '\\']);
    Path::new(trimmed)
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .unwrap_or(trimmed)
        .to_owned()
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    async fn wait_core_event(service: &JsonService, expected: &str) -> Value {
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                let encoded = service
                    .poll_event(Some(Duration::from_secs(1)))
                    .await
                    .unwrap_or_else(|| panic!("core event {expected} did not arrive"));
                let event: Value = serde_json::from_str(&encoded).unwrap();
                if event["event"] == expected {
                    return event["data"].clone();
                }
            }
        })
        .await
        .unwrap_or_else(|_| panic!("timed out waiting for core event {expected}"))
    }

    async fn send_direct(
        sender: &JsonService,
        sender_session: &Value,
        receiver_start: &Value,
        source: &Path,
    ) {
        let result = call_core(
            sender,
            "lan.send",
            json!({
                "session_id": sender_session,
                "path": source,
                "host": "127.0.0.1",
                "port": receiver_start["port"],
                "fingerprint": receiver_start["fingerprint"],
            }),
        )
        .await
        .unwrap();
        let sent = wait_core_event(sender, "lan.sent").await;
        assert_eq!(sent["send_id"], result["send_id"]);
        assert_eq!(sent["ok"], true, "{sent}");
    }

    #[test]
    fn one_time_ready_maps_send_ids_and_replays_early_completion() {
        let mut runtime = RuntimeState::new(DesktopConfig::default());
        runtime.pending_one_time.insert(
            "wormhole-1".into(),
            PendingOneTime {
                batch_id: "batch-1".into(),
                total: 2,
            },
        );
        runtime
            .orphan_sent
            .insert("core-2".into(), json!({ "send_id": "core-2", "ok": true }));

        let started = runtime
            .start_one_time("wormhole-1", vec!["core-1".into(), "core-2".into()])
            .unwrap();

        assert_eq!(started.batch_id, "batch-1");
        assert_eq!(started.total, 2);
        assert_eq!(started.orphan_sent.len(), 1);
        assert_eq!(runtime.core_to_batch["core-1"], "batch-1");
        assert_eq!(runtime.core_to_batch["core-2"], "batch-1");
        assert_eq!(runtime.batches["batch-1"].core_send_ids.len(), 2);
        assert!(runtime.pending_one_time.is_empty());
        assert!(runtime.orphan_sent.is_empty());
    }

    #[test]
    fn one_time_ready_rejects_duplicate_or_incomplete_send_ids() {
        for send_ids in [vec!["core-1"], vec!["core-1", "core-1"]] {
            let mut runtime = RuntimeState::new(DesktopConfig::default());
            runtime.pending_one_time.insert(
                "wormhole-1".into(),
                PendingOneTime {
                    batch_id: "batch-1".into(),
                    total: 2,
                },
            );
            assert!(
                runtime
                    .start_one_time(
                        "wormhole-1",
                        send_ids.into_iter().map(str::to_owned).collect(),
                    )
                    .is_err()
            );
            assert!(runtime.batches.is_empty());
            assert!(runtime.core_to_batch.is_empty());
        }
    }

    #[test]
    fn version_comparison_detects_newer_releases_only() {
        assert!(version_is_newer("2.0.4", "2.0.3"));
        assert!(version_is_newer("2.1.0", "2.0.9"));
        assert!(version_is_newer("3.0.0", "2.9.9"));
        assert!(!version_is_newer("2.0.3", "2.0.3"));
        assert!(!version_is_newer("2.0.2", "2.0.3"));
        assert!(!version_is_newer("", "2.0.3"));
        // 老客户端遇到形如 2.0.4-beta 的标签,数字段截断比较仍安全。
        assert!(version_is_newer("2.0.4-beta", "2.0.3"));
    }

    #[test]
    fn ssh_settings_input_accepts_the_frontend_host_key_field_name() {
        // 前端绑定发送 hostKeySHA256；camelCase 推导出的 hostKeySha256 会让保存 SSH 配置直接失败。
        let input: SshSettingsInput = serde_json::from_value(json!({
            "enabled": false,
            "host": "vps.example.com",
            "port": 22,
            "user": "inkhole",
            "privateKeyMode": "file",
            "privateKeyPath": "/home/inkhole/.ssh/id_ed25519",
            "hostKeySHA256": "SHA256:abcdef",
        }))
        .unwrap();
        assert_eq!(input.host_key_sha256, "SHA256:abcdef");
    }

    #[test]
    fn desktop_settings_input_accepts_the_batched_frontend_payload() {
        let input: DesktopSettingsInput = serde_json::from_value(json!({
            "peerName": "Desktop",
            "inbox": "C:/Inbox",
            "secret": "",
            "clearSecret": false,
            "port": 0,
            "showPet": true,
            "autostart": false,
            "encryptionEnabled": false,
            "autoClassify": true,
            "categoryDirs": { "media": "C:/Media" },
            "manualPeers": [],
            "wormholeRendezvous": "wss://relay.example.invalid",
            "wormholeRelay": "tcp://relay.example.invalid:4001",
            "ssh": {
                "enabled": false,
                "host": "",
                "port": 22,
                "user": "",
                "privateKeyMode": "file",
                "privateKeyPath": "",
                "pastedKey": "",
                "passphrase": "",
                "clearPassphrase": false,
                "hostKeySHA256": "",
                "peers": []
            }
        }))
        .unwrap();

        assert_eq!(input.peer_name, "Desktop");
        assert!(input.auto_classify);
        assert_eq!(input.category_dirs["media"], "C:/Media");
        assert_eq!(input.ssh.private_key_mode, "file");
    }

    #[test]
    fn invalidating_ssh_drops_events_from_the_previous_generation() {
        let mut runtime = RuntimeState::new(DesktopConfig::default());
        let first = runtime.invalidate_ssh();
        runtime.ssh_live = true;
        runtime.ssh_connected = true;

        // 重启期间旧会话的 ssh.* 事件必须被丢弃。
        let second = runtime.invalidate_ssh();
        assert!(second > first);
        assert!(!runtime.ssh_live);
        assert!(!runtime.ssh_connected);
    }

    #[test]
    fn peer_view_preserves_lan_and_ssh_routes() {
        let peer = LanPeer {
            instance_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            name: "Remote".into(),
            host: String::new(),
            hosts: Vec::new(),
            port: 45000,
            capabilities: vec!["ssh-relay-v2".into()],
            fingerprint: String::new(),
            public_key: "public".into(),
            service_name: "ssh".into(),
        };
        let view = PeerView::from(peer);
        assert_eq!(view.transport, "SSH/QUIC");
        assert_eq!(view.routes, vec!["SSH/QUIC"]);

        let peer = LanPeer {
            service_name: "lan+ssh".into(),
            host: "192.0.2.1".into(),
            fingerprint: "fingerprint".into(),
            ..LanPeer {
                instance_id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into(),
                name: "Remote".into(),
                host: String::new(),
                hosts: Vec::new(),
                port: 45000,
                capabilities: Vec::new(),
                fingerprint: String::new(),
                public_key: String::new(),
                service_name: String::new(),
            }
        };
        let view = PeerView::from(peer);
        assert_eq!(view.transport, "局域网 + SSH/QUIC");
        assert_eq!(view.routes, vec!["局域网", "SSH/QUIC"]);
    }

    #[tokio::test]
    async fn desktop_classification_reaches_the_quic_core_end_to_end() {
        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let custom_media = root.path().join("custom-media");
        let mut config = DesktopConfig {
            inbox: inbox.to_string_lossy().into_owned(),
            inbox_auto_classify: true,
            ..DesktopConfig::default()
        };
        config
            .inbox_category_dirs
            .insert("media".into(), custom_media.to_string_lossy().into_owned());

        let receiver = JsonService::new();
        let sender = JsonService::new();
        let receiver_start = call_core(
            &receiver,
            "lan.start",
            json!({
                "peer_name": "Desktop receiver",
                "instance_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "inbox": inbox,
                "inbox_category_roots": inbox_category_roots(&config),
                "capabilities": ["quic-v2", "blake3", "folder-v1"],
                "disable_mdns": true,
                "disable_udp": true,
            }),
        )
        .await
        .unwrap();
        let sender_start = call_core(
            &sender,
            "lan.start",
            json!({
                "peer_name": "Desktop sender",
                "instance_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "inbox": root.path().join("sender-inbox"),
                "capabilities": ["quic-v2", "blake3", "folder-v1"],
                "disable_mdns": true,
                "disable_udp": true,
            }),
        )
        .await
        .unwrap();

        let photo = root.path().join("PHOTO.JPEG");
        let archive = root.path().join("backup.TAR");
        let document = root.path().join("notes.txt");
        let folder = root.path().join("album");
        tokio::fs::write(&photo, b"photo").await.unwrap();
        tokio::fs::write(&archive, b"archive").await.unwrap();
        tokio::fs::write(&document, b"document").await.unwrap();
        tokio::fs::create_dir_all(folder.join("empty"))
            .await
            .unwrap();
        tokio::fs::write(folder.join("cover.txt"), b"cover")
            .await
            .unwrap();

        let cases = [
            (photo.as_path(), custom_media.join("PHOTO.JPEG")),
            (archive.as_path(), inbox.join("压缩包").join("backup.TAR")),
            (document.as_path(), inbox.join("文件").join("notes.txt")),
            (folder.as_path(), inbox.join("文件夹").join("album")),
        ];
        for (source, expected) in cases {
            let send = send_direct(
                &sender,
                &sender_start["session_id"],
                &receiver_start,
                source,
            );
            let received = wait_core_event(&receiver, "lan.received");
            let (_, received) = tokio::join!(send, received);
            assert_eq!(PathBuf::from(received["path"].as_str().unwrap()), expected);
        }

        assert_eq!(
            tokio::fs::read(custom_media.join("PHOTO.JPEG"))
                .await
                .unwrap(),
            b"photo"
        );
        assert_eq!(
            tokio::fs::read(inbox.join("压缩包").join("backup.TAR"))
                .await
                .unwrap(),
            b"archive"
        );
        assert_eq!(
            tokio::fs::read(inbox.join("文件").join("notes.txt"))
                .await
                .unwrap(),
            b"document"
        );
        assert_eq!(
            tokio::fs::read(inbox.join("文件夹").join("album").join("cover.txt"))
                .await
                .unwrap(),
            b"cover"
        );
        assert!(
            tokio::fs::try_exists(inbox.join("文件夹").join("album").join("empty"))
                .await
                .unwrap()
        );

        sender.close().await.unwrap();
        receiver.close().await.unwrap();
    }
}
