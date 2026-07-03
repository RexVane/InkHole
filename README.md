# Wormhole

[![CI](https://github.com/RexVane/Wormhole/actions/workflows/ci.yml/badge.svg)](https://github.com/RexVane/Wormhole/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 局域网 P2P 文件传输：把文件拖进桌面上的黑洞桌宠，文件直接出现在另一台电脑上。无需服务器。

两台电脑连同一个 WiFi，各跑一个虫洞桌宠，mDNS 自动发现彼此，拖文件进去就 TCP 直连传过去。支持端到端加密、右键选目标设备、开机自启。

## Quick Start

```bash
# 1. 装依赖
pip install PySide6 zeroconf cryptography

# 2. 两台电脑各跑一个
PYTHONPATH=src python -m pyftp_server.wormhole.pet
PYTHONPATH=src python -m pyftp_server.wormhole.pet --name 我的Mac

# 3. 右键桌宠选目标设备 → 拖文件进去 → 传过去
```

也可以直接 `python -m pyftp_server`（等价于启动桌宠）。

无图形界面时用命令行版：

```bash
PYTHONPATH=src python -m pyftp_server.wormhole.p2p --inbox ~/Wormhole/收件箱 --outbox ~/Wormhole/发件箱
```

## Features

- **mDNS 自动发现**：zeroconf 注册 `_wormhole._tcp.local.` 服务，局域网内自动发现其他虫洞设备
- **TCP 直连传输**：WHPP 协议（magic + JSON 头 + 文件数据），不经过任何中转服务器
- **右键选目标设备**：发现多台设备时手动选择发给谁，不自动选中、不自动切换
- **端到端加密**：`--secret 口令` 启用 AES-256-GCM，传输全程只见密文
- **黑洞桌宠**：PySide6 + QML，无边框透明置顶可拖动，拖入吸入动画 / 收到喷出动画
- **开机自启**：右键菜单可勾选，Windows 注册表 / macOS LaunchAgent / Linux .desktop
- **路径穿越防御**：接收方 basename 裁剪文件名，禁止 `../` 越权

## 启动参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--inbox` | 收件箱目录 | 随平台（Win: `~/OneDrive/Desktop/wormhole`，Mac: `~/Documents/wormhole`） |
| `--port` | P2P 监听端口（0 = 操作系统自动分配） | `0` |
| `--name` | 本机显示名（对端右键菜单里看到的名字） | 主机名 |
| `--secret` | 端到端加密口令（两台电脑必须一致） | 关 |
| `--size` | 挂件边长像素（0 = 随系统自适应） | `0` |

## 轻量 app（免装 Python，双击即用）

```bash
# Windows
cd packaging && build-windows.bat        # 产物:packaging\dist\虫洞桌宠.exe

# macOS
cd packaging && bash build-mac.sh         # 产物:packaging/dist/虫洞桌宠.app
```

详见 [packaging/README.md](packaging/README.md)。

## Tests

```bash
make test        # P2P 端到端测试
```

覆盖：TCP 直连传输、端到端加密、设备选择切换、对端离线、回调触发、多文件连续发送、路径穿越防御。7 组 28 项全通过。

同名文件直接覆盖（新版本替掉旧版本）。

## Project Structure

```text
.
├── src/pyftp_server/
│   ├── __init__.py            # 顶层包
│   ├── __main__.py            # 入口(python -m pyftp_server 启动桌宠)
│   └── wormhole/              # 虫洞文件传输
│       ├── p2p.py             # P2P 引擎(mDNS 发现 + TCP 直连 + 可选加密)
│       ├── crypto.py          # 端到端加密(AES-256-GCM)
│       ├── pet.py             # 桌宠挂件(PySide6+QML)
│       └── wormhole.qml       # 黑洞虫洞视觉与动画
├── tests/
│   └── test_p2p.py            # P2P 端到端测试
├── packaging/                 # 轻量 app 打包(PyInstaller -> .exe/.app)
├── docs/                      # 使用与实现文档
├── .github/workflows/         # CI
├── Makefile
├── pyproject.toml
└── README.md
```

## License

本项目采用 [MIT License](LICENSE) 开源，Copyright (c) 2026 RexVane。

欢迎学习、使用、改造与二次开发。
