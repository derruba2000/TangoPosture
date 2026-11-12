"""High-performance video and overlay canvas using QPainter."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from ..theme import COLOR_BASE, COLOR_BORDER, COLOR_BURGUNDY, COLOR_TEXT_MUTED


class VideoStage(QWidget):
    """Central video display widget supporting arbitrary aspect ratios."""

    def __init__(self, placeholder_text: str = "Video preview will appear here", parent: QWidget | None = None):
        super().__init__(parent)
        self._image: QImage | None = None
        self._placeholder: str = placeholder_text
        self._badge_text: str = ""
        self._badge_color: str = COLOR_BURGUNDY
        self.setMinimumSize(320, 240)
        self.setStyleSheet(f"background-color: {COLOR_BASE}; border: 1px solid {COLOR_BORDER}; border-radius: 4px;")

    def set_frame(self, image: QImage) -> None:
        self._image = image
        self.update()

    def clear_frame(self) -> None:
        self._image = None
        self.update()

    def set_badge(self, text: str, color: str = COLOR_BURGUNDY) -> None:
        self._badge_text = text
        self._badge_color = color
        self.update()

    def clear_badge(self) -> None:
        self._badge_text = ""
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        rect = self.rect()
        painter.fillRect(rect, QColor(COLOR_BASE))

        if self._image is None or self._image.isNull():
            # Draw placeholder message
            painter.setPen(QColor(COLOR_TEXT_MUTED))
            font = QFont()
            font.setPointSize(13)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._placeholder)
        else:
            # Aspect-fit calculation
            img_size = self._image.size()
            scaled_size = img_size.scaled(rect.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target_x = rect.x() + (rect.width() - scaled_size.width()) // 2
            target_y = rect.y() + (rect.height() - scaled_size.height()) // 2
            target_rect = QRect(QPoint(target_x, target_y), scaled_size)

            painter.drawImage(target_rect, self._image)

        # Draw status badge if present (e.g. REC indicator)
        if self._badge_text:
            badge_font = QFont()
            badge_font.setPointSize(11)
            badge_font.setBold(True)
            painter.setFont(badge_font)

            badge_w = 120
            badge_h = 28
            badge_rect = QRect(rect.x() + 16, rect.y() + 16, badge_w, badge_h)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self._badge_color))
            painter.drawRoundedRect(badge_rect, 4, 4)

            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, self._badge_text)

        painter.end()
