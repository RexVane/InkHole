"""
mainwindow.py
=============
墨洞电脑端主界面(QtWidgets)——深色玻璃拟态(Dark Glassmorphism)：

  · 无边框窗口 + 自绘标题栏(拖动/最小化/关闭)，Win11 原生 Acrylic 磨砂
    背景(DwmSetWindowAttribute backdrop=3) + 系统圆角；Win10 回退
    SetWindowCompositionAttribute Acrylic；其他系统回退纯深色。
  · 自绘环境光背景：深墨底 + 两团青色辉光，让半透明玻璃面板"背后有东西"。
  · 玻璃面板配方：半透明深色填充 + 1px 低透明度亮边 + 顶部微渐变反光。
  · 中央墨洞动画(呼吸光晕/旋转弧线/雷达涟漪/吸积微粒)，点击选文件发送。
  · 设置是应用内翻页(QStackedWidget)，不弹新窗口。

与后端解耦：只依赖 pet.py 传进来的 Bridge 和 ctl 回调字典。
关闭窗口 = 隐藏(托盘/桌宠还在)，只有"退出"才结束进程。
"""

from __future__ import annotations
import os
import sys
import math
import random

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, Slot
from PySide6.QtGui import (QPainter, QColor, QRadialGradient, QLinearGradient,
                           QPen, QConicalGradient, QFont)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QFrame, QFileDialog,
                               QFormLayout, QLineEdit, QSpinBox, QCheckBox,
                               QMessageBox, QSizePolicy, QStackedWidget,
                               QSizeGrip)

# ---------- 设计令牌(与安卓端同一色相，统一取用) ----------
_TEAL = "#58E6C8"
_TEAL_DIM = "#2E7A6A"
_TEXT = "#ECF4F2"
_TEXT_SECOND = "#9FB4B1"
_TEXT_DIM = "#5C706E"
# 玻璃面板配方：填充/亮边/悬停亮边 全局各一个令牌，不逐卡片即兴
_GLASS_FILL = ("qlineargradient(x1:0,y1:0,x2:0,y2:1,"
               " stop:0 rgba(255,255,255,7%), stop:1 rgba(255,255,255,3%))")
_GLASS_FILL_HOVER = ("qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                     " stop:0 rgba(255,255,255,11%), stop:1 rgba(255,255,255,5%))")
_GLASS_SEL = ("qlineargradient(x1:0,y1:0,x2:0,y2:1,"
              " stop:0 rgba(88,230,200,16%), stop:1 rgba(88,230,200,6%))")
_EDGE = "rgba(255,255,255,9%)"
_EDGE_HOVER = "rgba(88,230,200,45%)"

_QSS = f"""
QWidget {{ background: transparent; color: {_TEXT}; font-size: 13px;
           font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif; }}
QLabel {{ background: transparent; }}

QPushButton {{ background: {_GLASS_FILL}; color: {_TEXT_SECOND};
               border: 1px solid {_EDGE}; border-radius: 9px; padding: 7px 14px; }}
QPushButton:hover {{ background: {_GLASS_FILL_HOVER}; border-color: {_EDGE_HOVER};
                     color: {_TEAL}; }}
QPushButton:pressed {{ background: rgba(0,0,0,18%); }}

QPushButton#Win {{ border: none; background: transparent; border-radius: 7px;
                   color: {_TEXT_DIM}; font-size: 13px; padding: 5px 13px; }}
QPushButton#Win:hover {{ background: rgba(255,255,255,9%); color: {_TEXT}; }}
QPushButton#WinClose {{ border: none; background: transparent; border-radius: 7px;
                        color: {_TEXT_DIM}; font-size: 13px; padding: 5px 13px; }}
QPushButton#WinClose:hover {{ background: rgba(232,84,84,75%); color: white; }}

QPushButton#Link {{ border: none; background: transparent; color: {_TEAL};
                    font-size: 12px; padding: 2px 4px; }}
QPushButton#Link:hover {{ color: #8CF4DC; }}
QPushButton#Primary {{ color: #06231C; font-weight: bold; border: none;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 #63EFD2, stop:1 #3EC9AC); padding: 9px 30px; border-radius: 10px; }}
QPushButton#Primary:hover {{ background: #7DF5DC; }}

QFrame#Panel {{ background: rgba(10,18,18,42%);
                border: 1px solid {_EDGE}; border-radius: 16px; }}
QFrame#Card {{ background: {_GLASS_FILL}; border: 1px solid {_EDGE};
               border-radius: 12px; }}
QFrame#Card:hover {{ background: {_GLASS_FILL_HOVER}; border-color: {_EDGE_HOVER}; }}
QFrame#Card[sel="true"] {{ background: {_GLASS_SEL};
                           border: 1px solid {_EDGE_HOVER}; }}
QFrame#HLine {{ background: rgba(255,255,255,6%); max-height: 1px; border: none; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 5px; margin: 0; }}
QScrollBar:vertical {{ background: transparent; width: 5px; margin: 0; }}
QScrollBar::handle {{ background: rgba(255,255,255,12%); border-radius: 2px;
                      min-width: 28px; min-height: 28px; }}
QScrollBar::handle:hover {{ background: rgba(88,230,200,45%); }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QLineEdit, QSpinBox {{ background: rgba(0,0,0,26%); border: 1px solid {_EDGE};
                       border-radius: 9px; padding: 8px 11px; color: {_TEXT};
                       selection-background-color: {_TEAL_DIM}; }}
QLineEdit:focus, QSpinBox:focus {{ border-color: {_EDGE_HOVER};
                                   background: rgba(0,0,0,36%); }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; }}

QCheckBox {{ spacing: 9px; color: {_TEXT_SECOND}; padding: 3px 0; }}
QCheckBox:hover {{ color: {_TEXT}; }}
QCheckBox::indicator {{ width: 17px; height: 17px; border: 1px solid {_EDGE_HOVER};
                        border-radius: 5px; background: rgba(0,0,0,25%); }}
QCheckBox::indicator:checked {{ background: {_TEAL}; border-color: {_TEAL}; }}
QSizeGrip {{ background: transparent; width: 14px; height: 14px; }}
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
    """区块小标题：小号、次级色、加字距(玻璃面板上的排版层级)。"""
    lbl = QLabel(text)
    font = QFont()
    font.setPointSize(9)
    font.setLetterSpacing(QFont.AbsoluteSpacing, 2.0)
    font.setBold(True)
    lbl.setFont(font)
    lbl.setStyleSheet(f"color:{_TEXT_DIM};")
    return lbl


# ---------- 中央墨洞动画 ----------
class HoleWidget(QWidget):
    """深黑核心 + 呼吸光晕 + 三条旋转弧线 + 雷达涟漪 + 吸积微粒。

    鼠标悬停整体提速加亮；点击 = 选文件发送。
    """

    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self._t = 0.0
        self._hover = 0.0
        self._a = [0.0, 120.0, 245.0]
        rnd = random.Random(42)
        self._parts = [(rnd.uniform(0, 360), rnd.uniform(0.55, 1.15),
                        rnd.uniform(0.4, 1.2), rnd.uniform(1.2, 2.6))
                       for _ in range(14)]
        self.setMinimumSize(230, 230)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        timer = QTimer(self)
        timer.timeout.connect(self._tick)
        timer.start(30)
        self._timer = timer

    def _tick(self):
        speed = 1.0 + self._hover * 0.9
        self._t += 0.030 * speed
        self._a[0] = (self._a[0] + 0.30 * speed) % 360
        self._a[1] = (self._a[1] - 0.19 * speed) % 360
        self._a[2] = (self._a[2] + 0.11 * speed) % 360
        target = 1.0 if self.underMouse() else 0.0
        self._hover += (target - self._hover) * 0.08
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._on_click:
            self._on_click()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        R = min(w, h) * 0.34
        breath = 0.90 + 0.10 * math.sin(self._t * 2.4)
        lum = breath * (1.0 + 0.35 * self._hover)

        # 光晕 + 深黑核心
        g = QRadialGradient(cx, cy, R * 1.5)
        g.setColorAt(0.00, QColor(0, 0, 0))
        g.setColorAt(0.48, QColor(2, 8, 7))
        g.setColorAt(0.66, QColor(12, 46, 40, min(255, int(200 * lum))))
        g.setColorAt(0.78, QColor(46, 130, 112, min(255, int(120 * lum))))
        g.setColorAt(1.00, QColor(6, 10, 12, 0))
        p.setBrush(g)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), R * 1.5, R * 1.5)

        # 雷达涟漪(两圈错相扩散淡出)
        for phase in (0.0, 0.5):
            k = ((self._t / 2.4) + phase) % 1.0
            rr = R * (0.95 + 0.55 * k)
            alpha = int(85 * (1.0 - k) * lum)
            if alpha > 2:
                p.setPen(QPen(QColor(88, 230, 200, alpha), 1.6))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(cx, cy), rr, rr)

        # 三条旋转弧线(锥形渐变拖尾)
        for (radius, angle, span, alpha, width) in (
                (R * 1.04, self._a[0], 150, 170, 2.6),
                (R * 0.88, self._a[1], 110, 110, 1.9),
                (R * 1.18, self._a[2], 70, 70, 1.4)):
            cg = QConicalGradient(cx, cy, -angle)
            c0 = QColor(88, 230, 200, 0)
            c1 = QColor(88, 230, 200, min(255, int(alpha * lum)))
            cg.setColorAt(0.0, c0)
            cg.setColorAt(span / 720, c1)
            cg.setColorAt(span / 360, c0)
            pen = QPen(cg, width)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
            p.drawArc(rect, int(angle * 16), int(span * 16))

        # 吸积微粒(由外向内螺旋，两端淡出)
        p.setPen(Qt.NoPen)
        for (a0, r0, spd, size) in self._parts:
            k = ((self._t * spd * 0.11) + a0 / 360.0) % 1.0
            rr = R * (1.18 - 0.68 * k) * r0
            ang = math.radians(a0 + self._t * spd * 60.0 + k * 240.0)
            x = cx + rr * math.cos(ang)
            y = cy + rr * math.sin(ang) * 0.96
            fade = math.sin(k * math.pi)
            alpha = int(180 * fade * lum)
            if alpha > 4:
                p.setBrush(QColor(140, 244, 220, min(255, alpha)))
                p.drawEllipse(QPointF(x, y), size, size)


# ---------- 自绘标题栏 ----------
class TitleBar(QWidget):
    """无边框窗口的标题栏：品牌 + 设置/最小化/关闭，可拖动窗口。"""

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self._win = window
        self.setFixedHeight(46)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 8, 10, 6)
        lay.setSpacing(6)

        dot = QLabel("●")
        dot.setStyleSheet(f"color:{_TEAL}; font-size:10px; padding-right:2px;")
        t1 = QLabel("墨洞")
        t1.setStyleSheet(f"color:{_TEXT}; font-size:16px; font-weight:bold;")
        t2 = QLabel("InkHole")
        t2.setStyleSheet(f"color:{_TEXT_DIM}; font-size:11px; padding-top:3px;")
        lay.addWidget(dot)
        lay.addWidget(t1)
        lay.addWidget(t2)
        lay.addStretch(1)

        b_gear = QPushButton("⚙")
        b_gear.setObjectName("Win")
        b_gear.setToolTip("设置")
        b_gear.setCursor(Qt.PointingHandCursor)
        b_gear.clicked.connect(window._open_settings)
        b_min = QPushButton("─")
        b_min.setObjectName("Win")
        b_min.setCursor(Qt.PointingHandCursor)
        b_min.clicked.connect(window.showMinimized)
        b_close = QPushButton("✕")
        b_close.setObjectName("WinClose")
        b_close.setToolTip("隐藏到托盘")
        b_close.setCursor(Qt.PointingHandCursor)
        b_close.clicked.connect(window.hide)
        for b in (b_gear, b_min, b_close):
            lay.addWidget(b)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._win.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, e):   # 双击标题栏切换最大化
        w = self._win
        w.showNormal() if w.isMaximized() else w.showMaximized()


# ---------- 主窗口 ----------
class MainWindow(QWidget):
    """墨洞主窗口：无边框玻璃拟态 + 横向双栏 + 应用内设置页。"""

    def __init__(self, bridge, ctl: dict, icon=None):
        super().__init__()
        self._bridge = bridge
        self._ctl = ctl
        self.setWindowTitle("墨洞 InkHole")
        if icon:
            self.setWindowIcon(icon)
        self.resize(880, 580)
        self.setMinimumSize(700, 500)
        self.setStyleSheet(_QSS)
        self.setAcceptDrops(True)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._backdrop_ok = False
        self._backdrop_tried = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(TitleBar(self))
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_home_page())      # 0
        self._stack.addWidget(self._build_settings_page())  # 1
        outer.addWidget(self._stack, 1)
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 2, 2)
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(self))
        outer.addLayout(grip_row)

        bridge.peersChanged.connect(self._refresh_peers)
        bridge.status.connect(self._on_status)
        bridge.errorState.connect(self._on_error)
        bridge.emit_out.connect(lambda _n: self._refresh_recent())
        self._refresh_peers()
        self._refresh_recent()

    # ---- 环境光背景：磨砂之上再铺深墨底 + 两团青色辉光(玻璃面板背后的层次) ----
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        base_alpha = 205 if self._backdrop_ok else 255
        base = QLinearGradient(0, 0, 0, h)
        base.setColorAt(0.0, QColor(7, 12, 13, base_alpha))
        base.setColorAt(1.0, QColor(4, 7, 8, base_alpha))
        p.fillRect(self.rect(), base)
        # 辉光一：左中(墨洞背后)
        g1 = QRadialGradient(w * 0.28, h * 0.44, w * 0.42)
        g1.setColorAt(0.0, QColor(24, 96, 82, 46))
        g1.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g1)
        # 辉光二：右下(面板底部透出一点冷光)
        g2 = QRadialGradient(w * 0.86, h * 0.95, w * 0.34)
        g2.setColorAt(0.0, QColor(30, 70, 90, 34))
        g2.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g2)

    # ================= 主页 =================
    def _build_home_page(self) -> QWidget:
        page = QWidget()
        body = QHBoxLayout(page)
        body.setContentsMargins(26, 6, 26, 14)
        body.setSpacing(22)

        # 左：墨洞 + 状态
        left = QVBoxLayout()
        left.setSpacing(2)
        self._hole = HoleWidget(self._pick_and_send)
        left.addWidget(self._hole, 1)
        self._state_lbl = QLabel("搜索设备中…")
        self._state_lbl.setAlignment(Qt.AlignCenter)
        self._state_lbl.setStyleSheet(f"color:{_TEXT_DIM}; font-size:15px;")
        hint = QLabel("轻点墨洞选择文件 · 或把文件拖进窗口")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(f"color:{_TEXT_DIM}; font-size:11.5px;")
        self._status_lbl = QLabel(" ")
        self._status_lbl.setAlignment(Qt.AlignCenter)
        self._status_lbl.setStyleSheet(f"color:{_TEAL_DIM}; font-size:11px;")
        left.addWidget(self._state_lbl)
        left.addSpacing(2)
        left.addWidget(hint)
        left.addWidget(self._status_lbl)
        body.addLayout(left, 11)

        # 右：玻璃面板(设备 + 已吐出)
        panel = QFrame()
        panel.setObjectName("Panel")
        right = QVBoxLayout(panel)
        right.setContentsMargins(18, 16, 18, 16)
        right.setSpacing(10)

        right.addWidget(_section_label("设备"))
        self._chip_area = QScrollArea()
        self._chip_area.setWidgetResizable(True)
        self._chip_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._chip_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._chip_box = QWidget()
        self._chip_lay = QVBoxLayout(self._chip_box)
        self._chip_lay.setContentsMargins(0, 0, 4, 0)
        self._chip_lay.setSpacing(8)
        self._chip_area.setWidget(self._chip_box)
        right.addWidget(self._chip_area, 5)

        divider = QFrame()
        divider.setObjectName("HLine")
        divider.setFixedHeight(1)
        right.addWidget(divider)

        rec_bar = QHBoxLayout()
        rec_bar.addWidget(_section_label("已吐出"))
        rec_bar.addStretch(1)
        b_inbox = QPushButton("下载目录 ›")
        b_inbox.setObjectName("Link")
        b_inbox.setCursor(Qt.PointingHandCursor)
        b_inbox.clicked.connect(self._bridge.openInbox)
        rec_bar.addWidget(b_inbox)
        right.addLayout(rec_bar)

        self._recent_area = QScrollArea()
        self._recent_area.setWidgetResizable(True)
        self._recent_box = QWidget()
        self._recent_lay = QVBoxLayout(self._recent_box)
        self._recent_lay.setContentsMargins(0, 0, 4, 0)
        self._recent_lay.setSpacing(6)
        self._recent_area.setWidget(self._recent_box)
        right.addWidget(self._recent_area, 4)

        body.addWidget(panel, 9)
        return page

    # ================= 设置页 =================
    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(26, 6, 26, 14)
        outer.setSpacing(12)

        top = QHBoxLayout()
        back = QPushButton("‹  返回")
        back.setObjectName("Link")
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        title = QLabel("设置")
        title.setStyleSheet(f"color:{_TEXT}; font-size:18px; font-weight:bold;")
        top.addWidget(back)
        top.addSpacing(8)
        top.addWidget(title)
        top.addStretch(1)
        outer.addLayout(top)

        panel = QFrame()
        panel.setObjectName("Panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(26, 20, 26, 20)
        lay.setSpacing(13)

        self._local_lbl = QLabel()
        self._local_lbl.setStyleSheet(f"color:{_TEXT_DIM}; font-size:11px;")
        lay.addWidget(self._local_lbl)

        f = QFormLayout()
        f.setSpacing(12)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._ed_name = QLineEdit()
        self._ed_secret = QLineEdit()
        self._ed_secret.setEchoMode(QLineEdit.Password)
        self._ed_secret.setPlaceholderText("两端一致才能互传 · 留空不加密")
        cb_show = QCheckBox("显示口令")
        cb_show.toggled.connect(lambda on: self._ed_secret.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        self._sp_port = QSpinBox()
        self._sp_port.setRange(0, 65535)
        self._sp_port.setToolTip("0 = 系统自动分配")
        row = QHBoxLayout()
        self._ed_inbox = QLineEdit()
        self._ed_inbox.setReadOnly(True)
        b_browse = QPushButton("更换…")
        b_browse.setCursor(Qt.PointingHandCursor)
        b_browse.clicked.connect(self._choose_inbox)
        row.addWidget(self._ed_inbox, 1)
        row.addWidget(b_browse)

        def _flabel(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color:{_TEXT_SECOND};")
            return lbl
        f.addRow(_flabel("设备名称"), self._ed_name)
        f.addRow(_flabel("加密口令"), self._ed_secret)
        f.addRow(QLabel(""), cb_show)
        f.addRow(_flabel("监听端口"), self._sp_port)
        f.addRow(_flabel("收件箱"), row)
        lay.addLayout(f)

        divider = QFrame()
        divider.setObjectName("HLine")
        divider.setFixedHeight(1)
        lay.addWidget(divider)

        self._cb_trusted = QCheckBox("仅接收当前选中目标设备的文件（拦截陌生设备）")
        self._cb_pet = QCheckBox("显示桌宠挂件（桌面上的墨洞小球）")
        self._cb_auto = QCheckBox("开机自启")
        lay.addWidget(self._cb_trusted)
        lay.addWidget(self._cb_pet)
        lay.addWidget(self._cb_auto)

        btns = QHBoxLayout()
        btns.addStretch(1)
        b_save = QPushButton("保存")
        b_save.setObjectName("Primary")
        b_save.setCursor(Qt.PointingHandCursor)
        b_save.clicked.connect(self._save_settings)
        btns.addWidget(b_save)
        lay.addLayout(btns)

        outer.addWidget(panel)
        outer.addStretch(1)
        return page

    def _open_settings(self):
        cfg = self._bridge.node.cfg
        self._ed_name.setText(cfg.peer_name)
        self._ed_secret.setText(cfg.secret)
        self._sp_port.setValue(cfg.listen_port)
        self._ed_inbox.setText(os.path.abspath(cfg.inbox))
        self._cb_trusted.setChecked(cfg.trusted_only)
        self._cb_pet.setChecked(bool(self._ctl["pet_visible"]()))
        self._cb_auto.setChecked(bool(self._ctl["is_autostart"]()))
        self._local_lbl.setText(
            f"本机：{cfg.peer_name}-{self._bridge.node._instance_id}"
            f" · 端口 {self._bridge.node.actual_port}")
        self._stack.setCurrentIndex(1)

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
        node = self._bridge.node
        inbox = self._ed_inbox.text()
        if inbox and os.path.abspath(inbox) != os.path.abspath(node.cfg.inbox):
            node.cfg.inbox = inbox
            os.makedirs(inbox, exist_ok=True)
        if self._cb_trusted.isChecked() != node.cfg.trusted_only:
            self._bridge.toggleTrustedOnly()
        self._ctl["set_pet_visible"](self._cb_pet.isChecked())
        if self._cb_auto.isChecked() != bool(self._ctl["is_autostart"]()):
            self._ctl["set_autostart"](self._cb_auto.isChecked())
        self._ctl["apply_settings"](name, self._ed_secret.text(),
                                    self._sp_port.value())
        self._stack.setCurrentIndex(0)
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

        if not peers:
            empty = QLabel("正在搜索局域网设备…")
            empty.setStyleSheet(f"color:{_TEXT_DIM}; padding: 10px 4px;")
            self._chip_lay.addWidget(empty)
        for p in peers:
            self._chip_lay.addWidget(self._device_card(p, p.name == selected))
        self._chip_lay.addStretch(1)
        self._update_state_text()

    def _device_card(self, peer, selected: bool) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        card.setProperty("sel", "true" if selected else "false")
        card.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(14, 10, 12, 10)
        lay.setSpacing(11)

        icon = QLabel("💻" if any(k in peer.name.upper() for k in
                                  ("PC", "DESKTOP", "MAC", "BOOK", "WIN")) else "📱")
        icon.setStyleSheet("font-size:17px;")
        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(peer.name)
        name.setStyleSheet(
            f"color:{_TEAL if selected else _TEXT}; font-size:13.5px; font-weight:600;")
        host = QLabel(peer.host)
        host.setStyleSheet(f"color:{_TEXT_DIM}; font-size:11px;")
        col.addWidget(name)
        col.addWidget(host)
        state = QLabel("● 目标" if selected else "")
        state.setStyleSheet(f"color:{_TEAL}; font-size:10.5px;")
        lay.addWidget(icon)
        lay.addLayout(col, 1)
        lay.addWidget(state)

        def _click(_e, n=peer.name):
            node = self._bridge.node
            node.select_peer(None if node.selected_peer() == n else n)
            self._refresh_peers()
        card.mouseReleaseEvent = _click
        return card

    def _update_state_text(self):
        node = self._bridge.node
        selected = node.selected_peer()
        n = len(node.peers())
        if selected:
            self._state_lbl.setText(f"→  {selected}")
            self._state_lbl.setStyleSheet(f"color:{_TEAL}; font-size:15px; font-weight:600;")
        elif n:
            self._state_lbl.setText(f"发现 {n} 台设备 · 点右侧卡片选择目标")
            self._state_lbl.setStyleSheet(f"color:{_TEXT_SECOND}; font-size:14px;")
        else:
            self._state_lbl.setText("搜索设备中…")
            self._state_lbl.setStyleSheet(f"color:{_TEXT_DIM}; font-size:14px;")

    # ================= 最近接收 =================
    def _refresh_recent(self):
        while self._recent_lay.count():
            it = self._recent_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        recents = self._bridge.recentFiles()
        if not recents:
            empty = QLabel("还没有收到过文件")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(f"color:{_TEXT_DIM}; padding: 14px; font-size:12px;")
            self._recent_lay.addWidget(empty)
        for path in recents:
            self._recent_lay.addWidget(self._file_card(path))
        self._recent_lay.addStretch(1)

    def _file_card(self, path: str) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(13, 8, 11, 8)
        lay.setSpacing(10)
        icon = QLabel("📄")
        name = QLabel(os.path.basename(path))
        name.setStyleSheet(f"color:{_TEXT}; font-size:12.5px;")
        name.setToolTip(path)
        b_open = QPushButton("打开")
        b_open.setObjectName("Link")
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
        self._status_lbl.setStyleSheet(f"color:{_TEAL_DIM}; font-size:11px;")
        self._status_lbl.setText(msg or " ")

    @Slot(str)
    def _on_error(self, msg: str):
        if msg:
            self._status_lbl.setStyleSheet("color:#FF8A78; font-size:11px;")
            self._status_lbl.setText(msg)

    # ================= 拖拽发送 =================
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            self._bridge.dropFile(url.toString())
        e.acceptProposedAction()

    # ================= 磨砂 & 关闭 =================
    def showEvent(self, e):
        super().showEvent(e)
        if not self._backdrop_tried:
            self._backdrop_tried = True
            self._backdrop_ok = _enable_backdrop(self.winId())
            self.update()   # base_alpha 依据磨砂是否可用而不同

    def closeEvent(self, e):
        e.ignore()
        self.hide()
