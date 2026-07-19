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

from PySide6.QtCore import QObject, QItemSelectionModel, Signal, Qt  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import (QApplication, QBoxLayout, QDialog,
                               QDialogButtonBox, QFileDialog,
                               QTreeView)  # noqa: E402

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
    recentChanged = Signal()
    progress = Signal(str, int)
    progressCleared = Signal()
    sendStateChanged = Signal(bool)
    updateCheckFinished = Signal(bool, str, str, str)

    def __init__(self):
        super().__init__()
        self.node = _FakeNode()
        self._manual = []
        self.opened_paths = []
        self.dropped_paths = []
        self.applied_settings = None
        self.cancel_calls = 0

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

    def localAddresses(self):
        return ["192.168.1.10", "100.64.0.9"]

    def actualPort(self):
        return 43123

    def lastStatus(self):
        return "墨洞已开启 · SMOKE"

    def refreshDiscovery(self):
        pass

    def clearRecent(self):
        pass

    def appVersion(self):
        return "0.0.0"

    def releasesPage(self):
        return "https://example.com"

    def repositoryPage(self):
        return "https://example.com/repository"

    def checkUpdate(self):
        pass

    def cancelTransfer(self):
        self.cancel_calls += 1
        return True

    def performUpdate(self, url):
        pass

    def openPath(self, path):
        self.opened_paths.append(path)

    def dropFile(self, path):
        self.dropped_paths.append(path)

    # ---- 手动设备 ----
    def manualPeers(self):
        return [dict(m) for m in self._manual]

    def setManualPeers(self, peers):
        self._manual = [dict(m) for m in peers]
        return True

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

    bridge = FakeBridge()
    ctl = {
        "pet_visible": lambda: True,
        "set_pet_visible": lambda on: None,
        "is_autostart": lambda: False,
        "set_autostart": lambda on: None,
        "apply_settings": lambda name, secret, port, enabled: setattr(
            bridge, "applied_settings", (name, secret, port, enabled)),
    }
    return MainWindow(bridge, ctl), bridge


def _capture_android_dialogs(monkeypatch):
    from inkhole.mainwindow import AndroidStyleDialog

    dialogs = []
    monkeypatch.setattr(
        AndroidStyleDialog, "exec", lambda self: dialogs.append(self) or 0)
    return dialogs


def test_window_constructs_without_attribute_error(app):
    """Constructing the window must not reference undefined bridge members."""
    window, _ = _make_window(app)
    assert window is not None
    assert window._stack.count() == 2   # 主页 + 单一设置页


def test_home_hole_uses_prominent_preferred_size(app):
    window, _ = _make_window(app)
    window.resize(960, 640)
    window.show()
    app.processEvents()

    assert window._hole.width() >= 224
    assert window._hole.height() >= 224


def test_send_content_dialog_accepts_files_and_folders(app, tmp_path):
    from inkhole.mainwindow import SendContentDialog

    folder = tmp_path / "folder"
    folder.mkdir()
    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")
    dialog = SendContentDialog(start_dir=str(tmp_path))
    dialog.show()
    app.processEvents()

    assert dialog.labelText(QFileDialog.LookIn) == "位置："
    assert dialog.labelText(QFileDialog.FileName) == "文件名称："
    assert dialog.labelText(QFileDialog.FileType) == "文件类型："
    assert dialog.labelText(QFileDialog.Accept) == "发送"
    assert dialog.labelText(QFileDialog.Reject) == "取消"

    view = dialog.findChild(QTreeView, "treeView")
    model = view.model()
    for path in (folder, file_path):
        index = model.index(str(path))
        assert index.isValid()
        view.selectionModel().select(
            index, QItemSelectionModel.Select | QItemSelectionModel.Rows)
    buttons = dialog.findChild(QDialogButtonBox, "buttonBox")
    buttons.button(QDialogButtonBox.Open).click()
    app.processEvents()

    assert dialog.result() == QDialog.Accepted
    assert set(dialog.selected_paths()) == {
        os.path.normpath(str(folder)), os.path.normpath(str(file_path))}


def test_send_content_dialog_uses_system_palette_inside_dark_parent(app, tmp_path):
    from inkhole.mainwindow import SendContentDialog

    window, _bridge = _make_window(app)
    dialog = SendContentDialog(window, start_dir=str(tmp_path))
    dialog.show()
    app.processEvents()

    view = dialog.findChild(QTreeView, "treeView")
    system_palette = app.palette()
    dialog_palette = dialog.palette()
    view_palette = view.palette()

    assert dialog.testAttribute(Qt.WA_StyledBackground)
    assert dialog_palette.color(QPalette.Window) == system_palette.color(QPalette.Window)
    assert dialog_palette.color(QPalette.WindowText) == system_palette.color(QPalette.WindowText)
    assert view_palette.color(QPalette.Base) == system_palette.color(QPalette.Base)
    assert view_palette.color(QPalette.Text) == system_palette.color(QPalette.Text)


def test_send_button_uses_one_picker_for_files_and_folders(
        app, monkeypatch, tmp_path):
    from inkhole.mainwindow import SendContentDialog

    folder = tmp_path / "folder"
    folder.mkdir()
    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")
    paths = [str(file_path), str(folder)]
    monkeypatch.setattr(SendContentDialog, "exec", lambda _self: QDialog.Accepted)
    monkeypatch.setattr(SendContentDialog, "selected_paths", lambda _self: paths)

    window, bridge = _make_window(app)
    bridge.node._selected = "peer"
    window._send_btn.click()

    assert window._send_btn.menu() is None
    assert bridge.dropped_paths == paths


def test_send_button_uses_native_macos_picker_when_available(
        app, monkeypatch, tmp_path):
    import inkhole.mainwindow as mainwindow

    folder = tmp_path / "folder"
    folder.mkdir()
    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")
    paths = [str(file_path), str(folder)]
    monkeypatch.setattr(mainwindow, "_use_macos_native_send_panel", lambda: True)
    monkeypatch.setattr(
        mainwindow, "_pick_macos_send_paths",
        lambda _start_dir: (paths, str(tmp_path)))

    def reject_qt_fallback(_self):
        raise AssertionError("macOS should use NSOpenPanel, not QFileDialog")

    monkeypatch.setattr(mainwindow.SendContentDialog, "exec", reject_qt_fallback)
    window, bridge = _make_window(app)
    bridge.node._selected = "peer"
    window._send_btn.click()

    assert bridge.dropped_paths == paths
    assert window._send_dialog_dir == str(tmp_path)


def test_macos_native_picker_declares_chinese_and_english_localizations():
    from inkhole.macos import configure_bundle_localizations

    info = {}

    class FakeBundle:
        @staticmethod
        def infoDictionary():
            return info

    configure_bundle_localizations(FakeBundle())

    assert info == {
        "CFBundleAllowMixedLocalizations": True,
        "CFBundleDevelopmentRegion": "zh-Hans",
        "CFBundleLocalizations": ["zh-Hans", "en"],
    }


def test_settings_page_opens_and_populates(app):
    window, _ = _make_window(app)
    window._open_settings()
    assert window._stack.currentIndex() == 1
    assert window._ed_name.text() == "SMOKE"
    assert window._sp_port.text() == ""
    assert window._manual_name.text() == ""
    assert window._manual_host.text() == ""
    assert window._manual_port.text() == ""
    assert window._ed_name.labelText() == "设备名称"
    assert window._sp_port.labelText() == "本机监听端口（留空=自动，建议 1024-49151，如 41300）"
    assert window._manual_host.labelText() == "Tailscale IP 或 MagicDNS 名称"
    assert window._local_info_lbl.text().startswith("本机：SMOKE-")
    assert window._version_info_lbl.text() == "版本：v0.0.0"
    assert window._port_info_lbl.text() == "端口：43123（建议自定义 1024-49151 固定端口）"
    assert "IP" not in window._local_info_lbl.text()


def test_encryption_toggle_controls_password_and_preserves_it(app):
    window, bridge = _make_window(app)
    bridge.node.cfg.secret = "saved-secret"
    bridge.node.cfg.encryption_enabled = True
    window._open_settings()

    assert window._cb_encrypt.isChecked()
    assert window._ed_secret.isEnabled()
    window._cb_encrypt.setChecked(False)
    assert not window._ed_secret.isEnabled()
    window._save_settings()

    assert bridge.applied_settings == ("SMOKE", "saved-secret", 0, False)


def test_encryption_toggle_requires_password_when_enabled(app, monkeypatch):
    dialogs = _capture_android_dialogs(monkeypatch)
    window, bridge = _make_window(app)
    window._open_settings()
    window._cb_encrypt.setChecked(True)
    window._ed_secret.clear()
    window._save_settings()

    assert dialogs
    assert "必须填写加密口令" in dialogs[-1].body_html
    assert bridge.applied_settings is None


def test_compact_window_keeps_transfer_metrics_visible(app):
    window, bridge = _make_window(app)
    window.resize(720, 480)
    window.show()
    app.processEvents()

    long_name = "macOS-" + "very-long-filename-" * 8 + ".dmg"
    bridge.status.emit(f"↓ 接收 {long_name} 63% · 12.4 MB/s")
    app.processEvents()

    assert window.width() == 720
    assert window.height() == 480
    assert window._status_meta_lbl.isVisible()
    assert window._status_meta_lbl.text() == "63% · 12.4 MB/s"
    status_bottom = window._status_bar.mapTo(
        window, window._status_bar.rect().bottomRight()).y()
    assert status_bottom < window.height()


def test_settings_layout_reflows_on_narrow_windows(app):
    window, _bridge = _make_window(app)
    window.resize(800, 520)
    window.show()
    window._open_settings()
    app.processEvents()

    assert window._settings_columns.direction() == QBoxLayout.TopToBottom
    assert not window._settings_divider.isVisible()
    assert len(window._settings_groups) == 5

    window.resize(960, 640)
    app.processEvents()
    assert window._settings_columns.direction() == QBoxLayout.LeftToRight
    assert window._settings_divider.isVisible()


def test_in_app_dialog_paints_translucent_dark_backdrop(app):
    from inkhole.mainwindow import AndroidStyleDialog

    window, _bridge = _make_window(app)
    window.show()
    dialog = AndroidStyleDialog(
        window, "使用说明", "<p style='color:#B2BFBC;'>说明内容</p>")
    dialog.addAction("ok", "知道了", True)
    dialog.show()
    app.processEvents()

    backdrop = dialog.grab().toImage().pixelColor(2, 2)
    assert dialog.testAttribute(Qt.WA_TranslucentBackground)
    assert dialog.testAttribute(Qt.WA_NoSystemBackground)
    assert backdrop.alpha() < 255
    assert max(backdrop.red(), backdrop.green(), backdrop.blue()) < 20


def test_manual_peer_add_and_remove(app):
    """Manual peers stay as drafts until the settings page is saved."""
    window, bridge = _make_window(app)
    window._open_settings()
    window._manual_name.setText("我的电脑")
    window._manual_host.setText("100.64.0.2")
    window._manual_port.setText("52130")
    window._add_manual_peer()
    assert bridge.manualPeers() == []
    assert window._manual_draft == [
        {"name": "我的电脑", "host": "100.64.0.2", "port": 52130}]
    assert window._manual_name.text() == ""
    assert window._manual_host.text() == ""
    assert window._manual_port.text() == ""
    window._save_settings()
    assert bridge.manualPeers() == [
        {"name": "我的电脑", "host": "100.64.0.2", "port": 52130}]


def test_manual_peer_edit_and_cancel_are_non_destructive(app):
    """Editing a draft must not touch live config until main Save."""
    window, bridge = _make_window(app)
    identity = "0123456789abcdef0123456789abcdef"
    bridge._manual = [
        {"name": "旧备注", "host": "100.64.0.3", "port": 52130,
         "instance_id": identity}]
    window._open_settings()
    window._edit_manual_peer(0)
    assert bridge.manualPeers()[0]["name"] == "旧备注"
    assert window._manual_add_btn.text() == "保存设备"
    window._manual_name.setText("新备注")
    window._add_manual_peer()
    assert bridge.manualPeers()[0]["name"] == "旧备注"
    window._cancel_settings()
    assert bridge.manualPeers()[0]["name"] == "旧备注"

    window._open_settings()
    window._edit_manual_peer(0)
    window._manual_name.setText("新备注")
    window._add_manual_peer()
    window._save_settings()
    assert bridge.manualPeers() == [
        {"name": "新备注", "host": "100.64.0.3", "port": 52130,
         "instance_id": identity}]


def test_manual_peer_remove_is_draft_until_save(app):
    window, bridge = _make_window(app)
    bridge._manual = [
        {"name": "目标", "host": "100.64.0.4", "port": 52130}]
    window._open_settings()
    window._remove_manual_peer(0)
    assert bridge.manualPeers()
    window._save_settings()
    assert bridge.manualPeers() == []


def test_manual_peer_rejects_bad_input(app, monkeypatch):
    """Ambiguous/invalid host must be rejected with a warning, not stored."""
    dialogs = _capture_android_dialogs(monkeypatch)
    window, bridge = _make_window(app)
    window._open_settings()
    window._manual_host.setText("11111")   # 多种拆分方式,有歧义必须拒绝
    window._add_manual_peer()
    assert bridge.manualPeers() == []
    assert dialogs
    assert dialogs[-1].title_text == "手动设备"
    assert "Tailscale 地址无效" in dialogs[-1].body_html


def test_explicit_zero_listen_port_is_rejected(app, monkeypatch):
    """Only a blank field means automatic; typed 0 is invalid like Android."""
    dialogs = _capture_android_dialogs(monkeypatch)
    window, bridge = _make_window(app)
    window._open_settings()
    window._sp_port.setText("0")
    window._save_settings()
    assert dialogs
    assert "本机监听端口必须在 1-65535 范围内" in dialogs[-1].body_html
    assert bridge.applied_settings is None
    assert window._stack.currentIndex() == 1


def test_peer_and_transfer_signals_drive_window(app):
    """Transfer progress, cancellation state and errors flow into the window."""
    window, bridge = _make_window(app)
    bridge.peersChanged.emit()
    bridge.progress.emit("send", 42)
    assert window._hole._progress_target == pytest.approx(0.42)
    bridge.progressCleared.emit()
    assert window._hole._progress_target == 0
    assert window._hole._progress == 0
    bridge.sendStateChanged.emit(True)
    assert window._send_action_stack.currentIndex() == 1
    assert not window._hole.isEnabled()
    window._cancel_send_btn.click()
    assert bridge.cancel_calls == 1
    bridge.sendStateChanged.emit(False)
    assert window._send_action_stack.currentIndex() == 0
    assert window._hole.isEnabled()
    bridge.errorState.emit("boom")
    bridge.errorState.emit("")
    assert window._state_lbl._full_text == "等待附近的墨洞上线…"


def test_repository_button_opens_github(app):
    window, bridge = _make_window(app)
    window._repository_btn.click()
    assert bridge.opened_paths == ["https://example.com/repository"]


def test_update_dialog_matches_android_summary(app, monkeypatch):
    """Available-update dialog exposes versions, availability and changes."""
    dialogs = _capture_android_dialogs(monkeypatch)
    window, _ = _make_window(app)
    window._on_update_check(True, "v1.2.3", "• 修复设备发现", "")
    assert dialogs
    dialog = dialogs[-1]
    assert dialog.title_text == "发现新版本"
    assert "当前版本：v0.0.0" in dialog.body_html
    assert "最新版本：v1.2.3" in dialog.body_html
    assert "更新状态：可前往发布页下载" in dialog.body_html
    assert "本次更新" in dialog.body_html
    assert "• 修复设备发现" in dialog.body_html
    assert set(dialog.actions) == {"cancel", "release"}


def test_usage_guide_uses_android_style_in_app_dialog(app, monkeypatch):
    dialogs = _capture_android_dialogs(monkeypatch)
    window, _ = _make_window(app)
    window._show_usage_guide()
    assert dialogs
    dialog = dialogs[-1]
    assert dialog.title_text == "使用说明"
    assert "局域网" in dialog.body_html
    assert "跨网络" in dialog.body_html
    assert set(dialog.actions) == {"ok"}


def test_outlined_field_label_floats_on_focus_or_content(app):
    from inkhole.mainwindow import OutlinedLineEdit

    field = OutlinedLineEdit("设备名称")
    assert not field.isLabelFloating()
    field.setText("桌面电脑")
    assert field.isLabelFloating()
    field.clear()
    assert not field.isLabelFloating()

    field.show()
    field.setFocus()
    app.processEvents()
    assert field.isLabelFloating()
    field.clearFocus()
    app.processEvents()
    assert not field.isLabelFloating()
    field.close()


def test_outlined_field_floating_label_has_a_real_top_notch(app):
    from inkhole.mainwindow import OutlinedLineEdit, _QSS

    field = OutlinedLineEdit("Remote port")
    field.setStyleSheet(_QSS)
    field.resize(220, 48)
    field.show()
    field.setFocus()
    field._set_label_progress(1.0)
    app.processEvents()

    image = field.grab().toImage()
    _, _, _, label_height, label_x, label_y, _, ink_left, ink_width = \
        field._label_geometry(1.0)
    label_start = label_x + ink_left
    # Sample the notch padding, not the glyph itself: the top edge is
    # intentionally aligned with the label's vertical center.
    notch_x = int(round(label_start + ink_width + 2))
    top_y = int(round(label_y + label_height / 2.0))
    left_border = int(round(label_start - 6))
    right_border = min(
        field.width() - 12, int(round(label_start + ink_width + 10)))
    notch = image.pixelColor(notch_x, top_y)
    left_line = image.pixelColor(left_border, top_y)
    right_line = image.pixelColor(right_border, top_y)
    old_top = image.pixelColor(right_border, 0)

    assert notch.green() < 40
    assert left_line.green() > 100
    assert right_line.green() > 100
    assert old_top.green() < 40
    field.close()


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
    # IPv6 使用标准解析，不进入 IPv4 自动分段
    assert fix("fd7a:115c:a1e0::1") == "fd7a:115c:a1e0::1"
    assert fix("2001:0:0::1") == "2001::1"
    assert fix("fd7a:115c:a1e0::gg") is None
    # 主机名放行(Tailscale MagicDNS)
    assert fix("my-pc") == "my-pc"
    assert fix("my pc") == "mypc"
    assert fix("https://my-pc") is None


def test_mask_manual_host_typing():
    """边打边分段:段满 3 位或再加一位超 255 自动落点(经典 IP 输入框)。"""
    from inkhole.mainwindow import mask_manual_host_typing as mask
    # 连续输入自动分段(用户诉求场景)
    assert mask("1001274626") == "100.127.46.26"
    assert mask("100127") == "100.127"
    # 全角句号/逗号即落点
    assert mask("100。127") == "100.127"
    assert mask("100，127") == "100.127"
    assert mask("2001:0:0::1") == "2001:0:0::1"
    # 段没满不越权补点(46 之后等用户自己点或数字溢出)
    assert mask("100.127.46") == "100.127.46"
    # 段溢出自动断段:192.168.1 后打 5 并入(合法),打 999 会断
    assert mask("19216815") == "192.168.15"
    assert mask("192168999") == "192.168.99.9"
    # 尾部点保留(正在输入下一段)
    assert mask("100.") == "100."
    # 第四段允许打满 3 位(超 255 由添加时校验拦),第 4 位起丢弃
    assert mask("100.127.46.261") == "100.127.46.261"
    assert mask("100.127.46.2611") == "100.127.46.261"
    # 主机名不做掩码
    assert mask("my-pc") == "my-pc"


def test_file_metadata_format_matches_android():
    from inkhole.mainwindow import format_file_size, format_file_time

    assert format_file_size(512) == "512B"
    assert format_file_size(1536) == "2KB"
    assert format_file_size(2_400_000) == "2.3MB"
    now = 2_000_000.0
    assert format_file_time(now - 20, now) == "刚刚"
    assert format_file_time(now - 120, now) == "2 分钟前"
    assert format_file_time(now - 7200, now) == "2 小时前"
    assert format_file_time(now - 90000, now) == "昨天"


def test_version_newer():
    """更新检查的版本比较:语义化、容 v 前缀、位数不齐。"""
    from inkhole.pet import _version_newer as newer
    assert newer("v1.3.7", "1.3.6")
    assert newer("1.10.0", "1.9.9")
    assert not newer("1.3.6", "1.3.6")
    assert not newer("v1.3.5", "1.3.6")
    assert newer("2.0", "1.9.9.9")
    assert not newer("", "1.0.0")


def test_summarize_release_notes():
    """更新说明摘要:清洗 Markdown 为干净要点,遇安装/下载段停止,与安卓一致。"""
    from inkhole.pet import _summarize_release_notes as summarize
    raw = """
## Changes
- First feature change
- Second bug fix
## Download
- Windows installer
- macOS dmg
"""
    result = summarize(raw)
    assert "• First feature change" in result
    assert "• Second bug fix" in result
    assert "Windows installer" not in result  # 遇 Download 段停止
    assert "macOS dmg" not in result
