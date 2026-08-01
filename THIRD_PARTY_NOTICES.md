# Third-party notices

InkHole 2.0 uses the Rust crates resolved by `rust/Cargo.lock`. Their license
and source metadata remain available through Cargo. The most relevant runtime
dependencies are:

- Tokio for asynchronous tasks and cancellation.
- Quinn and rustls for authenticated QUIC.
- mdns-sd and if-addrs for LAN discovery.
- BLAKE3, Ed25519, SPAKE2, and russh for integrity and authenticated relay
  sessions.
- Tauri 2 for the desktop host and Flutter plugins for the mobile host.

No vendored Go, Python, Kotlin, gomobile, or Wails transport implementation is
part of the 2.0 source tree.
