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
  PYTHONPATH=src python3 -m inkhole.pet
  PYTHONPATH=src python3 -m inkhole.pet --name 我的电脑
  PYTHONPATH=src python3 -m inkhole.pet --secret 加密口令
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
    """定位 inkhole.qml。

    源码运行时它就在本模块同级目录；被 PyInstaller 打包成单文件后,数据文件
    会解压到临时目录 sys._MEIPASS,需按打包时的相对路径(inkhole/)
    去那里找。两种环境都覆盖,打包/源码运行同一份代码。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        bundled = os.path.join(base, "inkhole", "inkhole.qml")
        if os.path.exists(bundled):
            return bundled
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "inkhole.qml")


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

    Windows: <真实桌面>/inkhole (桌面可能被 OneDrive 重定向,动态查询)
    macOS:   ~/Documents/inkhole
    其他:    ~/InkHole/收件箱
    """
    if sys.platform == "win32":
        return os.path.join(_windows_desktop(), "inkhole")
    if sys.platform == "darwin":
        return os.path.expanduser(os.path.join("~", "Documents", "inkhole"))
    return os.path.expanduser(os.path.join("~", "InkHole", "收件箱"))


# ---------- 设置持久化 ----------
# 双击 exe 的用户没有命令行：名字/口令/收件箱改一次就记住。
# 显式 CLI 参数 > 配置文件 > 默认值；显式参数会写回配置(下次不带参数也生效)。

def _config_path() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "InkHole", "config.json")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/InkHole/config.json")
    return os.path.expanduser("~/.config/inkhole/config.json")


def _load_saved_config() -> dict:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(cfg: P2PConfig, **extra) -> None:
    """写回配置。读改写合并：不丢掉 P2PConfig 之外的界面项(如 show_pet)。"""
    path = _config_path()
    try:
        data = _load_saved_config()
        # SSH 中转方案已移除:清掉历史遗留的相关键,避免污染配置
        for stale in ("ssh_relay", "relay", "transport_mode"):
            data.pop(stale, None)
        data.update({"name": cfg.peer_name, "secret": cfg.secret,
                     "inbox": cfg.inbox, "port": cfg.listen_port,
                     "trusted_only": cfg.trusted_only,
                     "instance_id": cfg.instance_id,
                     "manual_peers": list(cfg.manual_peers or [])})
        data.update(extra)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


class SendQueue:
    """串行发送队列：一次拖 N 个文件不再开 N 个并发连接互踩。

    单工作线程按序发送；一批(队列清空)结束后回调 on_batch_done(成功数, 总数)，
    多文件时给用户一个聚合结果。此外每个文件发送完成后可回调 per-item 的
    on_done(path, ok)——临时打包目录用它「谁的 zip 发完就删谁」，精确绑定单个
    文件，不再跨批次共享可变列表(消除连拖文件夹的清理竞态)。纯标准库实现，
    不依赖 Qt，可单测。
    """

    def __init__(self, send_fn, on_batch_done=None, on_busy_changed=None):
        self._send = send_fn
        self._on_batch_done = on_batch_done
        self._on_busy_changed = on_busy_changed
        # 队列元素是 (path, on_done)：on_done 可为 None
        self._q: queue.Queue[tuple[str, object]] = queue.Queue()
        self._lock = threading.Lock()
        self._batch_total = 0
        self._batch_ok = 0
        self._working = False
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def put(self, path: str, on_done=None) -> None:
        """把一个文件排入发送队列。

        on_done(path, ok)：该文件发送结束后在工作线程内回调一次(无论成败)，
        用于「发完即清理」它自己的临时资源(如目录打包产生的临时 zip 目录)。
        """
        notify = False
        with self._lock:
            if self._batch_total == 0 and not self._working:
                notify = True
            self._batch_total += 1
            self._q.put((path, on_done))
        if notify and self._on_busy_changed:
            self._on_busy_changed(True)

    def busy(self) -> bool:
        with self._lock:
            return self._working or self._batch_total > 0

    def _loop(self) -> None:
        while True:
            path, on_done = self._q.get()
            with self._lock:
                self._working = True
            ok = False
            try:
                ok = bool(self._send(path))
            except Exception:
                pass
            # per-item 清理：谁的文件发完就清谁的临时资源，成败都清(失败不泄漏)。
            # 在工作线程内、发送完成之后调用——绝不会提前删掉尚未发送的文件。
            if on_done is not None:
                try:
                    on_done(path, ok)
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
                    self._working = False
            if batch_done and self._on_batch_done:
                try:
                    self._on_batch_done(*batch_done)
                except Exception:
                    pass
            if batch_done and self._on_busy_changed:
                try:
                    self._on_busy_changed(False)
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
    instance_id = str(saved.get("instance_id") or "")   # 空则 P2PConfig 自动生成
    manual_peers = []
    for m in (saved.get("manual_peers") or []):
        try:
            m_host = str(m.get("host", "")).strip()
            m_port = int(m.get("port", 0))
            m_name = str(m.get("name", "")).strip()
            if m_host and 1 <= m_port <= 65535:
                manual_peers.append({"name": m_name, "host": m_host,
                                     "port": m_port})
        except (TypeError, ValueError, AttributeError):
            continue   # 配置文件被手改坏的条目直接丢弃

    cfg = P2PConfig(inbox=inbox, listen_port=port, peer_name=name, secret=secret,
                    trusted_only=trusted_only, instance_id=instance_id,
                    manual_peers=manual_peers)
    # 首次运行(配置里还没有 instance_id)时生成一个并落盘，之后重启复用同一 ID
    if not saved.get("instance_id"):
        _save_config(cfg)
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
    log_path = os.path.join(inbox, "inkhole-pet.log")
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
# 注意:名字不能用 "InkHolePet"/"WormholePet" —— 这两个已被联想电脑管家
# 记入启动项黑名单(搬进 Run\LenovoDisabled 并加 rem| 前缀禁用)。
# 换成管家未收录的名字才能稳定保留在 Run 键。
_APP_NAME = "InkHole"


def _src_dir() -> str:
    """src 目录绝对路径(pet.py 上两级)。"""
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _startup_script_path() -> str:
    """开机自启脚本/配置文件路径(跨平台)。"""
    if sys.platform == "win32":
        return os.path.join(os.path.expanduser("~"), "inkhole-startup.bat")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/LaunchAgents/com.rexvane.inkhole-pet.plist")
    return os.path.expanduser("~/.config/autostart/inkhole-pet.desktop")


def is_autostart_enabled() -> bool:
    """检查当前是否已设置开机自启。"""
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_READ)
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
            try:
                if frozen:
                    # 打包 exe：注册表直接指向 exe
                    cmd = f'"{python}"'
                else:
                    # 源码运行：生成 .bat 脚本
                    content = "\r\n".join([
                        "@echo off",
                        f'cd /d "{proj}"',
                        f'set "PYTHONPATH={src}"',
                        f'"{python}" -m inkhole.pet',
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
            except Exception as e:
                print(f"[ERROR] 设置开机自启失败: {e}")
                import traceback
                traceback.print_exc()
                return False

        elif sys.platform == "darwin":
            try:
                if frozen:
                    exec_path = sys.executable  # .app 内的可执行文件
                    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.rexvane.inkhole-pet</string>
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
    <key>Label</key><string>com.rexvane.inkhole-pet</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-c</string>
        <string>import os,sys;os.chdir({proj!r});sys.path.insert(0,{src!r});from inkhole.pet import main;main()</string>
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
            except Exception as e:
                print(f"[ERROR] 设置开机自启失败: {e}")
                import traceback
                traceback.print_exc()
                return False

        else:
            # Linux: .desktop
            try:
                if frozen:
                    exec_line = f'"{python}"'
                else:
                    exec_line = f'sh -c \'cd "{proj}" && PYTHONPATH="{src}" "{python}" -m inkhole.pet\''
                content = f"""[Desktop Entry]
Type=Application
Name=墨洞桌宠
Exec={exec_line}
Terminal=false
X-GNOME-Autostart-enabled=true"""
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                print(f"[ERROR] 设置开机自启失败: {e}")
                import traceback
                traceback.print_exc()
                return False

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
            except (FileNotFoundError, OSError) as e:
                print(f"[INFO] 注册表项不存在或删除失败: {e}")
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                print(f"[ERROR] 删除启动脚本失败: {e}")

    result = is_autostart_enabled()
    print(f"[INFO] 开机自启设置{'成功' if result == enabled else '失败'}: enabled={enabled}, result={result}")
    return result


def main(argv=None) -> None:
    cfg, size_override = _build_config(argv)
    _install_crash_log(cfg.inbox)   # 尽早安装:之后任何崩溃/print 都安全且留痕
    try:
        from PySide6.QtCore import QObject, Signal, Slot, QUrl, Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from .branding import make_app_icon as _make_app_icon
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
            "(P2P 引擎本身无需 GUI，可用 python -m inkhole.p2p 跑命令行版)\n")
        raise SystemExit(1)

    def _setup_tray(app, bridge):
        """构建右键菜单 +(可用时)系统托盘图标。

        托盘与桌宠各有一份菜单,尾部动作语义不同(用户明确要求):
          桌宠右键 → 「关闭桌宠」= 只收起挂件(等于设置里关掉开关),程序继续跑;
                     另保留「退出程序」。
          托盘     → 「退出」= 退出整个应用。
        发送目标子菜单在 aboutToShow 时动态重建——对端随时上下线。
        """
        if not _HAS_WIDGETS:
            return None

        def _fill_menu(menu, from_pet):
            try:
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
                        # 显示设备名-完整instance_id，确保唯一标识
                        marker = "●" if peer.name == selected else "○"
                        suffix = f"-{peer.instance_id}" if peer.instance_id else ""
                        label = f"{marker} {peer.name}{suffix}"
                        act = peer_menu.addAction(label)
                        act.setCheckable(True)
                        act.setChecked(peer.name == selected)
                        # lambda 默认绑定技巧:用 name=peer.name 固定当前值
                        act.triggered.connect(
                            lambda checked=False, name=peer.name: bridge._select_peer(name))
                    peer_menu.addSeparator()
                    act_none = peer_menu.addAction("○ 不选目标")
                    act_none.setCheckable(True)
                    act_none.setChecked(selected is None)
                    act_none.triggered.connect(
                        lambda checked=False: bridge._select_peer(None))

                menu.addSeparator()

                act_main = menu.addAction("打开主界面")
                act_main.triggered.connect(bridge.showMain)

                act_open = menu.addAction("打开收件箱")
                act_open.triggered.connect(bridge.openInbox)

                menu.addSeparator()

                if from_pet:
                    act_hide = menu.addAction("关闭桌宠")
                    act_hide.triggered.connect(bridge.hidePet)
                    act_quit = menu.addAction("退出程序")
                else:
                    act_quit = menu.addAction("退出")
                act_quit.triggered.connect(bridge.quit)
            except Exception as e:
                print(f"[ERROR] 菜单构建失败: {e}")
                import traceback
                traceback.print_exc()

        tray_menu = QMenu()
        tray_menu.aboutToShow.connect(lambda: _fill_menu(tray_menu, False))
        pet_menu = QMenu()
        pet_menu.aboutToShow.connect(lambda: _fill_menu(pet_menu, True))
        bridge._tray_menu = pet_menu   # 桌宠右键弹出的是桌宠版菜单

        # 仅当系统托盘可用时才创建并显示托盘图标;不可用也不影响右键菜单
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(_make_app_icon(), app)
        tray.setToolTip("墨洞")
        tray.setContextMenu(tray_menu)
        tray.activated.connect(
            lambda reason: bridge.showMain()
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
        transferStateChanged = Signal(bool)

        def __init__(self, cfg: P2PConfig):
            super().__init__()
            self._tray_menu = None        # 由 _setup_tray 注入:桌宠右键时弹出
            self._main_window = None      # 由 main() 注入:主界面窗口
            self._recent: deque[str] = deque(maxlen=8)   # 最近收到的文件(路径)
            self._engine_lock = threading.RLock()
            self._progress_lock = threading.Lock()
            self._progress_generation = 0
            self._progress_active = False
            self._lan_cfg = cfg
            # 设置保存会后台重启节点(mDNS 重新注册,阻塞数秒不能占 UI 线程);
            # _restart_gate 保证同一时刻只有一次重启,_restarting 供发送路径守卫。
            self._restart_gate = threading.Lock()
            self._restarting = False
            self.node = self._make_node(cfg)
            # 串行发送队列：拖一堆文件不再开一堆并发连接
            self._sendq = SendQueue(
                lambda p: self.node.send_file(p),
                on_batch_done=lambda ok, total: self._on_batch_done(ok, total),
                on_busy_changed=lambda _busy: self.transferStateChanged.emit(
                    self._transfer_active()),
            )
            self.node.start()

        def _make_node(self, cfg):
            return P2PNode(
                cfg,
                on_sent=lambda n: self.absorb.emit(n),
                on_received=lambda p: self._on_received_file(p),
                on_status=lambda s: self._route_status(s),
                on_peers_changed=lambda: self.peersChanged.emit(),
                on_progress=lambda kind, name, done, total: self._on_progress(
                    kind, name, done, total),
            )

        @Slot(result=bool)
        def isTransferActive(self) -> bool:
            return self._transfer_active()

        def _transfer_active(self) -> bool:
            with self._progress_lock:
                progress_active = self._progress_active
            return self._sendq.busy() or progress_active

        def lanConfig(self) -> P2PConfig:
            return self._lan_cfg

        def _on_received_file(self, path: str) -> None:
            self._recent.appendleft(path)
            self.emit_out.emit(os.path.basename(path))

        def _on_progress(self, kind: str, name: str, done: int, total: int) -> None:
            with self._progress_lock:
                self._progress_generation += 1
                generation = self._progress_generation
                self._progress_active = True
            self.transferStateChanged.emit(True)

            def settle():
                with self._progress_lock:
                    if generation != self._progress_generation:
                        return
                    self._progress_active = False
                self.transferStateChanged.emit(self._transfer_active())

            threading.Timer(1.25, settle).start()
            pct = done * 100 // total if total else 100
            self.progress.emit(kind, pct)
            arrow = "↑" if kind == "send" else "↓"
            self.status.emit(f"{arrow} {name} {pct}%")

        def _apply_settings(self, peer_name: str | None = None,
                            secret: str | None = None,
                            port: int | None = None) -> None:
            """改名/改口令/改端口:写回配置并重启 P2P 节点(mDNS 需重新注册)。

            节点重启会阻塞数秒,放到后台线程;_restart_gate 串行化多次保存。
            """
            def worker():
                self._restart_gate.acquire()
                self._restarting = True
                try:
                    self._apply_settings_blocking(peer_name, secret, port)
                finally:
                    self._restarting = False
                    self._restart_gate.release()

            threading.Thread(target=worker, daemon=True).start()

        def _apply_settings_blocking(self, peer_name, secret, port) -> None:
            cfg = self._lan_cfg
            with self._engine_lock:
                self.node.stop()
                if peer_name is not None:
                    cfg.peer_name = peer_name
                if secret is not None:
                    cfg.secret = secret
                if port is not None:
                    cfg.listen_port = port
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

        @Slot()
        def refreshDiscovery(self) -> None:
            """手动重启设备发现(主界面刷新按钮)。mDNS 层偶发卡死的自救手段。"""
            node = self.node
            if isinstance(node, P2PNode):
                self.status.emit("正在重新搜索设备…")
                threading.Thread(
                    target=lambda: node._rebuild_mdns("手动刷新"),
                    daemon=True).start()

        @Slot(result="QVariantList")
        def localAddresses(self) -> list:
            """本机全部非环回 IPv4(含 Tailscale 100.x)。设置页展示用。"""
            try:
                return [ip for ip in self.node._get_local_ips()
                        if ip != "127.0.0.1"]
            except Exception:
                return []

        @Slot(result=str)
        def peerStatus(self) -> str:
            """持续状态：始终返回空(桌宠不显示持续文字，只有出错时才显示)。"""
            return ""

        def _select_peer(self, name):
            """选中目标设备(由右键菜单触发)。"""
            self.node.select_peer(name)

        @Slot(str)
        def dropFile(self, url: str):
            """QML DropArea / 主窗口拖入的 url：文件直接入队，目录先打包成 zip。
            队列单线程串行发送：一次拖 N 项不会开 N 个并发连接。"""
            if self._restarting:
                self.status.emit("正在应用新设置，请稍候再发送")
                return
            path = QUrl(url).toLocalFile() if url.startswith("file:") else url
            if not path:
                return
            if not self.node.selected_peer():
                self.status.emit("右键选择目标设备")
                return
            self._enqueue_path(path)

        def _enqueue_path(self, path: str):
            """文件直接入队；目录打包成临时 zip 再入队,该 zip 发送完成后立即
            清理它自己的临时目录。

            清理精确绑定单个文件(SendQueue 的 per-item on_done),不再用跨批次
            共享的可变列表——连续快速拖入多个文件夹时,每个文件夹各清各的临时
            目录,不会被别的批次提前删除(漏发)或漏删(泄漏)。"""
            if os.path.isfile(path):
                self._sendq.put(path)
                return
            if os.path.isdir(path):
                from .p2p import _zip_dir
                try:
                    self.status.emit("正在打包文件夹…")
                    zip_path = _zip_dir(path)
                except Exception as e:
                    self.status.emit(f"打包失败：{e}")
                    return
                # 用闭包捕获这个 zip 自己的临时目录：它发送完成(成/败)后即清理。
                # 只在工作线程内、发送之后访问该目录变量,无共享状态、无需加锁。
                temp_dir = os.path.dirname(zip_path)

                def _cleanup(_path, _ok, _d=temp_dir):
                    import shutil as _sh
                    _sh.rmtree(_d, ignore_errors=True)

                self._sendq.put(zip_path, on_done=_cleanup)

        def _on_batch_done(self, ok: int, total: int):
            """一批发送结束：多文件时聚合提示。

            临时打包目录的清理已下沉到 per-file 回调(_enqueue_path 里的
            _cleanup),这里不再统一清理,避免跨批次误删/漏删。"""
            if total > 1:
                self.status.emit(f"已吞入 {ok}/{total} 个")

        @Slot(result=bool)
        def hasTarget(self) -> bool:
            """QML 用来判断拖入文件时是否该播吸入动画。"""
            return self.node.selected_peer() is not None

        @Slot(result=bool)
        def isTrustedOnly(self) -> bool:
            return self._lan_cfg.trusted_only

        @Slot(result=bool)
        def toggleTrustedOnly(self) -> bool:
            """切换「仅接收目标设备」：拦掉局域网里陌生设备的投喂。"""
            self._lan_cfg.trusted_only = not self._lan_cfg.trusted_only
            _save_config(self._lan_cfg)
            return self._lan_cfg.trusted_only

        @Slot(result="QVariantList")
        def recentFiles(self) -> list:
            """最近收到的文件路径列表(新的在前)。"""
            return [p for p in self._recent if os.path.exists(p)]

        @Slot()
        def clearRecent(self) -> None:
            """清空最近接收列表(只清记录,不动磁盘上的文件)。"""
            self._recent.clear()
            self.emit_out.emit("")   # 复用信号触发界面刷新列表

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
            return os.path.abspath(self._lan_cfg.inbox)

        @Slot()
        def openInbox(self):
            """在系统文件管理器中打开收件箱目录(跨平台)。"""
            path = os.path.abspath(self._lan_cfg.inbox)
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
                None, "选择收件箱目录", self._lan_cfg.inbox)
            if directory:
                self._lan_cfg.inbox = directory
                os.makedirs(directory, exist_ok=True)
                _save_config(self._lan_cfg)   # 记住，重启后仍生效
                self.status.emit(f"收件箱: {os.path.basename(directory)}")

        @Slot(str)
        def setInbox(self, directory: str) -> None:
            """设置收件箱路径并持久化(主界面「保存」用)。

            与 chooseInbox 的差别:直接收路径,无对话框。即使其他设置项没变,
            也保证收件箱改动落盘——修复"只改收件箱、重启后回退"的问题。
            """
            directory = (directory or "").strip()
            if not directory:
                return
            if os.path.abspath(directory) == os.path.abspath(self._lan_cfg.inbox):
                return   # 没变,不必写盘
            self._lan_cfg.inbox = directory
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                pass
            _save_config(self._lan_cfg)

        # ---- 手动设备(Tailscale/固定 IP 直连) ----
        @Slot(result="QVariantList")
        def manualPeers(self) -> list:
            return [dict(m) for m in (self._lan_cfg.manual_peers or [])]

        @Slot(str, str, int, result=bool)
        def addManualPeer(self, name: str, host: str, port: int) -> bool:
            """添加手动设备并持久化。局域网模式下立即出现在设备列表。"""
            host = (host or "").strip()
            name = (name or "").strip()
            try:
                port = int(port)
            except (TypeError, ValueError):
                return False
            if not host or " " in host or not 1 <= port <= 65535:
                return False
            if isinstance(self.node, P2PNode):
                self.node.add_manual_peer(name, host, port)   # 就地注册(共享 cfg)
            else:
                # 远程模式:只更新配置,切回局域网时 start() 会注册
                self._lan_cfg.manual_peers = [
                    m for m in (self._lan_cfg.manual_peers or [])
                    if not (m["host"] == host and int(m["port"]) == port)]
                self._lan_cfg.manual_peers.append(
                    {"name": name, "host": host, "port": port})
            _save_config(self._lan_cfg)
            return True

        @Slot(str, int)
        def removeManualPeer(self, host: str, port: int) -> None:
            if isinstance(self.node, P2PNode):
                self.node.remove_manual_peer(host, int(port))
            else:
                self._lan_cfg.manual_peers = [
                    m for m in (self._lan_cfg.manual_peers or [])
                    if not (m["host"] == host and int(m["port"]) == int(port))]
            _save_config(self._lan_cfg)

        @Slot()
        def renameDevice(self):
            """改本机显示名(对端右键菜单里看到的名字)。"""
            if not _HAS_WIDGETS:
                self.status.emit("无法打开对话框")
                return
            from PySide6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(
                None, "设备名称", "对端设备看到的名字：",
                text=self._lan_cfg.peer_name)
            if ok:
                name = name.strip()
                if name and name != self._lan_cfg.peer_name:
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
                QLineEdit.Normal, self._lan_cfg.secret)
            if ok and secret != self._lan_cfg.secret:
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
            return set_autostart(enabled, self._lan_cfg)

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
        def showMain(self):
            """显示并激活主窗口(托盘单击/右键菜单"打开主界面")。"""
            w = getattr(self, "_main_window", None)
            if w is not None:
                w.show()
                w.raise_()
                w.activateWindow()

        @Slot()
        def hidePet(self):
            """关闭桌宠挂件(桌宠右键菜单"关闭桌宠")。

            等价于主界面设置里关掉「桌宠挂件」开关:只收起挂件并记住,
            程序与传输继续运行;重新开启走主界面设置。
            """
            hide = getattr(self, "_hide_pet", None)
            if hide is not None:
                hide()
                self.status.emit("桌宠已关闭，可在主界面设置中重新开启")

        @Slot()
        def showMenu(self):
            """桌宠被右键时,在鼠标位置弹出菜单。"""
            if self._tray_menu is not None:
                from PySide6.QtGui import QCursor
                self._tray_menu.popup(QCursor.pos())
            else:
                self.showMain()

        @Slot()
        def quit(self):
            """退出:节点停止放后台并限时等待,不让 mDNS 注销卡住退出。"""
            closer = threading.Thread(target=self.node.stop, daemon=True)
            closer.start()
            closer.join(2.0)   # 给 mDNS goodbye 留 2 秒,超时直接退
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

    # 桌宠 = 主界面里可开关的选项(show_pet)，默认开启
    pet_root = engine.rootObjects()[0]
    show_pet = bool(_load_saved_config().get("show_pet", True))
    pet_root.setVisible(show_pet)

    def _set_pet_visible(on: bool) -> None:
        pet_root.setVisible(bool(on))
        _save_config(bridge._lan_cfg, show_pet=bool(on))

    # 桌宠右键"关闭桌宠"走这里:与设置页开关同一持久化路径
    bridge._hide_pet = lambda: _set_pet_visible(False)

    def _apply_identity(name: str, secret: str, port: int) -> None:
        c = bridge._lan_cfg
        if (name, secret, port) == (c.peer_name, c.secret, c.listen_port):
            return   # 没变就不重启节点
        bridge._apply_settings(peer_name=name, secret=secret, port=port)

    # 主界面窗口(需要 QtWidgets；不可用时退回纯桌宠模式)
    main_win = None
    if _HAS_WIDGETS:
        from .mainwindow import MainWindow
        main_win = MainWindow(bridge, {
            "pet_visible": lambda: pet_root.isVisible(),
            "set_pet_visible": _set_pet_visible,
            "is_autostart": is_autostart_enabled,
            "set_autostart": lambda on: set_autostart(on, bridge._lan_cfg),
            "apply_settings": _apply_identity,
        }, icon=_make_app_icon())
        bridge._main_window = main_win
        main_win.show()

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
