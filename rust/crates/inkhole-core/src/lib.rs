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
