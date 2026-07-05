# 墨洞桌宠 · 轻量 app 打包

把墨洞桌宠客户端(`src/inkhole/pet.py`,PySide6 + QML)打包成免装 Python 的可执行程序。
Windows 产出 **onedir 目录**(压成 zip 发布,就地运行不写临时目录,绕开 Defender 拦截),
macOS 产出**单文件 `.app`**。产物名用 ASCII(`InkHolePet`):GitHub Release 附件不支持非 ASCII 文件名。

## 目录

| 文件 | 作用 |
|---|---|
| `inkhole-pet.spec` | PyInstaller 打包规格(跨平台,Win/.exe + Mac/.app 同一份) |
| `pet_entry.py` | 打包入口脚本(解决 `pet.py` 相对导入,使其可作顶层入口) |
| `build-windows.bat` | Windows 一键构建 |
| `build-mac.sh` | macOS 一键构建 |

## 构建

**Windows**
```bat
cd packaging
build-windows.bat
:: 产物:packaging\dist\InkHolePet\ (onedir:InkHolePet.exe + _internal 依赖目录,整体压 zip 发布)
```

**macOS**
```bash
cd packaging
bash build-mac.sh
# 产物:packaging/dist/InkHolePet.app
```

依赖(脚本会自动补装):`PySide6`、`zeroconf`、`cryptography`、`pyinstaller`;
macOS 上「挂件常驻所有桌面」效果另需 `pyobjc-framework-Cocoa`(可选,不装功能不受影响)。

## 运行

打包后的程序接受与 `pet.py` 完全相同的命令行参数:

```
InkHolePet --name 我的电脑 \
        --secret '<两端一致的端到端加密口令>' \
        --inbox ~/InkHole/收件箱
```

- `--name`:本机显示名,对端右键菜单里看到的就是这个名字(默认主机名)
- `--secret`:端到端加密口令,两台设备必须一致
- `--inbox`:收件箱目录(默认随平台:Win `~/OneDrive/Desktop/inkhole`，Mac `~/Documents/inkhole`)
- `--port`:P2P 监听端口(0 = 操作系统自动分配)
- `--size N`:挂件边长像素(0 = 随屏幕自适应)
- 收到的文件落在收件箱目录

> 直接双击不带参数时,会以默认值(主机名、无加密)启动。
> P2P 模式下无需指定服务器地址——mDNS 自动发现局域网内的其他墨洞设备。

## 体积说明

包含整个 Qt Quick 运行时:Windows zip 约 170MB(解压后约 425MB)、macOS zip 约 150MB,
属 PySide6 应用的正常范围。spec 已排除 WebEngine、Multimedia、3D、Charts 等无用重型模块以尽量压缩。

## 原理要点

- `inkhole.qml` 作为数据文件打进包;运行时 `pet.py:_qml_path()` 优先从
  PyInstaller 解压目录 `sys._MEIPASS/inkhole/` 读取,源码运行时
  回退到模块同级目录——同一份代码兼容两种环境。
- `console=False`:GUI 程序不弹命令行黑窗。
- 加密所需的 `cryptography` 由 PyInstaller 钩子自动收集进包。
