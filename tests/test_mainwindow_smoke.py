"""Offscreen smoke tests for the Windows desktop main window.

These guard the Python<->UI contract: every ``bridge.*`` attribute the window
touches must exist with the right shape (an earlier regression shipped a
window wired to signals the bridge never defined -> AttributeError on
construction). The app now runs a single unified transport: LAN auto-discovery
plus manually added peers (Tailscale / fixed-IP direct connect).

Run headless via the Qt "offscreen" platform so CI needs no display.
"""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from inkhole.p2p import P2PConfig  # noqa: E402


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


class _FakeNode:
    def __init__(self):
        self.cfg = P2PConfig(inbox="_smoke_inbox", peer_name="SMOKE")
        self._selected = None

    def peers(self):
        return []

    def selected_peer(self):
        return self._selected

    def select_peer(self, name):
        self._selected = name


class FakeBridge(QObject):
    """Mirrors the exact signal/method surface MainWindow depends on."""

    peersChanged = Signal()
    status = Signal(str)
    errorState = Signal(str)
    emit_out = Signal(str)
    progress = Signal(str, int)

    def __init__(self):
        super().__init__()
        self.node = _FakeNode()
        self._manual = []

    # ---- query surface used by the window ----
    def lanConfig(self):
        return self.node.cfg

    def recentFiles(self):
        return []

    def toggleTrustedOnly(self):
        self.node.cfg.trusted_only = not self.node.cfg.trusted_only
        return self.node.cfg.trusted_only

    def setInbox(self, directory):
        self.node.cfg.inbox = directory

    def openInbox(self):
        pass

    # ---- 手动设备 ----
    def manualPeers(self):
        return [dict(m) for m in self._manual]

    def addManualPeer(self, name, host, port):
        if not host or " " in host or not 1 <= int(port) <= 65535:
            return False
        self._manual = [m for m in self._manual
                        if not (m["host"] == host and m["port"] == int(port))]
        self._manual.append({"name": name, "host": host, "port": int(port)})
        return True

    def removeManualPeer(self, host, port):
        self._manual = [m for m in self._manual
                        if not (m["host"] == host and m["port"] == int(port))]


def _make_window(app):
    from inkhole.mainwindow import MainWindow

    ctl = {
        "pet_visible": lambda: True,
        "set_pet_visible": lambda on: None,
        "is_autostart": lambda: False,
        "set_autostart": lambda on: None,
        "apply_settings": lambda name, secret, port: None,
    }
    bridge = FakeBridge()
    return MainWindow(bridge, ctl), bridge


def test_window_constructs_without_attribute_error(app):
    """Constructing the window must not reference undefined bridge members."""
    window, _ = _make_window(app)
    assert window is not None
    assert window._stack.count() == 2   # 主页 + 单一设置页


def test_settings_page_opens_and_populates(app):
    window, _ = _make_window(app)
    window._open_settings()
    assert window._stack.currentIndex() == 1
    assert window._ed_name.text() == "SMOKE"


def test_manual_peer_add_and_remove(app):
    """Manual peer UI round-trips through the bridge and refreshes the list."""
    window, bridge = _make_window(app)
    window._open_settings()
    window._manual_host.setText("100.64.0.2")
    window._manual_port.setValue(52130)
    window._add_manual_peer()
    assert bridge.manualPeers() == [
        {"name": "", "host": "100.64.0.2", "port": 52130}]
    assert window._manual_host.text() == ""     # 添加成功后清空输入
    window._remove_manual_peer("100.64.0.2", 52130)
    assert bridge.manualPeers() == []


def test_manual_peer_rejects_bad_input(app, monkeypatch):
    """Ambiguous/invalid host must be rejected with a warning, not stored."""
    from PySide6.QtWidgets import QMessageBox
    warned = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a)))
    window, bridge = _make_window(app)
    window._open_settings()
    window._manual_host.setText("11111")   # 多种拆分方式,有歧义必须拒绝
    window._add_manual_peer()
    assert bridge.manualPeers() == []
    assert warned


def test_peer_and_transfer_signals_drive_window(app):
    """peersChanged / progress / errorState flow through without raising."""
    window, bridge = _make_window(app)
    bridge.peersChanged.emit()
    bridge.progress.emit("send", 42)
    bridge.errorState.emit("boom")
    bridge.errorState.emit("")
    assert window._state_lbl._full_text == "正在发现设备"


def test_normalize_manual_host():
    """IP 输入自动纠正:补点/清理标点/歧义拒绝/主机名放行。"""
    from inkhole.mainwindow import normalize_manual_host as fix
    # 正常输入原样通过
    assert fix("100.127.46.26") == "100.127.46.26"
    assert fix(" 192.168.1.5 ") == "192.168.1.5"
    # 用户案例:缺一个点,唯一合法拆分
    assert fix("100127.46.26") == "100.127.46.26"
    assert fix("192168.1.5") == "192.168.1.5"
    # 全角句号/逗号当分隔符
    assert fix("100。127。46。26") == "100.127.46.26"
    assert fix("100,127,46,26") == "100.127.46.26"
    # 全无分隔符但唯一可拆
    assert fix("1.2.3.4") == "1.2.3.4"
    # 歧义必须拒绝(11111 有多种拆法)
    assert fix("11111") is None
    # 非法值拒绝
    assert fix("300.1.1.1") is None
    assert fix("1.2.3") is None      # 三段且无法拆分,补不成四段
    assert fix("") is None
    # 主机名放行(Tailscale MagicDNS)
    assert fix("my-pc") == "my-pc"
    assert fix("my pc") == "mypc"
