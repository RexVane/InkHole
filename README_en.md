<p align="center">
  <h1 align="center">InkHole · 墨洞</h1>
  <p align="center">
    <strong>Cross-Platform P2P Mobile File Transfer (Android / iOS)</strong>
  </p>
  <p align="center">
    <a href="README.md">简体中文</a> | <b>English</b>
  </p>
  <p align="center">
    <a href="https://github.com/RexVane/InkHole/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/RexVane/InkHole/ci.yml?branch=main&label=CI&logo=github" alt="CI Status"></a>
    <a href="https://github.com/RexVane/InkHole/actions/workflows/android.yml"><img src="https://img.shields.io/github/actions/workflow/status/RexVane/InkHole/android.yml?branch=main&label=Android%20APK&logo=android" alt="Android Build"></a>
    <a href="https://github.com/RexVane/InkHole/actions/workflows/ios.yml"><img src="https://img.shields.io/github/actions/workflow/status/RexVane/InkHole/ios.yml?branch=main&label=iOS&logo=apple" alt="iOS Build"></a>
    <a href="https://github.com/RexVane/InkHole/releases"><img src="https://img.shields.io/github/v/release/RexVane/InkHole?logo=github" alt="Release"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
  </p>
</p>

---

## 📖 Introduction

**InkHole** is a **local-first, decentralized, and end-to-end encrypted** peer-to-peer (P2P) file transfer tool designed specifically for mobile devices.

Say goodbye to file size limitations, image/video compression, and cloud privacy concerns from instant messaging apps. On local networks, InkHole automatically discovers peers and establishes certificate-pinned **QUIC** direct connections at wire speed. Across networks, InkHole seamlessly negotiates transfers via one-time short codes (Magic Wormhole), SSH relays, or Tailscale fixed addresses. **Your files never land on any intermediate server; relays only forward opaque encrypted bytes.**

---

## ✨ Key Features

- 🚀 **Blazing-Fast QUIC Direct Transfer**: Built on the Rust `quinn` protocol stack. Zero-config peer discovery over dual-channel UDP beacons & mDNS-SD on shared Wi-Fi or hotspots, streaming at full wireless bandwidth.
- 🔑 **One-Time Short Codes (Magic Wormhole)**: Integrated SPAKE2 PAKE authentication. Generate a temporary code or QR code on your phone, scan it from another phone to pair securely. Prioritizes IPv6 hole-punching and falls back to encrypted TCP tunnels.
- 🔄 **Deterministic Resumable Transfers**: Computes deterministic `transfer_id`s derived with BLAKE3 over file metadata. Interrupted transfers automatically resume from `.part` temporary files without re-sending completed bytes.
- 🛡️ **Zero-Trust Security Boundary**: Peer discovery and offer handshakes are signed with Ed25519. QUIC connections enforce certificate fingerprint pinning. Secrets and keys are securely stored in the system KeyStore / Keychain.
- 🌐 **Anti-Blackhole Network Resilience**: Tailored for complex mobile carrier environments. Built-in public DNS racing (AliDNS / DNSPod / Google over UDP:53) prevents system resolver hangs; Happy Eyeballs-inspired IPv4-first staggered racing bypasses international IPv6 blackholes.
- ⚡ **Adaptive MTU Probing**: Probes QUIC path MTU starting from a safe 1200-byte baseline, preventing packet loss and window collapse on Tailscale, WireGuard, and VPN mobile tunnels.
- 📁 **Safe Folder Streaming**: Directory transfers generate a signed and sorted `FolderManifest`, strictly rejecting path traversal (`..`), symlinks, and directory injection attacks.

---

## 🏛️ Architecture

InkHole adheres to a **single Rust transport core + Flutter mobile host** philosophy:

```text
Flutter Mobile UI (Android / iOS)
       │ (Port messaging / SendPort, non-blocking UI frame scheduler)
       ▼
Dedicated Background Dart Isolate
       │ (Versioned C ABI: inkhole-ffi)
       ▼
Rust Transport Engine (inkhole-core)
       ├── Tokio asynchronous runtime & CancellationToken hierarchy
       ├── Quinn QUIC protocol stack (Default port 41300 / auto-fallback)
       ├── UDP discovery (Port 41301) + mDNS-SD daemon
       ├── Magic Wormhole SPAKE2 pairing & TCP tunnel encapsulation
       ├── BLAKE3 verification & deterministic resumable checkpoints
       └── Happy Eyeballs & UDP:53 DNS racing net dialer
```

---

## 📱 Download & Installation

Download pre-built release artifacts from [GitHub Releases](https://github.com/RexVane/InkHole/releases):

* **Android**:
  * `InkHole-<version>-arm64-v8a.apk` (Recommended for modern Android phones)
  * `InkHole-<version>-armeabi-v7a.apk` (For legacy 32-bit devices)
  * `InkHole-<version>-x86_64.apk` (For emulators or x86 tablets)
* **iOS**:
  * Download `InkHole-<version>-ios.zip` from Actions / Releases, and install via self-signing tools (AltStore, Sideloadly, or Xcode).

---

## 🛠️ Building from Source

### Prerequisites
- **Rust** 1.93.0+ (`rustup toolchain install 1.93.0`)
- **Flutter** 3.24+ / Dart 3.5+
- **For Android**: Android SDK (API 35), NDK (r27+), `cargo-ndk` (`cargo install cargo-ndk`)
- **For iOS**: macOS host with Xcode 15+

### 1. Check and Test the Rust Core
```bash
# Verify code formatting
cargo fmt --all --manifest-path rust/Cargo.toml -- --check

# Run linter (zero warnings policy)
cargo clippy --workspace --all-targets --manifest-path rust/Cargo.toml -- -D warnings

# Execute full test suite
cargo test --workspace --manifest-path rust/Cargo.toml
```

### 2. Build Android APK
```bash
cd mobile

# 1) Fetch dependencies
flutter pub get

# 2) Compile native Rust libraries (arm64-v8a / armeabi-v7a / x86_64)
bash tool/build_native.sh

# 3) Build release split APKs
flutter build apk --release --split-per-abi
```

### 3. Build iOS App
```bash
cd mobile

# 1) Build Rust XCFramework
bash tool/build_native_ios.sh

# 2) Build unsigned iOS bundle
flutter build ios --release --no-codesign
```

---

## ❓ FAQ & Troubleshooting

<details>
<summary><b>1. Why can't peers discover each other when a VPN/proxy (e.g., Clash, Tailscale) is active?</b></summary>
<br>
When a phone runs a TUN-mode global proxy, the system proxy rewrites the source port of outbound UDP datagrams. While UDP broadcast beacons may still be heard, the subsequent mutual QUIC handshake validates address continuity, causing handshake replies to be dropped.<br>
<b>Solution:</b> In your proxy app's split-tunneling (App Routing / Bypass) settings, add "InkHole" to the direct/bypass list.
</details>

<details>
<summary><b>2. Short-code pairing succeeds, but data transfer gets stuck or fails?</b></summary>
<br>
The default Magic Wormhole transit relay is hosted in the US (<code>transit.magic-wormhole.io:4001</code>). In some cellular or regional networks, outbound traffic to port 4001 abroad is throttled or blocked.<br>
<b>Solution:</b>
1. Deploy your own lightweight relay (see <a href="docs/自建短码服务器.md">Self-hosting Short Code Server</a>) and update the server address in InkHole settings;
2. For transfers between your own devices, use Tailscale and configure the peer's <code>100.x.y.z</code> address under "Manual Peers" for direct, un-relayed QUIC.
</details>

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
