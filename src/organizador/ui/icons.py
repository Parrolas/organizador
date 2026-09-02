"""Small programmatic icon set, avoiding platform-dependent glyphs."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

# The app icon is deliberately theme-independent: one recognizable mark everywhere.
ICON_INK = "#08111D"
ICON_ACCENT = "#49CFC0"


def app_icon(size: int = 64) -> QIcon:
    """Draw the Organizador mark as a filed page inside an ink tile."""

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    tile = QRectF(size * 0.05, size * 0.05, size * 0.9, size * 0.9)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ICON_INK))
    painter.drawRoundedRect(tile, size * 0.19, size * 0.19)

    page = QPainterPath()
    page.moveTo(QPointF(size * 0.29, size * 0.22))
    page.lineTo(QPointF(size * 0.62, size * 0.22))
    page.lineTo(QPointF(size * 0.73, size * 0.34))
    page.lineTo(QPointF(size * 0.73, size * 0.76))
    page.lineTo(QPointF(size * 0.29, size * 0.76))
    page.closeSubpath()
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawPath(page)

    pen = QPen(QColor(ICON_ACCENT), max(2, int(size * 0.055)))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    for ratio in (0.43, 0.56, 0.69):
        painter.drawLine(QPointF(size * 0.39, size * ratio), QPointF(size * 0.64, size * ratio))
    painter.end()
    return QIcon(pixmap)
