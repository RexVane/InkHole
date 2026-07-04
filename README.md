# Wormhole

[![CI](https://github.com/RexVane/Wormhole/actions/workflows/ci.yml/badge.svg)](https://github.com/RexVane/Wormhole/actions/workflows/ci.yml)
[![Android APK](https://github.com/RexVane/Wormhole/actions/workflows/android.yml/badge.svg)](https://github.com/RexVane/Wormhole/actions/workflows/android.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 局域网 P2P 文件传输：把文件拖进桌面上的黑洞桌宠（或手机端虫洞），文件直接出现在另一台设备上。无需服务器。

两台设备连同一个 WiFi，各跑一个虫洞，mDNS 自动发现彼此，拖文件进去就 TCP 直连传过去。支持端到端加密、手动选目标设备、开机自启。Windows / macOS / Android 三平台互通。

## Quick Start

```bash
# 1. 装依赖
pip install PySide6 zeroconf cryptography

# 2. 两台电脑各跑一个
PYTHONPATH=src python -m wormhole.pet
PYTHONPATH=src python -m wormhole.pet --name 我的Mac

# 3. 右键桌宠选目标设备 → 拖文件进去 → 传过去
```

也可以直接 `python -m wormhole`（等价于启动桌宠）。

无图形界面时用命令行版：

```bash
PYTHONPATH=src python -m wormhole.p2p --inbox ~/Wormhole/收件箱 --outbox ~/Wormhole/发件箱
```

## Features

- **mDNS 自动发现**：zeroconf 注册 `_wormhole._tcp.local.` 服务，局域网内自动发现其他虫洞设备；服务名带唯一实例 ID，两台同名设备也不冲突
- **TCP 直连传输**：WHPP 协议（magic + JSON 头 + 文件数据 + 1 字节回执），不经过任何中转服务器
- **传输回执（ACK）**：接收方落盘成功才算发送成功——对端解密失败、磁盘满，发送方都能感知（与 v1.0.0 老版本互通时自动降级）
- **传输进度**：发送/接收实时显示百分比（桌宠提示文字 / Android 状态条）
- **右键选目标设备**：发现多台设备时手动选择发给谁，不自动选中、不自动切换
- **端到端加密**：AES-256-GCM，传输全程只见密文；口令可在右键菜单/App 设置里配置
- **设置持久化**：设备名/口令/收件箱改一次就记住（桌面 `config.json`，Android `SharedPreferences`），双击 exe 无需命令行参数
- **黑洞桌宠**：PySide6 + QML，无边框透明置顶可拖动，拖入吸入动画 / 收到喷出动画
- **Android 前台服务**：锁屏/切后台持续接收；收到的文件进系统 `Downloads/Wormhole`（文件管理器可见）并发通知
- **系统分享入口**：手机上任意 App 分享 → 虫洞，选中设备即发送，支持多选
- **开机自启**：右键菜单可勾选，Windows 注册表 / macOS LaunchAgent / Linux .desktop（不含明文口令）
- **接收防御**：文件名 basename 裁剪防 `../` 穿越；size 合法性校验 + 磁盘余量检查；半截文件绝不落盘

## 安全模型（请阅读）

虫洞面向**可信局域网**（家里/自己的路由器）设计：

- **接收无发送方认证**：同一网络里任何运行虫洞协议的人都可以向你的收件箱发送文件（同名会覆盖旧文件）。在公司、咖啡馆等公共 WiFi 使用请知晓此风险。
- **`--secret` 只加密内容**：口令保证文件内容端到端加密、口令不一致的文件会被拒收——这同时起到"只接收知道口令的设备"的准认证作用。公共网络建议始终设置口令。
- **口令明文存储在本机配置**（桌面 `config.json` / Android `SharedPreferences`），与浏览器记住密码同级别；它不会进开机自启脚本或注册表。
- 传输不经过任何服务器，文件不出局域网。

## 启动参数

参数改一次就会记住（写入配置文件），下次双击直接生效；也可以全部通过右键菜单修改。

| 参数 | 说明 | 默认 |
|------|------|------|
| `--inbox` | 收件箱目录 | 随平台（Win: `桌面\wormhole`（自动识别 OneDrive 重定向），Mac: `~/Documents/wormhole`） |
| `--port` | P2P 监听端口（0 = 操作系统自动分配） | `0` |
| `--name` | 本机显示名（对端右键菜单里看到的名字） | 主机名 |
| `--secret` | 端到端加密口令（两台设备必须一致） | 关 |
| `--size` | 挂件边长像素（0 = 随系统自适应） | `0` |

## 下载（免装环境，直接用）

前往 [Releases](https://github.com/RexVane/Wormhole/releases/tag/v1.0.0) 下载：

| 平台 | 文件 | 大小 | 用法 |
|------|------|------|------|
| Windows | 虫洞桌宠.exe | 165 MB | 双击即用 |
| macOS | 虫洞桌宠.zip | 148 MB | 解压拖进"应用程序" |
| Android | 虫洞-android.apk | 14 MB | 传到手机安装 |

也可以自行打包：

```bash
# Windows
cd packaging && build-windows.bat        # 产物:packaging\dist\虫洞桌宠.exe

# macOS
cd packaging && bash build-mac.sh         # 产物:packaging/dist/虫洞桌宠.app

# Android
# GitHub Actions 自动构建: gh workflow run android.yml
# 或用 Android Studio 打开 android/ 目录构建 APK
```

## Tests

```bash
make test        # P2P 端到端测试
```

覆盖：TCP 直连传输、端到端加密、设备选择切换、对端离线、回调触发、多文件连续发送、路径穿越防御、半截文件不落盘、恶意 size 拒收、同名设备共存与精确离线、口令不一致 ACK 失败、传输进度回调。12 组 48 项全通过（也兼容 `pytest`）。

同名文件：桌面端直接覆盖（新版本替掉旧版本）；Android 端进系统下载目录，同名自动加 " (1)" 后缀（系统行为）。传输中断的半截文件不会落盘、不会覆盖已有文件。

## Project Structure

```text
.
├── src/wormhole/
│   ├── __init__.py            # 顶层包
│   ├── __main__.py            # 入口(python -m wormhole 启动桌宠)
│   ├── p2p.py                 # P2P 引擎(mDNS 发现 + TCP 直连 + ACK/进度 + 可选加密)
│   ├── crypto.py              # 端到端加密(AES-256-GCM)
│   ├── pet.py                 # 桌宠挂件(PySide6+QML) + 设置持久化
│   └── wormhole.qml           # 黑洞虫洞视觉与动画
├── tests/
│   └── test_p2p.py            # P2P 端到端测试(12 组 48 项)
├── packaging/                 # 轻量 app 打包(PyInstaller -> .exe/.app)
├── android/                   # Android 客户端(Kotlin + Jetpack Compose + 前台服务)
├── docs/                      # 使用与实现文档
├── .github/workflows/         # CI
├── Makefile
├── pyproject.toml
└── README.md
```

## License

本项目采用 [MIT License](LICENSE) 开源，Copyright (c) 2026 RexVane。

欢迎学习、使用、改造与二次开发。
