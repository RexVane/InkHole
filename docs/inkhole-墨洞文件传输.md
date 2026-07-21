# 墨洞文件传输（InkHole）

版本：`1.5.4`

墨洞是 Windows、macOS 和 Android 之间的文件互传工具。局域网内使用
mDNS 自动发现和 TCP 直连；跨网络保留 Tailscale，并提供 Magic Wormhole
一次性短码和自有 SSH VPS 长期中继。应用没有墨洞云盘，文件不会先上传到
墨洞服务器。

## 传输方式

| 方式 | 适合场景 | 连接寿命 | 用户需要准备 |
| --- | --- | --- | --- |
| 局域网 mDNS | 同一 WiFi/路由器 | 临时 | 两端打开墨洞 |
| Tailscale | 自己的固定设备 | 长期 | 两端登录同一 Tailnet，填对方地址和端口 |
| Magic Wormhole 短码 | 临时给另一台设备发送 | 一次 | 发送端生成码，接收端输入或扫描二维码 |
| SSH VPS 中继 | 固定设备长期互传 | 长期 | 自有 VPS、SSH 用户和已有私钥 |

## 一次性短码

发送端在“跨网络传输”中选择文件，墨洞向 Magic Wormhole 会合服务申请一
个一次性代码。代码通常形如：

```text
7-reproduce-freedom
```

数字是会合服务分配的 mailbox 编号，后面的英文词组是 PAKE 密码组件。
英文不是系统语言设置造成的，也不应翻译；接收端必须原样输入。用户界面
使用中文“短码”，二维码内容使用墨洞自己的 URI：

```text
inkhole://receive?code=7-reproduce-freedom
```

接收端输入代码后，发送端先公布设备名、文件数量、总大小和名称摘要。接收
端确认后才接受通道。代码只使用一次，默认十分钟超时；会合服务只负责让
两端找到彼此，不接触文件明文。两端优先尝试直连，失败时使用加密 Transit
中继。桌面端自动继承系统 HTTP 代理，Android 自动继承当前网络的 HTTP
代理（如果系统配置了代理）。

Magic Wormhole 的 PAKE 只负责建立加密隧道。墨洞不会调用其自带的 ZIP 文件
传输流程：通道建立后使用 yamux 复用多个独立流，每个文件或目录流继续承载
WHPP/WHF1，因此多文件不需要先合成 ZIP，也不在中继落盘。

## SSH VPS 中继

VPS 只需要运行标准 SSH 服务并允许 TCP forwarding，不需要安装墨洞服务、
数据库或对象存储。两端填写：

- VPS 公网 IP 或域名；
- SSH 端口（通常为 `22`）；
- SSH 用户名；
- 已有 SSH 私钥文件，或直接粘贴已有私钥。

墨洞不会生成 SSH 密钥。首次连接先显示服务器主机指纹，用户确认后才固定
该指纹。应用在 VPS 回环地址申请反向端口，端口不会绑定公网网卡。然后一端
生成 SSH 配对码，另一端输入或扫描该码；配对码通过
`com.rexvane.inkhole/ssh-pair-v1` 下的 SPAKE2 认证，交换设备名和固定 Noise
公钥。配对成功后设备保存到列表，SSH 断线会自动重连。

共享传输口令、私钥、私钥口令和 Noise 私钥不写普通配置：桌面使用系统凭据存储，
Android 使用 Keystore 加密存储。旧版明文共享口令首次启动时自动迁移并清除；
文件模式只保存 SAF URI 或路径，使用时再读取。
默认开启 Noise 外层端到端加密；若用户关闭该选项，SSH 链路仍然加密，但
VPS 管理员可能看到转发内容和元数据。

## 共享核心

```text
Windows/macOS UI -> JSON sidecar -> transport-core -> 已认证 loopback 端点
Android UI       -> gomobile AAR -> transport-core -> 已认证 loopback 端点
```

`transport-core` 负责 Magic Wormhole、SSH 反向转发、SPAKE2、Noise IK 和
yamux。每个跨网会话都映射为本机回环端点：

- 发送端连接端点前发送 `IKAT + 随机能力令牌`；
- 核心向接收节点回注前发送 `IKCI + 节点令牌`；
- 令牌只存在于进程内，不广播、不持久化。

这样 Python 和 Kotlin 仍然只处理同一套 WHPP/WHF1 字节协议，局域网、
Tailscale、短码和 SSH 的文件名、目录结构、强制 ACK/SHA-256、断点续传、
进度、取消与原子落盘行为保持一致。桌面和 Android 在发布正式目标前都会写
`.commit.json` 提交日志；若进程在改名后、写完成回执前退出，发送方重试时会
重新校验目标或 WHF1 检查点，补写回执并复用原目标。路径越界、符号链接、大小
或摘要不匹配时不会恢复成功。

## 安全与限制

- WHPP v3 会校验发送设备的 ECDSA 签名，但局域网默认不要求该设备已被信任；
  公共 WiFi 应开启“仅接收目标设备”。首次选择设备会固定公钥指纹，设置页
  可以查看并撤销。
- Magic Wormhole 会合服务和 Transit 只看到加密会话元数据，不保存文件。
- Tailscale 无法点对点时可能经过 DERP 加密中继；SSH 模式由用户自己的 VPS
  转发流量。
- Android 前台服务持有 WiFiLock，锁屏后仍保持监听；系统厂商若强行停止应用，
  设备自然会离线，服务下次启动会恢复。
- Android 10+ 导出到 `Download/InkHole`。受 Scoped Storage 限制，完全空的
  目录不能可靠创建；包含文件的目录结构会保留。接收完成 ACK 已发出、公共目录
  尚未导出时若进程退出，下次启动会扫描完成回执继续导出，并按 `transfer_id`
  合并接收历史。

## 构建与验证

桌面依赖：

```bash
pip install -e ".[gui,test]"
```

共享核心：

```bash
cd transport-core
make test
make build-desktop
```

Android AAR 和 APK：

```bash
cd transport-core
make init-gomobile
make build-android
cd ../android
./gradlew testDebugUnitTest lintDebug assembleDebug
```

根目录 `make test` 会依次运行 116 项 Python 桌面测试和 Go 共享核心 race 测试。
Android CI 会在干净环境安装固定版本的 NDK，现场生成 AAR，再运行 42 项单测、Lint
和 APK 构建。正式标签会等待 Windows、macOS 与 Android 产物全部构建、签名和
校验成功后再一次性创建 Release。第三方依赖和本地 `wormhole-william` fork 见
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。

## 名称说明

`Magic Wormhole` 和 `wormhole-william` 是已有开源项目名称，不是墨洞的自创
命名。墨洞自己的标识包括应用名称 InkHole/墨洞、专用 AppID
`com.rexvane.inkhole/transport-v1`、`inkhole://` 深链、共享传输核心，以及
WHPP/WHF1 文件流实现。`wormhole-william` 的本地源码仅用于建立原始加密隧道、
代理和连接可靠性修复；其上游许可证仍保留在 `transport-core/third_party/`。
