"""Scalable vector icons for the Qt navigation rail."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from .theme import COLOR_COPPER, COLOR_TEXT_MUTED

_ICON_SIZE = 24
_ICON_DRAWERS: dict[str, Callable[[QPainter], None]]


def _draw_home(painter: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(3, 11)
    path.lineTo(12, 3)
    path.lineTo(21, 11)
    path.lineTo(19, 11)
    path.lineTo(19, 21)
    path.lineTo(5, 21)
    path.lineTo(5, 11)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawLine(10, 21, 10, 15)
    painter.drawLine(14, 21, 14, 15)
    painter.drawLine(10, 15, 14, 15)


def _draw_practice(painter: QPainter) -> None:
    painter.drawEllipse(QRectF(9, 2, 6, 6))
    path = QPainterPath()
    path.moveTo(11, 9)
    path.lineTo(7, 12)
    path.lineTo(4, 10)
    path.moveTo(12, 9)
    path.lineTo(16, 11)
    path.lineTo(20, 7)
    path.moveTo(12, 9)
    path.lineTo(12, 16)
    path.lineTo(8, 21)
    path.moveTo(12, 16)
    path.lineTo(17, 21)
    painter.drawPath(path)


def _draw_references(painter: QPainter) -> None:
    painter.drawEllipse(QRectF(3, 3, 18, 18))
    painter.drawEllipse(QRectF(7, 7, 10, 10))
    painter.drawEllipse(QRectF(10, 10, 4, 4))


def _draw_library(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(4, 3, 16, 18), 2, 2)
    painter.drawLine(8, 7, 16, 7)
    painter.drawLine(8, 11, 16, 11)
    painter.drawLine(8, 15, 16, 15)
    painter.drawLine(8, 19, 16, 19)
    painter.drawLine(6, 6, 6, 8)
    painter.drawLine(6, 10, 6, 12)
    painter.drawLine(6, 14, 6, 16)
    painter.drawLine(6, 18, 6, 20)


def _draw_review(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(2.5, 5, 19, 14), 3, 3)
    path = QPainterPath()
    path.moveTo(10, 8)
    path.lineTo(16, 12)
    path.lineTo(10, 16)
    path.closeSubpath()
    painter.drawPath(path)


def _draw_analytics(painter: QPainter) -> None:
    painter.drawLine(4, 20, 4, 5)
    painter.drawLine(4, 20, 21, 20)
    painter.drawLine(7, 17, 7, 13)
    painter.drawLine(11, 17, 11, 9)
    painter.drawLine(15, 17, 15, 11)
    painter.drawLine(19, 17, 19, 6)
    path = QPainterPath()
    path.moveTo(6, 11)
    path.lineTo(10, 7)
    path.lineTo(14, 9)
    path.lineTo(19, 4)
    painter.drawPath(path)


def _draw_coach(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(3, 4, 18, 14), 4, 4)
    path = QPainterPath()
    path.moveTo(8, 18)
    path.lineTo(6, 21)
    path.lineTo(13, 18)
    painter.drawPath(path)
    painter.drawLine(8, 9, 16, 9)
    painter.drawLine(8, 13, 13, 13)


def _draw_settings(painter: QPainter) -> None:
    painter.drawEllipse(QRectF(7, 7, 10, 10))
    painter.drawEllipse(QRectF(10, 10, 4, 4))
    for x1, y1, x2, y2 in (
        (12, 2, 12, 5),
        (12, 19, 12, 22),
        (2, 12, 5, 12),
        (19, 12, 22, 12),
        (5, 5, 7, 7),
        (17, 17, 19, 19),
        (5, 19, 7, 17),
        (17, 7, 19, 5),
    ):
        painter.drawLine(x1, y1, x2, y2)


def _draw_help(painter: QPainter) -> None:
    painter.drawEllipse(QRectF(3, 3, 18, 18))
    path = QPainterPath()
    path.moveTo(9, 9)
    path.cubicTo(9, 5, 15, 5, 15, 9)
    path.cubicTo(15, 11, 12, 11, 12, 14)
    painter.drawPath(path)
    painter.drawEllipse(QRectF(11.25, 16.5, 1.5, 1.5))


def _draw_about(painter: QPainter) -> None:
    painter.drawEllipse(QRectF(3, 3, 18, 18))
    painter.drawEllipse(QRectF(11, 7, 2, 2))
    painter.drawLine(12, 11, 12, 17)


_ICON_DRAWERS = {
    "home": _draw_home,
    "practice": _draw_practice,
    "references": _draw_references,
    "library": _draw_library,
    "review": _draw_review,
    "analytics": _draw_analytics,
    "coach": _draw_coach,
    "settings": _draw_settings,
    "help": _draw_help,
    "about": _draw_about,
}


def navigation_icon(name: str) -> QIcon:
    """Create a consistent line icon with inactive, hover, and selected colors."""
    drawer = _ICON_DRAWERS[name]
    icon = QIcon()
    for color, mode, state in (
        (COLOR_TEXT_MUTED, QIcon.Mode.Normal, QIcon.State.Off),
        (COLOR_COPPER, QIcon.Mode.Active, QIcon.State.Off),
        ("#FFFFFF", QIcon.Mode.Normal, QIcon.State.On),
        ("#FFFFFF", QIcon.Mode.Selected, QIcon.State.On),
    ):
        pixmap = QPixmap(_ICON_SIZE, _ICON_SIZE)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(
            QPen(
                QColor(color),
                1.8,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        drawer(painter)
        painter.end()
        icon.addPixmap(pixmap, mode, state)
    return icon
