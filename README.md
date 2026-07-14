# 墨洞 InkHole

[![CI](https://github.com/RexVane/InkHole/actions/workflows/ci.yml/badge.svg)](https://github.com/RexVane/InkHole/actions/workflows/ci.yml)
[![Android APK](https://github.com/RexVane/InkHole/actions/workflows/android.yml/badge.svg)](https://github.com/RexVane/InkHole/actions/workflows/android.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 局域网直连或 SSH 跨网络传输：桌面与 Android 选择设备发送，文件直接出现在对端。

局域网模式使用 mDNS + TCP 直连。远程模式让设备登录用户自己的 OpenSSH 服务器，通过回环反向端口转发互通。服务器无需安装墨洞，两种模式严格二选一，Windows / macOS / Android 三平台互通。

## Quick Start

```bash
# 1. 装依赖
pip install PySide6 zeroconf cryptography psutil paramiko

# 2. 两台电脑各跑一个
PYTHONPATH=src python -m inkhole.pet
PYTHONPATH=src python -m inkhole.pet --name 我的Mac

# 3. 在主窗口选择目标设备 → 点击选择文件或直接拖入 → 传过去
```

也可以直接 `python -m inkhole`（等价于启动桌宠）。

## SSH 远程模式

在设备栏切换到“远程”，填写云服务器地址和 SSH 私钥即可。服务器只需启用
OpenSSH、公钥登录、SFTP 和 `AllowTcpForwarding yes`，不需要 `root`、Docker、
Web 服务或数据库，安全组也只需开放 SSH 端口。

所有需要互相发现的设备必须使用同一服务器、同一 SSH 账户和同一份私钥文本。
首次连接必须核对 SSH 主机密钥指纹；私钥与口令不写入应用配置。完整 WHPP 流
（包括文件名与 ACK）强制使用 P-256 ECDH + HKDF-SHA256 + AES-256-GCM 端到端
加密。详见 [服务器配置](docs/remote-relay.md) 与 [SSH 远程协议](docs/relay-protocol.md)。

无图形界面时用命令行版：

```bash
PYTHONPATH=src python -m inkhole.p2p --inbox ~/InkHole/收件箱 --outbox ~/InkHole/发件箱
```

## Windows 桌面端

![InkHole Windows desktop UI](docs/assets/inkhole-windows.png)

Windows 主窗口采用双栏工作台布局：左侧固定为发送目标、墨洞动画、文件选择和状态反馈，右侧集中显示附近设备与最近接收。设置页继续在同一窗口内切换，不弹出额外对话框。

默认窗口为 `960×640`，最小支持 `800×580`；长设备名、文件名和状态文本会自动省略。墨洞动画使用时间驱动的 60fps 刷新，窗口隐藏或切到设置页时自动停表；设备卡片、页面切换、拖拽状态、布尔开关和传输进度均有短时缓动。

## Features

- **mDNS 自动发现**：注册 `_inkhole._tcp.local.` 服务，局域网内自动发现其他墨洞设备；服务名带唯一实例 ID，两台同名设备不冲突；宣告本机物理网卡地址（自动过滤 VMware/VirtualBox/Hyper-V 等虚拟网卡与 169.254 链路本地地址），开 VPN/多网卡也能连上
- **局域网 / 远程二选一**：上次模式持久化，切换会停止旧引擎并清空目标；传输期间禁止切换
- **SSH 跨网络通道**：只依赖标准 OpenSSH、SFTP 与回环反向端口转发，不安装服务端程序，不公开随机转发端口
- **远程强制端到端加密**：每次传输使用独立 ECDH/HKDF 密钥和 64 KiB AES-GCM 严格序号帧；服务器不保存文件
- **应用内主界面**：深色双栏工作台——稳定的发送区、附近设备、最近接收、应用内设置页和自适应长文本；设备名/口令/收件箱/自启等设置集中管理，桌宠挂件保留为可开关选项
- **统一应用图标**：标题栏双弧墨洞标记同时用于桌面任务栏/托盘、Windows 可执行文件、macOS app、Android 自适应/主题图标和通知图标
- **TCP 直连传输**：WHPP 协议（magic + JSON 头 + 文件数据 + 1 字节回执），不经过任何中转服务器
- **传输回执（ACK）**：接收方落盘成功才算发送成功——对端解密失败、磁盘满、被拒收，发送方都能感知
- **传输进度**：主窗口、桌宠和 Android 端均显示墨洞进度环；发送使用青绿色，主窗口接收使用暖金色
- **端到端加密**：AES-256-GCM；超过 32MB 的文件自动切换 4MB 分块流式加密（WHE2），内存峰值恒定，块序号防篡改/防重排
- **发送队列**：一次拖一堆文件按序排队发送，完成后聚合报告"已吞入 k/N 个"
- **文件夹传输**：桌面端拖入文件夹自动递归打包成 `<文件夹名>.zip` 发送（保留完整目录结构），对端收到即一个 zip 文件（不自动解压，规避路径穿越）；Android 端可正常接收
- **仅接收目标设备**：可选开关，拦掉局域网里陌生设备发来的文件
- **设置持久化**：设备名/口令/收件箱改一次就记住（桌面 `config.json`，Android `SharedPreferences`），双击 exe 无需命令行参数
- **墨洞桌宠**：PySide6 + QML——墨黑核心、青色吸积弧缓慢旋转、呼吸光晕；拖入碎裂吞入动画 / 收到拼合吐出动画；贴边自动收起
- **Android 前台服务**：锁屏/切后台持续接收；收到的文件进系统 `Download/InkHole`（文件管理器可见）并发通知，点击直接打开；接收历史持久化
- **系统分享入口**：手机上任意 App 分享 → 墨洞，选中设备即发送，支持多选
- **开机自启**：在应用内设置页切换，Windows 注册表 / macOS LaunchAgent / Linux .desktop（不含明文口令）
- **接收防御**：文件名 basename 裁剪防 `../` 穿越；size 合法性校验 + 磁盘余量检查；半截文件绝不落盘

## 安全模型（请阅读）

局域网模式面向**可信局域网**（家里/自己的路由器）设计：

- **默认接收无发送方认证**：同一网络里任何运行墨洞协议的人都可以向你的收件箱发送文件。公共 WiFi 建议在设置页开启**「仅接收目标设备」**，或设置加密口令。
- **口令的双重作用**：`--secret` 保证文件内容端到端加密；口令不一致的文件会被拒收并回执失败——同时起到"只接收知道口令的设备"的准认证作用。
- **口令明文存储在本机配置**（桌面 `config.json` / Android `SharedPreferences`），与浏览器记住密码同级别；它不会进开机自启脚本或注册表。
- 局域网传输不经过服务器。远程模式经过用户指定的 SSH 服务器，但完整 WHPP 字节流强制端到端加密。
- 远程设备登记以共享私钥派生的 HMAC 认证；设备 E2E 私钥只保存在客户端。SSH 私钥和口令只存在当前应用进程内存中。

## 启动参数

参数改一次就会记住（写入配置文件），下次双击直接生效；桌面端也可以在应用内设置页修改。

| 参数 | 说明 | 默认 |
|------|------|------|
| `--inbox` | 收件箱目录 | 随平台（Win: `桌面\inkhole`（自动识别 OneDrive 重定向），Mac: `~/Documents/inkhole`） |
| `--port` | P2P 监听端口（0 = 操作系统自动分配） | `0` |
| `--name` | 本机显示名（对端菜单里看到的名字） | 主机名 |
| `--secret` | 端到端加密口令（两台设备必须一致） | 关 |
| `--size` | 挂件边长像素（0 = 随系统自适应） | `0` |

## 下载（免装环境，直接用）

前往 [Releases](https://github.com/RexVane/InkHole/releases) 下载：

| 平台 | 文件 | 用法 |
|------|------|------|
| Windows | `InkHolePet-windows.zip` | 解压后双击 `InkHolePet\InkHolePet.exe` |
| macOS | `InkHolePet-macos.zip` | 解压拖进"应用程序" |
| Android | `InkHole-v*.apk` | 传到手机安装 |

也可以自行打包：

```bash
# Windows
cd packaging && build-windows.bat        # 产物:packaging\dist\InkHolePet\

# macOS
cd packaging && bash build-mac.sh         # 产物:packaging/dist/InkHolePet.app

# Android
cd android && ./gradlew assembleDebug     # 产物:android/app/build/outputs/apk/debug/app-debug.apk
# 或 GitHub Actions 自动构建: gh workflow run android.yml
```

## 常见问题

### Windows：提示"Windows 已保护你的电脑"

应用未做代码签名，SmartScreen 会拦截首次运行：点击**更多信息** → **仍要运行**。

### Windows：首次运行弹出防火墙提示

墨洞需要在局域网收发文件：点击**允许访问**（至少勾选"专用网络"）。

### macOS：提示"无法验证开发者"或"已损坏，无法打开"

右键点击应用 → **打开** → 再点**打开**确认；仍不行则在终端执行：

```bash
xattr -cr /Applications/InkHolePet.app
```

### 找不到其他设备

1. 确认两台设备连的是**同一个 WiFi/网段**（`ipconfig` / `ifconfig` 查看 IP 是否同段）
2. 防火墙放行应用（mDNS 依赖 UDP 5353 端口）
3. 避开访客网络/企业网络——它们常开 AP 隔离，设备间互相不可见
4. 开着 VPN 时若发现异常，先断开 VPN 再试

### 远程模式看不到设备

确认两端都选择“远程”，并使用同一服务器、同一 SSH 用户名和同一份私钥文本。
服务器需要启用 SFTP 与 `AllowTcpForwarding yes`；云安全组只需开放 SSH 端口。

### 问题反馈

提 [Issue](https://github.com/RexVane/InkHole/issues) 时请附上：操作系统与版本、应用版本（如 v1.1.0）、网络环境（家庭 WiFi / 公司网络 / 热点）、具体报错信息或截图。

## Tests

```bash
pytest -q        # 局域网、SSH 远程协议与文件往返测试
cd android && ./gradlew testDebugUnitTest
```

覆盖：TCP 直连、SSH 反向通道契约、跨端 P-256/HKDF/AES-GCM 固定向量、登记与握手认证、文件往返与 ACK、设备选择、离线处理、路径穿越防御、半截文件清理、恶意 size、分块加密、发送队列、同名不覆盖、目录打包和文件夹发送。

桌面端同名文件：收件箱已有同名文件时自动加 " (2)" 后缀，绝不覆盖已有文件；传输中断的半截文件不会落盘。该自动加后缀保证当前由 Python 桌面端测试覆盖，Android 导出到系统下载目录时遵循 Android/MediaStore 的同名项处理方式。

## Project Structure

```text
.
├── src/inkhole/              # Python 包
│   ├── __init__.py            # 顶层包
│   ├── __main__.py            # 入口(python -m inkhole 启动桌宠)
│   ├── p2p.py                 # P2P 引擎(mDNS 发现 + TCP 直连 + ACK/进度 + 加密)
│   ├── crypto.py              # 端到端加密(AES-256-GCM，WHE1 整块 / WHE2 分块流)
│   ├── ssh_relay.py           # SSH 发现、反向端口转发与远程 WHPP/ACK
│   ├── relay_crypto.py        # P-256/HKDF/AES-GCM 严格序号帧
│   ├── transport.py           # 局域网与远程引擎的公共接口
│   ├── branding.py            # 统一品牌标记与桌面多尺寸应用图标
│   ├── pet.py                 # 应用生命周期、桌宠、配置持久化与发送队列
│   ├── mainwindow.py          # QtWidgets 桌面主窗口、设置页与交互动效
│   └── inkhole.qml           # 墨洞视觉与动画(吸积弧/进度环/碎片吞吐)
├── tests/                     # 局域网、SSH 远程与共享跨端密码学向量
├── packaging/                 # 轻量 app 打包(PyInstaller -> .exe/.app)
├── assets/                    # Windows/macOS 打包图标(PNG/ICO/ICNS)
├── android/                   # Android 客户端(Kotlin + Compose + 前台服务)
├── docs/                      # 使用与实现文档
├── .github/workflows/         # CI
├── Makefile
├── pyproject.toml
└── README.md
```

## License

本项目采用 [MIT License](LICENSE) 开源，Copyright (c) 2026 RexVane。

欢迎学习、使用、改造与二次开发。
