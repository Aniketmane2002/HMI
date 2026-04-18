import sys
import os
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
    QBrush
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGroupBox, QFormLayout, QPlainTextEdit, QFileDialog,
    QInputDialog, QMessageBox, QSplitter, QSizePolicy, QStatusBar, QTabWidget,
    QSlider, QGridLayout, QStackedWidget, QGraphicsDropShadowEffect, QStyle,
    QFrame, QScrollBar, QDialog, QDialogButtonBox, QLineEdit
)

import can

# ===========================
#         Constants
# ===========================

BITRATE            = 250000
CAN_CHANNEL_LINUX  = "can0"
CAN_IFACE_LINUX    = "socketcan"
CAN_IFACE_WINDOWS  = "vector"

TORQUE_MIN = -150
TORQUE_MAX = +150
SLIDER_TORQUE_MIN= -500
SLIDER_TORQUE_MAX = +500
TORQUE_ENDIAN = "little"

LOG_RING_MAX   = 50_000
LOG_WIDGET_MAX = 3_000
LOG_FLUSH_MS   = 80

# Directory where logs are saved 
DEFAULT_LOG_DIR = os.path.expanduser("~")

class SimpleFilenameDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Save CAN Log")
        self.setFixedSize(480, 140)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        ts = time.strftime("%Y%m%d_%H%M%S")
        suggested = f"can_log_{ts}.txt"

        info = QLabel(f"Save folder:  {DEFAULT_LOG_DIR}")
        info.setFont(QFont("Segoe UI", 9))
        info.setStyleSheet("color: #555;")
        layout.addWidget(info)

        lbl = QLabel("Filename:")
        lbl.setFont(QFont("Segoe UI", 10))
        layout.addWidget(lbl)

        self.edit = QLineEdit(suggested)
        self.edit.setFont(QFont("Segoe UI", 10))
        self.edit.setFixedHeight(32)
        self.edit.selectAll()
        layout.addWidget(self.edit)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_path(self) -> str:
        name = self.edit.text().strip()
        if not name:
            name = f"can_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        if not (name.endswith(".txt") or name.endswith(".csv")):
            name += ".txt"
        return os.path.join(DEFAULT_LOG_DIR, name)


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
        self._value = 0
        self._color = QColor(color)
        self._label = label_text
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

        p.fillRect(outer, QColor("#F4F4F4"))
        p.setPen(QPen(QColor("#708487"), 1))
        p.drawRoundedRect(outer, 6, 6)

        inner = outer.adjusted(4, 4, -4, -4)
        zero_y = inner.center().y()
        p.setPen(QPen(QColor("#666"), 1))
        p.drawLine(inner.left(), zero_y, inner.right(), zero_y)

        half_h = inner.height() / 2.0
        v    = float(self._value)
        vmin = float(self._min_value)
        vmax = float(self._max_value)

        p.setPen(Qt.NoPen)
        p.setBrush(self._color)

        if v > 0.0 and vmax > 0.0:
            frac = min(1.0, v / vmax)
            h = int(half_h * frac)
            if h > 0:
                top = int(zero_y - h)
                p.drawRoundedRect(QRect(inner.left(), top, inner.width(), h), 4, 4)
        elif v < 0.0 and vmin < 0.0:
            frac = min(1.0, abs(v) / abs(vmin))
            h = int(half_h * frac)
            if h > 0:
                p.drawRoundedRect(QRect(inner.left(), int(zero_y), inner.width(), h), 4, 4)

        p.setPen(QColor("#666"))
        p.drawLine(outer.left() - 10, int(zero_y), outer.left(), int(zero_y))

        p.setPen(QColor("#333"))
        p.setFont(QFont("Segoe UI", 9, QFont.Medium))
        p.drawText(self.rect().adjusted(0, 0, 0, -6),
                   Qt.AlignHCenter | Qt.AlignBottom,
                   f"{self._label}\n{self._value} Nm")


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
        p.setBrush(QColor("#B0B0B0") if self.isChecked() else QColor("#3A86FF"))
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
        self.setRange(SLIDER_TORQUE_MIN, SLIDER_TORQUE_MAX)
        self.setSingleStep(1)
        self.setPageStep(5)
        self.setTracking(True)
        self.setFixedHeight(58)
        self.setStyleSheet("""
            QSlider::groove:horizontal {
                height: 26px; margin: 18px 22px; border-radius: 13px;
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                            stop:0 #EDEDED, stop:1 #D3D3D3);
                border: 1px solid #B5B5B5;
            }
            QSlider::handle:horizontal {
                background: #1565C0; border: 1px solid #0D47A1;
                width: 46px; height: 46px; margin: -12px -8px; border-radius: 8px;
            }
            QSlider::handle:horizontal:hover   { background: #1B74E4; }
            QSlider::handle:horizontal:pressed { background: #0F5AB8; }
        """)


# ===========================
#    Log Widget
# ===========================

class CanLogView(QPlainTextEdit):
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
                background: #0D1117;
                color: #C9D1D9;
                border: 1px solid #30363D;
                border-radius: 6px;
                padding: 4px 6px;
                selection-background-color: #264F78;
            }
            QScrollBar:vertical {
                background: #161B22; width: 10px; border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #30363D; border-radius: 5px; min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QScrollBar:horizontal {
                background: #161B22; height: 10px; border-radius: 5px;
            }
            QScrollBar::handle:horizontal {
                background: #30363D; border-radius: 5px; min-width: 20px;
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


# ===========================
#       Threading
# ===========================

class CanReaderThread(QThread):
    messages_batch = Signal(list)
    interface_down = Signal(str)

    def __init__(self, bus: can.BusABC):
        super().__init__()
        self._bus = bus
        self._running = True

    def run(self):
        batch: list = []
        last_emit = time.monotonic()

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
            # sysname = platform.system()
            # if sysname == "Windows":
            #     try:
            #         bus = can.interface.Bus(
            #             interface=CAN_IFACE_WINDOWS, channel=0, bitrate=BITRATE)
            #     except Exception as e:
            #         self.failed.emit(f"Vector open failed: {e}")
            #         return
            #     self.opened.emit(bus)
            #     return

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
        self.setMaximumHeight(70)          # fixed header 
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setObjectName("HeaderBar")

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setOffset(0, 10)
        shadow.setBlurRadius(36)
        shadow.setColor(QColor(0, 0, 0, 20))
        self.setGraphicsEffect(shadow)

        root = QHBoxLayout(self)
        root.setContentsMargins(26, 4, 26, 10)
        root.setSpacing(0)

        self.leftLogo = QLabel()
        self.leftLogo.setObjectName("HeaderLogoLeft") # object for the stylesheet 
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

        self._sheen_x = -200.0
        self._sheen_timer = QTimer(self)
        self._sheen_timer.timeout.connect(self._tick_sheen)
        self._sheen_timer.start(30)

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

    def _tick_sheen(self):
        w = max(1, self.width())
        self._sheen_x += w * 0.01
        if self._sheen_x > w + 200:
            self._sheen_x = -200
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        radius = 12

        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0.0, QColor("#F0F5FF"))
        grad.setColorAt(1.0, QColor("#EAF3FF"))
        p.setBrush(grad)
        p.setPen(QColor(0, 0, 0, 22))
        p.drawRoundedRect(r, radius, radius)

        glass = QColor(255, 255, 255, 110)
        p.setBrush(glass)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), radius - 2, radius - 2)

        p.setOpacity(0.5)
        sheen_w = 140
        x = self._sheen_x
        path_rect = QRectF(x, r.top(), sheen_w, r.height())
        g2 = QLinearGradient(path_rect.topLeft(), path_rect.topRight())
        g2.setColorAt(0.0, QColor(255, 255, 255, 0))
        g2.setColorAt(0.5, QColor(255, 255, 255, 90))
        g2.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(path_rect, g2)
        p.setOpacity(1.0)


# ===========================
#       Main Window
# ===========================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Torque Vectoring HMI")
        self.resize(1280, 800) # screen size obtained from raspberrypi "xrandr"

        self.bus:             Optional[can.BusABC]       = None
        self.reader_thread:   Optional[CanReaderThread]  = None
        self.periodic_thread: Optional[PeriodicTxThread] = None
        self.open_thread:     Optional[CanOpenThread]    = None

        self.filter_ids:      Set[int]   = set()
        self.logging_enabled: bool       = False

        self.v_fl = self.v_fr = self.v_rl = self.v_rr = 0

        self._on_main_tab  = True
        self._on_demo_page = True
        self._bars_visible = True

        self._log_mutex        = QMutex()
        self._log_ring: deque  = deque(maxlen=LOG_RING_MAX)
        self._log_pending: deque = deque(maxlen=10_000)

        self._build_ui()
        self._setup_theme()
        self._set_status_off()

        self._log_flush_timer = QTimer(self)
        self._log_flush_timer.setInterval(LOG_FLUSH_MS)
        self._log_flush_timer.timeout.connect(self._flush_log_to_view)
        self._log_flush_timer.start()


    def _update_bars_visibility(self):
        self._bars_visible = self._on_main_tab and self._on_demo_page

    def _push_bars(self, fl: int, fr: int, rl: int, rr: int):
        if not self._bars_visible:
            return
        self.bar_fl.set_value(fl)
        self.bar_fr.set_value(fr)
        self.bar_rl.set_value(rl)
        self.bar_rr.set_value(rr)

    
    # CAN TX 

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

    # UI Build

    def _build_ui(self):
        self.setStatusBar(QStatusBar(self))
        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)
        self.setCentralWidget(splitter)

        #  Left slim panel
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

        # Right tabs 
        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 1060])

        # Main tab
        self.tab_main = QWidget()
        main_l = QVBoxLayout(self.tab_main)
        main_l.setContentsMargins(8, 8, 8, 8)
        main_l.setSpacing(8)

        self.stack = QStackedWidget()
        self.page_demo   = self._build_demo_page()
        self.page_manual = self._build_manual_page()
        self.stack.addWidget(self.page_demo)    # 0 = Demo
        self.stack.addWidget(self.page_manual)  # 1 = Manual
        main_l.addWidget(self.stack, 1)

        # Toggle row position (bottom), with fixed height 
        toggle_row = QWidget()
        toggle_row.setFixedHeight(46)           #  fixed height
        tr2 = QHBoxLayout(toggle_row)
        tr2.setContentsMargins(0, 8, 0, 0)
        tr2.setSpacing(12)
        lbl_manual = QLabel("MANUAL OVERRIDE")
        lbl_demo   = QLabel("DEMO MODES")
        lbl_manual.setFont(QFont("Segoe UI", 10, QFont.Bold))
        lbl_demo.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.toggle = ToggleSwitch()
        self.toggle.setChecked(True)
        self.toggle.clicked.connect(self._on_toggle_changed)
        tr2.addStretch(1)
        tr2.addWidget(lbl_manual)
        tr2.addWidget(self.toggle)
        tr2.addWidget(lbl_demo)
        tr2.addStretch(1)
        main_l.addWidget(toggle_row, 0)         # stretch=0 never shrinks/grows
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
            "  0x10  →  Torque feedback (FL, FR, RL, RR)\n"
            "  0x12  →  Diagnostic signals —\n"
            "  0x40  ←  Manual torque command\n\n"
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
        log_l.setSpacing(6)

        #  Toolbar fixed height 
        btn_row = QWidget()
        btn_row.setFixedHeight(46)
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

        self.btn_start_log.setProperty("variant", "log")
        self.btn_stop_log.setProperty("variant",  "log")

        for b in (self.btn_start_log, self.btn_stop_log,
                  self.btn_start_periodic, self.btn_stop_periodic,
                  self.btn_save, self.btn_filter, self.btn_clear):
            b.setMinimumHeight(34)
            b.setCursor(Qt.PointingHandCursor)
            br.addWidget(b)
        br.addStretch(1)

        log_l.addWidget(btn_row, 0)            

        # Column header  fixed height 
        hdr = QLabel(
            "[HH:MM:SS.mmm]    Dir   ID      DLC   "
            " B0  B1  B2  B3  B4  B5  B6  B7"
        )
        hdr.setFont(QFont("Consolas", 9, QFont.Bold))
        hdr.setFixedHeight(24)
        hdr.setStyleSheet(
            "background:#161B22; color:#8B949E; border:1px solid #30363D;"
            "border-radius:4px; padding:3px 6px;"
        )
        log_l.addWidget(hdr, 0)                 

        self.log_view = CanLogView()
        self.log_view.setFixedHeight(380)       
        log_l.addWidget(self.log_view, 0)      

        signals_splitter = QSplitter(Qt.Horizontal)
        signals_splitter.setFixedHeight(168)    

        torque_box = QGroupBox("Torque Signals — Rx  0x10")
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

        log_l.addWidget(signals_splitter, 0)    
        log_l.addStretch(1)                     

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
        page = QWidget()
        page.setObjectName("DemoPage")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(7)
        root.addWidget(self._build_header(), 0)

        content = QWidget()
        h = QHBoxLayout(content)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        canvas = QFrame()
        canvas.setObjectName("Showcase")
        canvas.setMinimumHeight(0)
        cl = QGridLayout(canvas)
        cl.setContentsMargins(24, 24, 24, 24)
        cl.setHorizontalSpacing(28)
        cl.setVerticalSpacing(22)

        self.bar_fl = VerticalBar("Front Left",  "#EF4444")
        self.bar_fr = VerticalBar("Front Right", "#EF4444")
        self.bar_rl = VerticalBar("Rear Left",   "#EF4444")
        self.bar_rr = VerticalBar("Rear Right",  "#EF4444")

        self.car = QLabel()
        self.car.setObjectName("CarHero")
        self.car.setAlignment(Qt.AlignCenter)
        self.car.setMinimumSize(460, 520)
        self._load_car_image("car_top.png")

        cl.addWidget(self.bar_fl, 0, 0, Qt.AlignRight | Qt.AlignVCenter)
        cl.addWidget(self.car,    0, 1, 4, 1)
        cl.addWidget(self.bar_fr, 0, 2, Qt.AlignLeft  | Qt.AlignVCenter)
        cl.addWidget(self.bar_rl, 2, 0, Qt.AlignRight | Qt.AlignVCenter)
        cl.addWidget(self.bar_rr, 2, 2, Qt.AlignLeft  | Qt.AlignVCenter)

        sidebar = QWidget()
        s = QVBoxLayout(sidebar)
        s.setContentsMargins(16, 16, 16, 16)
        s.setSpacing(14)

        prod_card = QGroupBox("PRODUCTION")
        prod_card.setFont(QFont("Segoe UI", 16, QFont.Bold))
        prod_card.setObjectName("ModeCard")
        pv = QVBoxLayout(prod_card)
        pv.setContentsMargins(14, 14, 14, 14)
        pv.setSpacing(10)

        btn_fwd = QPushButton("FWD\nFront wheel drive")
        btn_awd = QPushButton("AWD\nAll wheel drive")
        for b in (btn_fwd, btn_awd):
            b.setObjectName("ModeButton")
            b.setMinimumHeight(60)
            b.setCursor(Qt.PointingHandCursor)
            b.setProperty("variant", "primary")
        pv.addWidget(btn_fwd)
        pv.addWidget(btn_awd)

        proto_card = QGroupBox("PROTOTYPE")
        proto_card.setFont(QFont("Segoe UI", 16, QFont.Bold))
        proto_card.setObjectName("ModeCard")
        qv = QVBoxLayout(proto_card)
        qv.setContentsMargins(14, 14, 14, 14)
        qv.setSpacing(10)

        btn_tv   = QPushButton("4WD and\nTorque Vectoring\nHandling and stability")
        btn_lock = QPushButton("4WD and\nAxle Lock\nOff road traction")
        for b in (btn_tv, btn_lock):
            b.setObjectName("ModeButton")
            b.setMinimumHeight(68)
            b.setCursor(Qt.PointingHandCursor)
        btn_tv.setProperty("variant",   "magenta")
        btn_lock.setProperty("variant", "orange")
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

        h.addWidget(canvas, 3)
        h.addWidget(sidebar, 2)
        root.addWidget(content, 1)
        return page

    def _build_manual_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(12)
        lay.addWidget(self._build_header(), 0)

        title = QLabel("MANUAL OVERRIDE")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        title.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)

        self.manual_slider = BigSlider()
        self.manual_slider.valueChanged.connect(self._on_manual_slider_changed)

        lr = QWidget()
        lrh = QHBoxLayout(lr)
        lrh.setContentsMargins(8, 0, 8, 0)
        lrh.addWidget(QLabel(f"{SLIDER_TORQUE_MIN} Nm"), 0, Qt.AlignLeft)
        lrh.addWidget(QLabel(f"+{SLIDER_TORQUE_MAX} Nm"), 0, Qt.AlignRight)

        self.lbl_manual_val = QLabel("0 Nm")
        self.lbl_manual_val.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.lbl_manual_val.setAlignment(Qt.AlignHCenter)

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
        pal.setColor(QPalette.Window,          QColor(246, 248, 250))
        pal.setColor(QPalette.Base,            QColor(255, 255, 255))
        pal.setColor(QPalette.AlternateBase,   QColor(240, 240, 240))
        pal.setColor(QPalette.Text,            QColor(33,  33,  33))
        pal.setColor(QPalette.Button,          QColor(236, 236, 236))
        pal.setColor(QPalette.ButtonText,      QColor(33,  33,  33))
        pal.setColor(QPalette.Highlight,       QColor(58, 134, 255))
        pal.setColor(QPalette.HighlightedText, Qt.white)
        app.setPalette(pal)

        self.setStyleSheet("""
        QGroupBox#CanCard {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                        stop:0 #FFFFFF, stop:1 #F5F7FB);
            border: 1px solid #D9DEE6; border-radius: 14px; margin-top: 0px;
        }
        QLabel#CanTitle { color: #0F172A; letter-spacing: 0.5px; }
        QPushButton {
            background-color: #EFF2F7; border: 1px solid #CBD5E1;
            border-radius: 12px; color: #111827; padding: 8px 12px;
            font: 11pt "Segoe UI";
        }
        QPushButton:hover   { background-color: #E7ECF5; }
        QPushButton:pressed { background-color: #DEE5F0; }
        QPushButton[variant="success"] {
            background: #16A34A; color: white; border: 1px solid #15803D;
        }
        QPushButton[variant="success"]:hover   { background: #149247; }
        QPushButton[variant="success"]:pressed { background: #128342; }
        QPushButton[variant="danger"] {
            background: #DC2626; color: white; border: 1px solid #B91C1C;
        }
        QPushButton[variant="danger"]:hover   { background: #C22424; }
        QPushButton[variant="danger"]:pressed { background: #AE2121; }
        QTabBar::tab {
            background: #F3F4F6; color: #333; font: 11pt "Segoe UI";
            padding: 3px 6px; border-radius: 6px; margin: 2px;
        }
        QTabBar::tab:selected { background: #3A86FF; color: white; }
        QTabBar::tab:hover    { background: #E0E7FF; }
        QFrame#Showcase {
            border-radius: 16px; border: 1px solid rgba(0,0,0,0.08);
            background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                        stop:0 #F8FAFF, stop:1 #EFF4FF);
        }
        QLabel#CarHero { background: transparent; }
        QGroupBox#ModeCard {
            background: palette(Base); border: 1px solid #DFE5EF;
            border-radius: 14px; margin-top: 30px;
        }
        QGroupBox#ModeCard::title {
            subcontrol-origin: margin; subcontrol-position: top center;
            color: #0F172A; letter-spacing: 0.5px;
        }
        QPushButton#ModeButton {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 rgba(255,255,255,0.85), stop:1 rgba(243,246,255,0.85));
            border: 1px solid #CBD5E1; border-radius: 12px; color: #0B1324;
            padding: 10px 12px; text-align: center; font: 10.5pt "Segoe UI";
        }
        QPushButton#ModeButton:hover   { background: rgba(240,243,255,0.95); }
        QPushButton#ModeButton:pressed { background: rgba(232,237,255,1.00); }
        QPushButton#ModeButton[variant="primary"] {
            background: #707070; color: white; border: 1px solid #1E40AF;
        }
        QPushButton#ModeButton[variant="primary"]:hover   { background: #2E2E2E; }
        QPushButton#ModeButton[variant="primary"]:pressed { background: #191919; }
        QPushButton#ModeButton[variant="magenta"] {
            background: #8B5CF6; color: white; border: 1px solid #6D28D9;
        }
        QPushButton[variant="log"] {
            background: #707070; color: white; border: 1px solid #1E40AF;
        }
        QPushButton[variant="log"]:hover   { background: #2E2E2E; }
        QPushButton[variant="log"]:pressed { background: #191919; }
        QPushButton#ModeButton[variant="magenta"]:hover   { background: #7C3AED; }
        QPushButton#ModeButton[variant="magenta"]:pressed { background: #6D28D9; }
        QPushButton#ModeButton[variant="orange"] {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 #5DA9E9, stop:1 #1D6FC2);
            border: 1px solid #1C5CAB; border-radius: 14px; color: white;
            padding: 10px 12px; font: 11pt "Segoe UI";
        }
        QPushButton#ModeButton[variant="orange"]:hover {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 #7BB9F0, stop:1 #1B63B0);
        }
        QPushButton#ModeButton[variant="orange"]:pressed {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 #4C91CC, stop:1 #164E8A);
        }
        #HeaderBar  { color: #0F172A; }
        #HeaderTitle { color: #0B1324; letter-spacing: 0.8px; }
        QLabel#HeaderLogoLeft[missingLogo="true"],
        QLabel#HeaderLogoRight[missingLogo="true"] {
            background: rgba(0,0,0,0.03);
            border: 1px dashed rgba(0,0,0,0.12);
        }
        QGroupBox {
            font: 10pt "Segoe UI"; color: #1E293B;
            border: 1px solid #DFE5EF; border-radius: 10px; margin-top: 18px;
            padding-top: 6px;
        }
        QGroupBox::title {
            subcontrol-origin: margin; subcontrol-position: top left;
            left: 10px; padding: 0 4px;
        }
        """)

        def _soft_shadow(widget, blur=26, alpha=70, dy=8):
            eff = QGraphicsDropShadowEffect(widget)
            eff.setOffset(0, dy)
            eff.setBlurRadius(blur)
            eff.setColor(QColor(0, 0, 0, alpha))
            widget.setGraphicsEffect(eff)

        for gb in self.findChildren(QGroupBox):
            if gb.objectName() == "ModeCard":
                _soft_shadow(gb, blur=22, alpha=60, dy=8)
            if gb.objectName() == "CanCard":
                eff = QGraphicsDropShadowEffect(gb)
                eff.setOffset(0, 6)
                eff.setBlurRadius(24)
                eff.setColor(QColor(0, 0, 0, 60))
                gb.setGraphicsEffect(eff)
    # Images

    def _load_car_image(self, path: str):
        try:
            pix = QPixmap(path)
            if pix.isNull():
                self.car.setText("Place top-view car image as 'car_top.png'")
                return
            self.car.setPixmap(
                pix.scaled(self.car.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception:
            self.car.setText("car_top.png not found")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if isinstance(self.car.pixmap(), QPixmap):
            pix = self.car.pixmap()
            if pix:
                self.car.setPixmap(
                    pix.scaled(self.car.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # CAN ON / OFF

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
        if platform.system() == "Windows":
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

    # Save log 

    def _on_save_log(self):

        self._flush_log_to_view()

        dlg = SimpleFilenameDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return

        path = dlg.get_path()
       

        with QMutexLocker(self._log_mutex):
            lines_snapshot = list(self._log_ring)

        if not lines_snapshot:
            QMessageBox.information(self, "Save Log", "Log is empty — nothing to save.")
            return

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    f"# CAN Log — saved {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"# [HH:MM:SS.mmm]  Dir  ID      DLC  "
                    f"B0    B1    B2    B3    B4    B5    B6    B7\n"
                )
                f.write("\n".join(lines_snapshot))
                f.write("\n")
            QMessageBox.information(
                self, "Saved",
                f"Saved {len(lines_snapshot):,} lines to:\n{path}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to save log:\n{e}")

    # Filter / Clear

    def _on_set_filter(self):
        """Plain QDialog with QLineEdit — no native picker, no xkbcommon."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Set CAN ID Filter")
        dlg.setFixedSize(420, 130)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lbl = QLabel("Enter CAN IDs (comma-separated hex). Leave empty to remove filter:")
        lbl.setWordWrap(True)
        lbl.setFont(QFont("Segoe UI", 10))
        lay.addWidget(lbl)
        edit = QLineEdit()
        edit.setFont(QFont("Consolas", 10))
        edit.setFixedHeight(30)
        if self.filter_ids:
            edit.setText(", ".join(hex(x) for x in sorted(self.filter_ids)))
        lay.addWidget(edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        if dlg.exec() != QDialog.Accepted:
            return
        txt = edit.text()
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
            msg = ("Active filter: " + ", ".join(hex(x) for x in sorted(ids))
                   if ids else "No filter — showing all IDs.")
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

    # Logging / Periodic TX

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

    # RX batch handler


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

            if msg.arbitration_id == 0x10:
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
        dlc    = max(0, min(8, dlc))
        padded = (list(data_bytes) + [None] * 8)[:8]
        bytes_str = "  ".join(
            f"{b:02X}" if b is not None and i < dlc else "  "
            for i, b in enumerate(padded)
        )
        dir_col = "  RX" if direction == "RX" else "  TX"
        return (
            f"[{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}]"
            f"  {dir_col}    {arb_id:04X}     {dlc}     {bytes_str}"
        )


    # Log info

    def _info(self, line: str):
        ts = time.strftime("%H:%M:%S")
        full = f"[{ts}    ]    --   ----    -     {line}"
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

    # Parsing

    def _decode_s8_signed(self, b: int) -> int:
        return b if b < 128 else b - 256

    def _parse_torque_msg(self, data: bytes):
        d = bytes(data)
        if len(d) < 4:
            return
        fl = self._decode_s8_signed(d[0])
        fr = self._decode_s8_signed(d[1])
        rl = self._decode_s8_signed(d[2])
        rr = self._decode_s8_signed(d[3])
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
        self.lbl_estop.setStyleSheet("color: #DC2626;" if es else "color: #16A34A;")
        self.lbl_slip.setText(f"{slip:+d} deg")

    # Manual slider

    def _encode_s16_bytes(self, value: int) -> tuple:
        v = max(SLIDER_TORQUE_MIN, min(SLIDER_TORQUE_MAX, int(value)))
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

    # Toggle / Tab

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

    # Status indicator

    def _set_status_on(self):
        self.status_ind.set("ON", QColor("#16A34A"))
        self.btn_on.setEnabled(False)
        self.btn_off.setEnabled(True)

    def _set_status_off(self):
        self.status_ind.set("OFF", QColor("#C62828"))
        self.btn_on.setEnabled(True)
        self.btn_off.setEnabled(False)



if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
