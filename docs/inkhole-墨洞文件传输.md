# InkHole 2.0 文件传输

InkHole 2.0 使用 Rust 统一实现局域网、短码和 SSH 中继。桌面端是
Tauri 2.0，移动端是 Flutter；两端调用同一个 Tokio 异步核心。

## 局域网

同一 Wi-Fi、路由器或手机热点中的设备会通过签名 UDP/mDNS 自动发现。
发现结果包含设备身份、公钥、监听端口、能力和 QUIC 证书指纹。发送前
会再次执行挑战验证，连接建立后校验证书和可选共享口令。

局域网文件使用 QUIC 多流传输。文件、文件夹、进度、取消和断点续传都
在同一个核心服务中处理；BLAKE3 用于内容、文件夹清单和恢复点校验。

QUIC 监听端口默认固定为 41300（设置里可改，也可留空用随机端口），方便
防火墙放行与 Tailscale 直连；端口被占用时自动退回随机端口并提示，不会
让局域网启动失败。

## 短码（跨网络）

跨网络发送时，发送端生成一次性短码和二维码。接收端输入或扫描短码后，
先看到设备名、文件数量和大小摘要，确认后才开始传输。短码只用于建立
加密会话，公共 rendezvous/transit 服务不会保存文件内容。

短码分两步，且二者是独立的网络环节：

1. **配对（找到对方）** 经 rendezvous 服务器完成 PAKE 密钥交换。只要两端
   都能连上 rendezvous（WSS/443），跨网络配对即成功。
2. **传数据** 优先直连：同网段走局域网、双方都有公网 IPv6 时走 IPv6 直连，
   都不通时才回退到 **transit 中继** 中转。

⚠️ **默认中继是美国的免费公用服务器**（`transit.magic-wormhole.io:4001`），
从国内（尤其蜂窝）经常慢或连不上——它用的是非标准端口、连境外地址，运营商
常限速/阻断。**表现为：配对成功、却卡在传输并报 protocol error。这不是配对
失败，而是数据中继不可达。** 解决办法（设置里 rendezvous 与 transit 均可改）：

- **自建中继**：在双方都能连通的服务器（如国内 VPS）上部署，见
  [自建短码服务器.md](自建短码服务器.md)。之后短码纯跨网络稳跑，速度=服务器带宽。
- **Tailscale**：两台自己的设备装 Tailscale，把对方固定地址加为"固定地址设备"，
  直接 QUIC 直连、完全不经中继。

出码/连接慢同理：往返都发往美国服务器，自建国内服务器可显著提速。核心已内置
公共 DNS 兜底与 IPv4 优先竞速拨号，能修复"系统 DNS 解析不了导致连不上"，但
无法让境外中继本身变得可达。

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


## 常见问题:手机开着代理时,电脑发现不了手机

手机上的代理/加速器(TUN 模式,如 Clash、Tailscale 等)会改写 UDP 出站包的来源端口。普通发现报文不受影响,但传输通道使用 QUIC 协议,QUIC 会校验对端地址一致性——回包端口被代理改写后握手包会被丢弃,表现为:手机能看到电脑,电脑始终看不到手机。

解决办法:在代理 App 的分流设置里,把「墨洞 InkHole」加入绕过/直连名单(按应用分流),之后代理正常开启也不影响局域网传输。临时验证也可以直接关闭代理的 TUN/VPN 开关。
