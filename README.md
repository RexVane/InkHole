<p align="center">
  <h1 align="center">墨洞 · InkHole</h1>
  <p align="center">
    <strong>跨平台移动端 P2P 极速文件传输 (Android / iOS)</strong>
  </p>
  <p align="center">
    <b>简体中文</b> | <a href="README_en.md">English</a>
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

## 📖 项目简介

**墨洞 (InkHole)** 是一款**本地优先 (Local-first)、去中心化、端到端安全加密**的移动端 P2P 文件传输工具。

它专为移动设备设计，摆脱聊天软件对文件大小的限制、画质压缩以及第三方云端存储带来的隐私泄露顾虑。在局域网内，墨洞自动完成对端发现并建立经过证书指纹绑定的原生 **QUIC** 高速数据通道；当双端跨越不同网络时，墨洞无缝支持一次性短码 (Magic Wormhole)、SSH 中继以及 Tailscale 固定地址直连。**文件数据始终只在参与传输的终端之间流动，中继节点仅转发密文，无法窥探任何内容。**

---

## ✨ 核心特性

- 🚀 **极速 QUIC 局域网直连**：基于 Rust `quinn` 协议栈，同一 Wi-Fi 或热点下双向 UDP 信标 / mDNS-SD 秒级自动发现设备，流式直连跑满无线带宽。
- 🔑 **一次性短码跨网穿透**：集成 Magic Wormhole SPAKE2 PAKE 协议，手机生成短码或二维码，对端扫码即可安全配对。支持双端 IPv6 直连打洞，打洞失败自动降级为 TCP 隧道加密中继。
- 🔄 **确定性断点续传**：基于 BLAKE3 算法对文件元数据派生确定性 `transfer_id`，传输意外中断或重发无需从头开始，自动基于 `.part` 临时文件校验恢复进度。
- 🛡️ **严格安全边界**：每次发现与握手均使用 Ed25519 签名验证，数据传输实施证书指纹双向校验；设备私钥与配对凭据持久化保存在系统的安全钥匙串 (KeyStore / Keychain) 中。
- 🌐 **自研抗黑洞网络韧性**：针对蜂窝移动网络优化，内置 AliDNS / DNSPod / Google UDP:53 公共 DNS 错峰竞速兜底（解决系统解析卡死）；结合 Happy Eyeballs 简化版实现 IPv4 优先 300ms 错峰并发拨号，消除国际 IPv6 黑洞。
- ⚡ **自适应 MTU 探测**：QUIC 探测基准从 1200 字节起跳，完美兼容 Tailscale / VPN 等移动虚拟网隧道（避免 1280 握手黑洞导致的吞吐崩塌）。
- 📁 **文件夹递归安全传输**：传输目录时生成排序且带数字签名的 `FolderManifest`，自动防护目录穿越、越界符号链接 (Symlink) 及非法路径注入。

---

## 🏛️ 系统架构

墨洞采用**单一 Rust 传输内核 + Flutter 移动端前台**的分层架构：

```text
Flutter 移动端 UI (Android / iOS)
       │ (消息通道 / SendPort，主线程零阻塞)
       ▼
专用后台 Dart Isolate
       │ (版本化 C ABI: inkhole-ffi)
       ▼
Rust 核心引擎 (inkhole-core)
       ├── Tokio 异步执行引擎 & 全生命周期 CancellationToken
       ├── Quinn QUIC 协议栈 (固定端口 41300 / 被占自动回退)
       ├── UDP 广播 (41301) + mDNS 自动设备发现
       ├── Magic Wormhole SPAKE2 配对 + TCP 隧道封装
       ├── BLAKE3 哈希完整性校验 & 确定性断点检查点
       └── Happy Eyeballs & 公共 DNS 竞速网络库
```

---

## 📱 下载与安装

前往 [GitHub Releases](https://github.com/RexVane/InkHole/releases) 页面获取最新版本的安装包：

* **Android**：
  * `InkHole-<version>-arm64-v8a.apk`（绝大多数现代安卓手机推荐）
  * `InkHole-<version>-armeabi-v7a.apk`（老旧 32 位设备）
  * `InkHole-<version>-x86_64.apk`（模拟器或特定平板）
* **iOS**：
  * 通过 Actions 构建产物下载 `InkHole-<version>-ios.zip`，解压后使用签名工具（如 AltStore / Sideloadly / Xcode）自签名安装。

---

## 🛠️ 本地构建指南

### 前置环境
- **Rust** 1.93.0+ (`rustup toolchain install 1.93.0`)
- **Flutter** 3.24+ / Dart 3.5+
- **Android 构建**：Android SDK (API 35), NDK (r27+), `cargo-ndk` (`cargo install cargo-ndk`)
- **iOS 构建**：macOS 环境，Xcode 15+

### 1. 核心 Rust 代码检查与测试
```bash
# 格式化检查
cargo fmt --all --manifest-path rust/Cargo.toml -- --check

# 代码规范检查 (零警告要求)
cargo clippy --workspace --all-targets --manifest-path rust/Cargo.toml -- -D warnings

# 全量单元测试
cargo test --workspace --manifest-path rust/Cargo.toml
```

### 2. 编译并打包 Android APK
```bash
cd mobile

# 1) 获取依赖
flutter pub get

# 2) 编译 Rust 原生动态库 (arm64-v8a / armeabi-v7a / x86_64)
bash tool/build_native.sh

# 3) 构建发布版 APK
flutter build apk --release --split-per-abi
```

### 3. 编译并构建 iOS App
```bash
cd mobile

# 1) 生成 Rust XCFramework
bash tool/build_native_ios.sh

# 2) 构建无签名 iOS 包
flutter build ios --release --no-codesign
```

---

## ❓ 常见问题与网络排查

<details>
<summary><b>1. 手机开启代理/VPN（如 Clash / Tailscale）时，局域网搜不到对端？</b></summary>
<br>
手机在开启 TUN 模式全局代理时，代理系统会改写出站 UDP 包的来源端口。虽然 UDP 广播信标能被接收，但随后的 QUIC 双向加密握手会校验对端地址一致性，导致握手包被静默丢弃。<br>
<b>解决办法：</b>在代理工具的“分流设置”或“按应用分流”中，将「墨洞 InkHole」加入直连/绕过白名单。
</details>

<details>
<summary><b>2. 跨网络短码配对成功，但传输卡住报连接失败？</b></summary>
<br>
Magic Wormhole 默认中继服务器位于海外（<code>transit.magic-wormhole.io:4001</code>），国内部分运营商对 4001 端口境外连接存在限速或阻断。<br>
<b>解决办法：</b>
1. 建议在自己的 VPS 上部署轻量中继服务（详见 <a href="docs/自建短码服务器.md">自建短码服务器指南</a>），在墨洞设置中填入你的服务器地址；
2. 如果是个人多台设备，推荐开启 Tailscale，在墨洞设置「固定地址设备」中添加对端的 <code>100.x.y.z</code> IP，即可享受完全不经中继的原生 QUIC 直连。
</details>

---

## 📄 开源协议

本项目基于 [MIT License](LICENSE) 开源。
