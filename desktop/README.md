# 墨洞 Wails 桌面端

这是墨洞的轻量桌面壳，使用 Wails 3 调用 `transport-core` 中的共享 Go 局域网、WHPP/WHF1、一次性短码、SSH 中继和 QUIC 直连实现。

```bash
# 开发运行
wails3 dev

# 构建当前平台
wails3 build

# macOS 打包
wails3 package
```

前端位于 `frontend/`，Go 服务入口为 `main.go`。设备身份、传输口令和 SSH 密钥保存在系统安全存储中，`desktop.json` 仅保存非敏感配置。
