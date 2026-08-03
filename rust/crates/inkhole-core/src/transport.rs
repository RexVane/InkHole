use std::{
    fmt,
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    path::{Path, PathBuf},
    sync::{Arc, Mutex as StdMutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use quinn::{ClientConfig, Endpoint, ServerConfig};
use rustls::{
    DigitallySignedStruct, SignatureScheme,
    client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    crypto::CryptoProvider,
    pki_types::{CertificateDer, PrivatePkcs8KeyDer, ServerName, UnixTime},
};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::Value;
use tokio::{
    io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt},
    sync::broadcast,
    task::{JoinHandle, JoinSet},
};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use crate::{
    CoreError, DeviceIdentity, QUIC_ALPN, Result,
    folder::{FolderSource, scan_folder},
    hash::blake3_file_cancellable,
    inbox::InboxCategoryRoots,
    protocol::{
        AUTH_REJECTED_TYPE, AuthenticatedRequest, AuthenticationChallenge, AuthenticationHello,
        AuthenticationRejected, FolderEntryKind, FolderManifest, PEER_PROBE_REQUEST_TYPE,
        PeerProbeRequest, PeerProbeResponse, TransferKind, TransferOffer, TransferReceipt,
        TransferResponse, normalize_capabilities, read_folder_manifest, read_frame,
        source_filename, write_folder_manifest, write_frame,
    },
    state::{PrepareFolderOutcome, PrepareOutcome, TransferStore},
};

const TRANSFER_BUFFER_SIZE: usize = 256 * 1024;
/// Caps a single direct-address attempt so an unreachable endpoint cannot stall the
/// serial walk over a peer's addresses before the relay fallback is reached.
const DIRECT_CONNECT_TIMEOUT: Duration = Duration::from_secs(4);

enum OutboundPayload {
    File(PathBuf),
    Folder(FolderSource),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PeerEndpoint {
    pub address: SocketAddr,
    pub certificate_fingerprint: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct VerifiedPeer {
    pub address: SocketAddr,
    pub instance_id: String,
    pub name: String,
    pub capabilities: Vec<String>,
    pub public_key: String,
    pub certificate_fingerprint: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TransferProgress {
    pub transfer_id: String,
    pub filename: String,
    pub done: u64,
    pub total: u64,
}

pub type ProgressCallback = Arc<dyn Fn(TransferProgress) + Send + Sync + 'static>;

#[derive(Clone, Default)]
pub struct SendFileOptions {
    pub transfer_id: Option<Uuid>,
    pub shared_secret: String,
    pub cancellation: CancellationToken,
    pub on_progress: Option<ProgressCallback>,
}

impl fmt::Debug for SendFileOptions {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SendFileOptions")
            .field("transfer_id", &self.transfer_id)
            .field("has_shared_secret", &!self.shared_secret.is_empty())
            .field("cancelled", &self.cancellation.is_cancelled())
            .field("has_progress_callback", &self.on_progress.is_some())
            .finish()
    }
}

/// 收到入站 QUIC 连接时回调对端 IP,用于发现模块反向信标(单向可见修复)。
pub type InboundPeerCallback = Arc<dyn Fn(IpAddr) + Send + Sync + 'static>;

#[derive(Clone)]
pub struct QuicServerConfig {
    pub bind_address: SocketAddr,
    pub inbox: PathBuf,
    pub inbox_category_roots: InboxCategoryRoots,
    pub identity: DeviceIdentity,
    pub capabilities: Vec<String>,
    pub shared_secret: String,
    pub on_inbound_peer: Option<InboundPeerCallback>,
}

impl fmt::Debug for QuicServerConfig {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("QuicServerConfig")
            .field("bind_address", &self.bind_address)
            .field("inbox", &self.inbox)
            .field("inbox_category_roots", &self.inbox_category_roots)
            .field("identity", &self.identity)
            .field("capabilities", &self.capabilities)
            .field("has_shared_secret", &!self.shared_secret.is_empty())
            .field("has_inbound_peer_hook", &self.on_inbound_peer.is_some())
            .finish()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "event", content = "data")]
pub enum TransferEvent {
    Progress(TransferProgress),
    Received {
        transfer_id: String,
        path: PathBuf,
        filename: String,
        size: u64,
        blake3: String,
        sender_instance_id: String,
        sender_name: String,
    },
    Status {
        message: String,
    },
}

pub type TransferEventCallback = Arc<dyn Fn(TransferEvent) + Send + Sync + 'static>;

#[derive(Clone)]
struct TransferEventSink {
    broadcast: broadcast::Sender<TransferEvent>,
    callback: Option<TransferEventCallback>,
}

impl TransferEventSink {
    fn emit(&self, event: TransferEvent) {
        let _ = self.broadcast.send(event.clone());
        if let Some(callback) = &self.callback {
            callback(event);
        }
    }
}

#[derive(Clone)]
struct IncomingTransferContext {
    store: TransferStore,
    identity: DeviceIdentity,
    capabilities: Arc<Vec<String>>,
    shared_secret: Arc<str>,
    events: TransferEventSink,
    on_inbound_peer: Option<InboundPeerCallback>,
}

pub struct QuicServer {
    endpoint: Endpoint,
    local_address: SocketAddr,
    certificate_fingerprint: String,
    cancellation: CancellationToken,
    task: StdMutex<Option<JoinHandle<()>>>,
    events: broadcast::Sender<TransferEvent>,
}

impl fmt::Debug for QuicServer {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("QuicServer")
            .field("local_address", &self.local_address)
            .field("cancelled", &self.cancellation.is_cancelled())
            .finish_non_exhaustive()
    }
}

impl QuicServer {
    pub async fn bind(config: QuicServerConfig) -> Result<Self> {
        Self::bind_with_event_handler(config, None).await
    }

    pub async fn bind_with_event_handler(
        config: QuicServerConfig,
        callback: Option<TransferEventCallback>,
    ) -> Result<Self> {
        let store =
            TransferStore::with_category_roots(&config.inbox, config.inbox_category_roots.clone())
                .await?;
        let capabilities = normalize_capabilities(config.capabilities)?;
        let certificate_fingerprint = config.identity.certificate_fingerprint().to_owned();
        let endpoint = Endpoint::server(server_config(&config.identity)?, config.bind_address)?;
        let local_address = endpoint.local_addr()?;
        let cancellation = CancellationToken::new();
        let (events, _) = broadcast::channel(256);
        let task = tokio::spawn(run_server(
            endpoint.clone(),
            IncomingTransferContext {
                store,
                identity: config.identity,
                capabilities: Arc::new(capabilities),
                shared_secret: Arc::<str>::from(config.shared_secret),
                events: TransferEventSink {
                    broadcast: events.clone(),
                    callback,
                },
                on_inbound_peer: config.on_inbound_peer,
            },
            cancellation.clone(),
        ));
        Ok(Self {
            endpoint,
            local_address,
            certificate_fingerprint,
            cancellation,
            task: StdMutex::new(Some(task)),
            events,
        })
    }

    pub fn local_address(&self) -> SocketAddr {
        self.local_address
    }

    pub fn certificate_fingerprint(&self) -> &str {
        &self.certificate_fingerprint
    }

    pub fn subscribe(&self) -> broadcast::Receiver<TransferEvent> {
        self.events.subscribe()
    }

    pub fn close_immediately(&self) {
        self.cancellation.cancel();
        self.endpoint.close(0_u8.into(), b"server shutdown");
        if let Ok(mut task) = self.task.lock()
            && let Some(task) = task.take()
        {
            task.abort();
        }
    }

    pub async fn close(&self) -> Result<()> {
        self.cancellation.cancel();
        self.endpoint.close(0_u8.into(), b"server shutdown");
        let task = self
            .task
            .lock()
            .map_err(|_| CoreError::Protocol("server task lock was poisoned".into()))?
            .take();
        if let Some(task) = task {
            task.await
                .map_err(|error| CoreError::Protocol(format!("server task failed: {error}")))?;
        }
        self.endpoint.wait_idle().await;
        Ok(())
    }
}

impl Drop for QuicServer {
    fn drop(&mut self) {
        self.cancellation.cancel();
        self.endpoint.close(0_u8.into(), b"server dropped");
        if let Ok(task) = self.task.get_mut()
            && let Some(task) = task.take()
        {
            task.abort();
        }
    }
}

pub async fn probe_peer(
    peer: &PeerEndpoint,
    expected_instance_id: Option<&str>,
    shared_secret: &str,
    cancellation: &CancellationToken,
) -> Result<VerifiedPeer> {
    if cancellation.is_cancelled() {
        return Err(CoreError::Cancelled);
    }
    let expected_fingerprint = peer.certificate_fingerprint.trim().to_ascii_lowercase();
    let expected_instance_id = expected_instance_id.map(|value| value.trim().to_ascii_lowercase());
    let bind_address = if peer.address.is_ipv4() {
        SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), 0)
    } else {
        SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), 0)
    };
    let mut endpoint = Endpoint::client(bind_address)?;
    endpoint.set_default_client_config(client_config(&expected_fingerprint)?);
    let connection = tokio::select! {
        _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = endpoint.connect(peer.address, "inkhole.local")? => result?,
    };
    let (mut send, mut receive) = tokio::select! {
        _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = connection.open_bi() => result?,
    };
    let request = PeerProbeRequest::new();
    write_authenticated_request(
        &mut send,
        &mut receive,
        &request,
        shared_secret,
        cancellation,
    )
    .await?;
    send.finish()
        .map_err(|error| CoreError::Protocol(format!("finish QUIC probe stream: {error}")))?;
    let response: PeerProbeResponse = read_application_frame(&mut receive, cancellation).await?;
    response.validate(
        &request,
        expected_instance_id.as_deref(),
        &expected_fingerprint,
    )?;
    connection.close(0_u8.into(), b"probe complete");
    endpoint.wait_idle().await;
    Ok(VerifiedPeer {
        address: peer.address,
        instance_id: response.instance_id,
        name: response.peer_name,
        capabilities: response.capabilities,
        public_key: response.public_key,
        certificate_fingerprint: response.certificate_fingerprint,
    })
}

pub async fn send_file(
    identity: &DeviceIdentity,
    peer: &PeerEndpoint,
    source: impl AsRef<Path>,
    options: SendFileOptions,
) -> Result<TransferReceipt> {
    if options.cancellation.is_cancelled() {
        return Err(CoreError::Cancelled);
    }
    let source = source.as_ref().to_path_buf();
    let metadata = tokio::select! {
        _ = options.cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = tokio::fs::symlink_metadata(&source) => result?,
    };
    if metadata.file_type().is_symlink() {
        return Err(CoreError::InvalidTransfer(
            "source path is a symbolic link".into(),
        ));
    }
    let modified_ms = metadata_modified_ms(&metadata);
    let transfer_id = options.transfer_id.unwrap_or_else(Uuid::new_v4);
    let (mut offer, payload) = if metadata.is_file() {
        let digest = blake3_file_cancellable(&source, &options.cancellation).await?;
        (
            TransferOffer::new(
                transfer_id,
                source_filename(&source)?,
                metadata.len(),
                modified_ms,
                digest,
                identity.summary(),
            ),
            OutboundPayload::File(source),
        )
    } else if metadata.is_dir() {
        let folder = scan_folder(&source, &options.cancellation).await?;
        let size = folder.manifest.validate()?;
        let digest = folder.manifest.digest()?;
        let mut offer = TransferOffer::new(
            transfer_id,
            source_filename(&source)?,
            size,
            modified_ms,
            digest,
            identity.summary(),
        );
        offer.kind = TransferKind::FolderV1;
        (offer, OutboundPayload::Folder(folder))
    } else {
        return Err(CoreError::InvalidTransfer(
            "source path is not a regular file or directory".into(),
        ));
    };
    offer.signature = identity.sign_base64(&offer.signing_bytes()?);
    offer.validate()?;

    let bind_address = if peer.address.is_ipv4() {
        SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), 0)
    } else {
        SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), 0)
    };
    let mut endpoint = Endpoint::client(bind_address)?;
    endpoint.set_default_client_config(client_config(&peer.certificate_fingerprint)?);
    let connection = tokio::select! {
        _ = options.cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = tokio::time::timeout(
            DIRECT_CONNECT_TIMEOUT,
            endpoint.connect(peer.address, "inkhole.local")?,
        ) => match result {
            Ok(result) => result?,
            Err(_) => {
                return Err(CoreError::Protocol(format!(
                    "direct QUIC connection to {} timed out",
                    peer.address
                )));
            }
        },
    };
    let (mut send, mut receive) = tokio::select! {
        _ = options.cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = connection.open_bi() => result?,
    };
    write_authenticated_request(
        &mut send,
        &mut receive,
        &offer,
        &options.shared_secret,
        &options.cancellation,
    )
    .await?;
    if let OutboundPayload::Folder(folder) = &payload {
        tokio::select! {
            _ = options.cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = write_folder_manifest(&mut send, &folder.manifest) => result?,
        }
    }
    let response: TransferResponse =
        read_application_frame(&mut receive, &options.cancellation).await?;
    if let OutboundPayload::Folder(folder) = payload {
        return send_folder_stream(
            folder, &offer, &options, response, send, receive, connection, endpoint,
        )
        .await;
    }
    let OutboundPayload::File(source) = payload else {
        unreachable!("folder payload returned above")
    };
    let offset = match response {
        TransferResponse::Resume { offset } if offset <= offer.size => offset,
        TransferResponse::Complete { receipt } => {
            validate_receipt(&offer, &receipt)?;
            connection.close(0_u8.into(), b"already complete");
            endpoint.wait_idle().await;
            return Ok(receipt);
        }
        TransferResponse::Rejected { reason } => {
            return Err(CoreError::Protocol(format!(
                "receiver rejected transfer: {reason}"
            )));
        }
        TransferResponse::Resume { .. } => {
            return Err(CoreError::Protocol(
                "receiver returned an invalid resume offset".into(),
            ));
        }
        TransferResponse::FolderResume { .. } => {
            return Err(CoreError::Protocol(
                "receiver returned a folder resume response for a file".into(),
            ));
        }
    };

    let mut file = tokio::select! {
        _ = options.cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = tokio::fs::File::open(&source) => result?,
    };
    tokio::select! {
        _ = options.cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = file.seek(std::io::SeekFrom::Start(offset)) => { result?; },
    }
    let mut sent = offset;
    emit_progress(&options, &offer, sent);
    let mut buffer = vec![0_u8; TRANSFER_BUFFER_SIZE];
    while sent < offer.size {
        let wanted = usize::try_from((offer.size - sent).min(buffer.len() as u64)).unwrap();
        let read = tokio::select! {
            _ = options.cancellation.cancelled() => {
                send.reset(1_u8.into()).ok();
                return Err(CoreError::Cancelled);
            }
            result = file.read(&mut buffer[..wanted]) => result?,
        };
        if read == 0 {
            return Err(CoreError::InvalidTransfer(
                "source file changed while it was being sent".into(),
            ));
        }
        tokio::select! {
            _ = options.cancellation.cancelled() => {
                send.reset(1_u8.into()).ok();
                return Err(CoreError::Cancelled);
            }
            result = send.write_all(&buffer[..read]) => result?,
        }
        sent += read as u64;
        emit_progress(&options, &offer, sent);
    }
    // 源文件在传输期间被追加写入会导致接收端拿到旧快照且 blake3 校验仍通过
    // (校验基于原始大小),发送方误报成功。读完声明大小后再读 1 字节确认 EOF,
    // 与文件夹路径一致(见 send_folder_stream 尾部检查)。
    let mut extra = [0_u8; 1];
    if file.read(&mut extra).await? != 0 {
        return Err(CoreError::InvalidTransfer(
            "source file grew while sending".into(),
        ));
    }
    send.finish()
        .map_err(|error| CoreError::Protocol(format!("finish QUIC stream: {error}")))?;
    let response: TransferResponse =
        read_application_frame(&mut receive, &options.cancellation).await?;
    let receipt = match response {
        TransferResponse::Complete { receipt } => receipt,
        TransferResponse::Rejected { reason } => {
            return Err(CoreError::Protocol(format!(
                "receiver rejected transfer: {reason}"
            )));
        }
        TransferResponse::Resume { .. } => {
            return Err(CoreError::Protocol(
                "receiver returned a duplicate resume response".into(),
            ));
        }
        TransferResponse::FolderResume { .. } => {
            return Err(CoreError::Protocol(
                "receiver returned a folder resume response for a file".into(),
            ));
        }
    };
    validate_receipt(&offer, &receipt)?;
    connection.close(0_u8.into(), b"transfer complete");
    endpoint.wait_idle().await;
    Ok(receipt)
}

#[allow(clippy::too_many_arguments)]
async fn send_folder_stream(
    folder: FolderSource,
    offer: &TransferOffer,
    options: &SendFileOptions,
    response: TransferResponse,
    mut send: quinn::SendStream,
    mut receive: quinn::RecvStream,
    connection: quinn::Connection,
    endpoint: Endpoint,
) -> Result<TransferReceipt> {
    let file_entries = folder
        .manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == FolderEntryKind::File)
        .collect::<Vec<_>>();
    if file_entries.len() != folder.files.len() {
        return Err(CoreError::InvalidTransfer(
            "folder source no longer matches its manifest".into(),
        ));
    }
    let (file_index, offset, completed) = match response {
        TransferResponse::FolderResume {
            file_index,
            offset,
            completed,
        } => (file_index as usize, offset, completed),
        TransferResponse::Complete { receipt } => {
            validate_receipt(offer, &receipt)?;
            connection.close(0_u8.into(), b"already complete");
            endpoint.wait_idle().await;
            return Ok(receipt);
        }
        TransferResponse::Rejected { reason } => {
            return Err(CoreError::Protocol(format!(
                "receiver rejected transfer: {reason}"
            )));
        }
        TransferResponse::Resume { .. } => {
            return Err(CoreError::Protocol(
                "receiver returned a file resume response for a folder".into(),
            ));
        }
    };
    if file_index > file_entries.len() {
        return Err(CoreError::Protocol(
            "receiver returned an invalid folder file index".into(),
        ));
    }
    let expected_completed = file_entries[..file_index]
        .iter()
        .try_fold(0_u64, |total, entry| total.checked_add(entry.size))
        .ok_or_else(|| CoreError::Protocol("folder resume size overflow".into()))?;
    if completed != expected_completed
        || (file_index == file_entries.len() && offset != 0)
        || file_entries
            .get(file_index)
            .is_some_and(|entry| offset > entry.size)
    {
        return Err(CoreError::Protocol(
            "receiver returned an inconsistent folder checkpoint".into(),
        ));
    }

    let mut sent = completed
        .checked_add(offset)
        .ok_or_else(|| CoreError::Protocol("folder progress overflow".into()))?;
    emit_progress(options, offer, sent);
    let mut buffer = vec![0_u8; TRANSFER_BUFFER_SIZE];
    for (index, (entry, source)) in file_entries
        .iter()
        .zip(&folder.files)
        .enumerate()
        .skip(file_index)
    {
        let entry_offset = if index == file_index { offset } else { 0 };
        let metadata = tokio::select! {
            _ = options.cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = tokio::fs::symlink_metadata(source) => result?,
        };
        if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() != entry.size
        {
            return Err(CoreError::InvalidTransfer(format!(
                "source file changed while sending folder: {}",
                entry.path
            )));
        }
        let mut file = tokio::select! {
            _ = options.cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = tokio::fs::File::open(source) => result?,
        };
        tokio::select! {
            _ = options.cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = file.seek(std::io::SeekFrom::Start(entry_offset)) => { result?; },
        }
        let mut file_sent = entry_offset;
        while file_sent < entry.size {
            let wanted = usize::try_from((entry.size - file_sent).min(buffer.len() as u64))
                .expect("transfer buffer length fits usize");
            let read = tokio::select! {
                _ = options.cancellation.cancelled() => {
                    send.reset(1_u8.into()).ok();
                    return Err(CoreError::Cancelled);
                }
                result = file.read(&mut buffer[..wanted]) => result?,
            };
            if read == 0 {
                return Err(CoreError::InvalidTransfer(format!(
                    "source file changed while sending folder: {}",
                    entry.path
                )));
            }
            tokio::select! {
                _ = options.cancellation.cancelled() => {
                    send.reset(1_u8.into()).ok();
                    return Err(CoreError::Cancelled);
                }
                result = send.write_all(&buffer[..read]) => result?,
            }
            file_sent += read as u64;
            sent += read as u64;
            emit_progress(options, offer, sent);
        }
        let mut extra = [0_u8; 1];
        if file.read(&mut extra).await? != 0 {
            return Err(CoreError::InvalidTransfer(format!(
                "source file grew while sending folder: {}",
                entry.path
            )));
        }
    }
    send.finish()
        .map_err(|error| CoreError::Protocol(format!("finish QUIC stream: {error}")))?;
    let response: TransferResponse =
        read_application_frame(&mut receive, &options.cancellation).await?;
    let receipt = match response {
        TransferResponse::Complete { receipt } => receipt,
        TransferResponse::Rejected { reason } => {
            return Err(CoreError::Protocol(format!(
                "receiver rejected transfer: {reason}"
            )));
        }
        TransferResponse::Resume { .. } | TransferResponse::FolderResume { .. } => {
            return Err(CoreError::Protocol(
                "receiver returned a duplicate resume response".into(),
            ));
        }
    };
    validate_receipt(offer, &receipt)?;
    connection.close(0_u8.into(), b"transfer complete");
    endpoint.wait_idle().await;
    Ok(receipt)
}

fn metadata_modified_ms(metadata: &std::fs::Metadata) -> i64 {
    metadata
        .modified()
        .unwrap_or(SystemTime::now())
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(i64::MAX as u128) as i64
}

fn emit_progress(options: &SendFileOptions, offer: &TransferOffer, done: u64) {
    if let Some(callback) = &options.on_progress {
        callback(TransferProgress {
            transfer_id: offer.transfer_id.clone(),
            filename: safe_event_filename(&offer.filename),
            done,
            total: offer.size,
        });
    }
}

fn safe_event_filename(filename: &str) -> String {
    crate::protocol::safe_filename(filename)
}

fn validate_receipt(offer: &TransferOffer, receipt: &TransferReceipt) -> Result<()> {
    if receipt.transfer_id != offer.transfer_id
        || receipt.size != offer.size
        || receipt.blake3 != offer.blake3
    {
        return Err(CoreError::Protocol(
            "receiver returned a mismatched completion receipt".into(),
        ));
    }
    Ok(())
}

async fn run_server(
    endpoint: Endpoint,
    context: IncomingTransferContext,
    cancellation: CancellationToken,
) {
    let mut connections = JoinSet::new();
    loop {
        tokio::select! {
            _ = cancellation.cancelled() => break,
            incoming = endpoint.accept() => {
                let Some(incoming) = incoming else { break };
                let context = context.clone();
                let child_cancellation = cancellation.child_token();
                connections.spawn(async move {
                    match incoming.await {
                        Ok(connection) => {
                            handle_connection(connection, context, child_cancellation)
                            .await;
                        }
                        Err(error) => tracing::debug!(%error, "QUIC handshake failed"),
                    }
                });
            }
            result = connections.join_next(), if !connections.is_empty() => {
                if let Some(Err(error)) = result {
                    tracing::warn!(%error, "QUIC connection task failed");
                }
            }
        }
    }
    endpoint.close(0_u8.into(), b"server shutdown");
    connections.abort_all();
    while connections.join_next().await.is_some() {}
}

async fn handle_connection(
    connection: quinn::Connection,
    context: IncomingTransferContext,
    cancellation: CancellationToken,
) {
    if let Some(hook) = context.on_inbound_peer.as_ref() {
        tracing::debug!(remote = %connection.remote_address(), "inbound QUIC connection");
        hook(connection.remote_address().ip());
    }
    let mut streams = JoinSet::new();
    loop {
        tokio::select! {
            _ = cancellation.cancelled() => break,
            stream = connection.accept_bi() => match stream {
                Ok((send, receive)) => {
                    let context = context.clone();
                    let cancellation = cancellation.child_token();
                    streams.spawn(async move {
                        if let Err(error) = handle_stream(send, receive, &context, cancellation)
                        .await
                        {
                            context.events.emit(TransferEvent::Status { message: error.to_string() });
                        }
                    });
                }
                Err(quinn::ConnectionError::ApplicationClosed(_)
                    | quinn::ConnectionError::LocallyClosed) => break,
                Err(error) => {
                    tracing::debug!(%error, "QUIC connection closed");
                    break;
                }
            },
            result = streams.join_next(), if !streams.is_empty() => {
                if let Some(Err(error)) = result {
                    tracing::warn!(%error, "QUIC stream task failed");
                }
            }
        }
    }
    streams.abort_all();
    while streams.join_next().await.is_some() {}
}

async fn handle_stream(
    mut send: quinn::SendStream,
    mut receive: quinn::RecvStream,
    context: &IncomingTransferContext,
    cancellation: CancellationToken,
) -> Result<()> {
    let hello: AuthenticationHello = match read_frame(&mut receive).await {
        Ok(hello) => hello,
        Err(error) => {
            reject_authentication(&mut send).await;
            return Err(error);
        }
    };
    let challenge = match AuthenticationChallenge::new(&hello) {
        Ok(challenge) => challenge,
        Err(error) => {
            reject_authentication(&mut send).await;
            return Err(error);
        }
    };
    write_frame(&mut send, &challenge).await?;
    let authenticated: AuthenticatedRequest = match read_frame(&mut receive).await {
        Ok(request) => request,
        Err(error) => {
            reject_authentication(&mut send).await;
            return Err(error);
        }
    };
    let frame = match authenticated.authenticate(&challenge, &context.shared_secret) {
        Ok(frame) => frame,
        Err(error) => {
            reject_authentication(&mut send).await;
            return Err(error);
        }
    };
    if frame.get("type").and_then(Value::as_str) == Some(PEER_PROBE_REQUEST_TYPE) {
        let request: PeerProbeRequest = serde_json::from_value(frame)?;
        return handle_probe_stream(
            &mut send,
            request,
            &context.identity,
            context.capabilities.as_ref().clone(),
        )
        .await;
    }
    let offer: TransferOffer = serde_json::from_value(frame)?;
    if let Err(error) = offer.validate() {
        reject_stream(&mut send, error.to_string()).await;
        return Err(error);
    }
    if offer.kind == TransferKind::FolderV1 {
        let manifest = match tokio::select! {
            _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = read_folder_manifest(&mut receive) => result,
        } {
            Ok(manifest) => manifest,
            Err(error) => {
                reject_stream(&mut send, error.to_string()).await;
                return Err(error);
            }
        };
        if let Err(error) = manifest.validate_offer(&offer) {
            reject_stream(&mut send, error.to_string()).await;
            return Err(error);
        }
        let _transfer_guard = context.store.lock_transfer(&offer.transfer_id).await;
        return handle_folder_stream(
            &mut send,
            &mut receive,
            &context.store,
            &offer,
            &manifest,
            &cancellation,
            &context.events,
        )
        .await;
    }
    if offer.kind != TransferKind::File {
        let error = CoreError::InvalidTransfer("unsupported transfer kind".into());
        reject_stream(&mut send, error.to_string()).await;
        return Err(error);
    }
    let _transfer_guard = context.store.lock_transfer(&offer.transfer_id).await;
    let prepared = match context.store.prepare(&offer).await {
        Ok(prepared) => prepared,
        Err(error) => {
            reject_stream(&mut send, error.to_string()).await;
            return Err(error);
        }
    };
    let prepared = match prepared {
        PrepareOutcome::Complete(completed) => {
            write_frame(
                &mut send,
                &TransferResponse::Complete {
                    receipt: completed.receipt,
                },
            )
            .await?;
            send.finish()
                .map_err(|error| CoreError::Protocol(format!("finish QUIC stream: {error}")))?;
            return Ok(());
        }
        PrepareOutcome::Resume(prepared) => prepared,
    };
    write_frame(
        &mut send,
        &TransferResponse::Resume {
            offset: prepared.offset,
        },
    )
    .await?;

    let mut part = tokio::fs::OpenOptions::new()
        .append(true)
        .open(&prepared.part_path)
        .await?;
    let mut received = prepared.offset;
    context
        .events
        .emit(TransferEvent::Progress(TransferProgress {
            transfer_id: offer.transfer_id.clone(),
            filename: safe_event_filename(&offer.filename),
            done: received,
            total: offer.size,
        }));
    let mut buffer = vec![0_u8; TRANSFER_BUFFER_SIZE];
    while received < offer.size {
        let wanted = usize::try_from((offer.size - received).min(buffer.len() as u64)).unwrap();
        let read = tokio::select! {
            _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
            result = receive.read(&mut buffer[..wanted]) => result?.unwrap_or(0),
        };
        if read == 0 {
            part.sync_data().await?;
            return Err(CoreError::Protocol(format!(
                "sender disconnected after {received} of {} bytes",
                offer.size
            )));
        }
        part.write_all(&buffer[..read]).await?;
        received += read as u64;
        context
            .events
            .emit(TransferEvent::Progress(TransferProgress {
                transfer_id: offer.transfer_id.clone(),
                filename: safe_event_filename(&offer.filename),
                done: received,
                total: offer.size,
            }));
    }
    let mut extra = [0_u8; 1];
    if receive.read(&mut extra).await?.is_some() {
        return Err(CoreError::Protocol(
            "sender wrote more bytes than declared".into(),
        ));
    }
    part.sync_all().await?;
    drop(part);
    let completed = context.store.finalize(&offer).await?;
    context.events.emit(TransferEvent::Received {
        transfer_id: offer.transfer_id.clone(),
        path: completed.destination.clone(),
        filename: safe_event_filename(&offer.filename),
        size: offer.size,
        blake3: offer.blake3.clone(),
        sender_instance_id: offer.sender.instance_id.clone(),
        sender_name: offer.sender.name.clone(),
    });
    write_frame(
        &mut send,
        &TransferResponse::Complete {
            receipt: completed.receipt,
        },
    )
    .await?;
    send.finish()
        .map_err(|error| CoreError::Protocol(format!("finish QUIC stream: {error}")))?;
    Ok(())
}

async fn handle_folder_stream(
    send: &mut quinn::SendStream,
    receive: &mut quinn::RecvStream,
    store: &TransferStore,
    offer: &TransferOffer,
    manifest: &FolderManifest,
    cancellation: &CancellationToken,
    events: &TransferEventSink,
) -> Result<()> {
    let prepared = match store.prepare_folder(offer, manifest).await {
        Ok(PrepareFolderOutcome::Complete(completed)) => {
            write_frame(
                send,
                &TransferResponse::Complete {
                    receipt: completed.receipt,
                },
            )
            .await?;
            send.finish()
                .map_err(|error| CoreError::Protocol(format!("finish QUIC stream: {error}")))?;
            return Ok(());
        }
        Ok(PrepareFolderOutcome::Resume(prepared)) => prepared,
        Err(error) => {
            reject_stream(send, error.to_string()).await;
            return Err(error);
        }
    };
    let file_entries = manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == FolderEntryKind::File)
        .collect::<Vec<_>>();
    if prepared.file_index > file_entries.len() {
        let error = CoreError::InvalidTransfer("invalid folder checkpoint file index".into());
        reject_stream(send, error.to_string()).await;
        return Err(error);
    }
    let file_index = u32::try_from(prepared.file_index)
        .map_err(|_| CoreError::InvalidTransfer("too many folder files".into()))?;
    write_frame(
        send,
        &TransferResponse::FolderResume {
            file_index,
            offset: prepared.offset,
            completed: prepared.completed,
        },
    )
    .await?;

    let mut received = prepared
        .completed
        .checked_add(prepared.offset)
        .ok_or_else(|| CoreError::InvalidTransfer("folder progress overflow".into()))?;
    events.emit(TransferEvent::Progress(TransferProgress {
        transfer_id: offer.transfer_id.clone(),
        filename: safe_event_filename(&offer.filename),
        done: received,
        total: offer.size,
    }));
    let mut buffer = vec![0_u8; TRANSFER_BUFFER_SIZE];
    for (index, entry) in file_entries.iter().enumerate().skip(prepared.file_index) {
        let offset = if index == prepared.file_index {
            prepared.offset
        } else {
            0
        };
        let mut part = match store.open_folder_file(&prepared, entry, offset).await {
            Ok(file) => file,
            Err(error) => {
                reject_stream(send, error.to_string()).await;
                return Err(error);
            }
        };
        let mut file_received = offset;
        while file_received < entry.size {
            let wanted = usize::try_from((entry.size - file_received).min(buffer.len() as u64))
                .expect("transfer buffer length fits usize");
            let read = tokio::select! {
                _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
                result = receive.read(&mut buffer[..wanted]) => result?.unwrap_or(0),
            };
            if read == 0 {
                part.sync_data().await?;
                return Err(CoreError::Protocol(format!(
                    "sender disconnected after {received} of {} folder bytes",
                    offer.size
                )));
            }
            part.write_all(&buffer[..read]).await?;
            file_received += read as u64;
            received += read as u64;
            events.emit(TransferEvent::Progress(TransferProgress {
                transfer_id: offer.transfer_id.clone(),
                filename: safe_event_filename(&offer.filename),
                done: received,
                total: offer.size,
            }));
        }
        part.sync_all().await?;
        drop(part);
        if let Err(error) = store.verify_folder_file(&prepared, entry).await {
            reject_stream(send, error.to_string()).await;
            return Err(error);
        }
    }
    let mut extra = [0_u8; 1];
    if receive.read(&mut extra).await?.is_some() {
        let error = CoreError::Protocol("sender wrote more folder bytes than declared".into());
        reject_stream(send, error.to_string()).await;
        return Err(error);
    }
    let completed = match store.finalize_folder(offer, manifest, &prepared).await {
        Ok(completed) => completed,
        Err(error) => {
            reject_stream(send, error.to_string()).await;
            return Err(error);
        }
    };
    events.emit(TransferEvent::Received {
        transfer_id: offer.transfer_id.clone(),
        path: completed.destination.clone(),
        filename: safe_event_filename(&offer.filename),
        size: offer.size,
        blake3: offer.blake3.clone(),
        sender_instance_id: offer.sender.instance_id.clone(),
        sender_name: offer.sender.name.clone(),
    });
    write_frame(
        send,
        &TransferResponse::Complete {
            receipt: completed.receipt,
        },
    )
    .await?;
    send.finish()
        .map_err(|error| CoreError::Protocol(format!("finish QUIC stream: {error}")))?;
    Ok(())
}

async fn handle_probe_stream(
    send: &mut quinn::SendStream,
    request: PeerProbeRequest,
    identity: &DeviceIdentity,
    capabilities: Vec<String>,
) -> Result<()> {
    let mut response = PeerProbeResponse::unsigned(
        &request,
        identity.summary(),
        identity.certificate_fingerprint().to_owned(),
        capabilities,
    )?;
    response.signature = identity.sign_base64(&response.signing_bytes()?);
    write_frame(send, &response).await?;
    send.finish()
        .map_err(|error| CoreError::Protocol(format!("finish QUIC probe stream: {error}")))?;
    Ok(())
}

async fn reject_stream(send: &mut quinn::SendStream, reason: String) {
    let _ = write_frame(send, &TransferResponse::Rejected { reason }).await;
    let _ = send.finish();
}

async fn reject_authentication(send: &mut quinn::SendStream) {
    let _ = write_frame(send, &AuthenticationRejected::new()).await;
    let _ = send.finish();
}

async fn write_authenticated_request<T>(
    send: &mut quinn::SendStream,
    receive: &mut quinn::RecvStream,
    payload: &T,
    shared_secret: &str,
    cancellation: &CancellationToken,
) -> Result<()>
where
    T: Serialize,
{
    let hello = AuthenticationHello::new();
    tokio::select! {
        _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = write_frame(send, &hello) => result?,
    }
    let challenge: AuthenticationChallenge = tokio::select! {
        _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = read_frame(receive) => result?,
    };
    challenge.validate_for(&hello)?;
    let request = AuthenticatedRequest::new(&challenge, payload, shared_secret)?;
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        result = write_frame(send, &request) => result,
    }
}

async fn read_application_frame<T>(
    receive: &mut quinn::RecvStream,
    cancellation: &CancellationToken,
) -> Result<T>
where
    T: DeserializeOwned,
{
    let frame: Value = tokio::select! {
        _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
        result = read_frame(receive) => result?,
    };
    if frame.get("type").and_then(Value::as_str) == Some(AUTH_REJECTED_TYPE) {
        let rejected: AuthenticationRejected = serde_json::from_value(frame)?;
        return Err(CoreError::Protocol(rejected.reason));
    }
    Ok(serde_json::from_value(frame)?)
}

/// 局域网大文件吞吐调优:quinn 默认流窗口 1.25MB 在 Wi-Fi RTT 下会钉住带宽,
/// 放大到 16MB;初始 MTU 提到以太网安全值并保留 MTU 发现。
fn tune_transport(transport: &mut quinn::TransportConfig) {
    transport.stream_receive_window(quinn::VarInt::from_u32(16 * 1024 * 1024));
    transport.receive_window(quinn::VarInt::from_u32(32 * 1024 * 1024));
    transport.send_window(32 * 1024 * 1024);
    transport.initial_mtu(1452);
}

fn server_config(identity: &DeviceIdentity) -> Result<ServerConfig> {
    let certificate = CertificateDer::from(identity.tls_certificate_der().to_vec());
    let private_key = PrivatePkcs8KeyDer::from(identity.tls_private_key_der().to_vec());
    let mut tls = rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(vec![certificate], private_key.into())
        .map_err(|error| CoreError::QuinnConfig(error.to_string()))?;
    tls.alpn_protocols = vec![QUIC_ALPN.to_vec()];
    let crypto = quinn::crypto::rustls::QuicServerConfig::try_from(tls)
        .map_err(|error| CoreError::QuinnConfig(error.to_string()))?;
    let mut server = ServerConfig::with_crypto(Arc::new(crypto));
    let transport = Arc::get_mut(&mut server.transport)
        .ok_or_else(|| CoreError::QuinnConfig("shared server transport config".into()))?;
    transport.max_concurrent_uni_streams(0_u8.into());
    transport.max_concurrent_bidi_streams(64_u32.into());
    transport.keep_alive_interval(Some(Duration::from_secs(5)));
    tune_transport(transport);
    Ok(server)
}

fn client_config(expected_fingerprint: &str) -> Result<ClientConfig> {
    let verifier = PinnedCertificateVerifier::new(expected_fingerprint)?;
    let mut tls = rustls::ClientConfig::builder()
        .dangerous()
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth();
    tls.alpn_protocols = vec![QUIC_ALPN.to_vec()];
    let crypto = quinn::crypto::rustls::QuicClientConfig::try_from(tls)
        .map_err(|error| CoreError::QuinnConfig(error.to_string()))?;
    let mut config = ClientConfig::new(Arc::new(crypto));
    let mut transport = quinn::TransportConfig::default();
    transport.keep_alive_interval(Some(Duration::from_secs(5)));
    tune_transport(&mut transport);
    config.transport_config(Arc::new(transport));
    Ok(config)
}

#[derive(Debug)]
struct PinnedCertificateVerifier {
    expected: [u8; 32],
    provider: Arc<CryptoProvider>,
}

impl PinnedCertificateVerifier {
    fn new(expected_fingerprint: &str) -> Result<Arc<Self>> {
        let expected: [u8; 32] = hex::decode(expected_fingerprint)
            .map_err(|_| CoreError::InvalidIdentity("invalid certificate fingerprint".into()))?
            .try_into()
            .map_err(|_| CoreError::InvalidIdentity("invalid certificate fingerprint".into()))?;
        Ok(Arc::new(Self {
            expected,
            provider: Arc::new(rustls::crypto::ring::default_provider()),
        }))
    }
}

impl ServerCertVerifier for PinnedCertificateVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> std::result::Result<ServerCertVerified, rustls::Error> {
        if blake3::hash(end_entity.as_ref()).as_bytes() != &self.expected {
            return Err(rustls::Error::General(
                "InkHole certificate fingerprint mismatch".into(),
            ));
        }
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        certificate: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(
            message,
            certificate,
            signature,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        certificate: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(
            message,
            certificate,
            signature,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider
            .signature_verification_algorithms
            .supported_schemes()
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    #[tokio::test]
    async fn probes_signed_identity_over_pinned_quic() {
        let root = tempfile::tempdir().unwrap();
        let receiver = DeviceIdentity::generate(None, "Receiver").unwrap();
        let server = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: root.path().join("inbox"),
            inbox_category_roots: InboxCategoryRoots::default(),
            identity: receiver.clone(),
            capabilities: vec!["quic-v2".into(), "blake3".into()],
            shared_secret: "room secret".into(),
            on_inbound_peer: None,
        })
        .await
        .unwrap();
        let peer = PeerEndpoint {
            address: server.local_address(),
            certificate_fingerprint: receiver.certificate_fingerprint().to_owned(),
        };

        let verified = probe_peer(
            &peer,
            Some(receiver.instance_id()),
            "room secret",
            &CancellationToken::new(),
        )
        .await
        .unwrap();
        assert_eq!(verified.instance_id, receiver.instance_id());
        assert_eq!(verified.name, "Receiver");
        assert_eq!(verified.capabilities, vec!["blake3", "quic-v2"]);
        assert_eq!(
            verified.certificate_fingerprint,
            receiver.certificate_fingerprint()
        );

        let wrong_instance = DeviceIdentity::generate(None, "Wrong").unwrap();
        assert!(
            probe_peer(
                &peer,
                Some(wrong_instance.instance_id()),
                "room secret",
                &CancellationToken::new(),
            )
            .await
            .is_err()
        );
        assert!(
            probe_peer(
                &peer,
                Some(receiver.instance_id()),
                "wrong secret",
                &CancellationToken::new(),
            )
            .await
            .is_err()
        );
        server.close().await.unwrap();
    }

    #[tokio::test]
    async fn transfers_file_over_pinned_quic() {
        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let source = root.path().join("payload.bin");
        let payload = (0..1_000_000_u32)
            .flat_map(u32::to_le_bytes)
            .collect::<Vec<_>>();
        tokio::fs::write(&source, &payload).await.unwrap();
        let receiver = DeviceIdentity::generate(None, "Receiver").unwrap();
        let sender = DeviceIdentity::generate(None, "Sender").unwrap();
        let server = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: inbox.clone(),
            inbox_category_roots: InboxCategoryRoots::default(),
            identity: receiver.clone(),
            capabilities: Vec::new(),
            shared_secret: "room secret".into(),
            on_inbound_peer: None,
        })
        .await
        .unwrap();
        let peer = PeerEndpoint {
            address: server.local_address(),
            certificate_fingerprint: receiver.certificate_fingerprint().to_owned(),
        };

        let receipt = send_file(
            &sender,
            &peer,
            &source,
            SendFileOptions {
                shared_secret: "room secret".into(),
                ..SendFileOptions::default()
            },
        )
        .await
        .unwrap();
        assert_eq!(receipt.size, payload.len() as u64);
        assert_eq!(
            tokio::fs::read(inbox.join("payload.bin")).await.unwrap(),
            payload
        );
        server.close().await.unwrap();
    }

    #[tokio::test]
    async fn transfers_nested_folder_and_preserves_empty_directories() {
        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let source = root.path().join("project");
        tokio::fs::create_dir_all(source.join("empty/deep"))
            .await
            .unwrap();
        tokio::fs::create_dir_all(source.join("src")).await.unwrap();
        tokio::fs::write(source.join("README.md"), b"folder transfer")
            .await
            .unwrap();
        tokio::fs::write(source.join("src/main.rs"), b"fn main() {}")
            .await
            .unwrap();
        let receiver = DeviceIdentity::generate(None, "Receiver").unwrap();
        let sender = DeviceIdentity::generate(None, "Sender").unwrap();
        let server = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: inbox.clone(),
            inbox_category_roots: InboxCategoryRoots::default(),
            identity: receiver.clone(),
            capabilities: vec!["folder-v1".into()],
            shared_secret: "room secret".into(),
            on_inbound_peer: None,
        })
        .await
        .unwrap();
        let peer = PeerEndpoint {
            address: server.local_address(),
            certificate_fingerprint: receiver.certificate_fingerprint().to_owned(),
        };

        let receipt = send_file(
            &sender,
            &peer,
            &source,
            SendFileOptions {
                shared_secret: "room secret".into(),
                ..SendFileOptions::default()
            },
        )
        .await
        .unwrap();
        assert_eq!(receipt.size, 27);
        assert_eq!(
            tokio::fs::read(inbox.join("project/README.md"))
                .await
                .unwrap(),
            b"folder transfer"
        );
        assert_eq!(
            tokio::fs::read(inbox.join("project/src/main.rs"))
                .await
                .unwrap(),
            b"fn main() {}"
        );
        assert!(
            tokio::fs::metadata(inbox.join("project/empty/deep"))
                .await
                .unwrap()
                .is_dir()
        );
        server.close().await.unwrap();
    }

    #[tokio::test]
    async fn cancellation_keeps_a_resumable_folder_checkpoint() {
        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let source = root.path().join("project");
        tokio::fs::create_dir(&source).await.unwrap();
        let prefix = vec![0x2a; 512 * 1024];
        let payload = vec![0x5a; 8 * 1024 * 1024];
        tokio::fs::write(source.join("a-prefix.bin"), &prefix)
            .await
            .unwrap();
        tokio::fs::write(source.join("b-large.bin"), &payload)
            .await
            .unwrap();
        let total = (prefix.len() + payload.len()) as u64;
        let receiver = DeviceIdentity::generate(None, "Receiver").unwrap();
        let sender = DeviceIdentity::generate(None, "Sender").unwrap();
        let server = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: inbox.clone(),
            inbox_category_roots: InboxCategoryRoots::default(),
            identity: receiver,
            capabilities: vec!["folder-v1".into()],
            shared_secret: String::new(),
            on_inbound_peer: None,
        })
        .await
        .unwrap();
        let peer = PeerEndpoint {
            address: server.local_address(),
            certificate_fingerprint: server.certificate_fingerprint().to_owned(),
        };
        let transfer_id = Uuid::new_v4();
        let cancellation = CancellationToken::new();
        let cancel_from_progress = cancellation.clone();
        let interrupted = send_file(
            &sender,
            &peer,
            &source,
            SendFileOptions {
                transfer_id: Some(transfer_id),
                shared_secret: String::new(),
                cancellation,
                on_progress: Some(Arc::new(move |progress| {
                    if progress.done >= 2 * 1024 * 1024 {
                        cancel_from_progress.cancel();
                    }
                })),
            },
        )
        .await;
        assert!(matches!(interrupted, Err(CoreError::Cancelled)));
        tokio::time::sleep(Duration::from_millis(100)).await;

        let resumed_from = Arc::new(AtomicU64::new(u64::MAX));
        let resumed_from_callback = resumed_from.clone();
        let receipt = send_file(
            &sender,
            &peer,
            &source,
            SendFileOptions {
                transfer_id: Some(transfer_id),
                shared_secret: String::new(),
                cancellation: CancellationToken::new(),
                on_progress: Some(Arc::new(move |progress| {
                    let _ = resumed_from_callback.compare_exchange(
                        u64::MAX,
                        progress.done,
                        Ordering::SeqCst,
                        Ordering::SeqCst,
                    );
                })),
            },
        )
        .await
        .unwrap();
        let resumed_from = resumed_from.load(Ordering::SeqCst);
        assert!(resumed_from > 0 && resumed_from < total);
        assert_eq!(receipt.size, total);
        assert_eq!(
            tokio::fs::read(inbox.join("project/a-prefix.bin"))
                .await
                .unwrap(),
            prefix
        );
        assert_eq!(
            tokio::fs::read(inbox.join("project/b-large.bin"))
                .await
                .unwrap(),
            payload
        );
        server.close().await.unwrap();
    }

    #[tokio::test]
    async fn rejects_wrong_shared_secret_before_transfer() {
        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let source = root.path().join("payload.txt");
        tokio::fs::write(&source, b"not for this room")
            .await
            .unwrap();
        let receiver = DeviceIdentity::generate(None, "Receiver").unwrap();
        let sender = DeviceIdentity::generate(None, "Sender").unwrap();
        let server = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: inbox.clone(),
            inbox_category_roots: InboxCategoryRoots::default(),
            identity: receiver.clone(),
            capabilities: Vec::new(),
            shared_secret: "correct secret".into(),
            on_inbound_peer: None,
        })
        .await
        .unwrap();
        let peer = PeerEndpoint {
            address: server.local_address(),
            certificate_fingerprint: receiver.certificate_fingerprint().to_owned(),
        };

        let result = send_file(
            &sender,
            &peer,
            &source,
            SendFileOptions {
                shared_secret: "wrong secret".into(),
                ..SendFileOptions::default()
            },
        )
        .await;
        assert!(
            matches!(result, Err(CoreError::Protocol(message)) if message.contains("shared secret"))
        );
        assert!(
            !tokio::fs::try_exists(inbox.join("payload.txt"))
                .await
                .unwrap()
        );
        server.close().await.unwrap();
    }

    #[tokio::test]
    async fn rejects_unpinned_server_certificate() {
        let root = tempfile::tempdir().unwrap();
        let source = root.path().join("payload.txt");
        tokio::fs::write(&source, b"secret").await.unwrap();
        let receiver = DeviceIdentity::generate(None, "Receiver").unwrap();
        let wrong_receiver = DeviceIdentity::generate(None, "Wrong Receiver").unwrap();
        let sender = DeviceIdentity::generate(None, "Sender").unwrap();
        let server = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: root.path().join("inbox"),
            inbox_category_roots: InboxCategoryRoots::default(),
            identity: receiver,
            capabilities: Vec::new(),
            shared_secret: String::new(),
            on_inbound_peer: None,
        })
        .await
        .unwrap();
        let peer = PeerEndpoint {
            address: server.local_address(),
            certificate_fingerprint: wrong_receiver.certificate_fingerprint().to_owned(),
        };

        assert!(
            send_file(&sender, &peer, &source, SendFileOptions::default())
                .await
                .is_err()
        );
        assert!(
            probe_peer(&peer, None, "", &CancellationToken::new())
                .await
                .is_err()
        );
        server.close().await.unwrap();
    }

    #[tokio::test]
    async fn cancellation_keeps_a_resumable_checkpoint() {
        let root = tempfile::tempdir().unwrap();
        let inbox = root.path().join("inbox");
        let source = root.path().join("large.bin");
        let payload = vec![0x5a; 24 * 1024 * 1024];
        tokio::fs::write(&source, &payload).await.unwrap();
        let receiver = DeviceIdentity::generate(None, "Receiver").unwrap();
        let sender = DeviceIdentity::generate(None, "Sender").unwrap();
        let server = QuicServer::bind(QuicServerConfig {
            bind_address: "127.0.0.1:0".parse().unwrap(),
            inbox: inbox.clone(),
            inbox_category_roots: InboxCategoryRoots::default(),
            identity: receiver.clone(),
            capabilities: Vec::new(),
            shared_secret: String::new(),
            on_inbound_peer: None,
        })
        .await
        .unwrap();
        let peer = PeerEndpoint {
            address: server.local_address(),
            certificate_fingerprint: server.certificate_fingerprint().to_owned(),
        };
        let transfer_id = Uuid::new_v4();
        let cancellation = CancellationToken::new();
        let cancel_from_progress = cancellation.clone();
        let interrupted = send_file(
            &sender,
            &peer,
            &source,
            SendFileOptions {
                transfer_id: Some(transfer_id),
                shared_secret: String::new(),
                cancellation,
                on_progress: Some(Arc::new(move |progress| {
                    if progress.done >= 4 * 1024 * 1024 {
                        cancel_from_progress.cancel();
                    }
                })),
            },
        )
        .await;
        assert!(matches!(interrupted, Err(CoreError::Cancelled)));
        tokio::time::sleep(Duration::from_millis(100)).await;

        let resumed_from = Arc::new(AtomicU64::new(u64::MAX));
        let resumed_from_callback = resumed_from.clone();
        let receipt = send_file(
            &sender,
            &peer,
            &source,
            SendFileOptions {
                transfer_id: Some(transfer_id),
                shared_secret: String::new(),
                cancellation: CancellationToken::new(),
                on_progress: Some(Arc::new(move |progress| {
                    let _ = resumed_from_callback.compare_exchange(
                        u64::MAX,
                        progress.done,
                        Ordering::SeqCst,
                        Ordering::SeqCst,
                    );
                })),
            },
        )
        .await
        .unwrap();
        assert!(resumed_from.load(Ordering::SeqCst) > 0);
        assert_eq!(receipt.size, payload.len() as u64);
        assert_eq!(
            tokio::fs::read(inbox.join("large.bin")).await.unwrap(),
            payload
        );
        server.close().await.unwrap();
    }
}
