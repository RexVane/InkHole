"""
mainwindow.py
=============
墨洞电脑端主界面(QtWidgets)——克制的深色桌面工作台：

  · 无边框窗口 + 自绘标题栏，Win11 使用原生 Acrylic 与系统圆角。
  · 中性深墨表面、结构纹理与青绿/暖金双强调色，保证信息层级和可读性。
  · 中央墨洞使用时间驱动的 60fps 动画，并显示已有传输进度信号。
  · 设备卡片、布尔开关、拖拽反馈和应用内翻页使用短时缓动。

与后端解耦：只依赖 pet.py 传进来的 Bridge 和 ctl 回调字典。
关闭窗口 = 隐藏(托盘/桌宠还在)，只有"退出"才结束进程。
"""

from __future__ import annotations
import os
import re
import sys
import math
import time

from PySide6.QtCore import (Qt, QTimer, QRectF, QPointF, Slot, Signal,
                            QElapsedTimer, QVariantAnimation,
                            QPropertyAnimation, QEasingCurve)
from PySide6.QtGui import (QPainter, QColor, QRadialGradient, QLinearGradient,
                           QPen, QFont)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QFrame, QFileDialog,
                               QGridLayout, QLineEdit, QSpinBox, QCheckBox,
                               QMessageBox, QSizePolicy, QStackedWidget,
                               QSizeGrip, QToolButton, QStyle,
                               QGraphicsOpacityEffect,
                               QApplication)

# ---------- 设计令牌 ----------
_TEAL = "#5AD8C0"
_TEAL_BRIGHT = "#83E8D3"
_TEAL_DIM = "#347D70"
_AMBER = "#E9BD72"
_TEXT = "#F1F4F3"
_TEXT_SECOND = "#B2BFBC"
_TEXT_DIM = "#74817E"
_SURFACE = "rgba(23,28,29,238)"
_SURFACE_RAISED = "rgba(30,36,37,242)"
_EDGE = "rgba(255,255,255,24)"
_EDGE_HOVER = "rgba(90,216,192,118)"
_ERROR = "#F08A7C"

_QSS = f"""
QWidget {{ background: transparent; color: {_TEXT}; font-size: 13px;
           font-family: "Segoe UI Variable Text", "Microsoft YaHei UI", sans-serif;
           letter-spacing: 0px; }}
QLabel {{ background: transparent; }}

QPushButton {{ background: {_SURFACE_RAISED}; color: {_TEXT_SECOND};
               border: 1px solid {_EDGE}; border-radius: 8px;
               min-height: 24px; padding: 7px 14px; }}
QPushButton:hover {{ background: rgba(43,51,52,245); border-color: {_EDGE_HOVER};
                     color: {_TEXT}; }}
QPushButton:pressed {{ background: rgba(15,19,20,250); }}
QPushButton:focus {{ border-color: {_TEAL_DIM}; }}

QPushButton#TitleAction {{ border: 1px solid {_EDGE}; background: rgba(255,255,255,10);
                           border-radius: 7px; min-height: 20px; padding: 5px 12px;
                           color: {_TEXT_SECOND}; font-size: 12px; }}
QPushButton#TitleAction:hover {{ background: rgba(255,255,255,20);
                                color: {_TEXT}; border-color: rgba(255,255,255,35); }}
QToolButton#Win, QToolButton#WinClose {{ border: none; background: transparent;
                                        border-radius: 6px; padding: 0; }}
QToolButton#Win:hover {{ background: rgba(255,255,255,20); }}
QToolButton#WinClose:hover {{ background: rgba(224,74,74,180); }}

QPushButton#Link {{ border: none; background: transparent; color: {_TEAL};
                    font-size: 12px; min-height: 20px; padding: 3px 4px; }}
QPushButton#Link:hover {{ color: {_TEAL_BRIGHT}; }}
QPushButton#Primary {{ color: #09231E; font-weight: 700; border: none;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 #7BE3CE, stop:1 #4FC3AC); min-height: 28px;
        padding: 8px 22px; border-radius: 8px; }}
QPushButton#Primary:hover {{ background: #8BEAD6; color: #061A16; }}
QPushButton#QuietAction {{ background: rgba(255,255,255,9); border-color: transparent;
                           min-height: 20px; padding: 4px 10px; font-size: 12px; }}
QPushButton#QuietAction:hover {{ background: rgba(255,255,255,18);
                                border-color: {_EDGE}; color: {_TEAL_BRIGHT}; }}
QFrame#ModeSegment {{ background: rgba(5,8,9,170); border: 1px solid {_EDGE};
                      border-radius: 7px; }}
QPushButton#ModeOption {{ border: none; border-radius: 5px; background: transparent;
                          min-height: 20px; padding: 5px 14px; font-size: 11px; }}
QPushButton#ModeOption:checked {{ background: rgba(90,216,192,35);
                                  color: {_TEAL_BRIGHT}; }}
QPushButton#ModeOption:disabled {{ color: rgba(178,191,188,75); }}

QFrame#TransferPane {{ background: rgba(19,24,25,225); border: 1px solid {_EDGE};
                       border-radius: 8px; }}
QFrame#SettingsSurface {{ background: rgba(19,24,25,235); border: 1px solid {_EDGE};
                          border-radius: 8px; }}
QFrame#StatusBar {{ background: rgba(255,255,255,8); border: 1px solid rgba(255,255,255,15);
                    border-radius: 7px; }}
QFrame#HLine {{ background: rgba(255,255,255,18); max-height: 1px; border: none; }}
QFrame#VLine {{ background: rgba(255,255,255,18); max-width: 1px; border: none; }}
QLabel#CountBadge, QLabel#MetaBadge {{ color: {_TEXT_DIM}; background: rgba(255,255,255,10);
                                      border: 1px solid rgba(255,255,255,18);
                                      border-radius: 7px; padding: 3px 8px; font-size: 11px; }}
QLabel#SelectedBadge {{ color: {_TEAL_BRIGHT}; background: rgba(90,216,192,18);
                        border: 1px solid rgba(90,216,192,55);
                        border-radius: 6px; padding: 2px 7px; font-size: 10px; }}
QLabel#FileBadge {{ color: {_AMBER}; background: rgba(233,189,114,15);
                    border: 1px solid rgba(233,189,114,45); border-radius: 7px;
                    font-size: 9px; font-weight: 700; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px 0 0 0; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0 0 0 2px; }}
QScrollBar::handle {{ background: rgba(255,255,255,28); border-radius: 4px;
                      min-width: 32px; min-height: 32px; }}
QScrollBar::handle:hover {{ background: rgba(90,216,192,90); }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QLineEdit, QSpinBox, QPlainTextEdit {{ background: rgba(8,11,12,185); border: 1px solid {_EDGE};
                       border-radius: 7px; padding: 9px 11px; color: {_TEXT};
                       min-height: 22px;
                       selection-background-color: {_TEAL_DIM}; }}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border-color: {_EDGE_HOVER};
                                   background: rgba(6,9,10,225); }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; }}

QCheckBox {{ spacing: 9px; color: {_TEXT_SECOND}; padding: 3px 0; }}
QCheckBox:hover {{ color: {_TEXT}; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border: 1px solid rgba(255,255,255,45);
                        border-radius: 6px; background: rgba(4,7,8,180); }}
QCheckBox::indicator:checked {{ background: {_TEAL}; border-color: {_TEAL}; }}
QSizeGrip {{ background: transparent; width: 14px; height: 14px; }}
QToolTip {{ color: {_TEXT}; background: #252B2C; border: 1px solid {_EDGE};
            padding: 5px 7px; }}
"""


# ---------- Windows 原生磨砂 ----------
def _enable_backdrop(hwnd: int) -> bool:
    """Win11 原生 Acrylic backdrop；失败返回 False(调用方回退不透明深色)。

    关键点：Qt 半透明窗口自带 WS_EX_LAYERED，而 DWM backdrop 不作用于
    layered 窗口——表现为"只透明不模糊"。必须先去掉 LAYERED，再用
    DwmExtendFrameIntoClientArea 把 DWM 背景扩满客户区，backdrop 才生效。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        hwnd = int(hwnd)
        u32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi

        # 1. 去掉 WS_EX_LAYERED(0x80000)
        GWL_EXSTYLE = -20
        ex = u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex & ~0x00080000)

        # 2. DWM 背景扩满客户区
        class _Margins(ctypes.Structure):
            _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                        ("t", ctypes.c_int), ("b", ctypes.c_int)]
        dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(_Margins(-1, -1, -1, -1)))

        # 3. 系统圆角(33/ROUND=2) + 深色(20)
        pref, dark = ctypes.c_int(2), ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)

        # 4. Win11 22H2+: DWMWA_SYSTEMBACKDROP_TYPE=38, DWMSBT_TRANSIENTWINDOW(3)=Acrylic
        backdrop = ctypes.c_int(3)
        return dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), 4) == 0
    except Exception:
        return False


def _section_label(text: str) -> QLabel:
    """区块标题。"""
    lbl = QLabel(text)
    font = QFont()
    font.setPointSize(10)
    font.setWeight(QFont.DemiBold)
    lbl.setFont(font)
    lbl.setStyleSheet(f"color:{_TEXT_SECOND};")
    return lbl


def _divider(vertical: bool = False) -> QFrame:
    line = QFrame()
    line.setObjectName("VLine" if vertical else "HLine")
    if vertical:
        line.setFixedWidth(1)
    else:
        line.setFixedHeight(1)
    return line


def mask_manual_host_typing(text: str) -> str:
    """IP 输入的实时分段:边打边自动落点,四段封顶(经典 IP 输入框行为)。

    规则:纯数字/分隔符时生效(主机名不动);全角句号/逗号/空格即落点;
    当前段已 3 位、或再接一位就超过 255 时,自动补点开新段——
    连续输入 1001274626 会实时变成 100.127.46.26。
    """
    if any(c.isalpha() for c in text):
        return text   # 主机名(如 MagicDNS)不做掩码
    parts: list[str] = [""]
    for ch in text:
        if ch in ".。，, ":
            if parts[-1] and len(parts) < 4:
                parts.append("")
        elif ch.isdigit():
            candidate = parts[-1] + ch
            if (len(candidate) > 3 or int(candidate) > 255) and len(parts) < 4:
                parts.append(ch)          # 当前段容不下:落点,新段从这一位开始
            elif len(candidate) <= 3:
                parts[-1] = candidate
            # 第四段已满 3 位后的多余数字直接丢弃(非法输入,添加时还有校验兜底)
    return ".".join(parts)


def normalize_manual_host(raw: str) -> str | None:
    """手动添加设备的地址自动纠正。返回修正后的地址;非法/有歧义返回 None。

    - 含字母按主机名放行(如 Tailscale MagicDNS 名),只去空白;
    - 全角句号/逗号/空格当作分隔符(输入法常见误输);
    - 缺分隔符的数字段尝试拆分:100127.46.26 -> 100.127.46.26。
      只有全局唯一合法拆分才接受——有歧义宁可报错,绝不猜错 IP。
    """
    s = (raw or "").strip()
    if not s:
        return None
    if any(c.isalpha() for c in s):
        host = s.replace(" ", "")
        return host or None
    for sep in ("。", "，", ",", " "):
        s = s.replace(sep, ".")
    s = re.sub(r"\.+", ".", s).strip(".")
    if not s or not re.fullmatch(r"[0-9.]+", s):
        return None
    segments = s.split(".")
    if len(segments) > 4:
        return None

    solutions: list[list[str]] = []

    def piece_ok(piece: str) -> bool:
        if len(piece) > 1 and piece[0] == "0":
            return False   # 前导零(易歧义,真实 IP 不这么写)
        return int(piece) <= 255

    def search(seg_index: int, acc: list[str]):
        if len(solutions) > 1:
            return   # 已确认多解,提前停
        if seg_index == len(segments):
            if len(acc) == 4:
                solutions.append(acc)
            return
        seg = segments[seg_index]

        def cut(pos: int, parts: list[str]):
            if len(solutions) > 1:
                return
            if pos == len(seg):
                search(seg_index + 1, acc + parts)
                return
            for length in (1, 2, 3):
                if pos + length > len(seg):
                    break
                piece = seg[pos:pos + length]
                if not piece_ok(piece):
                    continue
                if len(acc) + len(parts) + 1 > 4:
                    continue
                cut(pos + length, parts + [piece])

        cut(0, [])

    search(0, [])
    return ".".join(solutions[0]) if len(solutions) == 1 else None


class ElidedLabel(QLabel):
    """在可用宽度内省略文本，并通过 tooltip 保留完整内容。"""

    def __init__(self, text: str = "", mode=Qt.ElideRight, parent=None):
        super().__init__(parent)
        self._full_text = text
        self._elide_mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setToolTip(text)

    def set_full_text(self, text: str):
        self._full_text = text
        self.setToolTip(text)
        self._apply_elide()

    def _apply_elide(self):
        width = max(8, self.contentsRect().width())
        QLabel.setText(self, self.fontMetrics().elidedText(
            self._full_text, self._elide_mode, width))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_elide()

    def showEvent(self, e):
        super().showEvent(e)
        self._apply_elide()


class BrandMark(QWidget):
    """标题栏中的小型墨洞标记。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(30, 30)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = QPointF(15, 15)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(6, 9, 10))
        p.drawEllipse(c, 8.5, 8.5)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(90, 216, 192, 210), 2.0, Qt.SolidLine,
                      Qt.RoundCap))
        p.drawArc(QRectF(4, 4, 22, 22), 30 * 16, 205 * 16)
        p.setPen(QPen(QColor(233, 189, 114, 190), 1.5, Qt.SolidLine,
                      Qt.RoundCap))
        p.drawArc(QRectF(7, 7, 16, 16), 215 * 16, 90 * 16)


class InteractiveCard(QFrame):
    """带轻量悬停过渡的设备/文件列表项。"""

    clicked = Signal()

    def __init__(self, selected: bool = False, compact: bool = False,
                 clickable: bool = True, parent=None):
        super().__init__(parent)
        self._selected = selected
        self._clickable = clickable
        self._hover = 0.0
        self.setCursor(Qt.PointingHandCursor if clickable else Qt.ArrowCursor)
        self.setFocusPolicy(Qt.StrongFocus if clickable else Qt.NoFocus)
        self.setMinimumHeight(54 if compact else 64)
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(150)
        self._hover_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._hover_anim.valueChanged.connect(self._set_hover)

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    def _animate_hover(self, target: float):
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(target)
        self._hover_anim.start()

    def enterEvent(self, e):
        self._animate_hover(1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._animate_hover(0.0)
        super().leaveEvent(e)

    def focusInEvent(self, e):
        self.update()
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self.update()
        super().focusOutEvent(e)

    def mouseReleaseEvent(self, e):
        if (self._clickable and e.button() == Qt.LeftButton
                and self.rect().contains(e.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if self._clickable and e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        h = self._hover
        if self._selected:
            base = QColor(23, 48, 44, 238)
            hover = QColor(29, 61, 55, 245)
            edge = QColor(90, 216, 192, int(130 + h * 65))
        else:
            base = QColor(27, 32, 33, 232)
            hover = QColor(38, 45, 46, 242)
            edge = QColor(255, 255, 255, int(22 + h * 30))
        if self.hasFocus():
            edge = QColor(131, 232, 211, 210)
        fill = QColor(
            int(base.red() + (hover.red() - base.red()) * h),
            int(base.green() + (hover.green() - base.green()) * h),
            int(base.blue() + (hover.blue() - base.blue()) * h),
            int(base.alpha() + (hover.alpha() - base.alpha()) * h),
        )
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(fill)
        p.setPen(QPen(edge, 1.0))
        p.drawRoundedRect(rect, 8, 8)
        if self._selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(90, 216, 192, 220))
            p.drawRoundedRect(QRectF(0, 14, 3, max(10, self.height() - 28)), 1.5, 1.5)


class ToggleSwitch(QCheckBox):
    """保留 QCheckBox API 的轻量动画开关。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._position = 1.0 if self.isChecked() else 0.0
        self.setFixedSize(42, 24)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("QCheckBox { padding: 0; background: transparent; }")
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.valueChanged.connect(self._set_position)
        self.toggled.connect(self._animate_toggle)

    def _set_position(self, value):
        self._position = float(value)
        self.update()

    def _animate_toggle(self, checked: bool):
        self._animation.stop()
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        t = self._position
        hover = 1.0 if self.underMouse() else 0.0
        off = QColor(48, 56, 57, 245)
        on = QColor(90, 216, 192, 245)
        track = QColor(
            int(off.red() + (on.red() - off.red()) * t),
            int(off.green() + (on.green() - off.green()) * t),
            int(off.blue() + (on.blue() - off.blue()) * t),
        )
        if hover:
            track = track.lighter(108)
        p.setPen(QPen(QColor(255, 255, 255, int(30 + 40 * t)), 1.0))
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0.5, 0.5, 41, 23), 11.5, 11.5)
        thumb_x = 3.0 + 18.0 * t
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(242, 246, 245))
        p.drawEllipse(QRectF(thumb_x, 3, 18, 18))


class DeviceGlyph(QWidget):
    """不依赖 emoji 字体的桌面/手机线性图标。"""

    def __init__(self, desktop: bool, selected: bool, parent=None):
        super().__init__(parent)
        self._desktop = desktop
        self._selected = selected
        self.setFixedSize(38, 38)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(90, 216, 192, 24) if self._selected
                   else QColor(255, 255, 255, 11))
        p.drawRoundedRect(QRectF(0, 0, 38, 38), 7, 7)
        color = QColor(131, 232, 211) if self._selected else QColor(178, 191, 188)
        pen = QPen(color, 1.7)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if self._desktop:
            p.drawRoundedRect(QRectF(9, 10, 20, 14), 2, 2)
            p.drawLine(QPointF(19, 24), QPointF(19, 28))
            p.drawLine(QPointF(14, 29), QPointF(24, 29))
        else:
            p.drawRoundedRect(QRectF(13, 7, 12, 24), 3, 3)
            p.drawPoint(QPointF(19, 27.5))


# ---------- 中央墨洞动画 ----------
class HoleWidget(QWidget):
    """与 Android 端 InkHoleHero 同一套视觉:墨黑核心径向渐变 + 双层反向
    旋转吸积弧 + 呼吸 + 无设备时雷达波纹 + 传输进度环。

    参数(转速/半径/透明度/线宽)逐项对齐 InkHoleUI.kt,尺寸沿用桌面布局。
    点击 = 选文件发送。
    """

    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self._t = 0.0
        self._searching = True        # 无设备时播放雷达波纹(由主窗口刷新)
        self._progress = 0.0
        self._progress_target = 0.0
        self._progress_kind = "send"
        self._progress_generation = 0
        self.setMinimumSize(270, 270)
        self.setMaximumSize(310, 310)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.PointingHandCursor)
        self._clock = QElapsedTimer()
        self._clock.start()
        timer = QTimer(self)
        timer.timeout.connect(self._tick)
        timer.setTimerType(Qt.PreciseTimer)
        timer.start(16)
        self._timer = timer

    def _tick(self):
        dt = min(0.05, max(0.001, self._clock.restart() / 1000.0))
        self._t += dt
        self._progress += (self._progress_target - self._progress) * min(1.0, dt * 10.0)
        self.update()

    @Slot(bool)
    def set_searching(self, searching: bool):
        if self._searching != searching:
            self._searching = searching
            self.update()

    @Slot(str, int)
    def set_transfer_progress(self, kind: str, percent: int):
        self._progress_kind = kind
        self._progress_target = max(0.0, min(1.0, percent / 100.0))
        self._progress_generation += 1
        generation = self._progress_generation
        if percent >= 100:
            QTimer.singleShot(850, lambda: self._clear_progress(generation))

    def _clear_progress(self, generation: int):
        if generation == self._progress_generation:
            self._progress_target = 0.0

    def showEvent(self, e):
        super().showEvent(e)
        self._clock.restart()
        if not self._timer.isActive():
            self._timer.start(16)

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._on_click:
            self._on_click()
        super().mouseReleaseEvent(e)

    # Android InkHoleHero 的角度体系是顺时针(y 向下),Qt drawArc 是逆时针:
    # Qt 角 = -Compose 角,扫角同理取负。
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        r = min(w, h) / 2.0
        t = self._t

        teal = QColor(0x58, 0xE6, 0xC8)
        teal_soft = QColor(0x7F, 0xEF, 0xD8)
        teal_dim = QColor(0x1E, 0x4A, 0x42)

        # 呼吸 0.55..1.0,周期 5.2s(Compose 2.6s tween 往返)
        breath = 0.775 + 0.225 * math.sin(2 * math.pi * t / 5.2)
        # 双层反向旋转:内层顺时针 360°/46s,外层逆时针 360°/71s
        spin1 = (t * 360.0 / 46.0) % 360.0
        spin2 = -(t * 360.0 / 71.0) % 360.0

        def arc(radius: float, start_deg: float, sweep_deg: float,
                color: QColor, width: float):
            pen = QPen(color, width)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
            p.drawArc(rect, int(-start_deg * 16), int(-sweep_deg * 16))

        def alpha(base: QColor, a: float) -> QColor:
            c = QColor(base)
            c.setAlphaF(max(0.0, min(1.0, a)))
            return c

        # 雷达波纹:无设备时从洞口向外扩散(2.4s 循环)
        if self._searching:
            k = (t % 2.4) / 2.4
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(alpha(teal, (1 - k) * 0.28), r * 2 / 115.0))
            rr = r * (0.62 + 0.36 * k)
            p.drawEllipse(QPointF(cx, cy), rr, rr)

        # 墨洞主体:墨黑核心 -> 暗青过渡 -> 青色光晕 -> 透明
        g = QRadialGradient(cx, cy, r * 0.94)
        g.setColorAt(0.00, QColor(0, 0, 0))
        g.setColorAt(0.42, QColor(0x02, 0x08, 0x07))
        g.setColorAt(0.60, alpha(QColor(0x0A, 0x2A, 0x25), 0.9))
        g.setColorAt(0.76, alpha(teal, 0.40 * breath))
        g.setColorAt(0.90, alpha(QColor(0x1E, 0x50, 0x46), 0.15))
        g.setColorAt(1.00, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), r * 0.94, r * 0.94)

        # 吸积弧·内层(顺时针)——不对称弧才看得出旋转
        inner_r = r * 0.66
        arc(inner_r, 15 + spin1, 105,
            alpha(teal_soft, 0.30 * breath), r * 4 / 115.0)
        arc(inner_r * 0.86, 195 + spin1, 70,
            alpha(teal_soft, 0.15 * breath), r * 2.5 / 115.0)
        # 吸积弧·外层(逆时针,更淡更慢)
        arc(r * 0.84, 60 + spin2, 140,
            alpha(teal_soft, 0.12), r * 2 / 115.0)

        # 传输进度环(与安卓一致:青色,-90° 起,轨道半透明)
        if self._progress > 0.003:
            ring_r = r * 0.97
            ring_w = r * 5 / 115.0
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(alpha(teal_dim, 0.5), ring_w))
            p.drawEllipse(QPointF(cx, cy), ring_r, ring_r)
            pen = QPen(teal, ring_w)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            rect = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            p.drawArc(rect, 90 * 16, int(-360 * 16 * self._progress))


# ---------- 自绘标题栏 ----------
class TitleBar(QWidget):
    """无边框窗口的标题栏：品牌 + 设置/最小化/关闭，可拖动窗口。"""

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self._win = window
        self.setFixedHeight(58)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(22, 10, 12, 8)
        lay.setSpacing(9)

        lay.addWidget(BrandMark())
        brand = QVBoxLayout()
        brand.setSpacing(0)
        t1 = QLabel("墨洞")
        t1.setStyleSheet(f"color:{_TEXT}; font-size:15px; font-weight:700;")
        t2 = QLabel("INKHOLE")
        t2.setStyleSheet(f"color:{_TEXT_DIM}; font-size:9px;")
        brand.addWidget(t1)
        brand.addWidget(t2)
        lay.addLayout(brand)
        lay.addSpacing(10)
        self._device_badge = QLabel()
        self._device_badge.setObjectName("MetaBadge")
        self._device_badge.hide()
        lay.addWidget(self._device_badge)
        lay.addStretch(1)

        b_settings = QPushButton("设置")
        b_settings.setObjectName("TitleAction")
        b_settings.setToolTip("打开设置")
        b_settings.setCursor(Qt.PointingHandCursor)
        b_settings.clicked.connect(window._open_settings)
        lay.addWidget(b_settings)

        b_min = QToolButton()
        b_min.setObjectName("Win")
        b_min.setIcon(self.style().standardIcon(QStyle.SP_TitleBarMinButton))
        b_min.setToolTip("最小化")
        b_min.setFixedSize(36, 32)
        b_min.setCursor(Qt.PointingHandCursor)
        b_min.clicked.connect(window.showMinimized)
        b_close = QToolButton()
        b_close.setObjectName("WinClose")
        b_close.setIcon(self.style().standardIcon(QStyle.SP_TitleBarCloseButton))
        b_close.setToolTip("隐藏到托盘")
        b_close.setFixedSize(36, 32)
        b_close.setCursor(Qt.PointingHandCursor)
        b_close.clicked.connect(window.hide)
        for b in (b_min, b_close):
            lay.addWidget(b)

    def set_device_count(self, count: int):
        self._device_badge.setVisible(count > 0)
        if count:
            self._device_badge.setText(f"{count} 台设备")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._win.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, e):   # 双击标题栏切换最大化
        w = self._win
        w.showNormal() if w.isMaximized() else w.showMaximized()


# ---------- 主窗口 ----------
class MainWindow(QWidget):
    """墨洞主窗口：无边框双栏工作台 + 应用内设置页。"""

    def __init__(self, bridge, ctl: dict, icon=None):
        super().__init__()
        self._bridge = bridge
        self._ctl = ctl
        self.setWindowTitle("墨洞 InkHole")
        if icon:
            self.setWindowIcon(icon)
        self.resize(960, 640)
        self.setMinimumSize(800, 580)
        self.setStyleSheet(_QSS)
        self.setAcceptDrops(True)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._backdrop_ok = False
        self._backdrop_tried = False
        self._page_animation = None
        self._drag_level = 0.0
        self._drag_animation = QVariantAnimation(self)
        self._drag_animation.setDuration(170)
        self._drag_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._drag_animation.valueChanged.connect(self._set_drag_level)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._titlebar = TitleBar(self)
        outer.addWidget(self._titlebar)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_home_page())      # 0
        self._stack.addWidget(self._build_settings_page())  # 1: 设置
        outer.addWidget(self._stack, 1)
        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(16, 16)
        self._grip.move(self.width() - self._grip.width() - 2,
                        self.height() - self._grip.height() - 2)
        self._grip.raise_()

        bridge.peersChanged.connect(self._refresh_peers)
        bridge.status.connect(self._on_status)
        bridge.errorState.connect(self._on_error)
        bridge.emit_out.connect(lambda _n: self._refresh_recent())
        if hasattr(bridge, "progress"):
            bridge.progress.connect(self._hole.set_transfer_progress)
        self._refresh_peers()
        self._refresh_recent()

    # ---- 中性深色背景 + 轻微结构纹理 ----
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        base_alpha = 232 if self._backdrop_ok else 255
        base = QLinearGradient(0, 0, 0, h)
        base.setColorAt(0.0, QColor(16, 21, 22, base_alpha))
        base.setColorAt(0.48, QColor(11, 15, 16, base_alpha))
        base.setColorAt(1.0, QColor(8, 11, 12, base_alpha))
        p.fillRect(self.rect(), base)
        side_light = QLinearGradient(0, 0, w, 0)
        side_light.setColorAt(0.0, QColor(90, 216, 192, 18))
        side_light.setColorAt(0.36, QColor(90, 216, 192, 3))
        side_light.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), side_light)

        p.setPen(QPen(QColor(255, 255, 255, 5), 1.0))
        for x in range(-h, w, 52):
            p.drawLine(x, 58, x + h, h)
        p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
        p.drawLine(0, 0, w, 0)

        if self._drag_level > 0.001:
            alpha = int(28 * self._drag_level)
            edge_alpha = int(185 * self._drag_level)
            p.setBrush(QColor(90, 216, 192, alpha))
            p.setPen(QPen(QColor(90, 216, 192, edge_alpha), 2.0))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(8, 8, -8, -8), 10, 10)

    # ================= 主页 =================
    def _build_home_page(self) -> QWidget:
        page = QWidget()
        body = QHBoxLayout(page)
        body.setContentsMargins(30, 10, 30, 24)
        body.setSpacing(24)

        # 左：稳定的发送工作区
        transfer = QFrame()
        transfer.setObjectName("TransferPane")
        left = QVBoxLayout(transfer)
        left.setContentsMargins(24, 22, 24, 18)
        left.setSpacing(8)
        left.addWidget(_section_label("发送目标"))
        self._state_lbl = ElidedLabel("正在发现设备")
        self._state_lbl.setFixedHeight(46)
        self._state_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._state_lbl.setStyleSheet(
            f"color:{_TEXT_SECOND}; font-size:18px; font-weight:650;")
        left.addWidget(self._state_lbl)

        self._hole = HoleWidget(self._pick_and_send)
        left.addWidget(self._hole, 1, Qt.AlignCenter)

        send_row = QHBoxLayout()
        send_row.addStretch(1)
        self._send_btn = QPushButton("选择文件")
        self._send_btn.setObjectName("Primary")
        self._send_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self._send_btn.setCursor(Qt.PointingHandCursor)
        self._send_btn.setFixedWidth(156)
        self._send_btn.clicked.connect(self._pick_and_send)
        send_row.addWidget(self._send_btn)
        send_row.addStretch(1)
        left.addLayout(send_row)

        self._status_bar = QFrame()
        self._status_bar.setObjectName("StatusBar")
        status_lay = QHBoxLayout(self._status_bar)
        status_lay.setContentsMargins(10, 7, 12, 7)
        status_lay.setSpacing(9)
        self._status_mark = QFrame()
        self._status_mark.setFixedSize(3, 18)
        self._status_mark.setStyleSheet(
            f"background:{_TEAL_DIM}; border-radius:1px;")
        self._status_lbl = ElidedLabel("等待操作")
        self._status_lbl.setStyleSheet(f"color:{_TEXT_DIM}; font-size:11px;")
        status_lay.addWidget(self._status_mark)
        status_lay.addWidget(self._status_lbl, 1)
        self._status_effect = QGraphicsOpacityEffect(self._status_bar)
        self._status_effect.setOpacity(1.0)
        self._status_bar.setGraphicsEffect(self._status_effect)
        self._status_animation = QPropertyAnimation(
            self._status_effect, b"opacity", self)
        self._status_animation.setDuration(180)
        self._status_animation.setEasingCurve(QEasingCurve.OutCubic)
        left.addWidget(self._status_bar)
        body.addWidget(transfer, 10)

        body.addWidget(_divider(vertical=True))

        # 右：设备与最近接收，使用信息栏而非嵌套面板
        side = QWidget()
        side.setMinimumWidth(330)
        right = QVBoxLayout(side)
        right.setContentsMargins(0, 2, 0, 0)
        right.setSpacing(12)

        device_bar = QHBoxLayout()
        self._device_section = _section_label("设备")
        device_bar.addWidget(self._device_section)
        device_bar.addStretch(1)
        self._peer_count_lbl = QLabel("0")
        self._peer_count_lbl.setObjectName("CountBadge")
        device_bar.addWidget(self._peer_count_lbl)
        right.addLayout(device_bar)
        self._chip_area = QScrollArea()
        self._chip_area.setWidgetResizable(True)
        self._chip_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._chip_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._chip_box = QWidget()
        self._chip_lay = QVBoxLayout(self._chip_box)
        self._chip_lay.setContentsMargins(0, 0, 6, 0)
        self._chip_lay.setSpacing(9)
        self._chip_area.setWidget(self._chip_box)
        right.addWidget(self._chip_area, 5)
        right.addWidget(_divider())

        rec_bar = QHBoxLayout()
        rec_bar.addWidget(_section_label("最近接收"))
        rec_bar.addStretch(1)
        self._recent_count_lbl = QLabel("0")
        self._recent_count_lbl.setObjectName("CountBadge")
        rec_bar.addWidget(self._recent_count_lbl)
        b_inbox = QToolButton()
        b_inbox.setObjectName("Win")
        b_inbox.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        b_inbox.setFixedSize(30, 28)
        b_inbox.setToolTip("打开收件箱")
        b_inbox.setCursor(Qt.PointingHandCursor)
        b_inbox.clicked.connect(self._bridge.openInbox)
        rec_bar.addWidget(b_inbox)
        right.addLayout(rec_bar)

        self._recent_area = QScrollArea()
        self._recent_area.setWidgetResizable(True)
        self._recent_box = QWidget()
        self._recent_lay = QVBoxLayout(self._recent_box)
        self._recent_lay.setContentsMargins(0, 0, 6, 0)
        self._recent_lay.setSpacing(8)
        self._recent_area.setWidget(self._recent_box)
        right.addWidget(self._recent_area, 4)

        body.addWidget(side, 11)
        return page

    # ================= 设置页 =================
    def _build_settings_header(self) -> QHBoxLayout:
        top = QHBoxLayout()
        back = QPushButton("返回")
        back.setObjectName("Link")
        back.setIcon(self.style().standardIcon(QStyle.SP_ArrowBack))
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(lambda: self._show_page(0))
        title = QLabel("设置")
        title.setStyleSheet(f"color:{_TEXT}; font-size:19px; font-weight:700;")
        top.addWidget(back)
        top.addSpacing(10)
        top.addWidget(title)
        top.addStretch(1)
        return top

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(30, 10, 30, 24)
        outer.setSpacing(14)
        outer.addLayout(self._build_settings_header())

        panel = QFrame()
        panel.setObjectName("SettingsSurface")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(18)

        columns = QHBoxLayout()
        columns.setSpacing(26)

        connection = QVBoxLayout()
        connection.setSpacing(13)
        connection.addWidget(_section_label("局域网配置"))

        fields = QGridLayout()
        fields.setHorizontalSpacing(14)
        fields.setVerticalSpacing(12)
        fields.setColumnStretch(0, 3)
        fields.setColumnStretch(1, 2)

        self._ed_name = QLineEdit()
        self._ed_name.setPlaceholderText("设备名称")
        self._ed_secret = QLineEdit()
        self._ed_secret.setEchoMode(QLineEdit.Password)
        self._ed_secret.setPlaceholderText("留空时不加密")
        cb_show = QCheckBox("显示")
        cb_show.toggled.connect(lambda on: self._ed_secret.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        self._sp_port = QSpinBox()
        self._sp_port.setRange(0, 65535)
        self._sp_port.setToolTip("0 = 系统自动分配")
        self._ed_inbox = QLineEdit()
        self._ed_inbox.setReadOnly(True)
        b_browse = QPushButton("浏览")
        b_browse.setCursor(Qt.PointingHandCursor)
        b_browse.clicked.connect(self._choose_inbox)

        def _field(title: str, control: QWidget) -> QWidget:
            box = QWidget()
            box_lay = QVBoxLayout(box)
            box_lay.setContentsMargins(0, 0, 0, 0)
            box_lay.setSpacing(6)
            label = QLabel(title)
            label.setStyleSheet(f"color:{_TEXT_DIM}; font-size:11px;")
            box_lay.addWidget(label)
            box_lay.addWidget(control)
            return box

        fields.addWidget(_field("设备名称", self._ed_name), 0, 0)
        fields.addWidget(_field("监听端口", self._sp_port), 0, 1)

        secret_row = QWidget()
        secret_lay = QHBoxLayout(secret_row)
        secret_lay.setContentsMargins(0, 0, 0, 0)
        secret_lay.setSpacing(10)
        secret_lay.addWidget(self._ed_secret, 1)
        secret_lay.addWidget(cb_show)
        fields.addWidget(_field("加密口令", secret_row), 1, 0, 1, 2)

        inbox_row = QWidget()
        inbox_lay = QHBoxLayout(inbox_row)
        inbox_lay.setContentsMargins(0, 0, 0, 0)
        inbox_lay.setSpacing(8)
        inbox_lay.addWidget(self._ed_inbox, 1)
        inbox_lay.addWidget(b_browse)
        fields.addWidget(_field("收件箱", inbox_row), 2, 0, 1, 2)
        connection.addLayout(fields)

        # ---- 跨网络配置(自动发现不可用时的直连入口) ----
        connection.addSpacing(10)
        connection.addWidget(_divider())
        connection.addSpacing(6)
        connection.addWidget(_section_label("跨网络配置（手动添加设备）"))
        manual_hint = QLabel("自动发现找不到对方时，填对方 IP 与墨洞监听端口直连"
                             "（对方需在设置里固定监听端口）。跨网络传输：两台电脑"
                             "安装 Tailscale 并登录同一账号，填对方的 Tailscale IP 即可。")
        manual_hint.setWordWrap(True)
        manual_hint.setStyleSheet(f"color:{_TEXT_DIM}; font-size:10.5px;")
        connection.addWidget(manual_hint)

        manual_row = QWidget()
        manual_lay = QHBoxLayout(manual_row)
        manual_lay.setContentsMargins(0, 2, 0, 0)
        manual_lay.setSpacing(8)
        self._manual_host = QLineEdit()
        self._manual_host.setPlaceholderText("对方 IP，如 192.168.1.23")
        self._manual_host.textEdited.connect(self._mask_manual_host)
        self._manual_port = QSpinBox()
        self._manual_port.setRange(1, 65535)
        self._manual_port.setValue(52130)
        self._manual_port.setFixedWidth(86)
        b_manual_add = QPushButton("添加")
        b_manual_add.setCursor(Qt.PointingHandCursor)
        b_manual_add.clicked.connect(self._add_manual_peer)
        manual_lay.addWidget(self._manual_host, 1)
        manual_lay.addWidget(self._manual_port)
        manual_lay.addWidget(b_manual_add)
        connection.addWidget(manual_row)

        manual_box = QWidget()
        self._manual_list_lay = QVBoxLayout(manual_box)
        self._manual_list_lay.setContentsMargins(0, 4, 0, 0)
        self._manual_list_lay.setSpacing(4)
        connection.addWidget(manual_box)

        connection.addStretch(1)
        columns.addLayout(connection, 3)
        columns.addWidget(_divider(vertical=True))

        behavior = QVBoxLayout()
        behavior.setSpacing(0)
        behavior.addWidget(_section_label("应用行为"))
        behavior.addSpacing(10)

        def _toggle_row(title_text: str, detail: str):
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(0, 8, 0, 8)
            row_lay.setSpacing(12)
            copy = QVBoxLayout()
            copy.setSpacing(2)
            title_lbl = QLabel(title_text)
            title_lbl.setStyleSheet(f"color:{_TEXT}; font-size:12.5px;")
            detail_lbl = QLabel(detail)
            detail_lbl.setWordWrap(True)
            detail_lbl.setStyleSheet(f"color:{_TEXT_DIM}; font-size:10.5px;")
            copy.addWidget(title_lbl)
            copy.addWidget(detail_lbl)
            checkbox = ToggleSwitch()
            checkbox.setAccessibleName(title_text)
            row_lay.addLayout(copy, 1)
            row_lay.addWidget(checkbox, 0, Qt.AlignVCenter)
            return row, checkbox

        trusted_row, self._cb_trusted = _toggle_row(
            "仅接收目标设备", "拒绝其他局域网设备发送的文件")
        pet_row, self._cb_pet = _toggle_row(
            "桌宠挂件", "在桌面保留可拖放的墨洞挂件")
        auto_row, self._cb_auto = _toggle_row(
            "开机自启", "登录 Windows 后自动启动墨洞")
        for index, row in enumerate((trusted_row, pet_row, auto_row)):
            behavior.addWidget(row)
            if index < 2:
                behavior.addWidget(_divider())
        behavior.addStretch(1)
        columns.addLayout(behavior, 2)
        # 内容装进滚动区:窗口高度不足时出滚动条,而不是把输入框压扁裁切
        content = QWidget()
        content.setLayout(columns)
        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content_scroll.setFrameShape(QFrame.NoFrame)
        content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content_scroll.setWidget(content)
        lay.addWidget(content_scroll, 1)
        lay.addWidget(_divider())

        btns = QHBoxLayout()
        self._local_lbl = ElidedLabel(mode=Qt.ElideMiddle)
        self._local_lbl.setStyleSheet(f"color:{_TEXT_DIM}; font-size:11px;")
        btns.addWidget(self._local_lbl)
        btns.addStretch(1)
        b_save = QPushButton("保存")
        b_save.setObjectName("Primary")
        b_save.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        b_save.setFixedWidth(120)
        b_save.setCursor(Qt.PointingHandCursor)
        b_save.clicked.connect(self._save_settings)
        btns.addWidget(b_save)
        lay.addLayout(btns)

        outer.addWidget(panel, 1)
        return page

    def _show_page(self, index: int):
        if index == self._stack.currentIndex():
            return
        if self._page_animation is not None:
            self._page_animation.stop()
        # 上一次动画若被打断,旧页面会残留半透明 effect,这里先清掉
        previous = getattr(self, "_animated_page", None)
        if previous is not None and previous.graphicsEffect() is not None:
            previous.setGraphicsEffect(None)
        page = self._stack.widget(index)
        self._animated_page = page
        self._stack.setCurrentIndex(index)
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(170)
        animation.setStartValue(0.18)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(
            lambda: page.setGraphicsEffect(None)
            if page.graphicsEffect() is effect else None)
        self._page_animation = animation
        animation.start()

    def _open_settings(self):
        if self._stack.currentIndex() == 1:
            return   # 已在本页:不重填表单,避免吃掉未保存的编辑
        cfg = self._bridge.lanConfig()
        self._ed_name.setText(cfg.peer_name)
        self._ed_secret.setText(cfg.secret)
        self._sp_port.setValue(cfg.listen_port)
        self._ed_inbox.setText(os.path.abspath(cfg.inbox))
        self._cb_trusted.setChecked(cfg.trusted_only)
        self._cb_pet.setChecked(bool(self._ctl["pet_visible"]()))
        self._cb_auto.setChecked(bool(self._ctl["is_autostart"]()))
        ips = " / ".join(self._bridge.localAddresses()) or "?"
        self._local_lbl.set_full_text(
            f"本机：{cfg.peer_name}-{cfg.instance_id} · 端口 {cfg.listen_port or '自动'}"
            f" · IP {ips}（对方手动添加本机时填这里的 IP 和端口）")
        self._refresh_manual_list()
        self._show_page(1)

    # ---- 手动设备(Tailscale/固定 IP 直连) ----
    def _refresh_manual_list(self):
        while self._manual_list_lay.count():
            it = self._manual_list_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for entry in self._bridge.manualPeers():
            host, port = str(entry.get("host")), int(entry.get("port"))
            name = str(entry.get("name") or "")
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(2, 0, 0, 0)
            row_lay.setSpacing(8)
            text = f"{name}  ·  {host}:{port}" if name else f"{host}:{port}"
            lbl = ElidedLabel(text, Qt.ElideMiddle)
            lbl.setStyleSheet(f"color:{_TEXT_SECOND}; font-size:11.5px;")
            b_del = QPushButton("移除")
            b_del.setObjectName("Link")
            b_del.setCursor(Qt.PointingHandCursor)
            b_del.clicked.connect(
                lambda checked=False, h=host, p=port: self._remove_manual_peer(h, p))
            row_lay.addWidget(lbl, 1)
            row_lay.addWidget(b_del)
            self._manual_list_lay.addWidget(row)

    def _mask_manual_host(self, text: str):
        """输入时实时分段(仅光标在末尾时,不打扰中间修改)。"""
        if self._manual_host.cursorPosition() != len(text):
            return
        masked = mask_manual_host_typing(text)
        if masked != text:
            self._manual_host.setText(masked)     # textEdited 不会因 setText 重入
            self._manual_host.setCursorPosition(len(masked))

    def _add_manual_peer(self):
        raw = self._manual_host.text()
        if not raw.strip():
            return
        host = normalize_manual_host(raw)
        if host is None:
            QMessageBox.warning(self, "手动设备",
                                "IP 无效或有歧义，请检查后重新输入")
            return
        if host != raw.strip():
            self._manual_host.setText(host)   # 回显修正结果,用户可见实际用的地址
            self._on_status(f"已自动修正为 {host}")
        if self._bridge.addManualPeer("", host, self._manual_port.value()):
            self._manual_host.clear()
            self._refresh_manual_list()
            self._refresh_peers()
        else:
            QMessageBox.warning(self, "手动设备", "地址无效，请检查 IP 与端口")

    def _remove_manual_peer(self, host: str, port: int):
        self._bridge.removeManualPeer(host, port)
        self._refresh_manual_list()
        self._refresh_peers()

    def _choose_inbox(self):
        d = QFileDialog.getExistingDirectory(self, "选择收件箱目录",
                                             self._ed_inbox.text())
        if d:
            self._ed_inbox.setText(d)

    def _save_settings(self):
        name = self._ed_name.text().strip()
        if not name:
            QMessageBox.warning(self, "墨洞", "设备名称不能为空")
            return
        inbox = self._ed_inbox.text()
        if inbox:
            self._bridge.setInbox(inbox)   # 单独持久化:即使名字/端口没变也要落盘
        if self._cb_trusted.isChecked() != self._bridge.lanConfig().trusted_only:
            self._bridge.toggleTrustedOnly()
        self._ctl["set_pet_visible"](self._cb_pet.isChecked())
        if self._cb_auto.isChecked() != bool(self._ctl["is_autostart"]()):
            self._ctl["set_autostart"](self._cb_auto.isChecked())
        self._ctl["apply_settings"](name, self._ed_secret.text(),
                                    self._sp_port.value())
        self._show_page(0)
        self._refresh_peers()

    # ================= 设备卡片 =================
    @Slot()
    def _refresh_peers(self):
        while self._chip_lay.count():
            it = self._chip_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        node = self._bridge.node
        peers = node.peers()
        selected = node.selected_peer()
        self._peer_count_lbl.setText(str(len(peers)))
        self._titlebar.set_device_count(len(peers))
        self._hole.set_searching(len(peers) == 0)   # 无设备时墨洞播放雷达波纹

        if not peers:
            empty = QLabel("正在发现设备 · 跨网设备可在设置中手动添加")
            empty.setAlignment(Qt.AlignCenter)
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"color:{_TEXT_DIM}; padding: 22px 4px; font-size:12px;")
            self._chip_lay.addWidget(empty)
        for p in peers:
            self._chip_lay.addWidget(self._device_card(p, p.name == selected))
        self._chip_lay.addStretch(1)
        self._update_state_text()

    def _device_card(self, peer, selected: bool) -> QFrame:
        card = InteractiveCard(selected=selected)
        card.setAccessibleName(f"设备 {peer.name}")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)

        desktop = any(k in peer.name.upper() for k in
                      ("PC", "DESKTOP", "MAC", "BOOK", "WIN", "LAPTOP"))
        icon = DeviceGlyph(desktop, selected)
        col = QVBoxLayout()
        col.setSpacing(3)
        name = ElidedLabel(peer.name)
        name.setStyleSheet(
            f"color:{_TEAL_BRIGHT if selected else _TEXT};"
            " font-size:13px; font-weight:650;")
        host = QLabel(peer.host)
        host.setStyleSheet(f"color:{_TEXT_DIM}; font-size:11px;")
        col.addWidget(name)
        col.addWidget(host)
        lay.addWidget(icon)
        lay.addLayout(col, 1)
        if selected:
            state = QLabel("已选择")
            state.setObjectName("SelectedBadge")
            lay.addWidget(state)

        def _click(n=peer.name):
            node = self._bridge.node
            node.select_peer(None if node.selected_peer() == n else n)
            self._refresh_peers()
        card.clicked.connect(_click)
        return card

    def _update_state_text(self):
        node = self._bridge.node
        selected = node.selected_peer()
        n = len(node.peers())
        if selected:
            self._state_lbl.setStyleSheet(
                f"color:{_TEAL_BRIGHT}; font-size:18px; font-weight:650;")
            self._state_lbl.set_full_text(f"发送至  {selected}")
        elif n:
            self._state_lbl.setStyleSheet(
                f"color:{_TEXT_SECOND}; font-size:18px; font-weight:650;")
            self._state_lbl.set_full_text(f"{n} 台设备可用")
        else:
            self._state_lbl.setStyleSheet(
                f"color:{_TEXT_DIM}; font-size:18px; font-weight:650;")
            self._state_lbl.set_full_text("正在发现设备")

    # ================= 最近接收 =================
    def _refresh_recent(self):
        while self._recent_lay.count():
            it = self._recent_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        recents = self._bridge.recentFiles()
        self._recent_count_lbl.setText(str(len(recents)))
        if not recents:
            empty = QLabel("暂无接收记录")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                f"color:{_TEXT_DIM}; padding: 22px 4px; font-size:12px;")
            self._recent_lay.addWidget(empty)
        for path in recents:
            self._recent_lay.addWidget(self._file_card(path))
        self._recent_lay.addStretch(1)

    def _file_card(self, path: str) -> QFrame:
        card = InteractiveCard(compact=True, clickable=False)
        card.setAccessibleName(f"打开文件 {os.path.basename(path)}")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(11, 8, 10, 8)
        lay.setSpacing(11)
        suffix = os.path.splitext(path)[1].lstrip(".").upper()[:3] or "FILE"
        icon = QLabel(suffix)
        icon.setObjectName("FileBadge")
        icon.setFixedSize(38, 36)
        icon.setAlignment(Qt.AlignCenter)
        name = ElidedLabel(os.path.basename(path), Qt.ElideMiddle)
        name.setStyleSheet(f"color:{_TEXT}; font-size:12px;")
        name.setToolTip(path)
        b_open = QPushButton("打开")
        b_open.setObjectName("QuietAction")
        b_open.setFixedWidth(52)
        b_open.setCursor(Qt.PointingHandCursor)
        b_open.clicked.connect(lambda checked=False, p=path: self._bridge.openPath(p))
        lay.addWidget(icon)
        lay.addWidget(name, 1)
        lay.addWidget(b_open)
        return card

    # ================= 发送 / 状态 =================
    def _pick_and_send(self):
        if not self._bridge.node.selected_peer():
            self._on_error("先在右侧点击卡片选择目标设备")
            return
        files, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件")
        for fp in files:
            self._bridge.dropFile(fp)

    @Slot(str)
    def _on_status(self, msg: str):
        self._status_mark.setStyleSheet(
            f"background:{_TEAL_DIM}; border-radius:1px;")
        self._status_lbl.setStyleSheet(f"color:{_TEXT_SECOND}; font-size:11px;")
        self._status_lbl.set_full_text(msg or "等待操作")
        self._animate_status()

    @Slot(str)
    def _on_error(self, msg: str):
        if msg:
            self._status_mark.setStyleSheet(
                f"background:{_ERROR}; border-radius:1px;")
            self._status_lbl.setStyleSheet(f"color:{_ERROR}; font-size:11px;")
            self._status_lbl.set_full_text(msg)
            self._animate_status()

    def _animate_status(self):
        self._status_animation.stop()
        self._status_animation.setStartValue(0.48)
        self._status_animation.setEndValue(1.0)
        self._status_animation.start()

    # ================= 拖拽发送 =================
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._animate_drag(1.0)

    def dragLeaveEvent(self, e):
        self._animate_drag(0.0)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._animate_drag(0.0)
        for url in e.mimeData().urls():
            self._bridge.dropFile(url.toString())
        e.acceptProposedAction()

    def _set_drag_level(self, value):
        self._drag_level = float(value)
        self.update()

    def _animate_drag(self, target: float):
        self._drag_animation.stop()
        self._drag_animation.setStartValue(self._drag_level)
        self._drag_animation.setEndValue(target)
        self._drag_animation.start()

    # ================= 磨砂 & 关闭 =================
    def resizeEvent(self, e):
        super().resizeEvent(e)
        grip = getattr(self, "_grip", None)
        if grip is not None:
            grip.move(self.width() - grip.width() - 2,
                      self.height() - grip.height() - 2)
            grip.raise_()

    def showEvent(self, e):
        super().showEvent(e)
        if not self._backdrop_tried:
            self._backdrop_tried = True
            self._backdrop_ok = _enable_backdrop(self.winId())
            self.update()   # base_alpha 依据磨砂是否可用而不同

    def closeEvent(self, e):
        e.ignore()
        self.hide()
