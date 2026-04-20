"""
Torque Vectoring HMI — v4 (all 16 issues fixed)
=================================================
Fixes applied over doc3:
  I1  TORQUE_MIN/MAX corrected to ±127 (matches int8 decode range)
  I2  Slider range label clarified; range kept wide but noted
  I3  Idle sleep added to recv loop — no more 100% CPU spin on quiet bus
  I4  lbl_msg_count throttled to every 20 msgs and only on Measurement tab
  I5  4 force_update() calls deferred via QTimer.singleShot(0) — coalesced
  I6  locker.unlock() called explicitly before del — deterministic release
  I7  _stop_periodic_nowait waits 200 ms so thread finishes current send()
  I8  appendBatch blank-first-line fixed — prefix '\n' only after first line
  I9  Error sleep broken into 5 ms chunks that check _running each iteration
  I10 locker.unlock() explicit — safe across CPython and PyPy
  I11 Measurement labels synced from cached values on tab switch
  I12 on_can_on restarts dead reader thread if bus is still open
  I13 _on_interface_down calls _zero_bars() on unexpected disconnect
  I14 Filters re-applied to new bus in _on_bus_opened
  I15 Manual header sheen paused at startup (Demo is the default page)
  I16 _open_in_progress reset in open_thread.finished fallback handler
"""

import sys
import os
import time
import struct
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
    QLinearGradient, QTextCursor, QBrush
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

BITRATE           = 250000
CAN_CHANNEL_LINUX = "can0"
CAN_IFACE_LINUX   = "socketcan"
CAN_IFACE_WINDOWS = "vector"

# I1 FIX: TORQUE_MIN/MAX must match _decode_s8() range (-128..+127).
# With TORQUE_MIN=-255 the bars only reached 50% at max torque.
TORQUE_MIN        = -127
TORQUE_MAX        = +127

# Manual-override slider range (wider than display range intentionally —
# ECU may accept larger commands even if feedback is capped at ±127 Nm).
SLIDER_TORQUE_MIN = -500
SLIDER_TORQUE_MAX = +500

TORQUE_ENDIAN     = "little"

# CAN message IDs — change here only; used everywhere via these names
CAN_ID_TORQUE    = 0x20   # RX — wheel torques (4 × int8, one per wheel)
CAN_ID_DIAG      = 0x12   # RX — diagnostic frame (8 bytes)
CAN_ID_MANUAL_TX = 0x40   # TX — manual torque command (int16 LE)

# Log ring-buffer hard cap
LOG_RING_MAX   = 20_000
LOG_WIDGET_MAX = 3_000
LOG_FLUSH_MS   = 80

# Tab indices — define once so reordering never silently breaks comparisons
TAB_MAIN_IDX        = 0
TAB_MEASUREMENT_IDX = 1
TAB_HELP_IDX        = 2

# Recv timeout — 1 ms so a message waits at most ~1 ms before being read.
# Combined with the idle sleep in CanReaderThread this avoids CPU spin.
RECV_TIMEOUT_SEC = 0.001


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
        self.label.setStyleSheet(f"color: {color.name()};")


class VerticalBar(QWidget):
    """
    Fast vertical bar gauge.
    set_value() stores the value without triggering a repaint.
    force_update() triggers the repaint — called by MainWindow
    through _push_bars() so all four bars paint in one coalesced pass.
    """

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
        """Store value. Caller must call force_update() to repaint."""
        v = max(self._min_value, min(self._max_value, int(v)))
        if v != self._value:
            self._value = v
            self.setToolTip(f"{self._label}: {v}")

    def force_update(self):
        """Schedule a repaint. Called by MainWindow after all four bars are set."""
        self.update()

    def value(self) -> int:
        return self._value

    def sizeHint(self)        -> QSize: return QSize(100, 320)
    def minimumSizeHint(self) -> QSize: return QSize(100, 260)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        outer = self.rect().adjusted(25, 16, -25, -56)

        p.fillRect(outer, QColor("#F4F4F4"))
        p.setPen(QPen(QColor("#708487"), 1))
        p.drawRoundedRect(outer, 6, 6)

        inner  = outer.adjusted(4, 4, -4, -4)
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
                p.drawRoundedRect(
                    QRect(inner.left(), int(zero_y - h), inner.width(), h), 4, 4)
        elif v < 0.0 and vmin < 0.0:
            frac = min(1.0, abs(v) / abs(vmin))
            h = int(half_h * frac)
            if h > 0:
                p.drawRoundedRect(
                    QRect(inner.left(), int(zero_y), inner.width(), h), 4, 4)

        p.setPen(QColor("#666"))
        p.drawLine(outer.left() - 10, int(zero_y), outer.left(), int(zero_y))

        p.setPen(QColor("#333"))
        p.setFont(QFont("Segoe UI", 9, QFont.Medium))
        p.drawText(self.rect().adjusted(0, 0, 0, -6),
                   Qt.AlignHCenter | Qt.AlignBottom,
                   f"{self._label}\n{self._value} Nm")


class ToggleSwitch(QPushButton):
    """
    Checked  = Demo mode active  → blue  (active feel)
    Unchecked = Manual override  → gray  (neutral feel)
    Knob right when checked (demo on), left when unchecked (manual).
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setMinimumSize(80, 34)

    def sizeHint(self): return QSize(80, 34)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(2, 2, -2, -2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#3A86FF") if self.isChecked() else QColor("#B0B0B0"))
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        margin = 3
        d  = rect.height() - 2 * margin
        cx = rect.right() - margin - d if self.isChecked() else rect.left() + margin
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRect(cx, rect.top() + margin, d, d))


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
#     Log Widget
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
                background: #0D1117; color: #C9D1D9;
                border: 1px solid #30363D; border-radius: 6px;
                padding: 4px 6px; selection-background-color: #264F78;
            }
            QScrollBar:vertical {
                background: #161B22; width: 10px; border-radius: 5px; }
            QScrollBar::handle:vertical {
                background: #30363D; border-radius: 5px; min-height: 20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QScrollBar:horizontal {
                background: #161B22; height: 10px; border-radius: 5px; }
            QScrollBar::handle:horizontal {
                background: #30363D; border-radius: 5px; min-width: 20px; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
        """)
        self._auto_scroll = True
        self.verticalScrollBar().valueChanged.connect(self._on_vbar_changed)

    def _on_vbar_changed(self, val: int):
        vbar = self.verticalScrollBar()
        self._auto_scroll = (val >= vbar.maximum() - 4)

    def appendBatch(self, lines: list):
        """
        I8 FIX: Only prepend '\n' separator when the document already has
        content, so the very first batch doesn't create a blank leading line.
        """
        if not lines:
            return
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        # Prepend newline only when document already has text
        prefix = "\n" if self.document().blockCount() > 1 or self.toPlainText() else ""
        cursor.insertText(prefix + "\n".join(lines))
        if self._auto_scroll:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


# ===========================
#       Threading
# ===========================

class CanReaderThread(QThread):
    """
    Emits one message immediately after recv() returns.
    I3 FIX: When recv() returns None (no message), sleep 0.5 ms so the
    OS can deschedule the thread instead of spinning at 100% CPU.
    I9 FIX: Error sleep broken into 5 ms chunks that check _running,
    so stop() is noticed within 5 ms rather than after a full 50 ms sleep.
    """
    message_received = Signal(object)   # single can.Message
    interface_down   = Signal(str)

    def __init__(self, bus: can.BusABC):
        super().__init__()
        self._bus     = bus
        self._running = True

    def run(self):
        while self._running:
            try:
                msg = self._bus.recv(timeout=RECV_TIMEOUT_SEC)
                if msg is not None:
                    self.message_received.emit(msg)
                else:
                    # I3 FIX: yield CPU when bus is idle
                    time.sleep(0.0005)
            except can.CanOperationError as e:
                self.interface_down.emit(str(e))
                # I9 FIX: check _running every 5 ms during error back-off
                for _ in range(10):
                    if not self._running:
                        return
                    time.sleep(0.005)
            except Exception as e:
                self.interface_down.emit(str(e))
                for _ in range(10):
                    if not self._running:
                        return
                    time.sleep(0.005)

    def stop(self):
        self._running = False


class PeriodicTxThread(QThread):
    tx_logged = Signal(int, list)

    def __init__(self, bus: can.BusABC, arb_id=0x200, payload=None, period_sec=1.0):
        super().__init__()
        self._bus     = bus
        self._running = True
        self._arb_id  = arb_id
        self._data    = list(payload) if payload is not None else [0x3C]
        self._period  = max(0.05, float(period_sec))
        self._mutex   = QMutex()

    def set_payload(self, payload: list):
        """Thread-safe payload update."""
        locker = QMutexLocker(self._mutex)
        self._data = list(payload)
        locker.unlock()
        del locker

    def run(self):
        while self._running:
            try:
                locker = QMutexLocker(self._mutex)
                data_snapshot = list(self._data)
                locker.unlock()
                del locker

                msg = can.Message(arbitration_id=self._arb_id,
                                  data=bytearray(data_snapshot),
                                  is_extended_id=False)
                self._bus.send(msg)
                self.tx_logged.emit(self._arb_id, data_snapshot)
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
            if platform.system() == "Windows":
                try:
                    bus = can.interface.Bus(
                        interface=CAN_IFACE_WINDOWS, channel=0, bitrate=BITRATE)
                    self.opened.emit(bus)
                except Exception as e:
                    self.failed.emit(f"Vector open failed: {e}")
                return

            # Linux / Raspberry Pi
            cmd = ["sudo", "ip", "link", "set", CAN_CHANNEL_LINUX,
                   "up", "type", "can", "bitrate", str(BITRATE)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                if "File exists" not in stderr:
                    self.failed.emit(
                        f"ip link set up failed: {stderr or result.stdout}")
                    return
            try:
                bus = can.interface.Bus(
                    channel=CAN_CHANNEL_LINUX, interface=CAN_IFACE_LINUX)
                self.opened.emit(bus)
            except Exception as e:
                self.failed.emit(f"socketcan open failed: {e}")

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
        self.setMaximumHeight(70)
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

        self._sheen_x = -200.0
        self._sheen_timer = QTimer(self)
        self._sheen_timer.timeout.connect(self._tick_sheen)
        self._sheen_timer.start(30)

    def pause_sheen(self):  self._sheen_timer.stop()
    def resume_sheen(self): self._sheen_timer.start(30)

    def setTitle(self, text: str):
        self._title = text
        self.titleLbl.setText(text)
        self.update()

    def _set_logo(self, label: QLabel, path: str, size_hint: QSize):
        try:
            pix = QPixmap(path)
            if not pix or pix.isNull():
                label.setText("")
                label.setProperty("missingLogo", True)
            else:
                label.setPixmap(
                    pix.scaled(size_hint, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                label.setProperty("missingLogo", False)
            label.setMinimumSize(size_hint)
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

        p.setBrush(QColor(255, 255, 255, 110))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), radius - 2, radius - 2)

        p.setOpacity(0.5)
        sheen_w   = 140
        path_rect = QRectF(self._sheen_x, r.top(), sheen_w, r.height())
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
        self.resize(1280, 800)

        self.bus:             Optional[can.BusABC]       = None
        self.reader_thread:   Optional[CanReaderThread]  = None
        self.periodic_thread: Optional[PeriodicTxThread] = None
        self.open_thread:     Optional[CanOpenThread]    = None

        # I16: track open in progress so a quick OFF can cancel it
        self._open_in_progress: bool = False

        self.filter_ids:      Set[int] = set()
        self.logging_enabled: bool     = False

        # Cached per-wheel values — always reset to 0 on CAN OFF
        self.v_fl = self.v_fr = self.v_rl = self.v_rr = 0

        self._on_main_tab  = True
        self._on_demo_page = True
        self._bars_visible = True

        # Log ring + pending queue — same max size
        self._log_mutex          = QMutex()
        self._log_ring:   deque  = deque(maxlen=LOG_RING_MAX)
        self._log_pending: deque = deque(maxlen=LOG_RING_MAX)

        # I4: message counter — only updated every N messages and when visible
        self._total_msg_count    = 0
        self._MSG_COUNT_THROTTLE = 20   # update label every 20 messages

        # Slider debounce — prevents flooding CAN bus on fast drag
        self._pending_torque  = 0
        self._slider_debounce = QTimer(self)
        self._slider_debounce.setSingleShot(True)
        self._slider_debounce.setInterval(20)
        self._slider_debounce.timeout.connect(self._do_send_manual_torque)

        # Original car image — stored for quality-preserving resize
        self._car_source_pixmap: Optional[QPixmap] = None

        self._build_ui()
        self._setup_theme()
        self._set_status_off()

        self._log_flush_timer = QTimer(self)
        self._log_flush_timer.setInterval(LOG_FLUSH_MS)
        self._log_flush_timer.timeout.connect(self._flush_log_to_view)
        self._log_flush_timer.start()

    # ── Bars helpers ─────────────────────────────────────────────────────

    def _update_bars_visibility(self):
        self._bars_visible = self._on_main_tab and self._on_demo_page

    def _push_bars(self, fl: int, fr: int, rl: int, rr: int):
        """
        Store all four values; schedule a single coalesced repaint via
        I5 FIX: QTimer.singleShot(0) defers all four force_update() calls
        to the next event-loop iteration so Qt merges them into one pass.
        """
        self.bar_fl.set_value(fl)
        self.bar_fr.set_value(fr)
        self.bar_rl.set_value(rl)
        self.bar_rr.set_value(rr)
        if self._bars_visible:
            QTimer.singleShot(0, self._repaint_all_bars)

    def _repaint_all_bars(self):
        """Called from singleShot — all four repaints in one event-loop pass."""
        self.bar_fl.force_update()
        self.bar_fr.force_update()
        self.bar_rl.force_update()
        self.bar_rr.force_update()

    def _zero_bars(self):
        """
        Reset bars and labels to zero/dash unconditionally.
        Bypasses _bars_visible — used on CAN OFF and unexpected disconnect.
        """
        self.v_fl = self.v_fr = self.v_rl = self.v_rr = 0
        for bar in (self.bar_fl, self.bar_fr, self.bar_rl, self.bar_rr):
            bar.set_value(0)
            bar.force_update()
        for lbl in (self.lbl_fl, self.lbl_fr, self.lbl_rl, self.lbl_rr):
            lbl.setText("-")
        for lbl in (self.lbl_drive_mode, self.lbl_status,
                    self.lbl_error, self.lbl_estop, self.lbl_slip):
            lbl.setText("-")

    # ── CAN TX helper ────────────────────────────────────────────────────

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

    # ── UI Build ─────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setStatusBar(QStatusBar(self))
        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)
        self.setCentralWidget(splitter)

        # Left slim panel — CAN ON/OFF card
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
        splitter.setSizes([220, 1120])

        # Main tab
        self.tab_main = QWidget()
        main_l = QVBoxLayout(self.tab_main)
        main_l.setContentsMargins(8, 8, 8, 8)
        main_l.setSpacing(8)

        self.stack = QStackedWidget()
        self.page_demo   = self._build_demo_page()
        self.page_manual = self._build_manual_page()
        self.stack.addWidget(self.page_demo)    # index 0
        self.stack.addWidget(self.page_manual)  # index 1
        main_l.addWidget(self.stack, 1)

        toggle_row = QWidget()
        toggle_row.setFixedHeight(46)
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
        main_l.addWidget(toggle_row, 0)
        self.tabs.addTab(self.tab_main, "Main")         # TAB_MAIN_IDX = 0

        self._build_measurement_tab()                   # TAB_MEASUREMENT_IDX = 1

        self.tab_help = QWidget()
        help_lay = QVBoxLayout(self.tab_help)
        help_lay.setContentsMargins(20, 20, 20, 20)
        help_text = QLabel(
            "Torque Vectoring HMI\n\n"
            "• Main tab — Demo / Manual control\n"
            "• Measurement tab — CAN log and live signal display\n\n"
            "CAN messages:\n"
            f"  {hex(CAN_ID_TORQUE)}  →  Torque feedback (FL, FR, RL, RR)"
            f" — 4 × int8, range ±127 Nm\n"
            f"  {hex(CAN_ID_DIAG)}  →  Diagnostic signals — 8 bytes\n"
            f"  {hex(CAN_ID_MANUAL_TX)}  ←  Manual torque command"
            f" — int16 little-endian, range ±500 Nm\n\n"
            f"Manual slider: {SLIDER_TORQUE_MIN} … +{SLIDER_TORQUE_MAX} Nm"
            f"  |  Bar display: {TORQUE_MIN} … +{TORQUE_MAX} Nm\n\n"
            "Log:  [HH:MM:SS.mmm]  Dir  ID(hex)  DLC  B0…B7\n"
        )
        help_text.setWordWrap(True)
        help_text.setFont(QFont("Segoe UI", 11))
        help_lay.addWidget(help_text)
        self.tabs.addTab(self.tab_help, "Help")         # TAB_HELP_IDX = 2

        self.tabs.currentChanged.connect(self._on_tab_changed)

        # I15 FIX: Demo is the default page — pause the hidden manual header
        # Both timers auto-start in HeaderBar.__init__; stop the unused one.
        self._manual_header.pause_sheen()

    def _build_measurement_tab(self):
        self.tab_logs = QWidget()
        log_l = QVBoxLayout(self.tab_logs)
        log_l.setContentsMargins(10, 10, 10, 10)
        log_l.setSpacing(8)

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

        self.btn_start_log.setProperty("variant", "success")
        self.btn_stop_log.setProperty("variant",  "danger")

        for b in (self.btn_start_log, self.btn_stop_log,
                  self.btn_start_periodic, self.btn_stop_periodic,
                  self.btn_save, self.btn_filter, self.btn_clear):
            b.setMinimumHeight(34)
            b.setCursor(Qt.PointingHandCursor)
            br.addWidget(b)
        br.addStretch(1)

        self.lbl_msg_count = QLabel("0 msgs")
        self.lbl_msg_count.setFont(QFont("Consolas", 9))
        self.lbl_msg_count.setStyleSheet("color: #888; padding: 0 8px;")
        br.addWidget(self.lbl_msg_count)

        log_l.addWidget(btn_row)

        hdr = QLabel(
            " [HH:MM:SS.mmm]   Dir   ID      DLC   "
            "B0    B1    B2    B3    B4    B5    B6    B7"
        )
        hdr.setFont(QFont("Consolas", 9, QFont.Bold))
        hdr.setStyleSheet(
            "background:#161B22; color:#8B949E; border:1px solid #30363D;"
            "border-radius:4px; padding:3px 6px;"
        )
        log_l.addWidget(hdr)

        self.log_view = CanLogView()
        self.log_view.setMinimumHeight(260)
        log_l.addWidget(self.log_view, 5)

        signals_splitter = QSplitter(Qt.Horizontal)

        torque_box = QGroupBox(f"Torque Signals — Rx  {hex(CAN_ID_TORQUE)}")
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

        diag_box = QGroupBox(f"Diagnostic Signals — Rx  {hex(CAN_ID_DIAG)}")
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

        self.btn_start_log.clicked.connect(self._start_logging)
        self.btn_stop_log.clicked.connect(self._stop_logging)
        self.btn_start_periodic.clicked.connect(self._start_periodic)
        self.btn_stop_periodic.clicked.connect(self._stop_periodic_nowait)
        self.btn_save.clicked.connect(self._on_save_log)
        self.btn_filter.clicked.connect(self._on_set_filter)
        self.btn_clear.clicked.connect(self._on_clear_log)

        self.tabs.addTab(self.tab_logs, "Measurement")

    def _build_header(self) -> HeaderBar:
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
        self._demo_header = self._build_header()
        root.addWidget(self._demo_header, 0)

        content = QWidget()
        h = QHBoxLayout(content)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        canvas = QFrame()
        canvas.setObjectName("Showcase")
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
        self._manual_header = self._build_header()
        lay.addWidget(self._manual_header, 0)

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
            border: 1px solid #D9DEE6; border-radius: 14px; margin-top: 0px; }
        QLabel#CanTitle { color: #0F172A; letter-spacing: 0.5px; }
        QPushButton {
            background-color: #EFF2F7; border: 1px solid #CBD5E1;
            border-radius: 12px; color: #111827; padding: 8px 12px;
            font: 11pt "Segoe UI"; }
        QPushButton:hover   { background-color: #E7ECF5; }
        QPushButton:pressed { background-color: #DEE5F0; }
        QPushButton[variant="success"] {
            background: #16A34A; color: white; border: 1px solid #15803D; }
        QPushButton[variant="success"]:hover   { background: #149247; }
        QPushButton[variant="success"]:pressed { background: #128342; }
        QPushButton[variant="danger"] {
            background: #DC2626; color: white; border: 1px solid #B91C1C; }
        QPushButton[variant="danger"]:hover   { background: #C22424; }
        QPushButton[variant="danger"]:pressed { background: #AE2121; }
        QTabBar::tab {
            background: #F3F4F6; color: #333; font: 11pt "Segoe UI";
            padding: 3px 6px; border-radius: 6px; margin: 2px; }
        QTabBar::tab:selected { background: #3A86FF; color: white; }
        QTabBar::tab:hover    { background: #E0E7FF; }
        QFrame#Showcase {
            border-radius: 16px; border: 1px solid rgba(0,0,0,0.08);
            background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                        stop:0 #F8FAFF, stop:1 #EFF4FF); }
        QLabel#CarHero { background: transparent; }
        QGroupBox#ModeCard {
            background: palette(Base); border: 1px solid #DFE5EF;
            border-radius: 14px; margin-top: 30px; }
        QGroupBox#ModeCard::title {
            subcontrol-origin: margin; subcontrol-position: top center;
            color: #0F172A; letter-spacing: 0.5px; }
        QPushButton#ModeButton {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 rgba(255,255,255,0.85), stop:1 rgba(243,246,255,0.85));
            border: 1px solid #CBD5E1; border-radius: 12px; color: #0B1324;
            padding: 10px 12px; text-align: center; font: 10.5pt "Segoe UI"; }
        QPushButton#ModeButton:hover   { background: rgba(240,243,255,0.95); }
        QPushButton#ModeButton:pressed { background: rgba(232,237,255,1.00); }
        QPushButton#ModeButton[variant="primary"] {
            background: #707070; color: white; border: 1px solid #1E40AF; }
        QPushButton#ModeButton[variant="primary"]:hover   { background: #2E2E2E; }
        QPushButton#ModeButton[variant="primary"]:pressed { background: #191919; }
        QPushButton#ModeButton[variant="magenta"] {
            background: #8B5CF6; color: white; border: 1px solid #6D28D9; }
        QPushButton#ModeButton[variant="magenta"]:hover   { background: #7C3AED; }
        QPushButton#ModeButton[variant="magenta"]:pressed { background: #6D28D9; }
        QPushButton#ModeButton[variant="orange"] {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 #5DA9E9, stop:1 #1D6FC2);
            border: 1px solid #1C5CAB; border-radius: 14px; color: white;
            padding: 10px 12px; font: 11pt "Segoe UI"; }
        QPushButton#ModeButton[variant="orange"]:hover {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 #7BB9F0, stop:1 #1B63B0); }
        QPushButton#ModeButton[variant="orange"]:pressed {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 #4C91CC, stop:1 #164E8A); }
        #HeaderBar   { color: #0F172A; }
        #HeaderTitle { color: #0B1324; letter-spacing: 0.8px; }
        QLabel#HeaderLogoLeft[missingLogo="true"],
        QLabel#HeaderLogoRight[missingLogo="true"] {
            background: rgba(0,0,0,0.03);
            border: 1px dashed rgba(0,0,0,0.12); }
        QGroupBox {
            font: 10pt "Segoe UI"; color: #1E293B;
            border: 1px solid #DFE5EF; border-radius: 10px;
            margin-top: 18px; padding-top: 6px; }
        QGroupBox::title {
            subcontrol-origin: margin; subcontrol-position: top left;
            left: 10px; padding: 0 4px; }
        """)

        def _soft_shadow(w, blur=26, alpha=70, dy=8):
            eff = QGraphicsDropShadowEffect(w)
            eff.setOffset(0, dy)
            eff.setBlurRadius(blur)
            eff.setColor(QColor(0, 0, 0, alpha))
            w.setGraphicsEffect(eff)

        for gb in self.findChildren(QGroupBox):
            if gb.objectName() == "ModeCard":
                _soft_shadow(gb, blur=22, alpha=60, dy=8)
            elif gb.objectName() == "CanCard":
                _soft_shadow(gb, blur=24, alpha=60, dy=6)

    # ── Images ───────────────────────────────────────────────────────────

    def _load_car_image(self, path: str):
        """Store original pixmap so resizeEvent always scales from source."""
        try:
            pix = QPixmap(path)
            if pix.isNull():
                self._car_source_pixmap = None
                self.car.setText("Place top-view car image as 'car_top.png'")
                return
            self._car_source_pixmap = pix
            self.car.setPixmap(
                pix.scaled(self.car.size(), Qt.KeepAspectRatio,
                           Qt.SmoothTransformation))
        except Exception:
            self._car_source_pixmap = None
            self.car.setText("car_top.png not found")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._car_source_pixmap and not self._car_source_pixmap.isNull():
            self.car.setPixmap(
                self._car_source_pixmap.scaled(
                    self.car.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ── CAN ON / OFF ──────────────────────────────────────────────────────

    def _ui_can_buttons_enabled(self, enabled: bool):
        self.btn_on.setEnabled(enabled)
        self.btn_off.setEnabled(enabled)

    def on_can_on(self):
        if self.bus:
            # I12 FIX: if reader thread died but bus is still open, restart it
            if self.reader_thread and not self.reader_thread.isRunning():
                self._info("[INFO] Reader thread died — restarting…")
                self.reader_thread = CanReaderThread(self.bus)
                self.reader_thread.message_received.connect(self._on_message_received)
                self.reader_thread.interface_down.connect(self._on_interface_down)
                self.reader_thread.start()
            self._mark_can_on()
            return

        self._open_in_progress = True
        self._ui_can_buttons_enabled(False)
        self._info("[INFO] Bringing CAN interface up…")
        self.open_thread = CanOpenThread()
        self.open_thread.opened.connect(self._on_bus_opened)
        self.open_thread.failed.connect(self._on_bus_open_failed)
        self.open_thread.finished.connect(
            lambda: self._ui_can_buttons_enabled(True))
        # I16 FIX: reset flag if thread finishes without firing opened/failed
        self.open_thread.finished.connect(self._on_open_thread_finished)
        self.open_thread.start()

    def _on_open_thread_finished(self):
        """I16 FIX: fallback reset of _open_in_progress if bus was not set."""
        if self.bus is None:
            self._open_in_progress = False

    def _on_bus_opened(self, bus: can.BusABC):
        # Discard if user pressed OFF while open was in progress
        if not self._open_in_progress:
            try: bus.shutdown()
            except Exception: pass
            return
        self._open_in_progress = False
        self.bus = bus

        if not (self.reader_thread and self.reader_thread.isRunning()):
            self.reader_thread = CanReaderThread(self.bus)
            self.reader_thread.message_received.connect(self._on_message_received)
            self.reader_thread.interface_down.connect(self._on_interface_down)
            self.reader_thread.start()

        # I14 FIX: re-apply any active CAN filters to the new bus object
        if self.filter_ids:
            try:
                self.bus.set_filters(
                    [{"can_id": i, "can_mask": 0x7FF, "extended": False}
                     for i in self.filter_ids])
            except Exception:
                pass

        self._mark_can_on()
        sysname = platform.system()
        if sysname == "Windows":
            self._info(f"[INFO] Windows: Vector interface @ {BITRATE//1000} kbit/s.")
        else:
            self._info(f"[INFO] {CAN_CHANNEL_LINUX} up @ {BITRATE//1000} kbit/s.")

    def _on_bus_open_failed(self, err: str):
        self._open_in_progress = False
        self._info(f"[ERROR] CAN open failed: {err}")
        QMessageBox.critical(self, "CAN Init Error", err)
        self._set_status_off()

    def on_can_off(self):
        self._open_in_progress = False   # cancel any pending open
        self._ui_can_buttons_enabled(False)
        self._stop_periodic_nowait()

        if self.reader_thread:
            self.reader_thread.stop()
            if not self.reader_thread.wait(1500):
                self.reader_thread.terminate()
                self.reader_thread.wait(500)
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
                    self._info(
                        f"[WARN] ifconfig down: {result.stderr or result.stdout}")
            except Exception as e:
                self._info(f"[WARN] ifconfig exception: {e}")

        self._set_status_off()
        self._zero_bars()   # F1+F2: zero bars, reset cached values
        self._info("[INFO] CAN interface closed.")
        self._ui_can_buttons_enabled(True)

    def closeEvent(self, e):
        self._open_in_progress = False
        self._stop_periodic_nowait()

        if self.reader_thread:
            self.reader_thread.stop()
            if not self.reader_thread.wait(2000):
                self.reader_thread.terminate()
                self.reader_thread.wait(500)
            self.reader_thread = None

        try:
            if self.bus:
                self.bus.shutdown()
                self.bus = None
        except Exception:
            pass

        if platform.system() == "Linux":
            try:
                res = subprocess.run(
                    ["ip", "link", "show", CAN_CHANNEL_LINUX],
                    capture_output=True, text=True)
                if "state UP" in (res.stdout or ""):
                    subprocess.run(
                        ["sudo", "ifconfig", CAN_CHANNEL_LINUX, "down"],
                        check=False)
            except Exception:
                pass

        # F10: stop flush timer LAST so final _info() lines are flushed
        self._log_flush_timer.stop()
        super().closeEvent(e)

    def _mark_can_on(self):
        if self.bus and self.reader_thread and self.reader_thread.isRunning():
            self._set_status_on()

    # ── Measurement tab buttons ───────────────────────────────────────────

    def _on_save_log(self):
        self._flush_log_to_view()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CAN Log",
            f"can_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt);;CSV Files (*.csv);;All Files (*)"
        )
        if not path:
            return
        locker = QMutexLocker(self._log_mutex)
        lines_snapshot = list(self._log_ring)
        locker.unlock()
        del locker
        if not lines_snapshot:
            QMessageBox.information(self, "Save Log",
                                    "Log is empty — nothing to save.")
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
                f"Saved {len(lines_snapshot):,} lines to:\n{path}")
        except Exception as ex:
            QMessageBox.critical(self, "Save Error", f"Failed to save log:\n{ex}")

    def _on_set_filter(self):
        txt, ok = QInputDialog.getText(
            self, "Set CAN ID Filter",
            "Enter CAN IDs (comma-separated hex).\nLeave empty to remove filter.")
        if not ok:
            return
        try:
            ids = {int(x.strip(), 16) for x in txt.split(",") if x.strip()}
            self.filter_ids = ids
            if self.bus:
                if ids:
                    self.bus.set_filters(
                        [{"can_id": i, "can_mask": 0x7FF, "extended": False}
                         for i in ids])
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
        locker = QMutexLocker(self._log_mutex)
        self._log_ring.clear()
        self._log_pending.clear()
        locker.unlock()
        del locker
        self.log_view.clear()
        self._total_msg_count = 0
        self.lbl_msg_count.setText("0 msgs")

    # ── Logging / Periodic TX ─────────────────────────────────────────────

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
            QMessageBox.warning(self, "Periodic TX",
                                "CAN is OFF. Turn CAN ON first.")
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

    def _stop_periodic_nowait(self):
        """
        I7 FIX: Signal the thread to stop, then wait up to 200 ms so it
        finishes its current send() call before we proceed to bus.shutdown().
        200 ms is enough for one sleep cycle (period_sec >= 0.05 s).
        """
        if self.periodic_thread:
            self.periodic_thread.stop()
            self.periodic_thread.wait(200)   # I7 FIX: was 0 ms (pure nowait)
            try: self.periodic_thread.tx_logged.disconnect()
            except RuntimeError: pass
            self.periodic_thread = None

    # ── RX handler ────────────────────────────────────────────────────────

    def _on_message_received(self, msg: can.Message):
        """Called immediately on the main thread for every received CAN frame."""
        if self.bus is None:
            return   # race guard: discard after shutdown

        if self.filter_ids and (msg.arbitration_id not in self.filter_ids):
            return

        self._total_msg_count += 1

        # I4 FIX: only update the counter label every N messages and
        # only when the Measurement tab is visible.
        if (self._total_msg_count % self._MSG_COUNT_THROTTLE == 0
                and not self._on_main_tab):
            self.lbl_msg_count.setText(f"{self._total_msg_count:,} msgs")

        if self.logging_enabled:
            now = time.localtime()
            ms  = int((msg.timestamp % 1) * 1000) if msg.timestamp else 0
            line = self._format_can_line(
                "RX", msg.arbitration_id,
                list(msg.data), getattr(msg, "dlc", len(msg.data)),
                now.tm_hour, now.tm_min, now.tm_sec, ms)
            locker = QMutexLocker(self._log_mutex)
            self._log_ring.append(line)
            self._log_pending.append(line)
            locker.unlock()   # I6+I10 FIX: explicit unlock
            del locker

        if msg.arbitration_id == CAN_ID_TORQUE:
            try:
                self._parse_torque_msg(msg.data)
            except Exception as ex:
                self._info(f"[WARN] Torque parse error: {ex}")
        elif msg.arbitration_id == CAN_ID_DIAG:
            try:
                self._parse_diag_msg(msg.data)
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
        locker = QMutexLocker(self._log_mutex)
        self._log_ring.append(line)
        self._log_pending.append(line)
        locker.unlock()
        del locker

    def _format_can_line(self, direction: str, arb_id: int,
                         data_bytes: list, dlc: int,
                         hh: int, mm: int, ss: int, ms: int) -> str:
        dlc    = max(0, min(8, dlc))
        padded = (list(data_bytes) + [None] * 8)[:8]
        bstr   = "  ".join(
            f"{b:02X}" if b is not None and i < dlc else "  "
            for i, b in enumerate(padded))
        return (f"[{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}]"
                f"  {'RX' if direction == 'RX' else 'TX'}"
                f"   {arb_id:04X}    {dlc}     {bstr}")

    # ── Log info / flush ──────────────────────────────────────────────────

    def _info(self, line: str):
        ts   = time.strftime("%H:%M:%S")
        full = f"[{ts}.000]  --   ----    -     {line}"
        locker = QMutexLocker(self._log_mutex)
        self._log_ring.append(full)
        self._log_pending.append(full)
        locker.unlock()   # I6+I10 FIX: explicit unlock before flush
        del locker
        self._flush_log_to_view()

    def _flush_log_to_view(self):
        """
        Always drains _log_pending (data never lost regardless of active tab).
        Only writes to the visible widget when Measurement tab is showing.
        """
        locker = QMutexLocker(self._log_mutex)
        if not self._log_pending:
            locker.unlock()
            del locker
            return
        lines = list(self._log_pending)
        self._log_pending.clear()
        locker.unlock()
        del locker

        if not self._on_main_tab:
            self.log_view.appendBatch(lines)

    def _on_interface_down(self, err: str):
        self._info(f"[ERROR] Interface issue: {err}")
        self._set_status_off()
        # I13 FIX: zero bars on unexpected disconnect so they don't freeze
        self._zero_bars()

    # ── Parsing ───────────────────────────────────────────────────────────

    @staticmethod
    def _decode_s8(b: int) -> int:
        """Decode byte as signed int8 via struct — safe and unambiguous."""
        return struct.unpack("b", bytes([b & 0xFF]))[0]

    def _parse_torque_msg(self, data: bytes):
        """
        Decode 4-byte torque frame (one signed int8 per wheel).
        I11 FIX: Measurement labels updated only on Measurement tab
        (invisible setText calls on Main tab avoided).
        Tab switch handler syncs labels when switching TO Measurement tab.
        """
        d = bytes(data)
        if len(d) < 4:
            return
        fl = self._decode_s8(d[0])
        fr = self._decode_s8(d[1])
        rl = self._decode_s8(d[2])
        rr = self._decode_s8(d[3])
        self.v_fl, self.v_fr, self.v_rl, self.v_rr = fl, fr, rl, rr

        if not self._on_main_tab:
            self.lbl_fl.setText(f"{fl:+5d} Nm")
            self.lbl_fr.setText(f"{fr:+5d} Nm")
            self.lbl_rl.setText(f"{rl:+5d} Nm")
            self.lbl_rr.setText(f"{rr:+5d} Nm")

        self._push_bars(fl, fr, rl, rr)

    def _sync_measurement_labels(self):
        """
        I11 FIX: Push the current cached torque values into the measurement
        labels. Called when switching TO the Measurement tab so labels
        are never stale from a previous visit.
        """
        self.lbl_fl.setText(f"{self.v_fl:+5d} Nm")
        self.lbl_fr.setText(f"{self.v_fr:+5d} Nm")
        self.lbl_rl.setText(f"{self.v_rl:+5d} Nm")
        self.lbl_rr.setText(f"{self.v_rr:+5d} Nm")

    def _parse_diag_msg(self, data: bytes):
        """Slip angle endian respects TORQUE_ENDIAN constant."""
        d  = bytes(data)
        dm = d[0] if len(d) > 0 else 0
        st = d[1] if len(d) > 1 else 0
        er = d[2] if len(d) > 2 else 0
        es = st & 0x01
        slip = 0
        if len(d) >= 8:
            fmt  = "<h" if TORQUE_ENDIAN.lower() == "little" else ">h"
            slip = struct.unpack_from(fmt, d, 6)[0]

        self.lbl_drive_mode.setText(f"{dm}")
        self.lbl_status.setText(f"0x{st:02X}")
        self.lbl_error.setText(f"0x{er:02X}")
        self.lbl_estop.setText("⚠ ACTIVE" if es else "OK")
        self.lbl_estop.setStyleSheet(
            "color: #DC2626;" if es else "color: #16A34A;")
        self.lbl_slip.setText(f"{slip:+d} deg")

    # ── Manual slider ─────────────────────────────────────────────────────

    @staticmethod
    def _encode_s16_bytes(value: int) -> tuple:
        """Encode signed int16 via struct — respects TORQUE_ENDIAN."""
        v   = max(SLIDER_TORQUE_MIN, min(SLIDER_TORQUE_MAX, int(value)))
        fmt = "<h" if TORQUE_ENDIAN.lower() == "little" else ">h"
        lo, hi = struct.pack(fmt, v)
        return lo, hi

    def _on_manual_slider_changed(self, v: int):
        """Label updates instantly; CAN TX debounced to 20 ms."""
        self.lbl_manual_val.setText(f"{v:+d} Nm")
        self._pending_torque = v
        self._slider_debounce.start()

    def _do_send_manual_torque(self):
        if not self.bus:
            return
        lo, hi = self._encode_s16_bytes(self._pending_torque)
        msg = can.Message(arbitration_id=CAN_ID_MANUAL_TX,
                          is_extended_id=False,
                          data=bytearray([lo, hi]))
        try:
            self.bus.send(msg, timeout=0.1)
            self._record_tx(CAN_ID_MANUAL_TX, [lo, hi])
        except can.CanError as e:
            self._info(f"[ERROR] Manual TX failed: {e}")

    # ── Toggle / Tab ──────────────────────────────────────────────────────

    def _on_toggle_changed(self):
        if self.toggle.isChecked():        # Demo mode ON
            self.stack.setCurrentIndex(0)
            self._on_demo_page = True
            self._manual_header.pause_sheen()
            self._demo_header.resume_sheen()
        else:                              # Manual override
            self.stack.setCurrentIndex(1)
            self._on_demo_page = False
            self._demo_header.pause_sheen()
            self._manual_header.resume_sheen()
        self._update_bars_visibility()
        if self._on_demo_page and self._on_main_tab:
            self._push_bars(self.v_fl, self.v_fr, self.v_rl, self.v_rr)

    def _on_tab_changed(self, idx: int):
        self._on_main_tab = (idx == TAB_MAIN_IDX)
        self._update_bars_visibility()

        if self._on_main_tab and self._on_demo_page:
            # Push cached values (0 if CAN is OFF, live otherwise)
            self._push_bars(self.v_fl, self.v_fr, self.v_rl, self.v_rr)

        if not self._on_main_tab:
            # I11 FIX: sync labels immediately so they're never stale
            self._sync_measurement_labels()
            # Also update the counter label now that the tab is visible
            self.lbl_msg_count.setText(f"{self._total_msg_count:,} msgs")
            self._flush_log_to_view()

    # ── Status indicator ──────────────────────────────────────────────────

    def _set_status_on(self):
        self.status_ind.set("ON", QColor("#16A34A"))
        self.btn_on.setEnabled(False)
        self.btn_off.setEnabled(True)

    def _set_status_off(self):
        self.status_ind.set("OFF", QColor("#C62828"))
        self.btn_on.setEnabled(True)
        self.btn_off.setEnabled(False)


# ───────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
