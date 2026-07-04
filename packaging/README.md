# 墨洞桌宠 · 轻量 app 打包

把墨洞桌宠客户端(`src/wormhole/pet.py`,PySide6 + QML)打包成
**单文件可执行**,双击即用、免装 Python。Windows 产出 `.exe`,macOS 产出 `.app`。

## 目录

| 文件 | 作用 |
|---|---|
| `wormhole-pet.spec` | PyInstaller 打包规格(跨平台,Win/.exe + Mac/.app 同一份) |
| `pet_entry.py` | 打包入口脚本(解决 `pet.py` 相对导入,使其可作顶层入口) |
| `build-windows.bat` | Windows 一键构建 |
| `build-mac.sh` | macOS 一键构建 |

## 构建

**Windows**
```bat
cd packaging
build-windows.bat
:: 产物:packaging\dist\墨洞桌宠.exe
```

**macOS**
```bash
cd packaging
bash build-mac.sh
# 产物:packaging/dist/墨洞桌宠.app
```

依赖(脚本会自动补装):`PySide6`、`zeroconf`、`cryptography`、`pyinstaller`;
macOS 上「挂件常驻所有桌面」效果另需 `pyobjc-framework-Cocoa`(可选,不装功能不受影响)。

## 运行

打包后的程序接受与 `pet.py` 完全相同的命令行参数:

```
墨洞桌宠 --name 我的电脑 \
        --secret '<两端一致的端到端加密口令>' \
        --inbox ~/Wormhole/收件箱
```

- `--name`:本机显示名,对端右键菜单里看到的就是这个名字(默认主机名)
- `--secret`:端到端加密口令,两台设备必须一致
- `--inbox`:收件箱目录(默认随平台:Win `~/OneDrive/Desktop/wormhole`，Mac `~/Documents/wormhole`)
- `--port`:P2P 监听端口(0 = 操作系统自动分配)
- `--size N`:挂件边长像素(0 = 随屏幕自适应)
- 收到的文件落在收件箱目录

> 直接双击不带参数时,会以默认值(主机名、无加密)启动。
> P2P 模式下无需指定服务器地址——mDNS 自动发现局域网内的其他虫洞设备。

## 体积说明

单文件包含整个 Qt Quick 运行时,体积约 150–170MB,属 PySide6 应用的正常范围。
spec 已排除 WebEngine、Multimedia、3D、Charts 等无用重型模块以尽量压缩。

## 原理要点

- `wormhole.qml` 作为数据文件打进包;运行时 `pet.py:_qml_path()` 优先从
  PyInstaller 解压目录 `sys._MEIPASS/wormhole/` 读取,源码运行时
  回退到模块同级目录——同一份代码兼容两种环境。
- `console=False`:GUI 程序不弹命令行黑窗。
- 加密所需的 `cryptography` 由 PyInstaller 钩子自动收集进包。
