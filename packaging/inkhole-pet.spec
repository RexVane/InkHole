# -*- mode: python ; coding: utf-8 -*-
"""
墨洞桌面客户端打包规格（Windows onedir / macOS .app bundle）。

构建:
    Windows:  cd packaging && pyinstaller inkhole-pet.spec --noconfirm
    macOS:    cd packaging && pyinstaller inkhole-pet.spec --noconfirm
产物:
    Windows:  packaging/dist/InkHolePet/          (InkHolePet.exe + _internal)
    macOS:    packaging/dist/InkHolePet.app       (拖进"应用程序"即用)

产物文件名用 ASCII(InkHolePet)而非中文:GitHub Release 附件不支持非 ASCII
文件名,中文名上传后会被剥成 default.exe 之类。文件名与界面显示名无关——
窗口/任务栏名由 setApplicationName("墨洞")、Finder 名由 CFBundleDisplayName
控制,见下方 info_plist 与 pet.py。

要点:
  - mainwindow.py 随 Python 模块收集；inkhole.qml 作为数据文件打进包。
  - QML 只用 QtQuick / QtQuick.Window,排除一切重型 Qt 模块以压体积。
  - zeroconf 提供 mDNS 局域网设备发现(P2P 模式核心依赖),已列入 hiddenimports。
  - cryptography 提供端到端加密(--secret),由 PyInstaller 钩子自动收集。
  - console=False:GUI 程序不弹黑窗/终端。
  - macOS 上 AppKit(pyobjc)提供原生文件/文件夹混选面板与挂件常驻所有桌面;
    未装时选择器回退 Qt 版本,Spaces 增强自动跳过。
"""
import os
import re
import sys

PROJ = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(PROJ, "src")
QML = os.path.join(SRC, "inkhole", "inkhole.qml")
CORE_NAME = "inkhole-core.exe" if sys.platform == "win32" else "inkhole-core"
CORE_BINARY = os.path.join(PROJ, "transport-core", "bin", CORE_NAME)
ICON_ICO = os.path.join(PROJ, "assets", "inkhole.ico")
ICON_ICNS = os.path.join(PROJ, "assets", "inkhole.icns")
APP_ICON = ICON_ICNS if sys.platform == "darwin" else ICON_ICO
CODESIGN_IDENTITY = os.environ.get("MACOS_SIGNING_IDENTITY") or None
with open(os.path.join(PROJ, "pyproject.toml"), encoding="utf-8") as version_file:
    version_match = re.search(
        r'^version\s*=\s*"([^"]+)"', version_file.read(), re.MULTILINE)
if version_match is None:
    raise SystemExit("Unable to read the application version from pyproject.toml")
APP_VERSION = version_match.group(1)

if not os.path.isfile(CORE_BINARY):
    raise SystemExit(
        f"Missing {CORE_BINARY}; run the platform build script so the shared "
        "transport core is compiled before PyInstaller.")

datas = [(QML, "inkhole")]
hiddenimports = [
    "inkhole.p2p", "inkhole.branding", "inkhole.macos", "inkhole.transport",
    "inkhole.secret_store", "inkhole.device_identity", "zeroconf", "psutil", "keyring", "qrcode",
    "PIL.Image",
]
if sys.platform == "darwin":
    # AppKit provides the native mixed file/folder NSOpenPanel on macOS.
    hiddenimports.extend(["AppKit", "Foundation", "keyring.backends.macOS"])
elif sys.platform == "win32":
    hiddenimports.append("keyring.backends.Windows")

# 排除明显用不到的重型 Qt 模块，降低发布包体积
excluded = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtWebSockets",
    "tkinter", "PyQt5", "PyQt6", "matplotlib", "numpy", "pandas",
]

a = Analysis(
    [os.path.join(SPECPATH, "pet_entry.py")],
    pathex=[SRC],
    binaries=[(CORE_BINARY, ".")],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded,
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

# PyInstaller 的 excludes 只挡 Python 模块绑定(.pyd),挡不住 PySide6 钩子
# 按目录整体收进来的 Qt DLL / QML 插件——WebEngineCore.dll 一个就 195MB。
# 这里在收集清单(binaries/datas)上做名称过滤,把界面用不到的重型 Qt 组件
# 直接剔除。本应用 UI = QtWidgets + QtQuick(仅 QtQuick/QtQuick.Window)+
# QtQml + QtNetwork/QtGui/QtCore,其余全部可删。
_DROP_TOKENS = (
    "webengine", "webenginecore", "webenginequick", "webchannel", "websockets",
    "qt63d", "quick3d", "3danimation", "3dcore", "3drender", "3dinput",
    "3dlogic", "3dextras", "3dquick",
    "charts", "datavisualization", "graphs",
    "multimedia", "spatialaudio",
    "pdf", "location", "positioning", "sensors", "serialport", "nfc",
    "bluetooth", "remoteobjects", "scxml", "sql", "designer", "help",
    "quicktest", "qttest", "virtualkeyboard",
    "opengl32sw",           # 软件 OpenGL 兜底,19.7MB,有独显/集显都用不到
    "d3dcompiler",          # ANGLE 的 HLSL 编译器,墨洞 UI 不需要
)


def _keep(dest_name: str) -> bool:
    low = dest_name.replace("\\", "/").lower()
    base = low.rsplit("/", 1)[-1]
    return not any(tok in base for tok in _DROP_TOKENS)


a.binaries = TOC([e for e in a.binaries if _keep(e[0])])
a.datas = TOC([e for e in a.datas if _keep(e[0])])


# 两个平台都使用 onedir。macOS 若把 onefile EXE 直接放进 BUNDLE，PyInstaller
# 会同时运行负责解包的父进程和真正的 Qt 子进程；LaunchServices 会把两者都
# 注册成前台 App，导致一次启动出现两个 Dock 图标。
if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="InkHolePet",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=CODESIGN_IDENTITY,
        entitlements_file=None,
        icon=ICON_ICNS,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="InkHolePet",
    )
    app = BUNDLE(
        coll,
        name="InkHolePet.app",
        icon=ICON_ICNS,
        bundle_identifier="com.rexvane.inkhole-pet",
        info_plist={
            "CFBundleName": "墨洞桌宠",
            "CFBundleDisplayName": "墨洞桌宠",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "CFBundleAllowMixedLocalizations": True,
            "CFBundleDevelopmentRegion": "zh-Hans",
            "CFBundleLocalizations": ["zh-Hans", "en"],
            "LSUIElement": False,
            "NSHighResolutionCapable": True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        name="InkHolePet",
        debug=False,
        bootloader_ignore_signals=False,
        strip=True,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=CODESIGN_IDENTITY,
        entitlements_file=None,
        icon=APP_ICON,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="InkHolePet",
    )
