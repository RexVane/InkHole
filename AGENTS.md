# InkHole(墨洞)— 开发指南

跨平台 P2P 文件传输:局域网自动发现 + 跨网络(一次性短码 / SSH 中继 / Tailscale 固定地址)。

## 仓库结构

- `rust/` — Cargo workspace(唯一的核心实现)
  - `crates/inkhole-core` — LAN 发现、QUIC 传输、断点续传、wormhole 短码、SSH 中继配对
  - `crates/inkhole-ffi` — 供 Flutter 的 C ABI(头文件 `include/inkhole.h`,状态码两边要同步)
  - `apps/inkhole-desktop` — Tauri 2 桌面端(Rust 后端)
- `desktop/frontend/` — 桌面端 TS 前端(Wails 时代沿用,经 `src/tauri-runtime.ts` 适配 Tauri;`bindings/` 目录是历史接口定义,方法名/字段名不要随意改)
- `mobile/` — Flutter 端(dart:ffi 调 inkhole-ffi;原生库经 `mobile/tool/build_native.sh` 产出到 `mobile/native/`)
- `desktop/build/` — 图标资源(`windows/icon-*.png` 是 LANCZOS 预缩放,运行时按 DPI 选用,别删)

## 构建与运行(Windows 桌面端,容易踩坑)

```bash
# 1) 前端必须先构建(产物进 rust/apps/inkhole-desktop/dist/,被 gitignore)
cd desktop/frontend && npm run build
node rust/apps/inkhole-desktop/scripts/frontend.mjs build

# 2) 编译必须带 custom-protocol,否则 release 仍按 dev 模式连 devUrl → 白屏拒连
cd rust && cargo build --release -p inkhole-desktop --features tauri/custom-protocol

# 3) 重编前先杀进程,否则链接器 os error 5
taskkill //IM inkhole-desktop.exe //F
```

- debug 构建是 CONSOLE 子系统,必弹终端窗口 —— 给用户测试一律用 release。
- 测试:`cargo test --workspace` 与 `cargo clippy --workspace --all-targets`(要求零警告);Flutter 端 `cd mobile && flutter analyze && flutter test`。

## ⚠️ 冻结区:Windows 桌面端已验收,禁止改动

`rust/apps/inkhole-desktop/` 与 `desktop/frontend/` 的 **Windows 行为**(代码、UI 布局、样式、交互)已经过用户逐项实测验收(2026-08-01),**除非用户明确提出桌面端的新需求或 bug,否则不得改变 Windows 上的任何行为**。macOS/Linux 侧允许调整,但必须:1) 布局与交互逻辑对齐 Windows 版(全平台统一无边框 + 自绘标题栏,macOS 拖宠用 CGEventSourceButtonState 检测松手);2) 平台差异一律用 `#[cfg(target_os = ...)]` 门控,不得触碰 Windows 代码路径。做移动端/核心库改动时不得顺手重构桌面端;若核心库接口变更迫使桌面端跟改,先向用户说明再动手。

## 关键约束(都是修过的 bug,别回退)

- **Tauri capability**(`apps/inkhole-desktop/capabilities/default.json`):窗口写操作(start-dragging/set-position/minimize/hide/toggle-maximize)必须显式授权,`core:window:default` 只读——少一条,拖窗/桌宠就废。
- **前后端字段名**:前端发 `hostKeySHA256`(Wails 遗留缩写),Rust 侧用 `#[serde(rename)]` 兼容,不要"顺手统一"。
- **`DragPet` 必须等鼠标释放才返回**(GetAsyncKeyState 轮询),前端依赖此语义做吸边。
- **桌宠窗口**:`transparent(true)` 时不可再设 `background_color`(Windows 忽略 alpha → 灰色底框)。
- **事件队列**:快照类事件(`lan.peers`)满队时丢旧不丢新;`ssh.*` 事件无 session_id,桌面侧靠代际计数过滤旧会话残留。
- **transfer_id** 用 blake3 对(instance、路径、大小、mtime、目标)确定性派生——换成随机 UUID 会杀死断点续传。
- **UDP 发现**遇非致命 io 错误要 continue 不退出(Windows 常见 NetworkReset)。
- **出站拨号一律走 `inkhole-core::net::dial_host_port`**:系统 DNS 与公共 DNS(阿里/腾讯/谷歌 UDP:53)竞速兜底 + IPv4 优先错峰竞速。别再直接 `TcpStream::connect(host)` 或 `connect_async(url)`——国内系统 DNS 常解析不了境外域名、且黑洞 IPv6 排最前会吃满超时(配对连不上的根因)。
- **QUIC 初始 MTU 不要硬编码成 1452**:用 `mtu_discovery_config` 从 1200 起探测。Tailscale(1280)/VPN 隧道路径 MTU 低于以太网,1452 会让握手包静默黑洞、吞吐坍缩。
- **QUIC 监听端口默认 41300**(设置可改/留空随机);固定端口被占时 core 自动退回随机端口并发 `lan.status`,不得让 `lan.start` 失败。UDP 发现端口 41301 被占时同理退回临时端口。
- **桌面端注册 `tauri-plugin-single-instance`**:二次启动只聚焦已有窗口,避免双实例争抢发现端口(用户报过的"端口被占")。
- **跨网络中继是硬约束不是 bug**:默认公共中继(美国)从国内蜂窝常不可达,配对成功但传输失败属预期;引导用户自建中继或 Tailscale,不要试图在代码里"修好"境外公共中继的可达性。

## 工程约定

- Commit message 用中文,当前风格见 `git log`;推送 GitHub 必须走代理 `127.0.0.1:7897`。
- CI:`ci.yml` 会自建前端 dist 并装 Linux Tauri 依赖;安卓发布产物命名 `InkHole-<tag>-<abi>.apk`,另有旧更新器依赖的稳定别名 `InkHole-<tag>.apk`(arm64-v8a),不要删。
- 文档在 `docs/`(架构见 `docs/rust-architecture.md`)。
