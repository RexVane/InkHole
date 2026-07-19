import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from inkhole.branding import MACOS_ICON_SCALE, app_icon_pixmap  # noqa: E402


def test_macos_icon_uses_transparent_safe_area():
    _app = QApplication.instance() or QApplication([])
    size = 128

    full = app_icon_pixmap(size).toImage()
    macos = app_icon_pixmap(size, MACOS_ICON_SCALE).toImage()

    assert full.pixelColor(0, size // 2).alpha() > 0
    assert macos.pixelColor(0, size // 2).alpha() == 0
    assert macos.pixelColor(14, size // 2).alpha() > 0
