# InkHole 2.0 文件传输

InkHole 2.0 使用 Rust 统一实现局域网、短码和 SSH 中继。桌面端是
Tauri 2.0，移动端是 Flutter；两端调用同一个 Tokio 异步核心。

## 局域网

同一 Wi-Fi、路由器或手机热点中的设备会通过签名 UDP/mDNS 自动发现。
发现结果包含设备身份、公钥、监听端口、能力和 QUIC 证书指纹。发送前
会再次执行挑战验证，连接建立后校验证书和可选共享口令。

局域网文件使用 QUIC 多流传输。文件、文件夹、进度、取消和断点续传都
在同一个核心服务中处理；BLAKE3 用于内容、文件夹清单和恢复点校验。

## 短码

跨网络发送时，发送端生成一次性短码和二维码。接收端输入或扫描短码后，
先看到设备名、文件数量和大小摘要，确认后才开始传输。短码只用于建立
加密会话，公共 rendezvous/transit 服务不会保存文件内容。

## SSH 中继

SSH 中继需要用户提供服务器、端口、用户名、私钥和 `SHA256:` 主机指纹。
首次连接必须匹配指纹；设备之间随后通过 PAKE 配对，并使用端到端加密的
转发通道。断线会自动重连，取消会关闭对应会话和所有文件流。

## 主机和构建

```text
Tauri 2 desktop  -> inkhole-core (Tokio/Quinn/BLAKE3)
Flutter mobile   -> inkhole-ffi C ABI -> inkhole-core
```

Rust 检查：

```bash
cargo fmt --all --manifest-path rust/Cargo.toml -- --check
cargo clippy --workspace --all-targets --manifest-path rust/Cargo.toml -- -D warnings
cargo test --workspace --manifest-path rust/Cargo.toml
```

移动端先运行 `mobile/tool/build_native.ps1` 或
`mobile/tool/build_native.sh` 生成各 ABI 的 Rust 动态库，再执行
`flutter build apk --release`。仓库不提交生成的 `.so` 或 XCFramework。
