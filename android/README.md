# InkHole Android

墨洞 Android 客户端（当前版本 `1.3.3`），与 Windows/macOS 桌面端在同一局域网内互传文件。

## 功能

- 使用 Android NSD 发现 `_inkhole._tcp` 服务并手动选择发送目标。
- **手动添加设备**（设置内填 IP+端口）：跨网络（如 Tailscale）直连，离线自动剔除、回线自动恢复。
- Jetpack Compose 主界面、系统文件选择器和最近接收历史。
- 前台服务维持 P2P 节点，锁屏或切到后台后仍可接收。
- 支持系统 `ACTION_SEND` / `ACTION_SEND_MULTIPLE` 分享入口。
- 接收文件导出到系统 `Download/InkHole`，并发送可点击通知。
- 与桌面端同款品牌图标（自适应 + Android 13 单色图标 + 通知小图标）。
- 兼容桌面端明文、WHE1 整块加密、WHE2 分块加密和 ACK 回执；桌面端发送的文件夹以 zip 文件接收。

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
- WHPP 协议: `[4B "WHPP"] [4B header_len] [JSON header] [文件数据]`
- AES-256-GCM 加密: PBKDF2-HMAC-SHA256 100k 迭代，格式与桌面版 crypto.py 一致

当前 Python 端 CI 覆盖 WHPP/WHE1/WHE2 协议行为，Android APK 由独立工作流构建；Python/Kotlin 固定向量互通测试仍列在项目待办中。

## 权限

- `INTERNET` / `ACCESS_NETWORK_STATE` / `ACCESS_WIFI_STATE`：NSD 发现和 TCP 传输。
- `FOREGROUND_SERVICE` / `FOREGROUND_SERVICE_DATA_SYNC`：后台持续接收。
- `POST_NOTIFICATIONS`：Android 13+ 接收完成通知。
- 文件选择使用 Storage Access Framework；Android 10+ 导出下载目录使用 MediaStore，不需要通用存储权限。
