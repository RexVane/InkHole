# 墨洞 InkHole

InkHole 2.0 is a local-first file transfer tool. It discovers peers on the
LAN, prefers direct authenticated QUIC, and falls back to one-time Wormhole
codes or an SSH relay for devices that cannot connect directly. Files stay on
the participating devices; the relay only forwards encrypted traffic.

## Architecture

- Rust 1.93 is the only transport implementation.
- Tokio owns asynchronous sessions and cancellation.
- Quinn provides certificate-pinned QUIC streams.
- BLAKE3 validates files, folders, and resumable checkpoints.
- Tauri 2 hosts the Windows/macOS desktop UI and the floating desktop pet.
- Flutter hosts the Android/iOS UI and mounts the same Rust core through the
  versioned `inkhole-ffi` C ABI in a Dart isolate.

The existing dark InkHole surface is kept: the hero animation, peer chips,
received-file list, short-code confirmation and SSH pairing remain in the
same layout and use the same user-facing actions.

## Build And Test

Rust checks require Rust 1.93 or newer:

```bash
cargo fmt --all --manifest-path rust/Cargo.toml -- --check
cargo clippy --workspace --all-targets --manifest-path rust/Cargo.toml -- -D warnings
cargo test --workspace --manifest-path rust/Cargo.toml
```

Desktop development requires Node.js 22 and Tauri CLI:

```bash
cd desktop/frontend && npm ci && cd ../..
cd rust/apps/inkhole-desktop && cargo tauri dev
```

The Flutter project is in [`mobile/`](mobile/). On a machine with Flutter
3.24+, run `flutter pub get`, build the Rust libraries with
`mobile/tool/build_native.ps1` or `mobile/tool/build_native.sh`, then run
`flutter build apk --release`. The iOS XCFramework recipe is in
`mobile/tool/build_native_ios.sh`.

## Transport

LAN discovery uses signed UDP/mDNS challenges and a pinned QUIC certificate.
Direct sends support files, folders, progress, cancellation and resumable
checkpoints. The one-time mode uses Magic Wormhole PAKE and exposes a QR code;
the receiver must accept the summarized offer before a transfer starts. SSH
relay setup requires a verified `SHA256:` host-key fingerprint, then pairs
devices with PAKE and authenticates the data channel end to end.

All calls share the JSON service methods in `inkhole-core` (`lan.*`,
`wormhole.*`, `ssh.*`). See [`docs/rust-architecture.md`](docs/rust-architecture.md)
for the host lifetime and native library layout.

## Security

Peer identities are signed on every discovery and transfer. Certificate
fingerprints and SSH host keys are checked before data channels are accepted.
Optional LAN shared secrets and private keys are stored by the host's secure
credential service; they are never written to ordinary JSON configuration.

## License

MIT. See [LICENSE](LICENSE).
