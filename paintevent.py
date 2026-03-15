def paintEvent(self, event):
    p = QPainter(self)
    p.setRenderHint(QPainter.Antialiasing, True)

    W = self.width()
    H = self.height()

    # Reserve fixed pixel rows at the bottom:
    #   bottom 14px  → value text  (e.g. "-75 Nm")
    #   next   16px  → label text  (e.g. "FL")
    #   2px gap between label and bar
    LABEL_H = 16
    VALUE_H = 14
    GAP     = 2
    BOTTOM_RESERVED = LABEL_H + VALUE_H + GAP

    outer = QRect(18, 10, W - 36, H - BOTTOM_RESERVED - 10)

    # Dark track background
    p.fillRect(outer, QColor("#0D1520"))
    p.setPen(QPen(QColor("#1E3A5C"), 1))
    p.drawRoundedRect(outer, 6, 6)

    inner = outer.adjusted(4, 4, -4, -4)
    zero_y = inner.center().y()

    # Zero line
    p.setPen(QPen(QColor("#2A4A6A"), 1))
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
            # Glow tip
            tip = QRect(inner.left(), top, inner.width(), min(6, h))
            p.setBrush(QColor(0, 229, 255, 180))
            p.drawRoundedRect(tip, 3, 3)

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

    # Tick at zero
    p.setPen(QColor("#3A5A7A"))
    p.drawLine(outer.left() - 6, int(zero_y), outer.left(), int(zero_y))

    # ── Label row (e.g. "FL") — sits directly below the bar track ────────
    label_rect = QRect(0, H - BOTTOM_RESERVED, W, LABEL_H)
    p.setPen(QColor("#00BCD4"))
    p.setFont(QFont("Segoe UI", 8, QFont.Bold))
    p.drawText(label_rect, Qt.AlignHCenter | Qt.AlignVCenter, self._label)

    # ── Value row (e.g. "-75 Nm") — sits at the very bottom ──────────────
    val_rect = QRect(0, H - VALUE_H, W, VALUE_H)
    val_color = QColor("#00E5FF") if self._value >= 0 else QColor("#FF6B35")
    p.setPen(val_color)
    p.setFont(QFont("Segoe UI", 8, QFont.Bold))
    p.drawText(val_rect, Qt.AlignHCenter | Qt.AlignVCenter, f"{self._value} Nm")
