"""Reusable chart of timestamped evidence without gap interpolation."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QWidget,
)

from ..analytics.data import (
    MetricData,
)
from ..theme import COLOR_BASE, COLOR_BORDER, COLOR_TEXT_MUTED


class MetricChart(QWidget):
    """Draw stored samples without interpolating across missing coverage."""

    seek_requested = Signal(float)

    def __init__(self) -> None:
        super().__init__()
        self.data: MetricData | None = None
        self.time_end = 0.0
        self.origin_timestamp = 0.0
        self.setMinimumHeight(155)
        self.setEnabled(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Persisted smoothed delta chart")

    def set_data(
        self, data: MetricData | None, time_end: float = 0.0, origin_timestamp: float = 0.0
    ) -> None:
        self.data, self.time_end, self.origin_timestamp = data, time_end, origin_timestamp
        self.setEnabled(data is not None and bool(data.segments))
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        left, right = 62.0, float(max(63, self.width() - 20))
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.isEnabled()
            and self.time_end > 0
            and left <= event.position().x() <= right
        ):
            fraction = (event.position().x() - left) / (right - left)
            self.seek_requested.emit(self.origin_timestamp + fraction * self.time_end)
        super().mousePressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLOR_BASE))
        painter.setPen(QColor(COLOR_TEXT_MUTED))
        if self.data is None or not self.data.segments:
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "No valid persisted deltas — unknown"
            )
            return
        plot = QRectF(62, 12, max(1, self.width() - 82), max(1, self.height() - 50))
        values = [value for segment in self.data.segments for _, value in segment]
        low, high = min(values), max(values)
        padding = max((high - low) * 0.1, 0.001)
        low, high = low - padding, high + padding
        duration = self.time_end or 1.0

        def point(timestamp: float, value: float) -> QPointF:
            return QPointF(
                plot.left() + timestamp / duration * plot.width(),
                plot.bottom() - (value - low) / (high - low) * plot.height(),
            )

        painter.setPen(QColor(COLOR_BORDER))
        painter.drawRect(plot)
        painter.setPen(QColor(COLOR_TEXT_MUTED))
        painter.drawText(2, int(plot.top()) + 8, f"{high:.3g}")
        painter.drawText(2, int(plot.bottom()), f"{low:.3g}")
        painter.drawText(int(plot.left()), self.height() - 8, "0 s")
        painter.drawText(int(plot.right()) - 90, self.height() - 8, f"{self.time_end:.2f} s")
        painter.setPen(QPen(QColor("#69B9CF"), 1.5))
        for segment in self.data.segments:
            previous: QPointF | None = None
            for timestamp, value in segment:
                current = point(timestamp, value)
                if previous is not None:
                    painter.drawLine(previous, current)
                painter.drawEllipse(current, 2, 2)
                previous = current
