# -*- mode: python ; coding: utf-8 -*-
"""
墨洞桌宠轻量 app 打包规格(跨平台:Windows .exe / macOS .app,均单文件 onefile)。

构建:
    Windows:  cd packaging && pyinstaller wormhole-pet.spec --noconfirm
    macOS:    cd packaging && pyinstaller wormhole-pet.spec --noconfirm
产物:
    Windows:  packaging/dist/墨洞桌宠.exe        (双击即用,免装 Python)
    macOS:    packaging/dist/墨洞桌宠.app        (拖进"应用程序"即用)

要点:
  - wormhole.qml 作为数据文件打进包,运行时从 sys._MEIPASS 读取(见 pet.py:_qml_path)。
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
QML = os.path.join(SRC, "wormhole", "wormhole.qml")

datas = [(QML, "wormhole")]

# 排除明显用不到的重型 Qt 模块,显著减小单文件体积
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
    hiddenimports=["wormhole.p2p", "zeroconf"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded,
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="墨洞桌宠",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# macOS:把单文件可执行进一步包成 .app 应用包(双击启动、Dock 图标、可拖入"应用程序")
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="墨洞桌宠.app",
        icon=None,
        bundle_identifier="com.rexvane.wormhole-pet",
        info_plist={
            "CFBundleName": "墨洞桌宠",
            "CFBundleDisplayName": "墨洞桌宠",
            "LSUIElement": False,          # 显示 Dock 图标
            "NSHighResolutionCapable": True,
        },
    )

