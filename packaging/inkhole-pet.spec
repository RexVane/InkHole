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
  - macOS 上 pet.py 会尝试 import AppKit(pyobjc)实现"挂件常驻所有桌面";
    未装 pyobjc 时自动跳过,功能不受影响。需要该效果时:pip install pyobjc-framework-Cocoa
"""
import os
import sys

PROJ = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(PROJ, "src")
QML = os.path.join(SRC, "inkhole", "inkhole.qml")
ICON_ICO = os.path.join(PROJ, "assets", "inkhole.ico")
ICON_ICNS = os.path.join(PROJ, "assets", "inkhole.icns")
APP_ICON = ICON_ICNS if sys.platform == "darwin" else ICON_ICO

datas = [(QML, "inkhole")]

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
    binaries=[],
    datas=datas,
    hiddenimports=[
        "inkhole.p2p", "inkhole.branding",
        "zeroconf", "psutil",
    ],
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
    codesign_identity=None,
    entitlements_file=None,
    icon=APP_ICON,
)

# Windows: onedir 模式——产物是文件夹，不解压到 %TEMP%，绕开 Defender 拦截
# macOS:   保持 onefile EXE + BUNDLE，.app 本身就是目录格式，不存在 Defender 问题
if sys.platform == "darwin":
    app = BUNDLE(
        EXE(
            pyz,
            a.scripts,
            a.binaries,
            a.datas,
            [],
            name="InkHolePet",
            debug=False,
            strip=False,
            upx=False,
            console=False,
            argv_emulation=False,
            target_arch=None,
            codesign_identity=None,
            entitlements_file=None,
            icon=ICON_ICNS,
        ),
        name="InkHolePet.app",
        icon=ICON_ICNS,
        bundle_identifier="com.rexvane.inkhole-pet",
        info_plist={
            "CFBundleName": "墨洞桌宠",
            "CFBundleDisplayName": "墨洞桌宠",
            "LSUIElement": False,
            "NSHighResolutionCapable": True,
        },
    )
else:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="InkHolePet",
    )
