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

## 关键约束(都是修过的 bug,别回退)

- **Tauri capability**(`apps/inkhole-desktop/capabilities/default.json`):窗口写操作(start-dragging/set-position/minimize/hide/toggle-maximize)必须显式授权,`core:window:default` 只读——少一条,拖窗/桌宠就废。
- **前后端字段名**:前端发 `hostKeySHA256`(Wails 遗留缩写),Rust 侧用 `#[serde(rename)]` 兼容,不要"顺手统一"。
- **`DragPet` 必须等鼠标释放才返回**(GetAsyncKeyState 轮询),前端依赖此语义做吸边。
- **桌宠窗口**:`transparent(true)` 时不可再设 `background_color`(Windows 忽略 alpha → 灰色底框)。
- **事件队列**:快照类事件(`lan.peers`)满队时丢旧不丢新;`ssh.*` 事件无 session_id,桌面侧靠代际计数过滤旧会话残留。
- **transfer_id** 用 blake3 对(instance、路径、大小、mtime、目标)确定性派生——换成随机 UUID 会杀死断点续传。
- **UDP 发现**遇非致命 io 错误要 continue 不退出(Windows 常见 NetworkReset)。

## 工程约定

- Commit message 用中文,当前风格见 `git log`;推送 GitHub 必须走代理 `127.0.0.1:7897`。
- CI:`ci.yml` 会自建前端 dist 并装 Linux Tauri 依赖;安卓发布产物命名 `InkHole-<tag>-<abi>.apk`,另有旧更新器依赖的稳定别名 `InkHole-<tag>.apk`(arm64-v8a),不要删。
- 文档在 `docs/`(架构见 `docs/rust-architecture.md`)。
