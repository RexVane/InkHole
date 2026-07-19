# 墨洞桌面客户端 · PyInstaller 打包

把墨洞桌面客户端（`pet.py` 应用生命周期、`mainwindow.py` QtWidgets 主窗口、`inkhole.qml` 桌宠）打包成免装 Python 的程序。
Windows 产出 **onedir 目录**（压成 zip 发布，就地运行不写临时目录，减少 Defender 误报），macOS 产出标准 `.app` bundle。产物名使用 ASCII（`InkHolePet`），界面显示名仍为「墨洞」。

## 目录

| 文件 | 作用 |
|---|---|
| `inkhole-pet.spec` | PyInstaller 打包规格（Windows onedir 与 macOS .app 共用） |
| `pet_entry.py` | 打包入口脚本（处理 `pet.py` 相对导入） |
| `generate-icons.py` | 从共享品牌绘制代码生成 PNG/ICO/ICNS |
| `build-windows.bat` | Windows 一键构建 |
| `build-mac.sh` | macOS 一键构建 |

## 构建

**Windows**
```bat
cd packaging
build-windows.bat
:: 产物:packaging\dist\InkHolePet\（InkHolePet.exe + _internal，整体压 zip 发布）
```

**macOS**
```bash
cd packaging
bash build-mac.sh
# 产物:packaging/dist/InkHolePet.app
```

依赖（构建脚本会自动补装）：`PySide6`、`zeroconf`、`cryptography`、`psutil`、`pyinstaller`。生成品牌图标时另需 `Pillow`；macOS 使用 `pyobjc-framework-Cocoa` 提供原生文件/文件夹混选和「挂件常驻所有桌面」，缺少时选择器回退 Qt 版本且跳过 Spaces 增强。

桌面任务栏、托盘、Windows 可执行文件和 macOS app bundle 使用同一双弧墨洞图标。修改 `src/inkhole/branding.py` 后安装 Pillow，并运行 `python packaging/generate-icons.py`，再提交 `assets/inkhole.png`、`.ico` 和 `.icns`。

## 运行

Windows 解压后运行 `InkHolePet\InkHolePet.exe`；打包程序接受与 `pet.py` 相同的命令行参数：

```
InkHolePet --name 我的电脑 \
        --secret '<两端一致的端到端加密口令>' \
        --inbox ~/InkHole/收件箱
```

- `--name`：本机显示名，对端设备列表里看到的就是这个名字（默认主机名）
- `--secret`：端到端加密口令，两台设备必须一致
- `--inbox`：收件箱目录（Windows 使用系统已知桌面目录并兼容 OneDrive 重定向；macOS 默认 `~/Documents/inkhole`）
- `--port`：P2P 监听端口（0 = 操作系统自动分配）
- `--size N`：挂件边长像素（0 = 随屏幕自适应）
- 收到的文件落在收件箱目录

> 直接双击会打开 `960×640` 桌面主窗口；设备名、口令、收件箱、桌宠显示和开机自启可在设置页管理。设备通过 mDNS 自动发现；自动发现不可用时可在设置里手动添加对方 IP 与端口直连。

## 体积说明

包含整个 Qt Quick 运行时，属于 PySide6 应用的正常范围。`inkhole-pet.spec` 在收集清单上过滤掉 WebEngine、Multimedia、3D、Charts、Quick3D、Pdf 等界面用不到的重型 Qt 模块（PyInstaller 的 `excludes` 只挡 Python 绑定，挡不住这些按目录整体收进来的 Qt DLL，故改为在 `binaries`/`datas` 上按名称过滤）。优化后 Windows 解压后约 **164MB**（压 zip 发布约 70MB），macOS zip 约 150MB。

> 单个 `Qt6WebEngineCore.dll`（浏览器内核）就有 195MB 且本应用完全不用——这是过滤的主要收益来源。若要进一步做到几 MB 级别，需改用 Tauri/C++ 等原生技术栈重写整个桌面端（后端 P2P 引擎也要用 Rust 重写才能保持体积优势），代价是全部功能推倒重做与重新验证，不建议。

## 原理要点

- `mainwindow.py` 随 Python 模块自动收集，提供 QtWidgets 主窗口；`inkhole.qml` 作为数据文件打进包。运行时 `pet.py:_qml_path()` 优先从
  PyInstaller 解压目录 `sys._MEIPASS/inkhole/` 读取，源码运行时
  回退到模块同级目录——同一份代码兼容两种环境。
- `console=False`：GUI 程序不弹命令行窗口。
- 加密所需的 `cryptography` 由 PyInstaller 钩子自动收集进包。
