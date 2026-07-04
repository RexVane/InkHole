"""
pet.py
======
桌宠墨洞挂件(PySide6 + QML) — P2P 局域网直连模式，无需服务器。

形态：黑洞吞噬感 —— 中心深邃黑点 + 乳白色吸积盘/光晕，向内吸卷旋转；
      桌面小图标大小，低调浮在角落，无边框、透明、置顶、可拖动。

交互：
  - 从桌面拖文件到挂件上 -> 黑洞放大"吸入"动画 -> P2P 直连发给目标设备。
  - 收到对端文件(node.on_received 回调) -> 黑洞放大"喷出"动画(文件已落在收件箱)。
  - 右键菜单：发送目标 / 打开收件箱 / 更换收件箱 / 开机自启 / 状态 / 退出。
  - 鼠标拖动窗口可挪到桌面任意位置。

后端：复用 p2p.P2PNode(mDNS 发现 + TCP 直连)。本文件只负责"面子"(动画/拖拽)，
      "里子"(传输)全交给 P2P 引擎。两层解耦，P2P 引擎已通过自动化测试。

运行(需在有图形界面的机器上，先 pip install PySide6 zeroconf)：
  PYTHONPATH=src python3 -m wormhole.pet
  PYTHONPATH=src python3 -m wormhole.pet --name 我的电脑
  PYTHONPATH=src python3 -m wormhole.pet --secret 加密口令
"""

from __future__ import annotations
import os
import sys
import json
import queue
import argparse
import threading
from collections import deque

from .p2p import P2PNode, P2PConfig

def _qml_path() -> str:
    """定位 wormhole.qml。

    源码运行时它就在本模块同级目录；被 PyInstaller 打包成单文件后,数据文件
    会解压到临时目录 sys._MEIPASS,需按打包时的相对路径(wormhole/)
    去那里找。两种环境都覆盖,打包/源码运行同一份代码。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        bundled = os.path.join(base, "wormhole", "wormhole.qml")
        if os.path.exists(bundled):
            return bundled
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "wormhole.qml")


_QML_FILE = _qml_path()


def _windows_desktop() -> str:
    """Windows 真实桌面目录：经 SHGetKnownFolderPath 查询。

    桌面可能被 OneDrive/域策略重定向到任意位置，不能假设 ~/Desktop
    或 ~/OneDrive/Desktop。查询失败回退 ~/Desktop。
    """
    try:
        import ctypes
        from ctypes import wintypes
        # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
        guid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                     (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
        path_ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)) == 0:
            desktop = path_ptr.value
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            if desktop:
                return desktop
    except Exception:
        pass
    return os.path.expanduser(os.path.join("~", "Desktop"))


def _default_inbox() -> str:
    """默认收件箱目录,按平台给出常用位置(均可被 --inbox 覆盖)。

    Windows: <真实桌面>/wormhole (桌面可能被 OneDrive 重定向,动态查询)
    macOS:   ~/Documents/wormhole
    其他:    ~/Wormhole/收件箱
    """
    if sys.platform == "win32":
        return os.path.join(_windows_desktop(), "wormhole")
    if sys.platform == "darwin":
        return os.path.expanduser(os.path.join("~", "Documents", "wormhole"))
    return os.path.expanduser(os.path.join("~", "Wormhole", "收件箱"))


# ---------- 设置持久化 ----------
# 双击 exe 的用户没有命令行：名字/口令/收件箱改一次就记住。
# 显式 CLI 参数 > 配置文件 > 默认值；显式参数会写回配置(下次不带参数也生效)。

def _config_path() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Wormhole", "config.json")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Wormhole/config.json")
    return os.path.expanduser("~/.config/wormhole/config.json")


def _load_saved_config() -> dict:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(cfg: P2PConfig) -> None:
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"name": cfg.peer_name, "secret": cfg.secret,
                       "inbox": cfg.inbox, "port": cfg.listen_port,
                       "trusted_only": cfg.trusted_only},
                      f, ensure_ascii=False, indent=2)
    except OSError:
        pass


class SendQueue:
    """串行发送队列：一次拖 N 个文件不再开 N 个并发连接互踩。

    单工作线程按序发送；一批(队列清空)结束后回调 on_batch_done(成功数, 总数)，
    多文件时给用户一个聚合结果。纯标准库实现，不依赖 Qt，可单测。
    """

    def __init__(self, send_fn, on_batch_done=None):
        self._send = send_fn
        self._on_batch_done = on_batch_done
        self._q: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._batch_total = 0
        self._batch_ok = 0
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def put(self, path: str) -> None:
        with self._lock:
            self._batch_total += 1
        self._q.put(path)

    def _loop(self) -> None:
        while True:
            path = self._q.get()
            ok = False
            try:
                ok = bool(self._send(path))
            except Exception:
                pass
            batch_done = None
            with self._lock:
                if ok:
                    self._batch_ok += 1
                if self._q.empty():
                    batch_done = (self._batch_ok, self._batch_total)
                    self._batch_ok = 0
                    self._batch_total = 0
            if batch_done and self._on_batch_done:
                try:
                    self._on_batch_done(*batch_done)
                except Exception:
                    pass


def _build_config(argv=None):
    saved = _load_saved_config()
    ap = argparse.ArgumentParser(description="墨洞桌宠挂件(P2P 局域网直连，无需服务器)")
    ap.add_argument("--inbox", default=None,
                    help="收件箱目录(收到的文件放这;默认随平台,改一次会记住)")
    ap.add_argument("--port", type=int, default=None,
                    help="P2P 监听端口(0=操作系统自动分配)")
    ap.add_argument("--name", default=None,
                    help="本机显示名(默认主机名；右键菜单里对端看到的就是这个名字)")
    ap.add_argument("--secret", default=None,
                    help="端到端加密口令(两台设备必须一致；需 cryptography 库)")
    ap.add_argument("--size", type=int, default=0,
                    help="挂件边长像素(0=随屏幕自适应，约为系统图标基准的 1.5 倍)")
    args = ap.parse_args(argv)

    inbox = args.inbox if args.inbox is not None else str(saved.get("inbox") or "") or _default_inbox()
    try:
        port = args.port if args.port is not None else int(saved.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    name = args.name if args.name is not None else str(saved.get("name") or "")
    secret = args.secret if args.secret is not None else str(saved.get("secret") or "")
    trusted_only = bool(saved.get("trusted_only", False))

    cfg = P2PConfig(inbox=inbox, listen_port=port, peer_name=name, secret=secret,
                    trusted_only=trusted_only)
    if any(a is not None for a in (args.inbox, args.port, args.name, args.secret)):
        _save_config(cfg)   # 显式 CLI 参数视为用户意图，记住
    return cfg, args.size


def _adaptive_pet_size(override: int) -> int:
    """挂件边长 = 系统程序图标尺寸 × 1.5。
    override>0 时直接用该值；否则在 macOS 上读取 Dock 实际图标尺寸
    (com.apple.dock tilesize)作为基准——用户把 Dock 图标调大，挂件随之变大。
    读不到(或非 macOS)则回退到常见图标基准 64。"""
    if override > 0:
        return override
    icon_base = 64
    if sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.run(["defaults", "read", "com.apple.dock", "tilesize"],
                                 capture_output=True, text=True, timeout=3)
            icon_base = int(float(out.stdout.strip()))
        except Exception:
            icon_base = 64
    return round(icon_base * 1.5)


def _install_crash_log(inbox: str) -> str:
    """让 GUI 版"莫名自己退出"可排查 + 兜底修复打包后的致命陷阱。

    打包成 windowed exe(spec 里 console=False)后,sys.stdout/sys.stderr 会变成
    None;此时任何 print() 都会抛异常,而后台线程每次状态变化/重连都 print,
    异常逐层上抛会直接打死同步线程——这是 exe 版"自己断掉"的元凶之一。这里把空的
    stdout/stderr 兜底重定向到日志文件,并记录未捕获异常,既消除崩溃源又留下现场。
    源码运行时 stdout 正常,不会被替换。返回日志路径。"""
    import time
    import traceback
    try:
        os.makedirs(inbox, exist_ok=True)
    except OSError:
        pass
    log_path = os.path.join(inbox, "wormhole-pet.log")
    try:
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return log_path
    # 仅在为 None(windowed 打包)时替换,避免影响源码运行时的真实控制台
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file

    def _hook(exc_type, exc, tb):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 未捕获异常:\n")
                traceback.print_exception(exc_type, exc, tb, file=f)
        except Exception:
            pass

    sys.excepthook = _hook    # 槽函数里漏出的异常改为记日志,尽量不让进程直接终止
    return log_path


# ---------- 开机自启 ----------
_APP_NAME = "WormholePet"


def _src_dir() -> str:
    """src 目录绝对路径(pet.py 上两级)。"""
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _startup_script_path() -> str:
    """开机自启脚本/配置文件路径(跨平台)。"""
    if sys.platform == "win32":
        return os.path.join(os.path.expanduser("~"), "wormhole-startup.bat")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/LaunchAgents/com.rexvane.wormhole-pet.plist")
    return os.path.expanduser("~/.config/autostart/wormhole-pet.desktop")


def is_autostart_enabled() -> bool:
    """检查当前是否已设置开机自启。"""
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run")
            winreg.QueryValueEx(key, _APP_NAME)
            winreg.CloseKey(key)
            return True
        except (FileNotFoundError, OSError):
            return False
    return os.path.exists(_startup_script_path())


def set_autostart(enabled: bool, cfg: P2PConfig) -> bool:
    """设置或取消开机自启，返回操作后的状态。

    自启项不带任何参数：名字/口令/收件箱都在配置文件(config.json)里，
    启动时自动读取——口令不会明文进注册表/自启脚本。
    """
    path = _startup_script_path()
    if enabled:
        src = _src_dir()
        proj = os.path.dirname(src)
        python = sys.executable
        frozen = getattr(sys, "frozen", False)

        if sys.platform == "win32":
            if frozen:
                # 打包 exe：注册表直接指向 exe
                cmd = f'"{python}"'
            else:
                # 源码运行：生成 .bat 脚本
                content = "\r\n".join([
                    "@echo off",
                    f'cd /d "{proj}"',
                    f'set "PYTHONPATH={src}"',
                    f'"{python}" -m wormhole.pet',
                ])
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                cmd = f'"{path}"'
            # 写注册表
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, cmd)
            winreg.CloseKey(key)

        elif sys.platform == "darwin":
            if frozen:
                exec_path = sys.executable  # .app 内的可执行文件
                content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.rexvane.wormhole-pet</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exec_path}</string>
    </array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>"""
            else:
                content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.rexvane.wormhole-pet</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-c</string>
        <string>import os,sys;os.chdir({proj!r});sys.path.insert(0,{src!r});from wormhole.pet import main;main()</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>WorkingDirectory</key><string>{proj}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key><string>{src}</string>
    </dict>
</dict>
</plist>"""
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

        else:
            # Linux: .desktop
            if frozen:
                exec_line = f'"{python}"'
            else:
                exec_line = f'sh -c \'cd "{proj}" && PYTHONPATH="{src}" "{python}" -m wormhole.pet\''
            content = f"""[Desktop Entry]
Type=Application
Name=Wormhole Pet
Exec={exec_line}
Terminal=false
X-GNOME-Autostart-enabled=true"""
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    else:
        # 取消自启
        if sys.platform == "win32":
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                     r"Software\Microsoft\Windows\CurrentVersion\Run",
                                     0, winreg.KEY_SET_VALUE)
                winreg.DeleteValue(key, _APP_NAME)
                winreg.CloseKey(key)
            except (FileNotFoundError, OSError):
                pass
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    return is_autostart_enabled()


def main(argv=None) -> None:
    cfg, size_override = _build_config(argv)
    _install_crash_log(cfg.inbox)   # 尽早安装:之后任何崩溃/print 都安全且留痕
    try:
        from PySide6.QtCore import QObject, Signal, Slot, QUrl, Qt, QPointF
        from PySide6.QtGui import (QGuiApplication, QIcon, QPixmap, QPainter,
                                   QColor, QRadialGradient)
        from PySide6.QtQml import QQmlApplicationEngine
        # 托盘菜单需要 QtWidgets(QApplication/QSystemTrayIcon/QMenu);
        # 不可用时降级:仍能跑桌宠,只是没有系统托盘(见 _setup_tray 的容错)
        try:
            from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
            _HAS_WIDGETS = True
        except ImportError:
            _HAS_WIDGETS = False
    except ImportError:
        sys.stderr.write(
            "未安装 PySide6。请先运行：pip install PySide6 --break-system-packages\n"
            "(P2P 引擎本身无需 GUI，可用 python -m wormhole.p2p 跑命令行版)\n")
        raise SystemExit(1)

    def _draw_icon_pixmap(size: int) -> QPixmap:
        """画单一尺寸的图标位图：圆角黑底 + 居中墨洞(青色视界环)。"""
        from PySide6.QtGui import QPainterPath
        from PySide6.QtCore import QRectF
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)                   # 圆角外保持透明
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        radius = size * 0.22                      # macOS 风格圆角(边长 22%)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
        p.setClipPath(clip)
        p.fillPath(clip, QColor(5, 7, 10))        # 圆角墨黑底
        cx = cy = size / 2
        R = size * 0.42
        g = QRadialGradient(cx, cy, R)            # 墨黑核心 -> 青色视界环 -> 融回黑底
        g.setColorAt(0.00, QColor(0, 0, 0, 255))
        g.setColorAt(0.45, QColor(2, 8, 7, 255))
        g.setColorAt(0.62, QColor(14, 58, 50, 255))
        g.setColorAt(0.76, QColor(88, 230, 200, 255))   # 亮青视界环
        g.setColorAt(0.86, QColor(20, 64, 56, 255))
        g.setColorAt(1.00, QColor(5, 7, 10, 255))
        p.setBrush(g)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), R, R)
        p.end()
        return pm

    def _make_app_icon() -> QIcon:
        """多分辨率自适应图标：装入多个尺寸，系统按 Dock/任务栏/高分屏自动选最合适的。"""
        icon = QIcon()
        for s in (16, 32, 64, 128, 256, 512, 1024):
            icon.addPixmap(_draw_icon_pixmap(s))
        return icon

    def _setup_tray(app, bridge):
        """构建右键菜单 +(可用时)系统托盘图标。

        菜单结构：
          发送目标 ▸  (动态子菜单，列出已发现的设备，可单选)
          ─────────
          打开收件箱
          ☑/☐ 开机自启  (可勾选，切换开机自动启动)
          ─────────
          状态：…
          ─────────
          退出

        关键修复:把"菜单的构建"与"系统托盘是否可用"解耦(见原版注释)。
        新增:发送目标子菜单在 aboutToShow 时动态重建——对端随时上下线。
        """
        if not _HAS_WIDGETS:
            return None
        menu = QMenu()

        # ---- 静态部分(每次弹出重建,因为目标子菜单要刷新) ----
        def _rebuild_menu():
            menu.clear()

            # 发送目标子菜单
            peers = bridge.node.peers()
            peer_menu = menu.addMenu("发送目标")
            if not peers:
                act = peer_menu.addAction("（等待发现设备…）")
                act.setEnabled(False)
            else:
                selected = bridge.node.selected_peer()
                for peer in peers:
                    label = f"● {peer.name}" if peer.name == selected else f"○ {peer.name}"
                    act = peer_menu.addAction(label)
                    act.setCheckable(True)
                    act.setChecked(peer.name == selected)
                    # lambda 默认绑定技巧:用 name=peer.name 固定当前值
                    _act = act  # 持引用
                    act.triggered.connect(
                        lambda checked=False, name=peer.name: bridge._select_peer(name))
                peer_menu.addSeparator()
                act_none = peer_menu.addAction("○ 不选目标")
                act_none.setCheckable(True)
                act_none.setChecked(selected is None)
                act_none.triggered.connect(
                    lambda checked=False: bridge._select_peer(None))

            menu.addSeparator()

            act_open = menu.addAction("打开收件箱")
            act_open.triggered.connect(bridge.openInbox)

            # 最近接收子菜单：点一下直接打开文件
            recents = bridge.recentFiles()
            recent_menu = menu.addMenu("最近接收")
            if not recents:
                act_none = recent_menu.addAction("（暂无）")
                act_none.setEnabled(False)
            else:
                for rp in recents:
                    act_r = recent_menu.addAction(os.path.basename(rp))
                    act_r.triggered.connect(
                        lambda checked=False, p=rp: bridge.openPath(p))

            act_inbox = menu.addAction("更换收件箱...")
            act_inbox.triggered.connect(bridge.chooseInbox)

            act_name = menu.addAction("设备名称...")
            act_name.triggered.connect(bridge.renameDevice)

            secret_on = bool(bridge.node.cfg.secret)
            act_secret = menu.addAction("加密口令..." + ("　🔒" if secret_on else ""))
            act_secret.triggered.connect(bridge.changeSecret)

            # 仅接收目标设备（可勾选）：拦掉陌生设备的投喂
            act_trusted = menu.addAction("仅接收目标设备")
            act_trusted.setCheckable(True)
            act_trusted.setChecked(bridge.isTrustedOnly())
            def _on_trusted():
                on = bridge.toggleTrustedOnly()
                act_trusted.setChecked(on)
                bridge.status.emit("只收目标设备的文件" if on else "接收所有设备的文件")
            act_trusted.triggered.connect(_on_trusted)

            # 开机自启（可勾选）
            act_autostart = menu.addAction("开机自启")
            act_autostart.setCheckable(True)
            act_autostart.setChecked(bridge.isAutoStart())
            def _on_autostart():
                ok = bridge.toggleAutoStart()
                act_autostart.setChecked(ok)
                bridge.status.emit("已开启开机自启" if ok else "已关闭开机自启")
            act_autostart.triggered.connect(_on_autostart)

            menu.addSeparator()
            act_status = menu.addAction("状态：" + bridge.connState())
            act_status.setEnabled(False)
            menu.addSeparator()

            act_quit = menu.addAction("退出")
            act_quit.triggered.connect(bridge.quit)

        menu.aboutToShow.connect(_rebuild_menu)
        bridge._tray_menu = menu

        # 仅当系统托盘可用时才创建并显示托盘图标;不可用也不影响右键菜单
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(_make_app_icon(), app)
        tray.setToolTip("墨洞")
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: bridge.openInbox()
            if reason == QSystemTrayIcon.Trigger else None)
        tray.show()
        return tray

    # ---- Python<->QML 桥：把 P2P 引擎的事件转成 QML 信号驱动动画 ----
    class Bridge(QObject):
        absorb = Signal(str)          # 通知 QML 播放"吸入"动画(参数=文件名)
        emit_out = Signal(str)        # 通知 QML 播放"喷出"动画(参数=文件名)
        status = Signal(str)          # 临时状态文字(2.2s 后消失)
        peersChanged = Signal()       # 设备列表变化(刷新菜单)
        errorState = Signal(str)      # 错误信息(持续显示，非空=有错误，空=清除)
        progress = Signal(str, int)   # 传输进度(kind "send"/"recv", 百分比 0-100)

        def __init__(self, cfg: P2PConfig):
            super().__init__()
            self._tray_menu = None        # 由 _setup_tray 注入:桌宠右键时弹出
            self._recent: deque[str] = deque(maxlen=8)   # 最近收到的文件(路径)
            self.node = self._make_node(cfg)
            self.node.start()
            # 串行发送队列：拖一堆文件不再开一堆并发连接
            self._sendq = SendQueue(
                lambda p: self.node.send_file(p),
                on_batch_done=lambda ok, total: (
                    self.status.emit(f"已吞入 {ok}/{total} 个文件") if total > 1 else None),
            )

        def _make_node(self, cfg: P2PConfig) -> P2PNode:
            """用统一的回调钩子构造 P2P 节点(初始启动与改设置重启共用)。"""
            return P2PNode(
                cfg,
                on_sent=lambda n: self.absorb.emit(n),
                on_received=lambda p: self._on_received_file(p),
                on_status=lambda s: self._route_status(s),
                on_peers_changed=lambda: self.peersChanged.emit(),
                on_progress=lambda kind, name, done, total: self._on_progress(
                    kind, name, done, total),
            )

        def _on_received_file(self, path: str) -> None:
            self._recent.appendleft(path)
            self.emit_out.emit(os.path.basename(path))

        def _on_progress(self, kind: str, name: str, done: int, total: int) -> None:
            pct = done * 100 // total if total else 100
            self.progress.emit(kind, pct)
            arrow = "↑" if kind == "send" else "↓"
            self.status.emit(f"{arrow} {name} {pct}%")

        def _apply_settings(self, peer_name: str | None = None,
                            secret: str | None = None) -> None:
            """改名/改口令：写回配置并重启 P2P 节点(mDNS 需重新注册)。"""
            cfg = self.node.cfg
            self.node.stop()
            if peer_name is not None:
                cfg.peer_name = peer_name
            if secret is not None:
                cfg.secret = secret
            _save_config(cfg)
            try:
                self.node = self._make_node(cfg)
            except SystemExit:
                # 设了口令但没装 cryptography：退回不加密，保持能用
                cfg.secret = ""
                _save_config(cfg)
                self.node = self._make_node(cfg)
                self.status.emit("缺少 cryptography 库，加密未开启")
            self.node.start()
            self.peersChanged.emit()

        def _route_status(self, msg: str) -> None:
            """出错信息走 persistentHint(持续显示)，普通信息走 hint(2.2s 消失)。"""
            if msg and ("失败" in msg or "无法" in msg):
                self.errorState.emit(msg)
            else:
                self.errorState.emit("")  # 清除之前的错误
                self.status.emit(msg)

        @Slot(result=str)
        def peerStatus(self) -> str:
            """持续状态：始终返回空(桌宠不显示持续文字，只有出错时才显示)。"""
            return ""

        def _select_peer(self, name):
            """选中目标设备(由右键菜单触发)。"""
            self.node.select_peer(name)

        @Slot(str)
        def dropFile(self, url: str):
            """QML DropArea 收到桌面拖来的文件 url，转本地路径后入发送队列。
            队列单线程串行发送：一次拖 N 个文件不会开 N 个并发连接。"""
            path = QUrl(url).toLocalFile() if url.startswith("file:") else url
            if path and os.path.isfile(path):
                if not self.node.selected_peer():
                    self.status.emit("右键选择目标设备")
                    return
                self._sendq.put(path)

        @Slot(result=bool)
        def hasTarget(self) -> bool:
            """QML 用来判断拖入文件时是否该播吸入动画。"""
            return self.node.selected_peer() is not None

        @Slot(result=bool)
        def isTrustedOnly(self) -> bool:
            return self.node.cfg.trusted_only

        @Slot(result=bool)
        def toggleTrustedOnly(self) -> bool:
            """切换「仅接收目标设备」：拦掉局域网里陌生设备的投喂。"""
            self.node.cfg.trusted_only = not self.node.cfg.trusted_only
            _save_config(self.node.cfg)
            return self.node.cfg.trusted_only

        @Slot(result="QVariantList")
        def recentFiles(self) -> list:
            """最近收到的文件路径列表(新的在前)。"""
            return [p for p in self._recent if os.path.exists(p)]

        @Slot(str)
        def openPath(self, path: str):
            """用系统默认程序打开一个文件(最近接收菜单用)。"""
            try:
                if sys.platform == "win32":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    import subprocess; subprocess.Popen(["open", path])
                else:
                    import subprocess; subprocess.Popen(["xdg-open", path])
            except Exception:
                self.status.emit("无法打开文件")

        @Slot(result=str)
        def inboxPath(self) -> str:
            return os.path.abspath(self.node.cfg.inbox)

        @Slot()
        def openInbox(self):
            """在系统文件管理器中打开收件箱目录(跨平台)。"""
            path = os.path.abspath(self.node.cfg.inbox)
            os.makedirs(path, exist_ok=True)
            try:
                if sys.platform == "win32":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    import subprocess; subprocess.Popen(["open", path])
                else:
                    import subprocess; subprocess.Popen(["xdg-open", path])
            except Exception as e:
                self.status.emit(f"打开收件箱失败")
                print(f"[托盘] 打开收件箱失败: {e}", flush=True)

        @Slot()
        def chooseInbox(self):
            """打开目录选择对话框，让用户自定义收件箱路径。"""
            if not _HAS_WIDGETS:
                self.status.emit("无法打开对话框")
                return
            from PySide6.QtWidgets import QFileDialog
            directory = QFileDialog.getExistingDirectory(
                None, "选择收件箱目录", self.node.cfg.inbox)
            if directory:
                self.node.cfg.inbox = directory
                os.makedirs(directory, exist_ok=True)
                _save_config(self.node.cfg)   # 记住，重启后仍生效
                self.status.emit(f"收件箱: {os.path.basename(directory)}")

        @Slot()
        def renameDevice(self):
            """改本机显示名(对端右键菜单里看到的名字)。"""
            if not _HAS_WIDGETS:
                self.status.emit("无法打开对话框")
                return
            from PySide6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(
                None, "设备名称", "对端设备看到的名字：",
                text=self.node.cfg.peer_name)
            if ok:
                name = name.strip()
                if name and name != self.node.cfg.peer_name:
                    self._apply_settings(peer_name=name)
                    self.status.emit(f"设备名: {name}")

        @Slot()
        def changeSecret(self):
            """设置/修改/清除端到端加密口令。"""
            if not _HAS_WIDGETS:
                self.status.emit("无法打开对话框")
                return
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            secret, ok = QInputDialog.getText(
                None, "端到端加密口令",
                "两台设备口令必须一致；留空关闭加密：",
                QLineEdit.Password, self.node.cfg.secret)
            if ok and secret != self.node.cfg.secret:
                self._apply_settings(secret=secret)
                self.status.emit("已开启端到端加密" if secret else "已关闭加密")

        @Slot(result=bool)
        def isAutoStart(self) -> bool:
            """是否已设置开机自启。"""
            return is_autostart_enabled()

        @Slot(result=bool)
        def toggleAutoStart(self) -> bool:
            """切换开机自启，返回切换后的状态。"""
            enabled = not is_autostart_enabled()
            return set_autostart(enabled, self.node.cfg)

        @Slot(result=str)
        def connState(self) -> str:
            """给菜单状态行用的当前状态文字(零网络开销)。"""
            peers = self.node.peers()
            if not peers:
                return "🔍 搜索设备中…"
            selected = self.node.selected_peer()
            if selected:
                return f"→ {selected}"
            return f"🔍 {len(peers)}台设备"

        @Slot()
        def showMenu(self):
            """桌宠被右键时,在鼠标位置弹出菜单。"""
            if self._tray_menu is not None:
                from PySide6.QtGui import QCursor
                self._tray_menu.popup(QCursor.pos())
            else:
                self.openInbox()

        @Slot()
        def quit(self):
            self.node.stop()
            QGuiApplication.quit()

    # 有 QtWidgets 用 QApplication(支持托盘菜单),否则退回 QGuiApplication
    app = (QApplication if _HAS_WIDGETS else QGuiApplication)(sys.argv)
    app.setApplicationName("墨洞")
    app.setWindowIcon(_make_app_icon())          # Dock/任务栏：墨黑底青环墨洞
    app.setQuitOnLastWindowClosed(False)         # 关挂件窗口不退出(托盘还在),仅菜单"退出"才退
    bridge = Bridge(cfg)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.rootContext().setContextProperty("petSizePx", _adaptive_pet_size(size_override))
    engine.load(QUrl.fromLocalFile(_QML_FILE))
    if not engine.rootObjects():
        sys.stderr.write("QML 加载失败\n")
        raise SystemExit(1)

    # 系统托盘(Windows 右下托盘 / macOS 顶部菜单栏,Qt 跨平台一套代码)
    _tray = _setup_tray(app, bridge)   # 返回托盘对象(需持引用防被回收),失败返回 None
    # macOS: 让挂件常驻所有桌面(Spaces)，切换桌面/应用失焦都不消失
    if sys.platform == "darwin":
        try:
            from AppKit import NSApp
            # canJoinAllSpaces(1<<0) | stationary(1<<4) | fullScreenAuxiliary(1<<8)
            # 注意先清掉 Qt.Tool 自带的 moveToActiveSpace(1<<1)，两者互斥
            for w in NSApp.windows():
                behavior = (w.collectionBehavior() & ~(1 << 1)) | (1 << 0) | (1 << 4) | (1 << 8)
                w.setCollectionBehavior_(behavior)
                w.setHidesOnDeactivate_(False)
        except ImportError:
            pass  # 未装 pyobjc 时跳过：功能不受影响，只是切桌面时挂件会隐藏
    os.makedirs(cfg.inbox, exist_ok=True)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
