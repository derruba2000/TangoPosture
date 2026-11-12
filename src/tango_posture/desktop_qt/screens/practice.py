"""Live practice and recording workspace screen."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...core.session_state import (
    SessionState,
    abort_unsaved,
    accept_reference,
    choose_source,
    close,
    pause_scoring,
    ready,
    resume_scoring,
    start_recording,
    start_scoring,
    stop_and_save,
    stop_scoring,
)
from ...core.types import SourceKind
from ..components.beat_strip import BeatStrip
from ..components.video_stage import VideoStage
from ..router import Route, Router
from ..service_bridge import ServiceBridge
from ..theme import (
    COLOR_BASE,
    COLOR_BORDER,
    COLOR_BURGUNDY,
    COLOR_COPPER,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
)


class PracticeScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.state = SessionState()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # Left / Center Area: Video Stage & Controls
        center_widget = QWidget()
        center_layout = QVBoxLayout(center_widget)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(10)

        # Top Bar: Setup info & status
        top_bar = QFrame()
        top_bar.setProperty("class", "card")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 8, 12, 8)

        self.session_title = QLabel(f"Practice_{time.strftime('%Y%m%d_%H%M%S')}")
        self.session_title.setStyleSheet("font-weight: bold; font-size: 14px;")

        self.ref_info = QLabel("Reference: Loading...")
        self.ref_info.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")

        self.auto_song_cb = QCheckBox("Auto-record songs")
        self.auto_song_cb.setChecked(False)

        top_layout.addWidget(self.session_title)
        top_layout.addSpacing(16)
        top_layout.addWidget(self.ref_info)
        top_layout.addStretch()
        top_layout.addWidget(self.auto_song_cb)
        center_layout.addWidget(top_bar)

        # Video Canvas
        self.stage = VideoStage(placeholder_text="Camera preview ready. Use controls below to start scoring.")
        center_layout.addWidget(self.stage, 1)

        # Bottom Controls Frame
        controls_card = QFrame()
        controls_card.setProperty("class", "card")
        ctrl_layout = QVBoxLayout(controls_card)
        ctrl_layout.setContentsMargins(12, 10, 12, 10)
        ctrl_layout.setSpacing(8)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.record_btn = QPushButton("Record (R)")
        self.record_btn.setProperty("class", "primary")
        self.record_btn.clicked.connect(self._toggle_recording)

        self.score_btn = QPushButton("Start Scoring (S)")
        self.score_btn.setProperty("class", "accent")
        self.score_btn.clicked.connect(self._toggle_scoring)

        self.pause_btn = QPushButton("Pause (P)")
        self.pause_btn.clicked.connect(self._toggle_pause)

        self.stop_btn = QPushButton("Stop & Save (E)")
        self.stop_btn.clicked.connect(self._stop_and_save)

        self.skeleton_btn = QPushButton("Skeleton (V)")
        self.skeleton_btn.setCheckable(True)

        self.axis_btn = QPushButton("Axis (Y)")
        self.axis_btn.setCheckable(True)

        self.abort_btn = QPushButton("Abort (X)")
        self.abort_btn.setProperty("class", "danger")
        self.abort_btn.clicked.connect(self._abort_recording)

        self.quit_btn = QPushButton("Close")
        self.quit_btn.clicked.connect(self._quit_session)

        btn_row.addWidget(self.score_btn)
        btn_row.addWidget(self.record_btn)
        btn_row.addWidget(self.pause_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addSpacing(12)
        btn_row.addWidget(self.skeleton_btn)
        btn_row.addWidget(self.axis_btn)
        btn_row.addStretch()
        btn_row.addWidget(self.abort_btn)
        btn_row.addWidget(self.quit_btn)

        ctrl_layout.addLayout(btn_row)
        center_layout.addWidget(controls_card)
        splitter.addWidget(center_widget)

        # Right Inspector: Insights & Metrics
        inspector = QFrame()
        inspector.setProperty("class", "card")
        inspector.setMinimumWidth(280)
        inspector.setMaximumWidth(360)
        insp_layout = QVBoxLayout(inspector)
        insp_layout.setContentsMargins(16, 14, 16, 14)
        insp_layout.setSpacing(14)

        insp_title = QLabel("Observation Insights")
        insp_title.setStyleSheet("font-size: 15px; font-weight: bold;")
        insp_subtitle = QLabel("Live observations relative to reference")
        insp_subtitle.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")

        self.beat_strip = BeatStrip()

        self.notes_box = QLabel("Frame observations will update when practice begins.")
        self.notes_box.setWordWrap(True)
        self.notes_box.setStyleSheet(
            f"background-color: {COLOR_BASE}; border: 1px solid {COLOR_BORDER}; "
            f"border-radius: 4px; padding: 12px; color: {COLOR_TEXT_PRIMARY}; font-size: 12px;"
        )

        insp_layout.addWidget(insp_title)
        insp_layout.addWidget(insp_subtitle)
        insp_layout.addWidget(self.beat_strip)
        insp_layout.addWidget(self.notes_box, 1)

        splitter.addWidget(inspector)
        splitter.setSizes([720, 320])

        self._init_session()

    def _init_session(self) -> None:
        self.state = ready(choose_source(SessionState(), SourceKind.CAMERA))
        refs = self.bridge.list_references()
        if refs:
            self.state = accept_reference(self.state, refs[0].id)
            self.ref_info.setText(f"Reference: {refs[0].name} ({refs[0].side})")
        else:
            self.ref_info.setText("Reference: None (Calibrate before scoring)")
        self._sync_ui()

    def _sync_ui(self) -> None:
        rec = self.state.recording
        scoring = self.state.scoring.value
        self.record_btn.setText("Stop Recording" if rec else "Record (R)")
        self.record_btn.setEnabled(True)
        self.score_btn.setText("Stop Scoring (S)" if scoring == "active" else "Start Scoring (S)")
        self.pause_btn.setEnabled(scoring in ("active", "paused"))
        self.pause_btn.setText("Resume (P)" if scoring == "paused" else "Pause (P)")
        self.stop_btn.setEnabled(rec)
        self.abort_btn.setEnabled(rec)

        if rec:
            self.stage.set_badge("● RECORDING", COLOR_BURGUNDY)
        elif scoring == "active":
            self.stage.set_badge("SCORING", COLOR_COPPER)
        else:
            self.stage.clear_badge()

    def _toggle_recording(self) -> None:
        if self.state.recording:
            self.state = stop_and_save(self.state)
        else:
            self.state = start_recording(self.state)
        self._sync_ui()

    def _toggle_scoring(self) -> None:
        if self.state.scoring.value == "off":
            if not self.state.reference_id:
                self.notes_box.setText("Please select or capture a reference first.")
                return
            self.state = start_scoring(self.state)
        else:
            self.state = stop_scoring(self.state)
        self._sync_ui()

    def _toggle_pause(self) -> None:
        if self.state.scoring.value == "active":
            self.state = pause_scoring(self.state)
        elif self.state.scoring.value == "paused":
            self.state = resume_scoring(self.state)
        self._sync_ui()

    def _stop_and_save(self) -> None:
        if self.state.recording:
            self.state = stop_and_save(self.state)
            self.notes_box.setText(f"Segment saved. Total segments: {self.state.saved_segments}")
        self._sync_ui()

    def _abort_recording(self) -> None:
        if self.state.recording:
            self.state = abort_unsaved(self.state)
            self.notes_box.setText("Recording discarded. Previous segments preserved.")
        self._sync_ui()

    def _quit_session(self) -> None:
        if self.state.recording:
            self.state = stop_and_save(self.state)
        self.state = close(self.state)
        self.router.navigate(Route.HOME)
