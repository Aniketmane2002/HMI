import sys
import time
import platform
import subprocess
from typing import Optional, Set, List
from collections import deque

from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QSize, QRect, QRectF,
    QMutex, QMutexLocker
)
from PySide6.QtGui import (
    QColor, QPainter, QFont, QPalette, QPixmap, QPen,
    QLinearGradient, QTextCursor, QFontMetrics, QTextCharFormat,
    QBrush, QRadialGradient
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGroupBox, QFormLayout, QPlainTextEdit, QFileDialog,
    QInputDialog, QMessageBox, QSplitter, QSizePolicy, QStatusBar, QTabWidget,
    QSlider, QGridLayout, QStackedWidget, QGraphicsDropShadowEffect, QStyle,
    QFrame, QScrollBar
)

import can

# ===========================
#         Constants
# ===========================

BITRATE            = 250000
CAN_CHANNEL_LINUX  = "can0"
CAN_IFACE_LINUX    = "socketcan"
CAN_IFACE_WINDOWS  = "vector"

TORQUE_MIN    = -500
TORQUE_MAX    = +500
TORQUE_ENDIAN = "little"

LOG_RING_MAX    = 20_000
LOG_WIDGET_MAX  = 3_000
LOG_FLUSH_MS    = 80

# ===========================
#       Custom Widgets
# ===========================

class StatusIndicator(QWidget):
    def __init__(self, text="OFF", color=QColor('#C62828'), parent=None):
        super().__init__(parent)
        self._dot_size = 20
        self.dot = QLabel()
        self.dot.setFixedSize(self._dot_size, self._dot_size)
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setOffset(0, 0)
        self._glow.setBlurRadius(14)
        self._glow.setColor(color)
        self.dot.setGraphicsEffect(self._glow)
        self.label = QLabel(text)
        self.label.setFont(QFont("Segoe UI", 12, QFont.DemiBold))
        self.set_color(color)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(8)
        lay.addWidget(self.dot, 0, Qt.AlignVCenter)
        lay.addWidget(self.label, 0, Qt.AlignVCenter)
        lay.addStretch(0)

    def set(self, text: str, color: QColor):
        self.label.setText(text)
        self.set_color(color)

    def set_color(self, color: QColor):
        r = self._dot_size // 2
        self.dot.setStyleSheet(
            f"background-color:{color.name()}; border-radius:{r}px;"
            f" border: 1px solid rgba(0,0,0,0.18);"
        )
        self._glow.setColor(color)


class VerticalBar(QWidget):
    def __init__(self, label_text: str = "", color: str = "#E53935", parent=None):
        super().__init__(parent)
        self._min_value = TORQUE_MIN
        self._max_value = TORQUE_MAX
        self._value     = 0
        self._color     = QColor(color)
        self._label     = label_text
        self.setMinimumSize(100, 260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setToolTip(f"{self._label}: 0")

    def setRange(self, vmin: int, vmax: int):
        self._min_value = int(vmin)
        self._max_value = int(vmax)
        self.update()

    def setColor(self, color: str):
        self._color = QColor(color)
        self.update()

    def set_value(self, v: int):
        v = max(self._min_value, min(self._max_value, int(v)))
        if v != self._value:
            self._value = v
            self.setToolTip(f"{self._label}: {v}")
            self.update()

    def value(self) -> int:
        return self._value

    def sizeHint(self) -> QSize:
        return QSize(100, 320)

    def minimumSizeHint(self) -> QSize:
        return QSize(100, 260)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        outer = self.rect().adjusted(25, 16, -25, -56)

        # Dark car-display style background
        p.fillRect(outer, QColor("#1A1F2E"))
        p.setPen(QPen(QColor("#2A3A5C"), 1))
        p.drawRoundedRect(outer, 6, 6)

        inner = outer.adjusted(4, 4, -4, -4)
        zero_y = inner.center().y()
        p.setPen(QPen(QColor("#3A4A6A"), 1))
        p.drawLine(inner.left(), zero_y, inner.right(), zero_y)

        half_h = inner.height() / 2.0
        v    = float(self._value)
        vmin = float(self._min_value)
        vmax = float(self._max_value)

        p.setPen(Qt.NoPen)

        if v > 0.0 and vmax > 0.0:
            frac = min(1.0, v / vmax)
            h = int(half_h * frac)
            if h > 0:
                top = int(zero_y - h)
                bar_rect = QRect(inner.left(), top, inner.width(), h)
                grad = QLinearGradient(bar_rect.topLeft(), bar_rect.bottomLeft())
                grad.setColorAt(0.0, QColor("#00E5FF"))
                grad.setColorAt(1.0, QColor("#0077B6"))
                p.setBrush(grad)
                p.drawRoundedRect(bar_rect, 4, 4)
        elif v < 0.0 and vmin < 0.0:
            frac = min(1.0, abs(v) / abs(vmin))
            h = int(half_h * frac)
            if h > 0:
                bar_rect = QRect(inner.left(), int(zero_y), inner.width(), h)
                grad = QLinearGradient(bar_rect.topLeft(), bar_rect.bottomLeft())
                grad.setColorAt(0.0, QColor("#FF6B35"))
                grad.setColorAt(1.0, QColor("#CC3300"))
                p.setBrush(grad)
                p.drawRoundedRect(bar_rect, 4, 4)

        p.setPen(QColor("#4A5A7A"))
        p.drawLine(outer.left() - 10, int(zero_y), outer.left(), int(zero_y))

        p.setPen(QColor("#00E5FF"))
        p.setFont(QFont("Segoe UI", 8, QFont.Medium))
        p.drawText(self.rect().adjusted(0, 0, 0, -30),
                   Qt.AlignHCenter | Qt.AlignBottom,
                   self._label)
        p.setFont(QFont("Segoe UI", 9, QFont.Bold))
        p.drawText(self.rect().adjusted(0, 0, 0, -6),
                   Qt.AlignHCenter | Qt.AlignBottom,
                   f"{self._value} Nm")


class ToggleSwitch(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setMinimumSize(80, 34)

    def sizeHint(self):
        return QSize(80, 34)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(2, 2, -2, -2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#00BCD4") if self.isChecked() else QColor("#5A6A7A"))
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        margin = 3
        d  = rect.height() - 2 * margin
        cx = rect.right() - margin - d if self.isChecked() else rect.left() + margin
        knob = QRect(cx, rect.top() + margin, d, d)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(knob)


class BigSlider(QSlider):
    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setRange(TORQUE_MIN, TORQUE_MAX)
        self.setSingleStep(1)
        self.setPageStep(5)
        self.setTracking(True)
        self.setFixedHeight(58)
        self.setStyleSheet("""
            QSlider::groove:horizontal {
                height: 26px; margin: 18px 22px; border-radius: 13px;
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                            stop:0 #1A2540, stop:1 #0D1520);
                border: 1px solid #2A3A5C;
            }
            QSlider::handle:horizontal {
                background: #00BCD4; border: 2px solid #00E5FF;
                width: 46px; height: 46px; margin: -12px -8px; border-radius: 8px;
            }
            QSlider::handle:horizontal:hover   { background: #00D4EE; }
            QSlider::handle:horizontal:pressed { background: #0099AA; }
        """)


# ===========================
#     Efficient Log Widget
# ===========================

# Fixed-width column specification (characters):
#   Timestamp : 16  (e.g. [00:13:29.281])
#   Dir       :  4  (RX / TX / --)
#   ID        :  6  (hex, e.g. 0020)
#   DLC       :  3
#   B0..B7    :  3 each  → total bytes area = 8*3-1 = 23
# We use a monospace font so every char is the same width.

_LOG_HEADER = "[HH:MM:SS.mmm]   Dir   ID      DLC   B0    B1    B2    B3    B4    B5    B6    B7"

class CanLogView(QPlainTextEdit):
    """
    Dark terminal-style CAN log.  Column widths match _LOG_HEADER exactly.
    appendBatch() is used for high-rate insertion.
    Auto-scroll unless user scrolls up.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(LOG_WIDGET_MAX)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setUndoRedoEnabled(False)

        font = QFont("Consolas", 9)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)

        self.setStyleSheet("""
            QPlainTextEdit {
                background: #0A0E1A;
                color: #C9D1D9;
                border: none;
                border-radius: 0px;
                padding: 2px 4px;
                selection-background-color: #264F78;
            }
            QScrollBar:vertical {
                background: #0D1117; width: 8px; border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #2A3A5C; border-radius: 4px; min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QScrollBar:horizontal {
                background: #0D1117; height: 8px; border-radius: 4px;
            }
            QScrollBar::handle:horizontal {
                background: #2A3A5C; border-radius: 4px; min-width: 20px;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
        """)

        self._auto_scroll = True
        vbar = self.verticalScrollBar()
        vbar.valueChanged.connect(self._on_vbar_changed)

    def _on_vbar_changed(self, val: int):
        vbar = self.verticalScrollBar()
        self._auto_scroll = (val >= vbar.maximum() - 4)

    def appendBatch(self, lines: list):
        if not lines:
            return
        text = "\n".join(lines)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        if self.document().blockCount() > 1:
            cursor.insertText("\n" + text)
        else:
            cursor.insertText(text)
        if self._auto_scroll:
            vbar = self.verticalScrollBar()
            vbar.setValue(vbar.maximum())


# Header widget that visually sits on top of CanLogView with zero gap
class LogPanel(QFrame):
    """
    Wraps the column-header label + CanLogView in one seamless dark container.
    No gap between header row and data — both have the same background.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LogPanel")
        self.setStyleSheet("""
            QFrame#LogPanel {
                background: #0A0E1A;
                border: 1px solid #1E2A40;
                border-radius: 6px;
            }
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header row ────────────────────────────────────────────────────
        self.header = QLabel(_LOG_HEADER)
        mono = QFont("Consolas", 9, QFont.Bold)
        mono.setStyleHint(QFont.Monospace)
        self.header.setFont(mono)
        self.header.setStyleSheet(
            "background:#0D1520; color:#4A90D9;"
            "padding: 3px 6px; border-bottom: 1px solid #1E2A40;"
        )
        lay.addWidget(self.header)

        # ── Log view ──────────────────────────────────────────────────────
        self.log_view = CanLogView()
        lay.addWidget(self.log_view)


# ===========================
#       Threading
# ===========================

class CanReaderThread(QThread):
    messages_batch = Signal(list)
    interface_down = Signal(str)

    def __init__(self, bus: can.BusABC):
        super().__init__()
        self._bus     = bus
        self._running = True

    def run(self):
        batch:     list  = []
        last_emit: float = time.monotonic()

        while self._running:
            try:
                msg = self._bus.recv(timeout=0.016)
                if msg is not None:
                    batch.append(msg)

                now = time.monotonic()
                if now - last_emit >= 0.016:
                    if batch:
                        self.messages_batch.emit(batch)
                        batch = []
                    last_emit = now

            except can.CanOperationError as e:
                self.interface_down.emit(str(e))
                time.sleep(0.3)
            except Exception as e:
                self.interface_down.emit(str(e))
                time.sleep(0.3)

    def stop(self):
        self._running = False


class PeriodicTxThread(QThread):
    tx_logged = Signal(int, list)

    def __init__(self, bus: can.BusABC, arb_id=0x200, payload=None, period_sec=1.0):
        super().__init__()
        self._bus     = bus
        self._running = True
        self._arb_id  = arb_id
        self._data    = payload if payload is not None else [0x3C]
        self._period  = max(0.05, float(period_sec))

    def run(self):
        while self._running:
            try:
                msg = can.Message(arbitration_id=self._arb_id,
                                  data=bytearray(self._data),
                                  is_extended_id=False)
                self._bus.send(msg)
                self.tx_logged.emit(self._arb_id, self._data.copy())
            except Exception:
                pass
            time.sleep(self._period)

    def stop(self):
        self._running = False


class CanOpenThread(QThread):
    opened = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            sysname = platform.system()
            if sysname == "Windows":
                try:
                    bus = can.interface.Bus(
                        interface=CAN_IFACE_WINDOWS, channel=0, bitrate=BITRATE)
                except Exception as e:
                    self.failed.emit(f"Vector open failed: {e}")
                    return
                self.opened.emit(bus)
                return

            cmd = ["sudo", "ip", "link", "set", CAN_CHANNEL_LINUX,
                   "up", "type", "can", "bitrate", str(BITRATE)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                if "File exists" not in stderr:
                    self.failed.emit(f"ip link set up failed: {stderr or result.stdout}")
                    return

            try:
                bus = can.interface.Bus(
                    channel=CAN_CHANNEL_LINUX, interface=CAN_IFACE_LINUX)
            except Exception as e:
                self.failed.emit(f"socketcan open failed: {e}")
                return

            self.opened.emit(bus)

        except Exception as e:
            self.failed.emit(str(e))


# ===========================
#       Header Widget
# ===========================

class HeaderBar(QWidget):
    def __init__(self, title="TORQUE VECTORING",
                 left_logo_path="Dana_logo.png",
                 right_logo_path="Dana_logo.png",
                 parent=None):
        super().__init__(parent)
        self._title = title
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setObjectName("HeaderBar")

        root = QHBoxLayout(self)
        root.setContentsMargins(26, 4, 26, 10)
        root.setSpacing(0)

        self.leftLogo = QLabel()
        self.leftLogo.setObjectName("HeaderLogoLeft")
        self.leftLogo.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.leftLogo.setMinimumSize(160, 64)
        self._set_logo(self.leftLogo, left_logo_path, QSize(220, 64))

        titleWrap = QWidget()
        tl = QVBoxLayout(titleWrap)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(2)

        self.titleLbl = QLabel(title)
        self.titleLbl.setObjectName("HeaderTitle")
        self.titleLbl.setAlignment(Qt.AlignCenter)
        self.titleLbl.setFont(QFont("Segoe UI", 20, QFont.Black))
        tl.addWidget(self.titleLbl)

        self.rightLogo = QLabel()
        self.rightLogo.setObjectName("HeaderLogoRight")
        self.rightLogo.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.rightLogo.setMinimumSize(160, 64)
        self._set_logo(self.rightLogo, right_logo_path, QSize(220, 64))

        root.addWidget(self.leftLogo, 1)
        root.addWidget(titleWrap, 2)
        root.addWidget(self.rightLogo, 1)

    def setTitle(self, text: str):
        self._title = text
        self.titleLbl.setText(text)
        self.update()

    def _set_logo(self, label: QLabel, path: str, size_hint: QSize):
        try:
            pix = QPixmap(path)
            if not pix or pix.isNull():
                label.setText("")
                label.setMinimumSize(size_hint)
                label.setProperty("missingLogo", True)
            else:
                label.setPixmap(
                    pix.scaled(size_hint, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                label.setMinimumSize(size_hint)
                label.setProperty("missingLogo", False)
        except Exception:
            label.setText("")
            label.setMinimumSize(size_hint)
            label.setProperty("missingLogo", True)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        radius = 12

        # Dark automotive header
        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0.0, QColor("#0D1520"))
        grad.setColorAt(1.0, QColor("#111827"))
        p.setBrush(grad)
        p.setPen(QPen(QColor("#00BCD4"), 1))
        p.drawRoundedRect(r, radius, radius)


# ===========================
#     Car Display Widget
# ===========================

class CarDisplayWidget(QWidget):
    """
    Draws a top-view car schematic with 4 wheel indicators — pure QPainter,
    no image file needed.  Torque values are shown as coloured arcs on each wheel.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(340, 440)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._fl = self._fr = self._rl = self._rr = 0

    def set_values(self, fl: int, fr: int, rl: int, rr: int):
        self._fl, self._fr, self._rl, self._rr = fl, fr, rl, rr
        self.update()

    def _wheel_color(self, v: int):
        if v > 0:
            t = min(1.0, v / TORQUE_MAX)
            r = int(0   + t * 0)
            g = int(180 + t * 75)
            b = int(255)
            return QColor(r, g, b)
        elif v < 0:
            t = min(1.0, abs(v) / abs(TORQUE_MIN))
            return QColor(int(255), int(80 * (1 - t)), 0)
        return QColor("#3A4A6A")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2

        # Car body
        body_w, body_h = min(w * 0.32, 110), min(h * 0.55, 260)
        body_x = cx - body_w / 2
        body_y = cy - body_h / 2

        # Outer glow
        glow_grad = QRadialGradient(cx, cy, max(body_w, body_h) * 0.9)
        glow_grad.setColorAt(0.0, QColor(0, 188, 212, 18))
        glow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(glow_grad)
        p.setPen(Qt.NoPen)
        p.drawEllipse(int(cx - body_w * 1.8), int(cy - body_h * 0.7),
                      int(body_w * 3.6),       int(body_h * 1.4))

        # Body shadow
        p.setBrush(QColor(0, 0, 0, 60))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(int(body_x + 8), int(body_y + 12),
                          int(body_w), int(body_h), 22, 22)

        # Body fill
        body_grad = QLinearGradient(body_x, body_y, body_x + body_w, body_y + body_h)
        body_grad.setColorAt(0.0, QColor("#1E2D45"))
        body_grad.setColorAt(0.5, QColor("#253555"))
        body_grad.setColorAt(1.0, QColor("#1A2438"))
        p.setBrush(body_grad)
        p.setPen(QPen(QColor("#00BCD4"), 1.5))
        p.drawRoundedRect(QRectF(body_x, body_y, body_w, body_h), 22, 22)

        # Windshield
        ws_margin = body_w * 0.15
        ws_h      = body_h * 0.16
        p.setBrush(QColor(0, 188, 212, 40))
        p.setPen(QPen(QColor("#00BCD4"), 1))
        p.drawRoundedRect(QRectF(body_x + ws_margin, body_y + body_h * 0.1,
                                 body_w - 2 * ws_margin, ws_h), 8, 8)
        # Rear window
        p.drawRoundedRect(QRectF(body_x + ws_margin,
                                 body_y + body_h - body_h * 0.1 - ws_h * 0.85,
                                 body_w - 2 * ws_margin, ws_h * 0.85), 6, 6)

        # Centre line
        p.setPen(QPen(QColor("#00BCD4"), 1, Qt.DashLine))
        p.drawLine(int(cx), int(body_y + 10), int(cx), int(body_y + body_h - 10))
        p.setPen(Qt.NoPen)

        # ── Wheels ────────────────────────────────────────────────────────
        ww = body_w * 0.32    # wheel width
        wh = body_h * 0.18   # wheel height
        horiz_off = body_w * 0.62
        front_y   = body_y + body_h * 0.22
        rear_y    = body_y + body_h * 0.65

        wheels = [
            (cx - horiz_off - ww / 2, front_y - wh / 2, self._fl),  # FL
            (cx + horiz_off - ww / 2, front_y - wh / 2, self._fr),  # FR
            (cx - horiz_off - ww / 2, rear_y  - wh / 2, self._rl),  # RL
            (cx + horiz_off - ww / 2, rear_y  - wh / 2, self._rr),  # RR
        ]

        for wx, wy, val in wheels:
            wr = QRectF(wx, wy, ww, wh)
            # Tyre shadow
            p.setBrush(QColor(0, 0, 0, 80))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(wr.adjusted(3, 3, 3, 3), 5, 5)
            # Tyre body
            p.setBrush(QColor("#0F1620"))
            p.setPen(QPen(self._wheel_color(val), 2.5))
            p.drawRoundedRect(wr, 5, 5)
            # Rim
            rim = wr.adjusted(4, 4, -4, -4)
            p.setBrush(QColor("#1C2A40"))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(rim, 3, 3)
            # Value text
            p.setPen(self._wheel_color(val))
            f = QFont("Segoe UI", 7, QFont.Bold)
            p.setFont(f)
            p.drawText(wr, Qt.AlignCenter, f"{val:+d}")

        # Labels
        p.setPen(QColor("#4A7A9B"))
        lf = QFont("Segoe UI", 7)
        p.setFont(lf)
        label_data = [
            (wheels[0][0], wheels[0][1], "FL"),
            (wheels[1][0], wheels[1][1], "FR"),
            (wheels[2][0], wheels[2][1], "RL"),
            (wheels[3][0], wheels[3][1], "RR"),
        ]
        for lx, ly, lbl in label_data:
            p.drawText(QRectF(lx, ly - 14, ww, 14), Qt.AlignCenter, lbl)


# ===========================
#       Main Window
# ===========================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Torque Vectoring HMI")
        self.resize(1280, 800)

        self.bus:             Optional[can.BusABC]       = None
        self.reader_thread:   Optional[CanReaderThread]  = None
        self.periodic_thread: Optional[PeriodicTxThread] = None
        self.open_thread:     Optional[CanOpenThread]    = None

        self.filter_ids:      Set[int] = set()
        self.logging_enabled: bool     = False

        self.v_fl = self.v_fr = self.v_rl = self.v_rr = 0

        self._on_main_tab  = True
        self._on_demo_page = True
        self._bars_visible = True

        self._log_mutex       = QMutex()
        self._log_ring:   deque = deque(maxlen=LOG_RING_MAX)
        self._log_pending: deque = deque(maxlen=10_000)

        self._build_ui()
        self._setup_theme()
        self._set_status_off()

        self._log_flush_timer = QTimer(self)
        self._log_flush_timer.setInterval(LOG_FLUSH_MS)
        self._log_flush_timer.timeout.connect(self._flush_log_to_view)
        self._log_flush_timer.start()

    # ──────────────────────────────────────────────────────────────────────
    # Bar visibility helpers
    # ──────────────────────────────────────────────────────────────────────

    def _update_bars_visibility(self):
        self._bars_visible = self._on_main_tab and self._on_demo_page

    def _push_bars(self, fl: int, fr: int, rl: int, rr: int):
        if not self._bars_visible:
            return
        self.bar_fl.set_value(fl)
        self.bar_fr.set_value(fr)
        self.bar_rl.set_value(rl)
        self.bar_rr.set_value(rr)
        self.car_display.set_values(fl, fr, rl, rr)

    # ──────────────────────────────────────────────────────────────────────
    # CAN TX helper
    # ──────────────────────────────────────────────────────────────────────

    def _send_button_cmd(self, arb_id: int, data_bytes: list) -> None:
        if not self.bus:
            QMessageBox.warning(self, "CAN TX", "CAN is OFF. Turn CAN ON first.")
            return
        if not (0 <= arb_id <= 0x7FF):
            QMessageBox.critical(self, "CAN TX", f"Invalid 11-bit ID: {hex(arb_id)}")
            return
        if not (0 <= len(data_bytes) <= 8):
            QMessageBox.critical(self, "CAN TX",
                                 f"Payload length must be 0..8, got {len(data_bytes)}")
            return
        try:
            data = bytearray(int(b) & 0xFF for b in data_bytes)
        except Exception:
            QMessageBox.critical(self, "CAN TX", f"Invalid data bytes: {data_bytes}")
            return
        msg = can.Message(arbitration_id=arb_id, is_extended_id=False, data=data)
        try:
            self.bus.send(msg, timeout=0.1)
            self._record_tx(arb_id, data)
        except can.CanError as e:
            QMessageBox.critical(self, "CAN TX Error", f"Message NOT sent:\n{e}")

    # ──────────────────────────────────────────────────────────────────────
    # UI Build
    # ──────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setStatusBar(QStatusBar(self))
        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)
        self.setCentralWidget(splitter)

        # ── Left slim panel ───────────────────────────────────────────────
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(16, 16, 16, 16)
        lv.setSpacing(12)

        card = QGroupBox()
        card.setObjectName("CanCard")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(16, 16, 16, 16)
        cv.setSpacing(14)

        title_row = QWidget()
        tr = QHBoxLayout(title_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(8)
        title_icon = QLabel()
        title_icon.setFixedSize(22, 22)
        title_icon.setPixmap(
            self.style().standardIcon(QStyle.SP_ComputerIcon).pixmap(22, 22))
        ltitle = QLabel("CAN STATUS")
        ltitle.setFont(QFont("Segoe UI", 12, QFont.Bold))
        ltitle.setObjectName("CanTitle")
        tr.addWidget(title_icon, 0, Qt.AlignVCenter)
        tr.addWidget(ltitle, 1, Qt.AlignVCenter)
        tr.addStretch(0)

        self.status_ind = StatusIndicator("OFF", QColor("#C62828"))
        self.btn_on  = QPushButton(
            self.style().standardIcon(QStyle.SP_DialogApplyButton),  " Turn ON")
        self.btn_off = QPushButton(
            self.style().standardIcon(QStyle.SP_DialogCancelButton), " Turn OFF")
        for b in (self.btn_on, self.btn_off):
            b.setMinimumHeight(44)
            b.setIconSize(QSize(18, 18))
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_on.setProperty("variant", "success")
        self.btn_off.setProperty("variant", "danger")
        self.btn_on.clicked.connect(self.on_can_on)
        self.btn_off.clicked.connect(self.on_can_off)

        cv.addWidget(title_row)
        cv.addSpacing(4)
        cv.addWidget(self.status_ind)
        cv.addSpacing(8)
        cv.addWidget(self.btn_on)
        cv.addWidget(self.btn_off)

        lv.addWidget(card)
        lv.addStretch(1)
        splitter.addWidget(left)

        # ── Right tabs ────────────────────────────────────────────────────
        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 1120])

        # Main tab
        self.tab_main = QWidget()
        main_l = QVBoxLayout(self.tab_main)
        main_l.setContentsMargins(8, 8, 8, 8)
        main_l.setSpacing(8)

        self.stack = QStackedWidget()
        self.page_demo   = self._build_demo_page()
        self.page_manual = self._build_manual_page()
        self.stack.addWidget(self.page_demo)
        self.stack.addWidget(self.page_manual)
        main_l.addWidget(self.stack, 1)

        toggle_row = QWidget()
        tr2 = QHBoxLayout(toggle_row)
        tr2.setContentsMargins(0, 8, 0, 0)
        tr2.setSpacing(12)
        lbl_manual = QLabel("MANUAL OVERRIDE")
        lbl_demo   = QLabel("DEMO MODES")
        lbl_manual.setFont(QFont("Segoe UI", 10, QFont.Bold))
        lbl_demo.setFont(QFont("Segoe UI", 10, QFont.Bold))
        lbl_manual.setStyleSheet("color:#8A9BB0;")
        lbl_demo.setStyleSheet("color:#8A9BB0;")
        self.toggle = ToggleSwitch()
        self.toggle.setChecked(True)
        self.toggle.clicked.connect(self._on_toggle_changed)
        tr2.addStretch(1)
        tr2.addWidget(lbl_manual)
        tr2.addWidget(self.toggle)
        tr2.addWidget(lbl_demo)
        tr2.addStretch(1)
        main_l.addWidget(toggle_row, 0)
        self.tabs.addTab(self.tab_main, "Main")

        # Measurement tab
        self._build_measurement_tab()

        # Help tab
        self.tab_help = QWidget()
        help_lay = QVBoxLayout(self.tab_help)
        help_lay.setContentsMargins(20, 20, 20, 20)
        help_text = QLabel(
            "Torque Vectoring HMI\n\n"
            "• Main tab — Demo / Manual control\n"
            "• Measurement tab — CAN log and live signal display\n\n"
            "CAN messages:\n"
            "  0x20  →  Torque feedback (FL, FR, RL, RR) — 4 bytes\n"
            "  0x12  →  Diagnostic signals — 8 bytes\n"
            "  0x40  ←  Manual torque command — 2 bytes (signed int16, little-endian)\n\n"
            "Manual slider range: -500 Nm … +500 Nm\n\n"
            "Log columns:  [HH:MM:SS.mmm]  Dir  ID(hex)  DLC  B0 B1 B2 B3 B4 B5 B6 B7\n"
        )
        help_text.setWordWrap(True)
        help_text.setFont(QFont("Segoe UI", 11))
        help_lay.addWidget(help_text)
        self.tabs.addTab(self.tab_help, "Help")

        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _build_measurement_tab(self):
        self.tab_logs = QWidget()
        log_l = QVBoxLayout(self.tab_logs)
        log_l.setContentsMargins(10, 10, 10, 10)
        log_l.setSpacing(8)

        # ── Toolbar ───────────────────────────────────────────────────────
        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        br.setSpacing(6)

        self.btn_start_log      = QPushButton("▶  Start Logging")
        self.btn_stop_log       = QPushButton("■  Stop Logging")
        self.btn_start_periodic = QPushButton("⟳  Start Periodic TX")
        self.btn_stop_periodic  = QPushButton("⊘  Stop Periodic TX")
        self.btn_save           = QPushButton("💾  Save Log")
        self.btn_filter         = QPushButton("⧖  Set Filter")
        self.btn_clear          = QPushButton("🗑  Clear")

        # Different colours from Turn ON/OFF (which use green/red)
        # Start Logging = teal/cyan, Stop Logging = amber/orange
        self.btn_start_log.setProperty("variant", "log_start")
        self.btn_stop_log.setProperty("variant",  "log_stop")

        for b in (self.btn_start_log, self.btn_stop_log,
                  self.btn_start_periodic, self.btn_stop_periodic,
                  self.btn_save, self.btn_filter, self.btn_clear):
            b.setMinimumHeight(34)
            b.setCursor(Qt.PointingHandCursor)
            br.addWidget(b)
        br.addStretch(1)

        log_l.addWidget(btn_row)

        # ── Seamless LogPanel (header + view, no gap) ─────────────────────
        self.log_panel = LogPanel()
        self.log_panel.setMinimumHeight(260)
        self.log_view = self.log_panel.log_view   # keep reference for compat
        log_l.addWidget(self.log_panel, 5)

        # ── Signals row ───────────────────────────────────────────────────
        signals_splitter = QSplitter(Qt.Horizontal)

        torque_box = QGroupBox("Torque Signals — Rx  0x20")
        torque_box.setFont(QFont("Segoe UI", 10, QFont.Bold))
        form1 = QFormLayout(torque_box)
        form1.setHorizontalSpacing(16)
        form1.setVerticalSpacing(6)
        self.lbl_fl = QLabel("-")
        self.lbl_fr = QLabel("-")
        self.lbl_rl = QLabel("-")
        self.lbl_rr = QLabel("-")
        for w in (self.lbl_fl, self.lbl_fr, self.lbl_rl, self.lbl_rr):
            w.setFont(QFont("Consolas", 11, QFont.Bold))
            w.setMinimumWidth(90)
        form1.addRow("Front Left: ", self.lbl_fl)
        form1.addRow("Front Right:", self.lbl_fr)
        form1.addRow("Rear Left:  ", self.lbl_rl)
        form1.addRow("Rear Right: ", self.lbl_rr)

        diag_box = QGroupBox("Diagnostic Signals — Rx  0x12")
        diag_box.setFont(QFont("Segoe UI", 10, QFont.Bold))
        form2 = QFormLayout(diag_box)
        form2.setHorizontalSpacing(16)
        form2.setVerticalSpacing(6)
        self.lbl_drive_mode = QLabel("-")
        self.lbl_status     = QLabel("-")
        self.lbl_error      = QLabel("-")
        self.lbl_estop      = QLabel("-")
        self.lbl_slip       = QLabel("-")
        for w in (self.lbl_drive_mode, self.lbl_status,
                  self.lbl_error, self.lbl_estop, self.lbl_slip):
            w.setFont(QFont("Consolas", 11, QFont.Bold))
            w.setMinimumWidth(90)
        form2.addRow("Drive Mode:", self.lbl_drive_mode)
        form2.addRow("Status:    ", self.lbl_status)
        form2.addRow("Error:     ", self.lbl_error)
        form2.addRow("EStop:     ", self.lbl_estop)
        form2.addRow("Slip Angle:", self.lbl_slip)

        signals_splitter.addWidget(torque_box)
        signals_splitter.addWidget(diag_box)
        signals_splitter.setStretchFactor(0, 1)
        signals_splitter.setStretchFactor(1, 1)

        log_l.addWidget(signals_splitter, 2)

        # Wire buttons
        self.btn_start_log.clicked.connect(self._start_logging)
        self.btn_stop_log.clicked.connect(self._stop_logging)
        self.btn_start_periodic.clicked.connect(self._start_periodic)
        self.btn_stop_periodic.clicked.connect(self._stop_periodic)
        self.btn_save.clicked.connect(self._on_save_log)
        self.btn_filter.clicked.connect(self._on_set_filter)
        self.btn_clear.clicked.connect(self._on_clear_log)

        self.tabs.addTab(self.tab_logs, "Measurement")

    def _build_header(self) -> QWidget:
        return HeaderBar(
            title="TORQUE VECTORING",
            left_logo_path="Dana_logo.png",
            right_logo_path="Dana_logo.png",
        )

    def _build_demo_page(self) -> QWidget:
        """
        Android-automotive style dark demo page:
         ┌────────────────────────────────────────────────┐
         │              HeaderBar                         │
         ├────────────────┬───────────────────────────────┤
         │  Bar FL  Car  Bar FR │  Mode sidebar           │
         │  Bar RL       Bar RR │                         │
         └────────────────┴───────────────────────────────┘
        """
        page = QWidget()
        page.setObjectName("DemoPage")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header(), 0)

        content = QWidget()
        h = QHBoxLayout(content)
        h.setContentsMargins(12, 12, 12, 12)
        h.setSpacing(12)

        # ── Canvas: bars + car diagram ─────────────────────────────────
        canvas = QFrame()
        canvas.setObjectName("Showcase")
        canvas.setMinimumHeight(0)
        cl = QGridLayout(canvas)
        cl.setContentsMargins(16, 16, 16, 16)
        cl.setHorizontalSpacing(16)
        cl.setVerticalSpacing(12)

        self.bar_fl = VerticalBar("FL", "#00BCD4")
        self.bar_fr = VerticalBar("FR", "#00BCD4")
        self.bar_rl = VerticalBar("RL", "#00BCD4")
        self.bar_rr = VerticalBar("RR", "#00BCD4")

        self.car_display = CarDisplayWidget()

        cl.addWidget(self.bar_fl, 0, 0, Qt.AlignRight | Qt.AlignVCenter)
        cl.addWidget(self.car_display, 0, 1, 4, 1)
        cl.addWidget(self.bar_fr, 0, 2, Qt.AlignLeft  | Qt.AlignVCenter)
        cl.addWidget(self.bar_rl, 2, 0, Qt.AlignRight | Qt.AlignVCenter)
        cl.addWidget(self.bar_rr, 2, 2, Qt.AlignLeft  | Qt.AlignVCenter)
        cl.setRowStretch(0, 1)
        cl.setRowStretch(1, 0)
        cl.setRowStretch(2, 1)
        cl.setRowStretch(3, 0)
        cl.setColumnStretch(1, 1)

        # ── Sidebar: mode buttons ──────────────────────────────────────
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        s = QVBoxLayout(sidebar)
        s.setContentsMargins(8, 8, 8, 8)
        s.setSpacing(10)

        # PRODUCTION card
        prod_card = QFrame()
        prod_card.setObjectName("ModeCard")
        pv = QVBoxLayout(prod_card)
        pv.setContentsMargins(12, 12, 12, 12)
        pv.setSpacing(8)
        prod_lbl = QLabel("PRODUCTION")
        prod_lbl.setFont(QFont("Segoe UI", 11, QFont.Bold))
        prod_lbl.setStyleSheet("color:#00BCD4; letter-spacing:1px;")
        pv.addWidget(prod_lbl)

        btn_fwd = self._make_mode_btn("FWD", "Front Wheel Drive", "#2A5298", "#3A6FCC")
        btn_awd = self._make_mode_btn("AWD", "All Wheel Drive",   "#2A5298", "#3A6FCC")
        pv.addWidget(btn_fwd)
        pv.addWidget(btn_awd)

        # PROTOTYPE card
        proto_card = QFrame()
        proto_card.setObjectName("ModeCard")
        qv = QVBoxLayout(proto_card)
        qv.setContentsMargins(12, 12, 12, 12)
        qv.setSpacing(8)
        proto_lbl = QLabel("PROTOTYPE")
        proto_lbl.setFont(QFont("Segoe UI", 11, QFont.Bold))
        proto_lbl.setStyleSheet("color:#FF8C00; letter-spacing:1px;")
        qv.addWidget(proto_lbl)

        btn_tv   = self._make_mode_btn("TV", "4WD + Torque Vectoring\nHandling & Stability",
                                        "#6A0DAD", "#8B2FCC")
        btn_lock = self._make_mode_btn("LOCK", "4WD + Axle Lock\nOff-Road Traction",
                                        "#8B4000", "#CC6600")
        qv.addWidget(btn_tv)
        qv.addWidget(btn_lock)

        btn_fwd.clicked.connect(
            lambda: self._send_button_cmd(0x20, [0x01, 0x00, 0, 0, 0, 0, 0, 0]))
        btn_awd.clicked.connect(
            lambda: self._send_button_cmd(0x21, [0x01, 0x00, 0, 0, 0, 0, 0, 0]))
        btn_tv.clicked.connect(
            lambda: self._send_button_cmd(0x30, [0xAA, 0x55, 0, 0, 0, 0, 0, 0]))
        btn_lock.clicked.connect(
            lambda: self._send_button_cmd(0x31, [0x55, 0xAA, 0, 0, 0, 0, 0, 0]))

        s.addWidget(prod_card)
        s.addWidget(proto_card)
        s.addStretch(1)

        h.addWidget(canvas,  3)
        h.addWidget(sidebar, 2)
        root.addWidget(content, 1)
        return page

    def _make_mode_btn(self, title: str, subtitle: str,
                       color_dark: str, color_light: str) -> QPushButton:
        btn = QPushButton()
        btn.setText(f"{title}\n{subtitle}")
        btn.setMinimumHeight(64)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 {color_light}, stop:1 {color_dark});
                border: 1px solid {color_dark};
                border-radius: 10px;
                color: #FFFFFF;
                font: bold 10pt "Segoe UI";
                text-align: center;
                padding: 8px;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 {color_light}CC, stop:1 {color_dark}CC);
                border: 1px solid #00BCD4;
            }}
            QPushButton:pressed {{
                background: {color_dark};
            }}
        """)
        return btn

    def _build_manual_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(12)
        lay.addWidget(self._build_header(), 0)

        title = QLabel("MANUAL OVERRIDE")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        title.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        title.setStyleSheet("color:#00BCD4; letter-spacing:1px;")

        self.manual_slider = BigSlider()
        self.manual_slider.valueChanged.connect(self._on_manual_slider_changed)

        lr = QWidget()
        lrh = QHBoxLayout(lr)
        lrh.setContentsMargins(8, 0, 8, 0)
        l1 = QLabel(f"{TORQUE_MIN} Nm")
        l2 = QLabel(f"+{TORQUE_MAX} Nm")
        l1.setStyleSheet("color:#8A9BB0;")
        l2.setStyleSheet("color:#8A9BB0;")
        lrh.addWidget(l1, 0, Qt.AlignLeft)
        lrh.addWidget(l2, 0, Qt.AlignRight)

        self.lbl_manual_val = QLabel("0 Nm")
        self.lbl_manual_val.setFont(QFont("Segoe UI", 14, QFont.Bold))
        self.lbl_manual_val.setAlignment(Qt.AlignHCenter)
        self.lbl_manual_val.setStyleSheet("color:#00E5FF;")

        lay.addWidget(title)
        lay.addSpacing(100)
        lay.addWidget(self.manual_slider)
        lay.addWidget(lr)
        lay.addWidget(self.lbl_manual_val)
        lay.addStretch(1)
        return page

    def _setup_theme(self):
        app = QApplication.instance()
        app.setStyle("Fusion")
        pal = app.palette()
        # Dark automotive palette
        pal.setColor(QPalette.Window,          QColor(10,  14,  26))
        pal.setColor(QPalette.Base,            QColor(13,  21,  32))
        pal.setColor(QPalette.AlternateBase,   QColor(20,  30,  48))
        pal.setColor(QPalette.Text,            QColor(200, 210, 225))
        pal.setColor(QPalette.Button,          QColor(20,  30,  48))
        pal.setColor(QPalette.ButtonText,      QColor(200, 210, 225))
        pal.setColor(QPalette.Highlight,       QColor(0, 188, 212))
        pal.setColor(QPalette.HighlightedText, Qt.white)
        app.setPalette(pal)

        self.setStyleSheet("""
        /* ── Left panel CAN card ── */
        QGroupBox#CanCard {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                        stop:0 #0D1520, stop:1 #0A0E1A);
            border: 1px solid #1E3A5C; border-radius: 14px; margin-top: 0px;
        }
        QLabel#CanTitle { color: #00BCD4; letter-spacing: 0.5px; }

        /* ── CAN ON / OFF buttons — green / red (unique to these) ── */
        QPushButton[variant="success"] {
            background: #16A34A; color: white; border: 1px solid #15803D;
            border-radius: 12px; font: 11pt "Segoe UI"; padding: 8px 12px;
        }
        QPushButton[variant="success"]:hover   { background: #149247; }
        QPushButton[variant="success"]:pressed { background: #128342; }
        QPushButton[variant="danger"] {
            background: #DC2626; color: white; border: 1px solid #B91C1C;
            border-radius: 12px; font: 11pt "Segoe UI"; padding: 8px 12px;
        }
        QPushButton[variant="danger"]:hover   { background: #C22424; }
        QPushButton[variant="danger"]:pressed { background: #AE2121; }

        /* ── Start Logging — teal; Stop Logging — amber ── */
        QPushButton[variant="log_start"] {
            background: #006B75; color: white; border: 1px solid #00838F;
            border-radius: 8px; font: 10pt "Segoe UI"; padding: 6px 10px;
        }
        QPushButton[variant="log_start"]:hover   { background: #00838F; }
        QPushButton[variant="log_start"]:pressed { background: #005F6B; }
        QPushButton[variant="log_stop"] {
            background: #B45309; color: white; border: 1px solid #92400E;
            border-radius: 8px; font: 10pt "Segoe UI"; padding: 6px 10px;
        }
        QPushButton[variant="log_stop"]:hover   { background: #CA6510; }
        QPushButton[variant="log_stop"]:pressed { background: #9A4A08; }

        /* ── Generic toolbar buttons ── */
        QPushButton {
            background-color: #1A2540; border: 1px solid #2A3A5C;
            border-radius: 8px; color: #C8D8E8; padding: 6px 10px;
            font: 10pt "Segoe UI";
        }
        QPushButton:hover   { background-color: #1E2D50; border-color: #3A4A6A; }
        QPushButton:pressed { background-color: #162038; }

        /* ── Tabs ── */
        QTabBar::tab {
            background: #0D1520; color: #6A7A8A; font: 11pt "Segoe UI";
            padding: 6px 18px; border-radius: 6px; margin: 2px;
            border: 1px solid #1A2540;
        }
        QTabBar::tab:selected { background: #00BCD4; color: #000E14; border-color: #00BCD4; }
        QTabBar::tab:hover    { background: #1A2D45; color: #C8D8E8; }
        QTabWidget::pane { border: 1px solid #1A2540; border-radius: 6px; }

        /* ── Demo page showcase frame ── */
        QFrame#Showcase {
            border-radius: 14px; border: 1px solid #1E3A5C;
            background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                        stop:0 #0A0F1E, stop:1 #0D1526);
        }

        /* ── Mode cards (sidebar) ── */
        QFrame#ModeCard {
            background: #0D1826; border: 1px solid #1E2E46;
            border-radius: 14px;
        }
        QFrame#Sidebar {
            background: transparent;
        }

        /* ── Header ── */
        #HeaderBar  { color: #00BCD4; }
        #HeaderTitle { color: #FFFFFF; letter-spacing: 0.8px; }
        QLabel#HeaderLogoLeft[missingLogo="true"],
        QLabel#HeaderLogoRight[missingLogo="true"] {
            background: rgba(0,188,212,0.05);
            border: 1px dashed rgba(0,188,212,0.2);
        }

        /* ── GroupBoxes (Measurement tab) ── */
        QGroupBox {
            font: 10pt "Segoe UI"; color: #8AB8D0;
            border: 1px solid #1E3A5C; border-radius: 10px; margin-top: 18px;
            padding-top: 6px; background: #0A0E1A;
        }
        QGroupBox::title {
            subcontrol-origin: margin; subcontrol-position: top left;
            left: 10px; padding: 0 4px; color: #00BCD4;
        }

        /* ── Splitter ── */
        QSplitter::handle { background: #1A2540; }

        /* ── Status bar ── */
        QStatusBar { background: #0A0E1A; color: #4A6A7A; }

        /* ── Main window background ── */
        QMainWindow { background: #0A0E1A; }
        QWidget { background: #0A0E1A; color: #C8D8E8; }
        """)

        def _soft_shadow(widget, blur=26, alpha=120, dy=8):
            eff = QGraphicsDropShadowEffect(widget)
            eff.setOffset(0, dy)
            eff.setBlurRadius(blur)
            eff.setColor(QColor(0, 0, 0, alpha))
            widget.setGraphicsEffect(eff)

        for gb in self.findChildren(QGroupBox):
            if gb.objectName() in ("CanCard",):
                _soft_shadow(gb, blur=22, alpha=100, dy=6)

    # ──────────────────────────────────────────────────────────────────────
    # CAN ON / OFF
    # ──────────────────────────────────────────────────────────────────────

    def _ui_can_buttons_enabled(self, enabled: bool):
        self.btn_on.setEnabled(enabled)
        self.btn_off.setEnabled(enabled)

    def on_can_on(self):
        if self.bus:
            self._mark_can_on()
            return
        self._ui_can_buttons_enabled(False)
        self._info("[INFO] Bringing CAN interface up…")
        self.open_thread = CanOpenThread()
        self.open_thread.opened.connect(self._on_bus_opened)
        self.open_thread.failed.connect(self._on_bus_open_failed)
        self.open_thread.finished.connect(lambda: self._ui_can_buttons_enabled(True))
        self.open_thread.start()

    def _on_bus_opened(self, bus: can.BusABC):
        self.bus = bus
        if not (self.reader_thread and self.reader_thread.isRunning()):
            self.reader_thread = CanReaderThread(self.bus)
            self.reader_thread.messages_batch.connect(self._on_rx_batch)
            self.reader_thread.interface_down.connect(self._on_interface_down)
            self.reader_thread.start()
        self._mark_can_on()
        sysname = platform.system()
        if sysname == "Windows":
            self._info(f"[INFO] Windows: Vector interface @ {BITRATE//1000} kbit/s.")
        else:
            self._info(f"[INFO] {CAN_CHANNEL_LINUX} up @ {BITRATE//1000} kbit/s.")

    def _on_bus_open_failed(self, err: str):
        self._info(f"[ERROR] CAN open failed: {err}")
        QMessageBox.critical(self, "CAN Init Error", err)
        self._set_status_off()

    def on_can_off(self):
        self._ui_can_buttons_enabled(False)
        try:
            self._stop_periodic()
        except Exception:
            pass

        if self.reader_thread:
            self.reader_thread.stop()
            self.reader_thread.wait(1500)
            self.reader_thread = None

        try:
            if self.bus:
                self.bus.shutdown()
                self.bus = None
        except Exception:
            pass

        if platform.system() == "Linux":
            try:
                result = subprocess.run(
                    ["sudo", "ifconfig", CAN_CHANNEL_LINUX, "down"],
                    capture_output=True, text=True)
                if result.returncode != 0:
                    self._info(f"[WARN] ifconfig down: {result.stderr or result.stdout}")
            except Exception as e:
                self._info(f"[WARN] ifconfig exception: {e}")

        self._set_status_off()
        self._info("[INFO] CAN interface closed.")
        self._ui_can_buttons_enabled(True)

    def closeEvent(self, e):
        self._log_flush_timer.stop()
        try:
            self._stop_periodic()
        except Exception:
            pass
        if self.reader_thread:
            self.reader_thread.stop()
            self.reader_thread.wait(2000)
            self.reader_thread = None
        try:
            if self.bus:
                self.bus.shutdown()
                self.bus = None
        except Exception:
            pass
        if platform.system() == "Linux":
            try:
                res = subprocess.run(["ip", "link", "show", CAN_CHANNEL_LINUX],
                                     capture_output=True, text=True)
                if "state UP" in (res.stdout or ""):
                    subprocess.run(
                        ["sudo", "ifconfig", CAN_CHANNEL_LINUX, "down"], check=False)
            except Exception:
                pass
        super().closeEvent(e)

    def _mark_can_on(self):
        if self.bus and self.reader_thread and self.reader_thread.isRunning():
            self._set_status_on()

    # ──────────────────────────────────────────────────────────────────────
    # Measurement tab button handlers
    # ──────────────────────────────────────────────────────────────────────

    def _on_save_log(self):
        """
        Fixed: snapshot ring buffer under mutex FIRST, then open file dialog.
        No blocking dialog while threads are actively writing.
        """
        # 1. Flush pending lines into the widget first (cosmetic only)
        self._flush_log_to_view()

        # 2. Snapshot ring atomically — this is the fix for the save issue
        with QMutexLocker(self._log_mutex):
            lines_snapshot = list(self._log_ring)

        if not lines_snapshot:
            QMessageBox.information(self, "Save Log", "Log is empty — nothing to save.")
            return

        # 3. Open save dialog (outside mutex — safe because snapshot is local)
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CAN Log", "can_log.txt",
            "Text Files (*.txt);;CSV Files (*.csv);;All Files (*)"
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    f"# CAN Log — saved {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"# {_LOG_HEADER}\n"
                )
                f.write("\n".join(lines_snapshot))
                f.write("\n")
            QMessageBox.information(
                self, "Saved",
                f"Saved {len(lines_snapshot):,} lines to:\n{path}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to save log:\n{e}")

    def _on_set_filter(self):
        txt, ok = QInputDialog.getText(
            self, "Set CAN ID Filter",
            "Enter CAN IDs (comma separated, hex):\nLeave empty to remove filter.")
        if not ok:
            return
        try:
            ids = {int(x.strip(), 16) for x in txt.split(",") if x.strip()}
            self.filter_ids = ids
            if self.bus:
                if ids:
                    filters = [{"can_id": i, "can_mask": 0x7FF, "extended": False}
                               for i in ids]
                    self.bus.set_filters(filters)
                else:
                    self.bus.set_filters(None)
            msg = ("Active filter: " + ", ".join(hex(x) for x in sorted(self.filter_ids))
                   if self.filter_ids else "No filter — showing all IDs.")
            QMessageBox.information(self, "Filter Updated", msg)
        except Exception:
            QMessageBox.critical(
                self, "Invalid Input",
                "Please enter valid hex IDs separated by commas (e.g. 10,12,200).")

    def _on_clear_log(self):
        with QMutexLocker(self._log_mutex):
            self._log_ring.clear()
            self._log_pending.clear()
        self.log_view.clear()

    # ──────────────────────────────────────────────────────────────────────
    # Logging / Periodic TX
    # ──────────────────────────────────────────────────────────────────────

    def _start_logging(self):
        if self.logging_enabled:
            self._info("[INFO] Logging already active.")
            return
        self.logging_enabled = True
        self._info("[INFO] Logging started.")

    def _stop_logging(self):
        if not self.logging_enabled:
            return
        self.logging_enabled = False
        self._info("[INFO] Logging stopped.")

    def _start_periodic(self):
        if self.bus is None:
            QMessageBox.warning(self, "Periodic TX", "CAN is OFF. Turn CAN ON first.")
            return
        if self.periodic_thread and self.periodic_thread.isRunning():
            self._info("[INFO] Periodic TX already running.")
            return
        payload = [0x3C, 0x00, 0xAA, 0x55, 0x11, 0x22, 0x33, 0x44]
        self.periodic_thread = PeriodicTxThread(
            self.bus, arb_id=0x200, payload=payload, period_sec=0.5)
        self.periodic_thread.tx_logged.connect(self._on_periodic_tx_logged)
        self.periodic_thread.start()
        self._info("[INFO] Periodic TX started (0x200 @ 500 ms).")

    def _stop_periodic(self):
        if self.periodic_thread:
            self.periodic_thread.stop()
            self.periodic_thread.wait(1200)
            self.periodic_thread = None
            self._info("[INFO] Periodic TX stopped.")

    # ──────────────────────────────────────────────────────────────────────
    # RX batch handler
    # ──────────────────────────────────────────────────────────────────────

    def _on_rx_batch(self, messages: list):
        latest_torque = None
        latest_diag   = None
        log_lines_batch: List[str] = []

        for msg in messages:
            if self.filter_ids and (msg.arbitration_id not in self.filter_ids):
                continue

            if self.logging_enabled:
                ts_ms = int((msg.timestamp % 86400) * 1000) if msg.timestamp else 0
                hh = ts_ms // 3_600_000;  ts_ms %= 3_600_000
                mm = ts_ms // 60_000;     ts_ms %= 60_000
                ss = ts_ms // 1_000;      ms = ts_ms % 1_000
                log_lines_batch.append(
                    self._format_can_line("RX", msg.arbitration_id,
                                          list(msg.data),
                                          getattr(msg, "dlc", len(msg.data)),
                                          hh, mm, ss, ms)
                )

            if msg.arbitration_id == 0x20:
                latest_torque = msg.data
            elif msg.arbitration_id == 0x12:
                latest_diag = msg.data

        if log_lines_batch:
            with QMutexLocker(self._log_mutex):
                self._log_ring.extend(log_lines_batch)
                self._log_pending.extend(log_lines_batch)

        if latest_torque is not None:
            try:
                self._parse_torque_msg(latest_torque)
            except Exception as ex:
                self._info(f"[WARN] Torque parse error: {ex}")

        if latest_diag is not None:
            try:
                self._parse_diag_msg(latest_diag)
            except Exception as ex:
                self._info(f"[WARN] Diag parse error: {ex}")

    def _on_periodic_tx_logged(self, arb_id: int, data: list):
        self._record_tx(arb_id, data)

    def _record_tx(self, arb_id: int, data_bytes):
        if not self.logging_enabled:
            return
        now = time.localtime()
        line = self._format_can_line(
            "TX", arb_id, list(data_bytes), len(data_bytes),
            now.tm_hour, now.tm_min, now.tm_sec, 0)
        with QMutexLocker(self._log_mutex):
            self._log_ring.append(line)
            self._log_pending.append(line)

    def _format_can_line(self, direction: str, arb_id: int,
                         data_bytes: list, dlc: int,
                         hh: int, mm: int, ss: int, ms: int) -> str:
        """
        Columns exactly match _LOG_HEADER width:
        [HH:MM:SS.mmm]   Dir   ID      DLC   B0    B1    B2    B3    B4    B5    B6    B7
        """
        dlc    = max(0, min(8, dlc))
        padded = (list(data_bytes) + [None] * 8)[:8]
        bytes_str = "  ".join(
            f"{b:02X}" if b is not None and i < dlc else "  "
            for i, b in enumerate(padded)
        )
        dir_col = "RX" if direction == "RX" else "TX"
        return (
            f"[{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}]"
            f"   {dir_col}   {arb_id:04X}    {dlc}     {bytes_str}"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Log info / flush
    # ──────────────────────────────────────────────────────────────────────

    def _info(self, line: str):
        ts = time.strftime("%H:%M:%S")
        full = f"[{ts}.000]   --   ----    -     {line}"
        with QMutexLocker(self._log_mutex):
            self._log_ring.append(full)
            self._log_pending.append(full)
        self._flush_log_to_view()

    def _flush_log_to_view(self):
        if self._on_main_tab:
            with QMutexLocker(self._log_mutex):
                if len(self._log_pending) > 2000:
                    excess = len(self._log_pending) - 2000
                    for _ in range(excess):
                        self._log_pending.popleft()
            return

        with QMutexLocker(self._log_mutex):
            if not self._log_pending:
                return
            lines = list(self._log_pending)
            self._log_pending.clear()

        self.log_view.appendBatch(lines)

    def _on_interface_down(self, err: str):
        self._info(f"[ERROR] Interface issue: {err}")
        self._set_status_off()

    # ──────────────────────────────────────────────────────────────────────
    # Parsing
    # ──────────────────────────────────────────────────────────────────────

    def _decode_s8_signed(self, b: int) -> int:
        return b if b < 128 else b - 256

    def _parse_torque_msg(self, data: bytes):
        d = bytes(data)
        if len(d) < 4:
            return
        SCALE = 1
        fl = self._decode_s8_signed(d[0]) * SCALE
        fr = self._decode_s8_signed(d[1]) * SCALE
        rl = self._decode_s8_signed(d[2]) * SCALE
        rr = self._decode_s8_signed(d[3]) * SCALE
        self.v_fl, self.v_fr, self.v_rl, self.v_rr = fl, fr, rl, rr

        self.lbl_fl.setText(f"{fl:+5d} Nm")
        self.lbl_fr.setText(f"{fr:+5d} Nm")
        self.lbl_rl.setText(f"{rl:+5d} Nm")
        self.lbl_rr.setText(f"{rr:+5d} Nm")

        self._push_bars(fl, fr, rl, rr)

    def _parse_diag_msg(self, data: bytes):
        d = bytes(data)
        dm = d[0] if len(d) > 0 else 0
        st = d[1] if len(d) > 1 else 0
        er = d[2] if len(d) > 2 else 0
        es = st & 0x01
        slip = 0
        if len(d) >= 8:
            slip_raw = (d[7] << 8) | d[6]
            if slip_raw & 0x8000:
                slip_raw -= 0x10000
            slip = slip_raw

        self.lbl_drive_mode.setText(f"{dm}")
        self.lbl_status.setText(f"0x{st:02X}")
        self.lbl_error.setText(f"0x{er:02X}")
        self.lbl_estop.setText("⚠ ACTIVE" if es else "OK")
        self.lbl_estop.setStyleSheet("color: #FF4444;" if es else "color: #00BCD4;")
        self.lbl_slip.setText(f"{slip:+d} deg")

    # ──────────────────────────────────────────────────────────────────────
    # Manual slider
    # ──────────────────────────────────────────────────────────────────────

    def _encode_s16_bytes(self, value: int) -> tuple:
        v = max(TORQUE_MIN, min(TORQUE_MAX, int(value)))
        if v < 0:
            v = (1 << 16) + v
        lo = v & 0xFF
        hi = (v >> 8) & 0xFF
        return (lo, hi) if TORQUE_ENDIAN.lower() == "little" else (hi, lo)

    def _send_manual_torque(self, value: int):
        if not self.bus:
            return
        lo, hi = self._encode_s16_bytes(value)
        msg = can.Message(arbitration_id=0x40, is_extended_id=False,
                          data=bytearray([lo, hi]))
        try:
            self.bus.send(msg, timeout=0.1)
            self._record_tx(0x40, [lo, hi])
        except can.CanError as e:
            self._info(f"[ERROR] Manual TX failed: {e}")

    def _on_manual_slider_changed(self, v: int):
        self.lbl_manual_val.setText(f"{v:+d} Nm")
        if self.bus:
            self._send_manual_torque(v)

    # ──────────────────────────────────────────────────────────────────────
    # Toggle / Tab
    # ──────────────────────────────────────────────────────────────────────

    def _on_toggle_changed(self):
        if self.toggle.isChecked():
            self.stack.setCurrentIndex(0)
            self._on_demo_page = True
        else:
            self.stack.setCurrentIndex(1)
            self._on_demo_page = False
        self._update_bars_visibility()

    def _on_tab_changed(self, idx: int):
        self._on_main_tab = (idx == 0)
        self._update_bars_visibility()
        if self._on_main_tab and self._on_demo_page:
            self._push_bars(self.v_fl, self.v_fr, self.v_rl, self.v_rr)
        if not self._on_main_tab:
            self._flush_log_to_view()

    # ──────────────────────────────────────────────────────────────────────
    # Status indicator
    # ──────────────────────────────────────────────────────────────────────

    def _set_status_on(self):
        self.status_ind.set("ON", QColor("#16A34A"))
        self.btn_on.setEnabled(False)
        self.btn_off.setEnabled(True)

    def _set_status_off(self):
        self.status_ind.set("OFF", QColor("#C62828"))
        self.btn_on.setEnabled(True)
        self.btn_off.setEnabled(False)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
