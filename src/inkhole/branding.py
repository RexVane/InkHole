"""InkHole brand mark and desktop application icon rendering."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QIcon, QLinearGradient, QPainter, QPen,
                           QPixmap)


TEAL = QColor(90, 216, 192)
AMBER = QColor(233, 189, 114)
MACOS_ICON_SCALE = 0.80


def paint_brand_mark(painter: QPainter, rect: QRectF) -> None:
    """Paint the two-arc InkHole mark used beside the desktop title."""
    size = min(rect.width(), rect.height())
    cx = rect.center().x()
    cy = rect.center().y()

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(2, 5, 6))
    core_radius = size * 0.2833
    painter.drawEllipse(QPointF(cx, cy), core_radius, core_radius)

    teal_radius = size * 0.3667
    teal_pen = QPen(TEAL, max(1.0, size * 0.0667), Qt.SolidLine, Qt.RoundCap)
    painter.setPen(teal_pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(QRectF(cx - teal_radius, cy - teal_radius,
                           teal_radius * 2, teal_radius * 2),
                    30 * 16, 205 * 16)

    amber_radius = size * 0.2667
    amber_pen = QPen(AMBER, max(1.0, size * 0.05), Qt.SolidLine, Qt.RoundCap)
    painter.setPen(amber_pen)
    painter.drawArc(QRectF(cx - amber_radius, cy - amber_radius,
                           amber_radius * 2, amber_radius * 2),
                    215 * 16, 90 * 16)
    painter.restore()


def app_icon_pixmap(size: int, canvas_scale: float = 1.0) -> QPixmap:
    """Render the brand mark on the dark rounded desktop app tile.

    ``canvas_scale`` leaves transparent space around the whole tile. macOS
    app icons use its safe area instead of filling the complete ICNS canvas.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    scale = max(0.1, min(1.0, float(canvas_scale)))
    tile_size = size * scale
    canvas_inset = (size - tile_size) / 2.0
    bounds = QRectF(canvas_inset + 0.5, canvas_inset + 0.5,
                    tile_size - 1.0, tile_size - 1.0)
    corner = tile_size * 0.22
    tile = QLinearGradient(bounds.topLeft(), bounds.bottomRight())
    tile.setColorAt(0.0, QColor(20, 27, 28))
    tile.setColorAt(1.0, QColor(7, 10, 11))
    painter.setBrush(tile)
    painter.setPen(QPen(QColor(255, 255, 255, 26),
                        max(1.0, tile_size / 256.0)))
    painter.drawRoundedRect(bounds, corner, corner)

    inset = tile_size * 0.06
    paint_brand_mark(painter, bounds.adjusted(inset, inset, -inset, -inset))
    painter.end()
    return pixmap


def make_app_icon(canvas_scale: float = 1.0) -> QIcon:
    """Build a multi-resolution icon for Windows, macOS and system trays."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256, 512, 1024):
        icon.addPixmap(app_icon_pixmap(size, canvas_scale))
    return icon
