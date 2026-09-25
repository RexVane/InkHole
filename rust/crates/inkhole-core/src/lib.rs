pub mod discovery;
pub mod error;
mod folder;
pub mod hash;
pub mod identity;
pub mod inbox;
mod net;
pub mod protocol;
pub mod service;
mod ssh;
mod state;
pub mod transport;
mod wormhole;

pub use discovery::{DiscoveredPeer, UdpDiscovery, UdpDiscoveryConfig, UdpDiscoveryTimings};
pub use error::{CoreError, Result};
pub use identity::DeviceIdentity;
pub use inbox::InboxCategoryRoots;
pub use service::{JsonService, LanPeer, ServiceEvent};
pub use transport::{
    PeerEndpoint, ProgressCallback, QuicServer, QuicServerConfig, SendFileOptions, TransferEvent,
    TransferEventCallback, TransferProgress, VerifiedPeer, probe_peer, send_file,
};

pub const CORE_PROTOCOL_VERSION: u16 = 1;
pub const LAN_DISCOVERY_PROTOCOL_VERSION: u16 = 5;
pub const QUIC_PROTOCOL_VERSION: u16 = 2;
pub const QUIC_ALPN: &[u8] = b"inkhole-quic/2";

/// 核心库版本，编译期取自 `rust/Cargo.toml` 的 `[workspace.package] version`。
///
/// 与移动端版本号的关系：pubspec.yaml 是三方(核心 / Flutter / Dart 兜底常量)
/// 中唯一的人工输入点，`inkhole-ffi` 的 `version_sources_agree` 测试负责保证
/// 三者一致，CI 与本地 `cargo test --workspace` 都会拦住漂移。
pub const CORE_VERSION: &str = env!("CARGO_PKG_VERSION");
