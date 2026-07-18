# InkHole Android

墨洞 Android 客户端（当前版本 `1.4.2`），与 Windows/macOS 桌面端在同一局域网或 Tailscale 网络内互传文件。

## 功能

- 使用 Android NSD 发现 `_inkhole._tcp` 服务并手动选择发送目标。
- **手动添加设备**（设置内填对方 Tailscale IP + 对方的监听端口）：跨网络直连，离线自动剔除、回线自动恢复。
- 设备列表只显示当前真实在线的设备：新发现与手动设备均先 TCP 验证连通才入列，对端下线后不会因系统 mDNS 缓存"复活"；回到前台立即重新核对在线状态。Tailscale 设备（100.x）的探活与传输强制走真正的 Tailscale VPN 网络——Tailscale 未连接时直接判离线，不会被 Clash 等代理 TUN 的本地假 accept 或运营商 CGNAT 欺骗成"永远在线"。
- 设置页顶部分行显示本机、版本和端口，并按设备设置、传输安全、跨网络配置分组；端到端加密使用独立开关控制，关闭时保留口令但不加密传输。
- 用户界面统一使用「发送 / 接收」术语，精简未发现设备和选文件提示；本机端口旁提示建议自定义固定端口。
- 首次打开自动显示简洁使用说明，区分局域网与 Tailscale 跨网络使用方式；设置中可随时重新查看。
- Jetpack Compose 主界面、系统文件选择器和最近接收历史。
- 发送中可随时取消；双方立即清除进度，接收端自动删除未完成文件。
- 已知大小的 `content://` 文件直接流式发送，不再为大文件完整复制一份缓存；传输状态显示实时速度。
- GB 级大文件高速传输：TCP 窗口缓冲在建立连接前按 4MB 协商（设晚了会被钉死在小窗口），收完导出到下载目录使用 1MB 缓冲复制。
- 传输状态栏百分比与实时速度前置并支持两行显示，长文件名只截断名字、不遮挡速度。
- 前台服务维持 P2P 节点，锁屏或切到后台后仍可接收。
- 支持系统 `ACTION_SEND` / `ACTION_SEND_MULTIPLE` 分享入口。
- 接收文件导出到系统 `Download/InkHole`，文件夹保留相对目录结构并直接可用；Android 10+ 无法可靠创建完全空的公共目录，因此空目录会被忽略。
- 设置内检查更新并应用内下载安装新 APK；更新弹窗显示当前/最新版本、可用状态和最多 4 条简洁版本变化（发布 APK 使用固定发布签名）。
- 与桌面端同款品牌图标（自适应 + Android 13 单色图标 + 通知小图标）。
- 兼容桌面端明文、WHE1 整块加密、WHE2 分块加密和 ACK 回执；支持 WHPC 能力协商与 WHF1 流式文件夹接收，不需要先收 ZIP 再解压。

## 构建

1. 用 Android Studio (或 IntelliJ IDEA + Android 插件) 打开 `android/` 目录
2. 等待 Gradle 同步和 SDK 自动下载
3. 点 Run 构建 APK 并安装到手机

也可以使用命令行：

```bash
cd android
./gradlew assembleDebug
# android/app/build/outputs/apk/debug/app-debug.apk
```

## 协议兼容

协议实现目标是与桌面版逐字节互通：

- mDNS 服务类型: `_inkhole._tcp`（Android NSD 格式，兼容桌面版 zeroconf）
- WHPP 协议: `[4B "WHPP"] [4B header_len] [JSON header] [文件或 WHF1 文件夹流]`
- WHPC 能力探测: 新客户端通告 `folder-v1`，旧客户端由发送方自动回退 ZIP。
- AES-256-GCM 加密: PBKDF2-HMAC-SHA256 100k 迭代，格式与桌面版 crypto.py 一致

当前 Python 端 CI 覆盖 WHPP/WHE1/WHE2 协议行为，Android APK 由独立工作流构建；Python/Kotlin 固定向量互通测试仍列在项目待办中。

## 权限

- `INTERNET` / `ACCESS_NETWORK_STATE` / `ACCESS_WIFI_STATE`：NSD 发现和 TCP 传输。
- `FOREGROUND_SERVICE` / `FOREGROUND_SERVICE_DATA_SYNC`：后台持续接收。
- `POST_NOTIFICATIONS`：Android 13+ 接收完成通知。
- 文件选择使用 Storage Access Framework；Android 10+ 导出下载目录使用 MediaStore，不需要通用存储权限。
