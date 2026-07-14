# InkHole Android

墨洞 Android 客户端（当前版本 `1.1.0`），支持局域网直连与 SSH 跨网络传输。

## 功能

- 使用 Android NSD 发现 `_inkhole._tcp` 服务并手动选择发送目标。
- 局域网 / 远程严格二选一，模式持久化，传输时禁止切换。
- 远程模式登录用户自己的 OpenSSH 服务器，通过回环反向端口转发发现并连接设备；服务器无需安装墨洞。
- 远程模式强制 P-256 ECDH + HKDF-SHA256 + AES-256-GCM，64 KiB 流式帧不占用整文件内存。
- 首次连接确认 SSH 主机密钥；私钥只保存在当前应用进程内存中，不写入 SharedPreferences。
- Jetpack Compose 主界面、系统文件选择器和最近接收历史。
- 前台服务维持 P2P 节点，锁屏或切到后台后仍可接收。
- 支持系统 `ACTION_SEND` / `ACTION_SEND_MULTIPLE` 分享入口。
- 接收文件导出到系统 `Download/InkHole`，并发送可点击通知。
- 兼容桌面端明文、WHE1 整块加密、WHE2 分块加密和 ACK 回执；桌面端发送的文件夹以 zip 文件接收。
- Launcher 自适应图标、Android 13 主题图标和通知小图标使用与桌面标题栏一致的双弧墨洞标记。

## 构建

1. 用 Android Studio (或 IntelliJ IDEA + Android 插件) 打开 `android/` 目录
2. 等待 Gradle 同步和 SDK 自动下载
3. 点 Run 构建 APK 并安装到手机

也可以使用命令行：

```bash
cd android
./gradlew testDebugUnitTest assembleDebug
# android/app/build/outputs/apk/debug/app-debug.apk
```

## 协议兼容

协议实现目标是与桌面版逐字节互通：

- mDNS 服务类型: `_inkhole._tcp`（Android NSD 格式，兼容桌面版 zeroconf）
- WHPP 协议: `[4B "WHPP"] [4B header_len] [JSON header] [文件数据]`
- AES-256-GCM 加密: PBKDF2-HMAC-SHA256 100k 迭代，格式与桌面版 crypto.py 一致

Python 与 Kotlin 测试共同读取 `tests/vectors/relay_crypto_v1.json`，覆盖密钥派生、帧字节、篡改、重放和乱序。远程数据面在 SSH channel 中承载完整 WHPP 与 ACK。

远程模式要求所有设备使用同一服务器、同一 SSH 账户和同一份私钥文本。服务器需
启用公钥登录、SFTP 与 `AllowTcpForwarding yes`，只需在安全组开放 SSH 端口。

## 权限

- `INTERNET` / `ACCESS_NETWORK_STATE` / `ACCESS_WIFI_STATE`：NSD、TCP 与 SSH 传输。
- `FOREGROUND_SERVICE` / `FOREGROUND_SERVICE_DATA_SYNC`：后台持续接收。
- `POST_NOTIFICATIONS`：Android 13+ 接收完成通知。
- 文件选择使用 Storage Access Framework；Android 10+ 导出下载目录使用 MediaStore，不需要通用存储权限。
