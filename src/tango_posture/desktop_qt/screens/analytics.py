"""D13: local persisted deltas and coverage, not scores or calculated alignment."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...domain import METRICS, UNITS
from ..analytics_data import (
    MetricData,
    SessionData,
    finite_number,
    list_sessions,
    load_session,
    recordings_for_session,
)
from ..router import Route, Router
from ..service_bridge import ServiceBridge
from ..theme import COLOR_BASE, COLOR_BORDER, COLOR_TEXT_MUTED

METRIC_NAMES = {
    "head_forward": "Head forward",
    "head_inclination": "Head inclination",
    "trunk_inclination": "Trunk inclination",
}


def _seconds(value: object) -> str:
    number = finite_number(value)
    return f"{number:.2f} s" if number is not None and number >= 0 else "Unknown"


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


class AnalyticsScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.dataset: SessionData | None = None
        self.recordings: list[tuple[Path, int, list[float]]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Persisted Session Analytics")
        title.setProperty("class", "title")
        layout.addWidget(title)
        subtitle = QLabel(
            "Smoothed deltas relative to the session reference; not technique scores. "
            "Time: seconds since first persisted observation (pauses retained). "
            "Click a chart to seek a matching archived recording."
        )
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        toolbar = QHBoxLayout()
        self.session_combo = QComboBox()
        self.session_combo.setAccessibleName("Saved session dataset")
        self.session_combo.currentIndexChanged.connect(self._select_session)
        toolbar.addWidget(self.session_combo, 1)
        refresh_btn = QPushButton("Refresh sessions")
        refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(refresh_btn)
        self.align_btn = QPushButton("Align Time Series")
        self.align_btn.setEnabled(False)
        self.align_btn.setToolTip(
            "Unavailable: no compatible paired time series or calculated alignment is persisted. "
            "Session reference deltas are not temporal alignment."
        )
        toolbar.addWidget(self.align_btn)
        layout.addLayout(toolbar)
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        alignment_note = QLabel(
            "Time alignment unavailable; no offset or scale has been calculated."
        )
        layout.addWidget(alignment_note)
        event_label = QLabel("Persisted posture events · double-click to seek")
        layout.addWidget(event_label)
        self.event_table = QTableWidget(0, 5)
        self.event_table.setHorizontalHeaderLabels(
            ["Metric", "Onset", "Detected", "End", "Peak deviation"]
        )
        self.event_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.event_table.setMaximumHeight(155)
        self.event_table.cellDoubleClicked.connect(self._seek_event)
        layout.addWidget(self.event_table)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        cards = QVBoxLayout(content)
        self.charts: dict[str, MetricChart] = {}
        self.coverage_labels: dict[str, QLabel] = {}
        for metric in METRICS:
            card = QFrame()
            card.setProperty("class", "card")
            card_layout = QVBoxLayout(card)
            label = QLabel(
                f"{METRIC_NAMES[metric]} — smoothed delta ({UNITS[metric].replace('_', ' ')})"
            )
            card_layout.addWidget(label)
            coverage = QLabel()
            coverage.setWordWrap(True)
            card_layout.addWidget(coverage)
            chart = MetricChart()
            chart.seek_requested.connect(self._seek_to_recording)
            card_layout.addWidget(chart)
            cards.addWidget(card)
            self.charts[metric] = chart
            self.coverage_labels[metric] = coverage
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.refresh()

    def _clear(self, message: str) -> None:
        self.dataset = None
        self.recordings = []
        self.state_label.setText(message)
        self.event_table.setRowCount(0)
        for metric in METRICS:
            self.charts[metric].set_data(None)
            self.coverage_labels[metric].setText("Coverage: Unknown · Events: Unknown")

    def refresh(self) -> None:
        selected = self.session_combo.currentData()
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem("Select a saved session…", None)
        message = "Select a saved session to view persisted observations and coverage."
        try:
            sessions = list_sessions(Path(self.bridge.config.data_dir))
            for identifier, label in sessions:
                self.session_combo.addItem(label, identifier)
            if not sessions:
                message = (
                    "No saved sessions. Save a practice session with metric observations first."
                )
        except (OSError, sqlite3.Error, ValueError) as exc:
            message = f"Analytics unavailable: {exc}"
        index = self.session_combo.findData(selected) if selected else 0
        self.session_combo.setCurrentIndex(max(0, index))
        self.session_combo.blockSignals(False)
        self._clear(message)
        self.recordings = []
        if self.session_combo.currentData():
            self._select_session()

    def _select_session(self) -> None:
        identifier = self.session_combo.currentData()
        self._clear("Select a saved session to view persisted observations and coverage.")
        if not identifier:
            return
        try:
            dataset = load_session(Path(self.bridge.config.data_dir), identifier)
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            self._clear(f"No compatible dataset: {exc}")
            return
        self.dataset = dataset
        try:
            self.recordings = recordings_for_session(
                Path(self.bridge.config.data_dir), dataset.identifier
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.recordings = []
            self.bridge.status_message.emit(f"Session video navigation unavailable: {exc}")
        time_end = max(
            (t for data in dataset.metrics.values() for t, _ in data.points), default=0.0
        )
        note = (
            f"Lines break at null/unknown samples and gaps > {dataset.max_gap:g} s."
            if dataset.max_gap is not None
            else "Tracking gap limit unknown: samples are not connected."
        )
        self.state_label.setText(
            f"Session {dataset.identifier} · {dataset.status} · "
            f"Active duration: {_seconds(dataset.active_duration)}. "
            f"{note} Coverage below is the saved interval accounting, not sample counts."
        )
        self.event_table.setRowCount(len(dataset.events))
        for row, event in enumerate(dataset.events):
            values = (
                event.metric,
                _seconds(event.onset - dataset.origin_timestamp),
                _seconds(event.detection - dataset.origin_timestamp),
                _seconds(event.end - dataset.origin_timestamp) if event.end is not None else "Open",
                f"{event.peak_deviation:+.3g}" if event.peak_deviation is not None else "Unknown",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, event.onset)
                self.event_table.setItem(row, column, item)
        for metric, data in dataset.metrics.items():
            self.charts[metric].set_data(data, time_end, dataset.origin_timestamp)
            ratio = finite_number(data.summary.get("valid_coverage"))
            coverage = f"{ratio:.1%}" if ratio is not None and 0 <= ratio <= 1 else "Unknown"
            valid = _seconds(data.summary.get("valid_duration"))
            unknown = _seconds(data.summary.get("unknown_duration"))
            available = sum(value is not None for _, value in data.points)
            self.coverage_labels[metric].setText(
                f"Valid coverage: {coverage} · Valid: {valid} · Unknown: {unknown} · "
                f"Delta samples: {available}/{len(data.points)} · "
                f"Persisted events: {data.event_count} ({data.open_event_count} open)"
            )

    def _seek_to_recording(self, timestamp: float) -> None:
        match = next(
            (
                (path, index)
                for path, index, timestamps in self.recordings
                if timestamps and timestamps[0] <= timestamp <= timestamps[-1]
            ),
            None,
        )
        if match is None:
            self.bridge.status_message.emit(
                "No archived video frame covers this chart timestamp."
            )
            return
        path, index = match
        self.router.navigate(
            Route.REVIEW,
            {"archive_path": path, "segment_index": index, "timestamp": timestamp},
        )

    def _seek_event(self, row: int, _column: int) -> None:
        item = self.event_table.item(row, 0)
        if item is None:
            return
        timestamp = finite_number(item.data(Qt.ItemDataRole.UserRole))
        if timestamp is not None:
            self._seek_to_recording(timestamp)
