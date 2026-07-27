# InkHole Android

墨洞 Android 客户端（当前版本 `1.7.1`），与 Windows/macOS 桌面端通过局域网、Tailscale、一次性短码或 SSH VPS 中继互传文件。

## 功能

- 使用 Android NSD 和显式网卡 JmDNS 发现 `_inkhole._tcp` 服务；手机热点不转发 mDNS/广播时，除 UDP 请求和单播回应外，已发现的一端会通过可达 TCP 通道提示另一端回连，并在 WHPC 签名验证后加入设备列表，支持普通 Wi-Fi 和 Android 手机提供热点两种局域网拓扑。
- 局域网设备卡片统一在第一行显示设备备注/名称，第二行显示 8 位唯一实例 ID；常规 mDNS 与反向发现使用相同格式。
- **手动添加设备**（设置内填对方 Tailscale IP 或 MagicDNS 名称 + 对方监听端口）：跨网络传输，离线自动剔除、回线自动恢复。
- **一次性短码**：选择多个文件后生成 PAKE 安全短码和二维码；接收端先确认设备、数量、大小与名称摘要，再建立 Magic Wormhole 加密会话。文件继续使用 WHPP，不走依赖自带的 ZIP 传输。
- 一次性接收支持扫描发送端生成的二维码，也可以手动输入短码；扫码仅在点击扫码按钮时请求相机权限，识别后仍需点击“连接并接收”确认。
- 短码服务会优先直连，无法直连时使用加密 Transit；如果 Android 系统配置了 HTTP 代理，跨网核心会自动继承该代理，不需要把代理口令写进墨洞配置。
- **SSH VPS 中继**：填写 VPS、端口和用户名，选择私钥文件或粘贴已有私钥；验证主机指纹后启用。设备通过一次性 PAKE 配对码交换 Noise 身份，长期出现在发送目标列表中。
- **安全存储**：共享传输口令、粘贴私钥、私钥口令和 Noise 私钥由 Android Keystore 加密；旧版 `SharedPreferences` 明文口令会自动迁移并清除。文件模式只保存持久化 SAF URI，不提供 SSH 密钥生成功能。
- 设备列表只显示通过 WHPC v3 身份与能力验证的在线设备；手动设备首次连接会固定身份，地址后来指向其他设备时拒绝重连。对端下线后不会因系统 mDNS 缓存"复活"，回到前台会立即重新核对在线状态；切换 Wi-Fi 或热点接口后会在约 5 秒内刷新局域网发现。
- Tailnet IPv4（`100.64.0.0/10`）和 IPv6（`fd7a:115c:a1e0::/48`）的探活与传输强制走真正的 Tailscale VPN 网络；Tailscale 未连接时直接判离线，不会回退到默认路由。能打洞时由两端直连，否则 Tailscale 可能通过加密 DERP 中继。
- 固定监听端口不可用时节点启动失败并显示错误，不会自动改用随机端口。
- 设置页顶部分行显示本机、版本和端口，并按设备设置、存储、传输安全、跨网络配置分组；跨网络配置包含 Tailscale、一次性短码和 SSH 中继三个页签，帮助与更新只在整个设置滚动内容的末尾出现一次。
- 局域网身份在每次发现和传输时实时验证，不保存设备信任列表；升级会清理旧版的信任指纹缓存，避免设备换钥后持续阻断发现。
- 用户界面统一使用「发送 / 接收」术语，精简未发现设备和选文件提示；本机端口旁提示建议自定义固定端口。
- 首次打开自动显示简洁使用说明，区分局域网与 Tailscale 跨网络使用方式；设置中可随时重新查看。
- 一次性接收短码支持竖屏扫码，识别后仍需确认连接。
- Jetpack Compose 主界面、系统文件选择器和最近接收历史。
- 发送中可随时取消；双方立即清除进度，接收端只保留不可见的续传检查点，不会导出未完成文件。
- 已知大小的 `content://` 文件直接流式发送，不再为大文件完整复制一份缓存；传输状态显示实时速度。
- GB 级大文件高速传输：建立连接前请求最高 16MB TCP 收发缓冲（实际值受系统上限约束），应用层与下载目录导出均使用 1MB 缓冲。
- 传输状态栏百分比与实时速度前置并支持两行显示，长文件名只截断名字、不遮挡速度。
- 前台服务维持 P2P 节点，锁屏或切到后台后仍可接收；厂商系统单独终止监听层时，应用回到前台会检测 TCP socket 并自动重建节点、局域网发现和跨网核心。
- 支持系统 `ACTION_SEND` / `ACTION_SEND_MULTIPLE` 分享入口。
- 接收文件统一导出到系统 `Download/InkHole`，不再按类型创建分类目录；文件夹保留相对目录结构并直接可用。ACK 后、导出前若进程被系统终止，前台服务下次启动会从完成回执恢复导出，并按 `transfer_id` 更新接收历史，避免重复文件和重复记录；Android 10+ 无法可靠创建完全空的公共目录，因此空目录会被忽略。
- 设置内检查更新并应用内下载安装新 APK；更新弹窗显示当前/最新版本、可用状态和最多 4 条简洁版本变化（发布 APK 使用固定发布签名）。
- 与桌面端同款品牌图标（自适应 + Android 13 单色图标 + 通知小图标）。
- 使用 WHPP v3 明文或 WHE3 分块加密传输，兼容接收 WHE2，并强制校验 ACK 与 SHA-256；支持 WHPC 能力协商与 WHF1 流式文件夹接收，不需要先收 ZIP 再解压。
- 明文、WHE2/WHE3 加密、普通文件和 WHF1 文件夹均支持跨断网与进程重启续传；发布正式目标前会先持久化提交日志。进程在目标改名后、完成回执写入前退出时，重试会校验收件目录范围、文件类型、大小和 SHA-256 后恢复原目标；目标被改动时拒绝假成功并从零重传。检查点、提交日志、完成回执和暂存数据最多保留 7 天。

## 构建

1. 安装 Go 1.25+、Java 17、Android SDK 34 和 NDK `27.2.12479018`
2. 在仓库根目录的 `transport-core/` 生成 gomobile AAR
3. 用 Android Studio 打开 `android/`，或使用命令行构建 APK

也可以使用命令行：

```bash
cd transport-core
make init-gomobile
ANDROID_HOME=/path/to/android-sdk \
ANDROID_NDK_HOME=/path/to/android-sdk/ndk/27.2.12479018 \
make build-android

cd android
./gradlew assembleDebug
# android/app/build/outputs/apk/debug/app-debug.apk
```

## 协议兼容

协议实现目标是与桌面版逐字节互通：

- 局域网发现: mDNS 服务类型 `_inkhole._tcp`；热点兜底使用 UDP `41301`，发现提示仍须通过 WHPC v3 验证。
- WHPP 协议: `[4B "WHPP"] [4B header_len] [JSON header] [文件或 WHF1 文件夹流]`
- WHPC v3 能力探测：同时校验随机挑战、设备公钥签名、32 位 `instance_id`、设备名和协议能力。
- AES-256-GCM 加密:新传输使用 PBKDF2-HMAC-SHA256 600k 迭代，并兼容接收旧版 100k 格式
- 共享核心入口认证: 本机发送端点先发 `IKAT + 会话令牌`，核心回注接收节点先发 `IKCI + 节点令牌`。
- Magic Wormhole AppID: `com.rexvane.inkhole/transport-v1`。
- SSH 配对 AppID: `com.rexvane.inkhole/ssh-pair-v1`；数据通道使用 Noise IK 和 yamux。

Python 端 CI 覆盖 WHPP v3/WHE2 协议行为；Android 的 48 项单元测试覆盖协议、加密原语、完成回执与提交日志恢复、配置、设备类型、热点发现和固定向量，独立工作流会现场构建共享 AAR、运行测试与 Lint，再生成 APK。正式标签由三端打包工作流调用该流程，只有 Windows、macOS 和 Android 全部成功才统一创建 Release。

## 权限

- `INTERNET` / `ACCESS_NETWORK_STATE` / `ACCESS_WIFI_STATE`：NSD 发现和 TCP 传输。
- `FOREGROUND_SERVICE` / `FOREGROUND_SERVICE_DATA_SYNC`：后台持续接收。
- `POST_NOTIFICATIONS`：Android 13+ 接收完成通知。
- 文件选择使用 Storage Access Framework；Android 10+ 导出下载目录使用 MediaStore，不需要通用存储权限。
