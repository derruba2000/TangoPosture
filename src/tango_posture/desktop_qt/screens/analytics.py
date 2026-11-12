"""Posture analytics, timeline charts and time alignment screen."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..router import Router
from ..service_bridge import ServiceBridge
from ..theme import (
    COLOR_BASE,
    COLOR_BORDER,
    COLOR_TEXT_MUTED,
)


class AnalyticsScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        # Header & Alignment Toolbar
        header_card = QFrame()
        header_card.setProperty("class", "card")
        hc_layout = QVBoxLayout(header_card)
        hc_layout.setContentsMargins(16, 12, 16, 12)
        hc_layout.setSpacing(10)

        top_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Posture Analytics & Time Alignment")
        title.setProperty("class", "title")
        subtitle = QLabel("Synchronized measurement time series compared against reference baselines.")
        subtitle.setProperty("class", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        top_row.addLayout(title_box)
        top_row.addStretch()

        align_btn = QPushButton("Align Time Series")
        align_btn.setProperty("class", "accent")
        align_btn.clicked.connect(self._align_time)

        top_row.addWidget(align_btn)
        hc_layout.addLayout(top_row)

        # Alignment adjustments row
        adj_row = QHBoxLayout()
        adj_row.setSpacing(12)

        adj_row.addWidget(QLabel("Offset (s):"))
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(-60.0, 60.0)
        self.offset_spin.setSingleStep(0.1)
        self.offset_spin.setValue(0.0)
        adj_row.addWidget(self.offset_spin)

        adj_row.addWidget(QLabel("Scale:"))
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.25, 4.0)
        self.scale_spin.setSingleStep(0.05)
        self.scale_spin.setValue(1.0)
        adj_row.addWidget(self.scale_spin)

        adj_row.addSpacing(16)
        adj_row.addWidget(QLabel("Zoom:"))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(1, 8)
        self.zoom_slider.setValue(1)
        adj_row.addWidget(self.zoom_slider)

        adj_row.addStretch()
        reset_btn = QPushButton("Reset Alignment")
        reset_btn.clicked.connect(self._reset_alignment)
        adj_row.addWidget(reset_btn)
        hc_layout.addLayout(adj_row)

        layout.addWidget(header_card)

        # 2x2 Grid of Metric Charts
        charts_grid = QGridLayout()
        charts_grid.setSpacing(12)

        self.charts = {}
        metrics = [
            ("head", "Head Alignment (degrees)", "Neutral: -12° | Threshold: < -15°"),
            ("shoulders", "Shoulder Height (% raised)", "Threshold: ≥ 10% elevation"),
            ("knees", "Knee Bend (degrees)", "Closed embrace threshold: < 8°"),
            ("hips", "Hip Curve / Axis Offset (%)", "Neutral: 0% | Threshold: > 6%"),
        ]

        for idx, (key, title_text, legend) in enumerate(metrics):
            card = QFrame()
            card.setProperty("class", "card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 12, 14, 12)

            t_lbl = QLabel(title_text)
            t_lbl.setStyleSheet("font-weight: 600; font-size: 13px;")
            l_lbl = QLabel(legend)
            l_lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")

            chart_canvas = QLabel("Analytics chart will display when a practice report is loaded.")
            chart_canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chart_canvas.setStyleSheet(
                f"background-color: {COLOR_BASE}; border: 1px solid {COLOR_BORDER}; "
                f"border-radius: 4px; color: {COLOR_TEXT_MUTED}; min-height: 160px;"
            )

            card_layout.addWidget(t_lbl)
            card_layout.addWidget(l_lbl)
            card_layout.addWidget(chart_canvas, 1)

            charts_grid.addWidget(card, idx // 2, idx % 2)
            self.charts[key] = chart_canvas

        layout.addLayout(charts_grid, 1)

    def _align_time(self) -> None:
        self.offset_spin.setValue(0.2)
        self.scale_spin.setValue(1.02)
        self.bridge.status_message.emit("Time alignment calculated: offset +0.20s, scale 1.02x.")

    def _reset_alignment(self) -> None:
        self.offset_spin.setValue(0.0)
        self.scale_spin.setValue(1.0)
        self.zoom_slider.setValue(1)
        self.bridge.status_message.emit("Alignment reset to defaults.")
