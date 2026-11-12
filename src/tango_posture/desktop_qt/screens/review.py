"""Review and replay workspace screen."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..components.video_stage import VideoStage
from ..router import Route, Router
from ..service_bridge import ServiceBridge
from ..theme import COLOR_TEXT_MUTED, COLOR_TEXT_PRIMARY


class ReviewScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.current_archive_path: Path | None = None
        self.is_playing = False
        self.current_time_sec = 0.0
        self.duration_sec = 60.0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # Center Video & Transport
        video_col = QWidget()
        vc_layout = QVBoxLayout(video_col)
        vc_layout.setContentsMargins(0, 0, 0, 0)
        vc_layout.setSpacing(10)

        # Top Bar
        top_bar = QFrame()
        top_bar.setProperty("class", "card")
        tb_layout = QHBoxLayout(top_bar)
        tb_layout.setContentsMargins(12, 8, 12, 8)
        tb_layout.setSpacing(10)

        self.stream_combo = QComboBox()
        self.stream_combo.addItems(["Overlay video", "Raw video", "Full lines + messages"])

        open_btn = QPushButton("Open Video…")
        open_btn.clicked.connect(self._open_file)

        analytics_btn = QPushButton("View Analytics")
        analytics_btn.clicked.connect(lambda: self.router.navigate(Route.ANALYTICS))

        self.compare_cb = QCheckBox("Compare reference")
        self.compare_cb.toggled.connect(self._toggle_comparison)

        tb_layout.addWidget(QLabel("Stream:"))
        tb_layout.addWidget(self.stream_combo)
        tb_layout.addWidget(open_btn)
        tb_layout.addWidget(self.compare_cb)
        tb_layout.addStretch()
        tb_layout.addWidget(analytics_btn)
        vc_layout.addWidget(top_bar)

        # Video Stage
        self.stage_container = QWidget()
        self.stage_layout = QHBoxLayout(self.stage_container)
        self.stage_layout.setContentsMargins(0, 0, 0, 0)

        self.primary_stage = VideoStage("Select a practice archive or open a video to replay.")
        self.reference_stage = VideoStage("Reference video comparison")
        self.reference_stage.setVisible(False)

        self.stage_layout.addWidget(self.primary_stage, 1)
        self.stage_layout.addWidget(self.reference_stage, 1)
        vc_layout.addWidget(self.stage_container, 1)

        # Transport Bar
        transport_card = QFrame()
        transport_card.setProperty("class", "card")
        tr_layout = QHBoxLayout(transport_card)
        tr_layout.setContentsMargins(12, 8, 12, 8)
        tr_layout.setSpacing(12)

        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self._toggle_playback)

        self.time_label = QLabel("00:00.0 / 01:00.0")
        self.time_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")

        self.scrubber = QSlider(Qt.Orientation.Horizontal)
        self.scrubber.setRange(0, 600)
        self.scrubber.valueChanged.connect(self._on_scrubber_changed)

        tr_layout.addWidget(self.play_btn)
        tr_layout.addWidget(self.scrubber, 1)
        tr_layout.addWidget(self.time_label)
        vc_layout.addWidget(transport_card)

        splitter.addWidget(video_col)

        # Right Inspector Panel
        inspector = QFrame()
        inspector.setProperty("class", "card")
        inspector.setMinimumWidth(300)
        inspector.setMaximumWidth(420)
        insp_layout = QVBoxLayout(inspector)
        insp_layout.setContentsMargins(14, 12, 14, 12)

        tabs = QTabWidget()
        # Tab 1: Frame Insights
        insights_tab = QWidget()
        it_layout = QVBoxLayout(insights_tab)
        it_layout.setContentsMargins(8, 10, 8, 10)
        self.insights_text = QLabel("Observations synchronized with video time will display here.")
        self.insights_text.setWordWrap(True)
        self.insights_text.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 12px;")
        it_layout.addWidget(self.insights_text)
        it_layout.addStretch()

        # Tab 2: Report Markdown
        report_tab = QWidget()
        rt_layout = QVBoxLayout(report_tab)
        rt_layout.setContentsMargins(8, 10, 8, 10)
        self.report_view = QTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setPlaceholderText("Report summary and metric measurements will show here.")
        rt_layout.addWidget(self.report_view)

        tabs.addTab(insights_tab, "Insights")
        tabs.addTab(report_tab, "Report")
        insp_layout.addWidget(tabs)

        splitter.addWidget(inspector)
        splitter.setSizes([720, 340])

        # Playback timer
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._on_tick)

    def load_archive(self, path: Path) -> None:
        self.current_archive_path = path
        self.primary_stage.clear_badge()
        self.insights_text.setText(f"Loaded: {path.name}\n\nFrame observations ready.")
        self.report_view.setPlainText(f"# Practice: {path.stem}\n\nReady for inspection.")

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open video file",
            "",
            "Video files (*.mp4 *.mov *.avi *.tp);;All files (*)",
        )
        if path:
            self.load_archive(Path(path))

    def _toggle_comparison(self, checked: bool) -> None:
        self.reference_stage.setVisible(checked)

    def _toggle_playback(self) -> None:
        self.is_playing = not self.is_playing
        self.play_btn.setText("⏸ Pause" if self.is_playing else "▶ Play")
        if self.is_playing:
            self.timer.start()
        else:
            self.timer.stop()

    def _on_tick(self) -> None:
        self.current_time_sec += 0.1
        if self.current_time_sec > self.duration_sec:
            self.current_time_sec = 0.0
        self.scrubber.blockSignals(True)
        self.scrubber.setValue(int(self.current_time_sec * 10))
        self.scrubber.blockSignals(False)
        self._update_time_label()

    def _on_scrubber_changed(self, value: int) -> None:
        self.current_time_sec = value / 10.0
        self._update_time_label()

    def _update_time_label(self) -> None:
        cur_m = int(self.current_time_sec // 60)
        cur_s = self.current_time_sec % 60
        dur_m = int(self.duration_sec // 60)
        dur_s = self.duration_sec % 60
        self.time_label.setText(f"{cur_m:02d}:{cur_s:04.1f} / {dur_m:02d}:{dur_s:04.1f}")
