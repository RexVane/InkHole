# 墨洞 InkHole (Mobile)

InkHole 2.0 是一款本地优先 (Local-first) 的跨平台 P2P 移动端大文件传输应用 (Android / iOS)。
它优先在局域网内通过经过证书指纹固定的 QUIC 协议极速直连传输；当无法直接直连时，无缝回退到一次性 Wormhole 短码或 SSH 中继通道。文件数据始终驻留在对端设备中，中继仅转发加密流量。

## 架构概览

- **Rust 1.93** 作为唯一核心传输实现 (`crates/inkhole-core`)。
- **Tokio** 提供高性能异步执行环境与基于 Token 的全生命周期取消机制。
- **Quinn** 提供证书指纹固定的安全 QUIC 流式数据传输，自适应 MTU 探测。
- **BLAKE3** 提供毫秒级文件完整性校验与基于元数据的确定性断点续传。
- **inkhole-ffi** 提供稳定的 C ABI，将 Rust 运行时安全桥接到移动端。
- **Flutter** 驱动移动端 UI (Android / iOS)，在专用的 Background Dart Isolate 中加载 Rust 核心。

## 构建与测试

### Rust 核心检查
```bash
cargo fmt --all --manifest-path rust/Cargo.toml -- --check
cargo clippy --workspace --all-targets --manifest-path rust/Cargo.toml -- -D warnings
cargo test --workspace --manifest-path rust/Cargo.toml
```

### 移动端开发与打包

移动端项目位于 [`mobile/`](mobile/)。在装有 Flutter 3.24+ 的环境中：

```bash
cd mobile

# 1. 获取依赖并进行静态分析
flutter pub get
flutter analyze
flutter test

# 2. 编译 Rust 原生库并打包 Android APK
bash tool/build_native.sh
flutter build apk --release

# iOS XCFramework 构建见：
bash tool/build_native_ios.sh
```

## 传输协议

* **局域网发现**：使用带数字签名的 UDP 广播与 mDNS-SD，并使用固定 QUIC 证书指纹相互验证。QUIC 监听端口默认为 41300（被占时自动回退临时端口）。
* **断点续传**：大文件与文件夹均支持流式切片传输，传输进度通过 BLAKE3 校验并在中断后依据 `.part` 临时文件平滑恢复。
* **短码跨网穿透**：基于 Magic Wormhole SPAKE2 PAKE 协议，双端生成并扫描二维码，经由 Rendezvous 服务器完成密钥协商；数据传输优先尝试 IPv6/局域网直连打洞，打洞失败回退到 Transit TCP 中继（隧道内仍封装原生 QUIC 数据包）。
* **网络韧性**：所有出站拨号均集成系统 DNS 与公共 DNS (阿里/腾讯/谷歌 UDP:53) 竞速兜底以及 IPv4 优先错峰竞速 (Happy Eyeballs)。

## 安全设计

* 每次设备发现与传输握手均包含 Ed25519 签名验证。
* 证书指纹与 SSH Host Key 在建立数据通道前严格比对。
* 设备密钥对安全保存在系统的安全凭据存储中（KeyStore / Keychain）。

## 开源协议

MIT License. 详见 [LICENSE](LICENSE)。
