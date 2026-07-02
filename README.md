# Wormhole

[![CI](https://github.com/RexVane/Wormhole/actions/workflows/ci.yml/badge.svg)](https://github.com/RexVane/Wormhole/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 基于 Python 标准库 socket 实现的 FTP 服务器 + 局域网 P2P 虫洞文件传输。FTP 支持三种并发模型、断点续传、chroot 隔离、限速；虫洞用 mDNS 自动发现设备、TCP 直连传文件，无需任何服务器。

PyFTP 是一个 FTP 服务器，严格遵循 RFC 959 的控制连接/数据连接分离机制，能被 FileZilla、Windows 自带 `ftp` 命令、浏览器 `ftp://` 正常连接、登录、浏览、上传、下载。在 FTP 之上还搭了一个「虫洞」——局域网点对点文件传输应用，拖文件进黑洞桌宠就直连发给对端，不需要架服务器。模块按职责拆分，便于阅读、运行、测试与维护。

## Features

基础功能

- 控制连接与数据连接分离，支持主动 `PORT` 与被动 `PASV` 两种模式。
- 命令集：`USER PASS PWD CWD CDUP MKD RMD DELE RNFR RNTO SIZE TYPE SYST FEAT OPTS NOOP PORT PASV LIST NLST RETR STOR REST QUIT`。
- 标准三位状态码响应（220/331/230/150/226/550 等）。

进阶特性

- **三种并发模型可切换**：多线程 `thread`、多进程 `process`(fork)、I/O 多路复用 `select`(selectors/epoll)。通过 `--model` 选择，便于做"并发数 vs 吞吐量"对比实验。
- **断点续传**：`REST` 命令，下载与上传都支持从指定偏移续传。
- **FTPS 加密**：显式 TLS(RFC 4217)，`AUTH TLS`/`PBSZ`/`PROT P`，控制与数据连接全加密；`make cert` 一键自签证书，`--tls` 启用。
- **安全加固**：防目录穿越（禁止 `../` 越权）、每用户独立根目录（chroot 风格隔离）、登录失败次数限制与锁定、可选传输限速。
- **兼容真实客户端**：FileZilla、Windows `ftp`、浏览器 `ftp://`。

## Quick Start

```bash
make run                                   # 默认多线程, 端口 2121
```

或直接用模块入口（推荐，支持全部启动参数）：

```bash
PYTHONPATH=src python3 -m pyftp_server                 # 默认 thread 模型
PYTHONPATH=src python3 -m pyftp_server --model process # 多进程模型
PYTHONPATH=src python3 -m pyftp_server --model select  # I/O 多路复用模型
PYTHONPATH=src python3 -m pyftp_server --port 21       # 标准端口(需 sudo)
PYTHONPATH=src python3 -m pyftp_server --rate-limit 1048576  # 限速 1MB/s
PYTHONPATH=src python3 -m pyftp_server --help          # 查看全部参数
```

默认监听 `0.0.0.0:2121`，总根目录 `examples/ftproot`。

默认账号（体现每用户 chroot 隔离）：

```text
admin     / admin       根目录可读写
alice     / alice123    隔离在 users/alice, 可读写
bob       / bob123      隔离在 users/bob, 可读写
user      / 123456      根目录可读写(兼容旧测试)
anonymous / 任意         隔离在 pub, 只读
```

## 三种并发模型说明

| 模型 | 参数 | 原理 | 适用场景 |
|------|------|------|----------|
| 多线程 | `--model thread` | 每连接一个线程，one-thread-per-connection | I/O 密集，连接数中等，默认推荐 |
| 多进程 | `--model process` | 每连接 fork 一个子进程，绕开 GIL | CPU 较重、需进程隔离（依赖 `os.fork`，Unix/macOS） |
| I/O 多路复用 | `--model select` | 单线程用 selectors 管理所有控制连接事件 | 海量空闲连接、轻量并发 |

> 说明：`select` 模型中数据传输（RETR/STOR）期间会临时切回阻塞，是一处简化取舍，因此高并发大文件传输时吞吐略低于线程模型——这也是性能上值得权衡的点。

## Tests

```bash
make test                                  # FTP 端到端功能测试
PYTHONPATH=src python3 tests/test_wormhole.py  # 虫洞 FTP 中转模式测试
PYTHONPATH=src python3 tests/test_p2p.py       # 虫洞 P2P 直连模式测试
```

FTP 测试覆盖：登录认证、错误密码拒绝、LIST/NLST、CWD/PWD/CDUP、RETR 下载、STOR 上传、SIZE、REST 断点续传、RNFR/RNTO 重命名、DELE/MKD/RMD、目录穿越防御、每用户 chroot 隔离、匿名只读、主动/被动模式、10 客户端并发。

P2P 测试覆盖：TCP 直连传输、端到端加密、设备选择切换、对端离线自动切换、回调触发、多文件连续发送、暂停/恢复、路径穿越防御。

## 性能对比测试

```bash
make bench                                 # 默认 5MB 文件, 并发 1/5/10/20
PYTHONPATH=src python3 tests/benchmark.py --size-mb 10 --concurrency 1,5,10,20,50
```

脚本会依次以三种并发模型启动服务器，对同一文件做并发下载，输出耗时与吞吐量对比表，便于做性能分析。

## 用 FileZilla 连接

1. 打开 FileZilla，站点管理器或快速连接栏。
2. 主机 `127.0.0.1`，端口 `2121`，用户 `admin`，密码 `admin`。
3. 加密选择「只使用普通 FTP（不安全）」。
4. 传输设置建议选「被动」模式（PASV）。
5. 连接后即可浏览、上传、下载。

## 用命令行连接

Windows 自带 `ftp`（默认主动模式）：

```text
ftp> open 127.0.0.1 2121
用户: admin
密码: admin
ftp> dir
ftp> cd docs
ftp> get poem.txt
ftp> put local.txt
ftp> bye
```

> Windows `ftp` 不便指定非 21 端口，可用 `open` 子命令；或直接用 FileZilla / Python `ftplib` 测试 2121 端口。浏览器可访问 `ftp://admin:admin@127.0.0.1:2121/`（部分新版浏览器已移除 FTP 支持）。

## 虫洞文件传输（Wormhole）

局域网点对点文件传输：把文件拖进桌面上的「虫洞」桌宠（黑洞造型、乳白吸积盘、小图标大小），文件就会直接出现在另一台电脑的收件箱里。**无需架设任何服务器**——两台电脑用 mDNS（zeroconf）自动发现彼此，TCP 直连传输文件。

```bash
# 两台电脑各跑一个（无需服务器）
pip install PySide6 zeroconf cryptography
PYTHONPATH=src python3 -m pyftp_server.wormhole.pet
PYTHONPATH=src python3 -m pyftp_server.wormhole.pet --name 我的Mac

# 拖文件进桌宠 → 右键选目标设备 → 直连传输
# 无图形界面时用命令行版
PYTHONPATH=src python3 -m pyftp_server.wormhole.p2p --inbox ~/Wormhole/收件箱 --outbox ~/Wormhole/发件箱
```

支持**端到端加密**：`--secret 口令` 启用 AES-256-GCM，传输全程只见密文。

详见 [docs/wormhole-虫洞文件传输.md](docs/wormhole-虫洞文件传输.md)。测试：`PYTHONPATH=src python3 tests/test_p2p.py`。

### 轻量 app（免装 Python，双击即用）

可把虫洞桌宠打包成**单文件可执行**分发给没有 Python 环境的电脑：Windows 产出 `.exe`，macOS 产出 `.app`。

```bash
# Windows
cd packaging && build-windows.bat        # 产物:packaging\dist\虫洞桌宠.exe

# macOS
cd packaging && bash build-mac.sh         # 产物:packaging/dist/虫洞桌宠.app
```

打包后程序接受与 `pet.py` 完全相同的参数（`--name --secret --inbox` 等）。
详见 [packaging/README.md](packaging/README.md)。

## Project Structure

```text
.
├── .github/workflows/         # CI 测试
├── docs/                      # 使用与实现文档
├── examples/ftproot/          # 示例 FTP 根目录(含 users/alice, users/bob, pub)
├── src/pyftp_server/
│   ├── config.py              # 配置与运行期参数对象、用户表
│   ├── cli.py                 # 命令行入口(argparse)
│   ├── server.py              # 三种并发模型主程序
│   ├── session.py             # 会话状态与控制连接主循环
│   ├── commands.py            # 命令解析与分发表 + 所有命令处理
│   ├── datachannel.py         # PORT/PASV 数据连接管理 + 限速收发
│   ├── auth.py                # 用户认证 + 登录失败锁定
│   ├── fs.py                  # 文件系统操作 + 路径穿越防御 + 每用户 chroot
│   ├── throttle.py            # 令牌桶限速
│   ├── utils.py               # 日志工具
│   └── wormhole/              # 虫洞文件传输(P2P 局域网直连)
│       ├── p2p.py             # P2P 引擎(mDNS 发现 + TCP 直连 + 可选加密)
│       ├── sync.py            # FTP 中转同步引擎(旧模式，保留兼容)
│       ├── crypto.py          # 端到端加密(AES-256-GCM)，p2p/sync 共用
│       ├── pet.py             # 桌宠挂件(PySide6+QML)
│       └── wormhole.qml       # 黑洞虫洞视觉与动画
├── tests/
│   ├── test_ftp.py            # FTP 端到端功能测试
│   ├── test_wormhole.py       # 虫洞 FTP 中转模式测试
│   ├── test_p2p.py            # 虫洞 P2P 直连模式测试
│   └── benchmark.py           # 三种并发模型性能对比脚本
├── packaging/                 # 轻量 app 打包(PyInstaller -> .exe/.app)
│   ├── wormhole-pet.spec      # 跨平台打包规格
│   ├── pet_entry.py           # 打包入口
│   ├── build-windows.bat      # Windows 一键构建
│   ├── build-mac.sh           # macOS 一键构建
│   └── README.md              # 打包说明
├── Makefile                   # 常用命令入口
├── pyproject.toml             # Python 项目元数据
└── README.md                  # 项目说明
```

## License

本项目采用 [MIT License](LICENSE) 开源，Copyright (c) 2026 RexVane。

欢迎学习、使用、改造与二次开发。
