# InkHole Android

墨洞安卓端 — 与桌面版协议互通的局域网 P2P 文件传输。

## 构建

1. 用 Android Studio (或 IntelliJ IDEA + Android 插件) 打开 `android/` 目录
2. 等待 Gradle 同步和 SDK 自动下载
3. 点 Run 构建 APK 并安装到手机

## 协议兼容

与桌面版完全互通：
- mDNS 服务类型: `_wormhole._tcp`（Android NSD 格式，兼容桌面版 zeroconf）
- WHPP 协议: `[4B "WHPP"] [4B header_len] [JSON header] [文件数据]`
- AES-256-GCM 加密: PBKDF2-HMAC-SHA256 100k 迭代，格式与桌面版 crypto.py 一致

## 权限

- `INTERNET` / `ACCESS_NETWORK_STATE` / `ACCESS_WIFI_STATE` — mDNS 发现 + TCP 传输
- 文件选择用 Storage Access Framework，不需要存储权限
