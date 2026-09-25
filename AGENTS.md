# InkHole(墨洞)— 移动端开发指南

跨平台 P2P 文件传输移动端(Android / iOS):局域网自动发现 + 跨网络(一次性短码 / SSH 中继 / Tailscale 固定地址)。

## 仓库结构

- `rust/` — Cargo workspace(核心网络与传输实现)
  - `crates/inkhole-core` — LAN 发现、QUIC 传输、断点续传、wormhole 短码、SSH 中继配对、DNS 竞速
  - `crates/inkhole-ffi` — 供 Flutter 的 C ABI(头文件 `include/inkhole.h`,状态码两边需保持同步)
- `mobile/` — Flutter 移动端(通过 dart:ffi 调用 inkhole-ffi)
  - `mobile/lib/` — Dart 业务与 UI 源码(核心在 `lib/core/inkhole_core.dart`，UI 在 `lib/home_page.dart` 与 `lib/widgets/`)
  - `mobile/native/` — 原生编译产物目录(`android/<abi>/libinkhole_ffi.so` 与 `ios/InkHoleCore.xcframework`)
  - `mobile/tool/` — 原生编译脚本(`build_native.sh` / `build_native.ps1` / `build_native_ios.sh`)

## 构建与测试

### Rust 核心库检查
```bash
cargo fmt --all --manifest-path rust/Cargo.toml -- --check
cargo clippy --workspace --all-targets --manifest-path rust/Cargo.toml -- -D warnings
cargo test --workspace --manifest-path rust/Cargo.toml
```

### 移动端构建与分析
```bash
cd mobile
# 1) 代码静态分析与单元测试
flutter analyze
flutter test

# 2) 编译 Rust 原生库并打包 Android
bash tool/build_native.sh
flutter build apk --release
```

## 关键约束与经验规范

- **单核心多端**：移动端所有传输逻辑严禁使用 Dart/Java/Kotlin 重写，必须统一调用 `inkhole-core` 导出的 FFI 接口。
- **Flutter 隔离与线程安全**：Flutter UI 线程严禁直接调用阻塞型 FFI；必须在独立的 Dart Background Isolate 中运行 `inkhole_service_call` 与 `inkhole_service_poll_event`，通过端口通信刷新 UI。
- **状态码同步**：`crates/inkhole-ffi/src/lib.rs` 中的状态码与 `include/inkhole.h` 及 `mobile/lib/core/inkhole_core.dart` 保持严格一致。
- **FFI 参数边界**：所有 `service.rs` 里的 `*Params`、`JsonRequest` 及嵌套类型（`InboxCategoryRoots` / `SshProfile` / `SshPeer`）一律加 `#[serde(deny_unknown_fields)]`。移动端与核心同版本发布，不存在跨版本字段漂移，收紧的代价为零；收益是拼错字段名立刻报 `unknown field`，而不是静默落到 `Default` 变成空值、让调用方在几百行外看到风马牛不相及的报错。注意 `deny_unknown_fields` **不递归**，嵌套结构体必须逐层声明。
- **确定性 transfer_id**：使用 blake3 对 (instance、源路径、**内容指纹**、目标) 派生。内容指纹必须反映实际传输的内容——文件用 blake3 文件摘要、文件夹用 `FolderManifest::digest()`，与 offer 侧的摘要口径完全一致；只有取不到时才退化到 len+mtime。**不要用目录自身的 metadata 派生文件夹 id**：目录内文件被改写不会改变父目录 mtime，重发会派生出同一个 id 却带不同 offer 摘要，接收端 `ensure_same_offer` 会永久硬拒。
- **出站拨号**：一律走 `inkhole-core::net::dial_host_port`，系统 DNS 与公共 DNS(阿里/腾讯/谷歌 UDP:53) 竞速兜底 + IPv4 优先错峰竞速。
- **QUIC 初始 MTU**：从 1200 起探测，防止在 Tailscale/VPN 移动网络环境下握手包静默黑洞。
- **监听端口兜底**：QUIC 监听端口默认 41300，UDP 发现端口 41301；若端口被占 core 自动退回随机端口，不得让局域网服务启动失败。
- **移动网络与代理分流**：手机开启 TUN 模式代理(Clash/Tailscale等)时会改写 UDP 端口导致电脑发现不到手机，需指导用户将墨洞加入直连/分流白名单。

## 工程约定

- Commit message 用中文，风格参考 `git log`；推送 GitHub 必须走代理 `127.0.0.1:7897`。
- CI/CD：`.github/workflows/ci.yml` 验证 Rust 核心与测试，`android.yml` 与 `ios.yml` 负责移动端打包。
- **版本号单一来源**：`mobile/pubspec.yaml` 的 `version` 是唯一人工输入点。`rust/Cargo.toml` 的 `[workspace.package] version` 与 `mobile/lib/models.dart` 里 `APP_VERSION` 的兜底默认值都必须与它一致。`inkhole-ffi` 的 `version_sources_agree` 测试会在 `cargo test --workspace` 拦下任何漂移（缺 `mobile/` 目录时自动跳过）。发布构建由 `mobile/tool/app_version.sh --semver` 读出并注入 `--dart-define=APP_VERSION`，**不要**手改 `models.dart` 里的字面量来"修版本"——改 `pubspec.yaml`。
- 文档位于 `docs/`。
