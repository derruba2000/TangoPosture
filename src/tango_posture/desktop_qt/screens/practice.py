"""Live practice and recording workspace screen."""

from __future__ import annotations

import queue
import re
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...archive import practice_directory, practice_path
from ...core.entities import ReferenceSummary
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
from ..practice_worker import PracticeThread, QtPracticeDisplay, QtPracticeWorker
from ..router import Router
from ..service_bridge import ServiceBridge, numpy_to_qimage
from ..theme import (
    COLOR_BASE,
    COLOR_BORDER,
    COLOR_BURGUNDY,
    COLOR_COPPER,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
)


class PracticeScreen(QWidget):
    stopped = Signal()

    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.state = SessionState()
        self._references: dict[str, ReferenceSummary] = {}
        self._active_reference: ReferenceSummary | None = None
        self._calibrating = False
        self._calibration_candidate = False
        self._calibration_saved = False
        self._thread: QThread | None = None
        self._worker: QtPracticeWorker | None = None
        self._display: QtPracticeDisplay | None = None
        self._save_pending = False
        self._worker_error: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        setup_card = QFrame()
        setup_card.setProperty("class", "card")
        setup_layout = QVBoxLayout(setup_card)
        setup_layout.setContentsMargins(12, 10, 12, 10)
        setup_layout.setSpacing(8)

        practice_row = QHBoxLayout()
        practice_row.addWidget(QLabel("Practice:"))
        self.session_title = QLineEdit(
            f"Practice_{time.strftime('%Y%m%d_%H%M%S')}"
        )
        self.session_title.setAccessibleName("Practice archive name")
        practice_row.addWidget(self.session_title, 1)
        practice_row.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        self.source_combo.addItem("Camera", "camera")
        self.source_combo.addItem("Video file", "video")
        self.source_combo.currentIndexChanged.connect(self._update_source_controls)
        practice_row.addWidget(self.source_combo)
        self.video_path = QLineEdit()
        self.video_path.setPlaceholderText("Choose a local video file")
        self.video_path.setAccessibleName("Local video path")
        practice_row.addWidget(self.video_path, 1)
        self.browse_video_btn = QPushButton("Browse...")
        self.browse_video_btn.clicked.connect(self._browse_video)
        practice_row.addWidget(self.browse_video_btn)
        setup_layout.addLayout(practice_row)

        reference_row = QHBoxLayout()
        reference_row.addWidget(QLabel("Reference:"))
        self.reference_combo = QComboBox()
        self.reference_combo.currentIndexChanged.connect(self._reference_changed)
        reference_row.addWidget(self.reference_combo, 1)
        self.confirm_camera_cb = QCheckBox("Camera placement unchanged")
        self.confirm_camera_cb.setToolTip(
            "Confirm that the physical camera placement matches the selected reference."
        )
        reference_row.addWidget(self.confirm_camera_cb)
        reference_row.addWidget(QLabel("New reference name:"))
        self.reference_name = QLineEdit("My reference")
        self.reference_name.setMaximumWidth(180)
        reference_row.addWidget(self.reference_name)
        setup_layout.addLayout(reference_row)
        layout.addWidget(setup_card)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(10)

        self.ref_info = QLabel("Select a saved reference to enable scoring.")
        self.ref_info.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        self.ref_info.setWordWrap(True)
        center_layout.addWidget(self.ref_info)

        self.stage = VideoStage(
            placeholder_text="Start a preview to open the selected camera or video."
        )
        center_layout.addWidget(self.stage, 1)

        self.status_label = QLabel("Camera and model are opened only after you start preview.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED};")
        center_layout.addWidget(self.status_label)

        controls_card = QFrame()
        controls_card.setProperty("class", "card")
        controls_layout = QVBoxLayout(controls_card)
        controls_layout.setContentsMargins(12, 10, 12, 10)
        controls_layout.setSpacing(8)

        control_row = QHBoxLayout()
        self.preview_btn = QPushButton("Start Preview")
        self.preview_btn.setProperty("class", "primary")
        self.preview_btn.clicked.connect(self._toggle_preview)
        control_row.addWidget(self.preview_btn)

        self.calibrate_btn = QPushButton("New Reference")
        self.calibrate_btn.clicked.connect(self._calibration_action)
        control_row.addWidget(self.calibrate_btn)

        self.accept_calibration_btn = QPushButton("Accept Reference")
        self.accept_calibration_btn.setEnabled(False)
        self.accept_calibration_btn.clicked.connect(self._accept_calibration)
        control_row.addWidget(self.accept_calibration_btn)

        self.score_btn = QPushButton("Start Scoring (S)")
        self.score_btn.setProperty("class", "accent")
        self.score_btn.clicked.connect(self._start_scoring)
        control_row.addWidget(self.score_btn)

        self.pause_btn = QPushButton("Pause Scoring (P)")
        self.pause_btn.clicked.connect(self._toggle_pause)
        control_row.addWidget(self.pause_btn)

        self.stop_score_btn = QPushButton("Stop Scoring (T)")
        self.stop_score_btn.clicked.connect(self._stop_scoring)
        control_row.addWidget(self.stop_score_btn)

        self.record_btn = QPushButton("Record (R)")
        self.record_btn.clicked.connect(self._toggle_recording)
        control_row.addWidget(self.record_btn)

        self.stop_btn = QPushButton("Stop & Save (E)")
        self.stop_btn.clicked.connect(self._stop_and_save)
        control_row.addWidget(self.stop_btn)
        controls_layout.addLayout(control_row)

        option_row = QHBoxLayout()
        self.auto_song_cb = QCheckBox("Automatically record songs")
        self.auto_song_cb.toggled.connect(self._set_auto_recording)
        option_row.addWidget(self.auto_song_cb)
        self.skeleton_btn = QCheckBox("Skeleton (V)")
        self.skeleton_btn.setChecked(True)
        self.skeleton_btn.toggled.connect(lambda checked: self._request_key("v"))
        option_row.addWidget(self.skeleton_btn)
        self.axis_btn = QCheckBox("Vertical axis (Y)")
        self.axis_btn.setChecked(True)
        self.axis_btn.toggled.connect(lambda checked: self._request_key("y"))
        option_row.addWidget(self.axis_btn)
        option_row.addStretch()

        self.abort_btn = QPushButton("Abort Recording (X)")
        self.abort_btn.setProperty("class", "danger")
        self.abort_btn.clicked.connect(self._abort_recording)
        option_row.addWidget(self.abort_btn)
        self.close_btn = QPushButton("Close Preview")
        self.close_btn.clicked.connect(self._close_preview)
        option_row.addWidget(self.close_btn)
        controls_layout.addLayout(option_row)
        center_layout.addWidget(controls_card)
        splitter.addWidget(center)

        inspector = QFrame()
        inspector.setProperty("class", "card")
        inspector.setMinimumWidth(280)
        inspector.setMaximumWidth(360)
        inspector_layout = QVBoxLayout(inspector)
        inspector_layout.setContentsMargins(16, 14, 16, 14)
        inspector_layout.setSpacing(14)

        title = QLabel("Live Observations")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        subtitle = QLabel("Evidence from the active capture; unknown values are not scores.")
        subtitle.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        self.beat_strip = BeatStrip()
        self.notes_box = QLabel("Live observations appear after preview starts.")
        self.notes_box.setWordWrap(True)
        self.notes_box.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.notes_box.setStyleSheet(
            f"background-color: {COLOR_BASE}; border: 1px solid {COLOR_BORDER}; "
            f"border-radius: 4px; padding: 12px; color: {COLOR_TEXT_PRIMARY}; font-size: 12px;"
        )
        inspector_layout.addWidget(title)
        inspector_layout.addWidget(subtitle)
        inspector_layout.addWidget(self.beat_strip)
        inspector_layout.addWidget(self.notes_box, 1)
        splitter.addWidget(inspector)
        splitter.setSizes([720, 320])

        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(33)
        self._frame_timer.timeout.connect(self._drain_display)
        self._shortcuts: list[QShortcut] = []
        for key, action in (
            ("R", self._toggle_recording),
            ("S", self._start_scoring),
            ("P", self._toggle_pause),
            ("E", self._stop_and_save),
            ("V", self.skeleton_btn.toggle),
            ("Y", self.axis_btn.toggle),
            ("X", self._abort_recording),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda action=action: self._run_shortcut(action))
            self._shortcuts.append(shortcut)
        self.refresh()
        self._update_source_controls()
        self._sync_controls()

    @property
    def worker_running(self) -> bool:
        return self._thread is not None

    def refresh(self) -> None:
        selected_id = self.reference_combo.currentData()
        references = self.bridge.list_references()
        self._references = {reference.id: reference for reference in references}
        self.reference_combo.blockSignals(True)
        self.reference_combo.clear()
        self.reference_combo.addItem("No reference (record only)", None)
        for reference in references:
            self.reference_combo.addItem(
                f"{reference.name} - {reference.side} side / {reference.facing} facing",
                reference.id,
            )
        if selected_id in self._references:
            self.reference_combo.setCurrentIndex(
                self.reference_combo.findData(selected_id)
            )
        self.reference_combo.blockSignals(False)
        self._reference_changed()

    def begin_calibration(self) -> None:
        if self.worker_running:
            self.bridge.status_message.emit(
                "Close the active preview before starting a new calibration."
            )
            return
        if self.source_combo.currentData() != "camera":
            self.bridge.status_message.emit(
                "New camera calibration requires the camera source. Select Camera first."
            )
            return
        name = self.reference_name.text().strip()
        if not name:
            self.status_label.setText("Enter a name for the new reference.")
            return
        self._launch_worker(calibration_only=True, reference_name=name)

    def stop_for_navigation(self) -> None:
        if self.worker_running and self._display is not None:
            self.status_label.setText(
                "Closing the practice preview before leaving; an active recording will be saved."
            )
            self._display.request("q")

    def _selected_reference(self) -> ReferenceSummary | None:
        identifier = self.reference_combo.currentData()
        return self._references.get(identifier) if isinstance(identifier, str) else None

    def _reference_changed(self, *_args: object) -> None:
        self.confirm_camera_cb.setChecked(False)
        reference = self._selected_reference()
        if reference is None:
            self.ref_info.setText(
                "No reference selected. Live preview and recording are available; scoring is disabled."
            )
        else:
            self.ref_info.setText(
                f"Reference: {reference.name} ({reference.side} side, "
                f"{reference.facing} facing). Confirm unchanged camera placement before scoring."
            )
        self._sync_controls()

    def _update_source_controls(self, *_args: object) -> None:
        is_video = self.source_combo.currentData() == "video"
        self.video_path.setEnabled(is_video and not self.worker_running)
        self.browse_video_btn.setEnabled(is_video and not self.worker_running)

    def _browse_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select a practice video",
            "",
            "Video files (*.mp4 *.mov *.avi *.mkv *.m4v *.webm);;All files (*)",
        )
        if path:
            self.video_path.setText(path)

    def _toggle_preview(self) -> None:
        if self.worker_running:
            self._close_preview()
        else:
            self._start_preview()

    def _start_preview(self) -> None:
        video: Path | None = None
        if self.source_combo.currentData() == "video":
            value = self.video_path.text().strip()
            if not value or not Path(value).is_file():
                self.status_label.setText("Choose an existing local video file.")
                return
            video = Path(value).expanduser().resolve()

        name = self.session_title.text().strip()
        try:
            practice_path(practice_directory(self.bridge.config.data_dir), name)
        except ValueError as error:
            self.status_label.setText(str(error))
            return

        selected = self._selected_reference()
        reference = selected if selected and self.confirm_camera_cb.isChecked() else None
        if selected and reference is None:
            self.bridge.status_message.emit(
                "Camera placement is not confirmed. Preview and recording will start without scoring."
            )
        self._launch_worker(
            reference=reference,
            video=video,
            practice_name=name,
        )

    def _launch_worker(
        self,
        *,
        reference: ReferenceSummary | None = None,
        video: Path | None = None,
        practice_name: str | None = None,
        calibration_only: bool = False,
        reference_name: str = "My reference",
    ) -> None:
        if self.worker_running:
            return
        display = QtPracticeDisplay()
        if self.auto_song_cb.isChecked() and not calibration_only:
            display.auto_recording.set()
        worker = QtPracticeWorker(
            self.bridge.config,
            display,
            reference_id=reference.id if reference else None,
            reference_path=reference.path if reference else None,
            video=video,
            practice_name=practice_name,
            calibration_only=calibration_only,
            reference_name=reference_name,
        )
        worker.setParent(self)
        thread = PracticeThread(worker, self)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._worker_finished)
        self._display = display
        self._worker = worker
        self._thread = thread
        self._active_reference = reference
        self._calibrating = calibration_only
        self._calibration_candidate = False
        self._calibration_saved = False
        self._save_pending = False
        self._worker_error = None
        if self.state.phase.value == "preview":
            self.state = close(self.state)
        source = SourceKind.VIDEO if video is not None else SourceKind.CAMERA
        self.state = ready(choose_source(SessionState(), source))
        if reference:
            self.state = accept_reference(self.state, reference.id)
        self.stage.clear_frame()
        self.notes_box.setText(
            "Starting camera and pose processing..." if not calibration_only
            else "Starting camera calibration..."
        )
        self.status_label.setText(
            "Starting local practice pipeline. No network service is used."
        )
        self._sync_controls()
        self._frame_timer.start()
        thread.start()

    def _calibration_action(self) -> None:
        if not self.worker_running:
            self.begin_calibration()
        elif self._calibrating and self._display is not None:
            self._display.request("c")
            self.status_label.setText(
                "Calibration started. Hold a steady side-on position until the capture completes."
            )

    def _accept_calibration(self) -> None:
        if self._display is None or not self._calibration_candidate:
            return
        self._display.request("a")
        self._calibration_candidate = False
        self.accept_calibration_btn.setEnabled(False)
        self.status_label.setText("Saving the accepted reference archive...")

    def _start_scoring(self) -> None:
        if self._display is None or self._calibrating:
            return
        if self._active_reference is None:
            self.status_label.setText(
                "Select a saved reference, confirm camera placement, and restart preview before scoring."
            )
            return
        if self.state.scoring.value != "off":
            return
        self._display.request("s")
        self.state = start_scoring(self.state)
        self.status_label.setText("Scoring started against the selected reference.")
        self._sync_controls()

    def _toggle_pause(self) -> None:
        if self._display is None:
            return
        if self.state.scoring.value == "active":
            self._display.request("p")
            self.state = pause_scoring(self.state)
            self.status_label.setText("Scoring paused; video preview and recording continue.")
        elif self.state.scoring.value == "paused":
            self._display.request("p")
            self.state = resume_scoring(self.state)
            self.status_label.setText("Scoring resumed.")
        self._sync_controls()

    def _stop_scoring(self) -> None:
        if self._display is None or self.state.scoring.value == "off":
            return
        self._display.request("t")
        self.state = stop_scoring(self.state)
        self.status_label.setText("Scoring stopped; any active video recording continues.")
        self._sync_controls()

    def _toggle_recording(self) -> None:
        if self._display is None or self._calibrating:
            return
        if self.state.recording:
            self._stop_and_save()
            return
        self._display.recording.set()
        self.state = start_recording(self.state)
        self.status_label.setText("Starting a new recording segment...")
        self._sync_controls()

    def _stop_and_save(self) -> None:
        if self._display is None or not self.state.recording:
            return
        self._save_pending = True
        self._display.request("e")
        self.status_label.setText("Saving recording segment to the practice archive...")
        self._sync_controls()

    def _abort_recording(self) -> None:
        if self._display is None or not self.state.recording:
            return
        answer = QMessageBox.question(
            self,
            "Abort current recording?",
            "Discard the unsaved current segment? Previously saved segments will be preserved.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self._display.request("x")
            self.status_label.setText(
                "Aborting current recording; previously saved archive segments are preserved."
            )
            self._sync_controls()

    def _close_preview(self) -> None:
        if self._display is None:
            return
        if self.state.recording:
            answer = QMessageBox.question(
                self,
                "Save and close preview?",
                "The active recording will be saved before the camera closes.",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer is not QMessageBox.StandardButton.Save:
                return
        self._display.request("q")
        self.status_label.setText("Closing preview and saving any active recording...")
        self._sync_controls()

    def _set_auto_recording(self, enabled: bool) -> None:
        if self._display is not None:
            if enabled:
                self._display.auto_recording.set()
            else:
                self._display.auto_recording.clear()

    def _request_key(self, key: str) -> None:
        if self._display is not None and not self._calibrating:
            self._display.request(key)

    @staticmethod
    def _run_shortcut(action: Callable[[], None]) -> None:
        focused = QApplication.focusWidget()
        while focused is not None:
            if isinstance(focused, QLineEdit):
                return
            focused = focused.parentWidget()
        action()

    def _drain_display(self) -> None:
        display = self._display
        if display is None:
            return
        try:
            frame = display.frames.get_nowait()
        except queue.Empty:
            frame = None
        if frame is not None:
            try:
                self.stage.set_frame(numpy_to_qimage(frame))
            except (ValueError, RuntimeError) as error:
                self.status_label.setText(f"Could not display camera frame: {error}")
                display.request("q")

        if isinstance(display.notes, str) and display.notes:
            self.notes_box.setText(display.notes)
            beat = re.search(r"Beat (\d+)/8 \((\d+(?:\.\d+)?) BPM\)", display.notes)
            if beat:
                self.beat_strip.update_beat(float(beat.group(2)), int(beat.group(1)), True)
            elif "Waiting for song to begin" in display.notes:
                self.beat_strip.update_beat(120.0, 1, False)

        while True:
            try:
                state = display.recording_states.get_nowait()
            except queue.Empty:
                break
            if state == "recording":
                if not self.state.recording:
                    self.state = start_recording(
                        self.state, max_auto=self.bridge.config.max_recordings
                    )
                self.stage.set_badge("RECORDING", COLOR_BURGUNDY)
            elif state == "ready":
                if self.state.recording:
                    self._save_pending = True
                self.stage.set_badge("SAVING", COLOR_COPPER)
            elif state == "limit":
                if self.state.recording:
                    self.state = abort_unsaved(self.state)
                self.status_label.setText("The configured recording limit has been reached.")
                self._sync_controls()

        while True:
            try:
                path, index = display.saved_segments.get_nowait()
            except queue.Empty:
                break
            if self.state.recording:
                self.state = stop_and_save(self.state)
            self._save_pending = False
            self.status_label.setText(
                f"Recording {index:02d} saved to {path.name}."
            )
            self.stage.clear_badge()

        while True:
            try:
                path = display.reports.get_nowait()
            except queue.Empty:
                break
            self.bridge.status_message.emit(f"Saved practice archive: {path.name}")

        while True:
            try:
                candidate_name = display.calibration_candidates.get_nowait()
            except queue.Empty:
                break
            self._calibration_candidate = True
            self.accept_calibration_btn.setEnabled(True)
            self.status_label.setText(
                f"Calibration '{candidate_name}' is ready. Accept it to save the reference."
            )

        while True:
            try:
                path = display.calibration_archives.get_nowait()
            except queue.Empty:
                break
            self._calibration_saved = True
            self.status_label.setText(f"Reference saved: {path.name}. Closing camera...")
            display.request("q")

    def _worker_failed(self, message: str) -> None:
        self._worker_error = message
        self.status_label.setText(f"Practice unavailable: {message}")
        self.bridge.status_message.emit(f"Practice unavailable: {message}")

    def _worker_finished(self) -> None:
        self._drain_display()
        display = self._display
        was_aborted = display is not None and display.aborted.is_set()
        if display is not None:
            if display.aborted.is_set() and self.state.recording:
                self.state = abort_unsaved(self.state)
                self._save_pending = False
                self.status_label.setText(
                    "Current recording discarded. Previously saved segments are preserved."
                )
            elif self.state.recording:
                self.state = abort_unsaved(self.state)
                self._save_pending = False
            if self.state.scoring.value != "off":
                self.state = stop_scoring(self.state)
            if self.state.phase.value == "preview":
                self.state = close(self.state)
        self._frame_timer.stop()
        self._display = None
        self._worker = None
        thread = self._thread
        self._thread = None
        self._active_reference = None
        self._calibrating = False
        self._calibration_candidate = False
        self.accept_calibration_btn.setEnabled(False)
        if self._calibration_saved:
            self.refresh()
            self.bridge.status_message.emit("Calibration saved and camera released.")
        elif self._worker_error:
            self.status_label.setText(
                f"Practice ended with error: {self._worker_error}. Previously saved segments are preserved."
            )
        elif was_aborted:
            self.status_label.setText(
                "Current recording discarded. Previously saved segments are preserved."
            )
        elif not self.status_label.text().startswith("Practice unavailable:"):
            self.status_label.setText("Practice preview closed.")
        self._worker_error = None
        self.stage.clear_badge()
        self._sync_controls()
        self.stopped.emit()
        if thread is not None:
            thread.deleteLater()

    def _sync_controls(self) -> None:
        running = self.worker_running
        calibrating = running and self._calibrating
        self.preview_btn.setText("Close Preview" if running and not calibrating else "Start Preview")
        self.preview_btn.setEnabled(not calibrating)
        self.calibrate_btn.setText(
            "Start Calibration (C)" if calibrating else "New Reference"
        )
        self.calibrate_btn.setEnabled(not running or calibrating)
        if calibrating and self._calibration_candidate:
            self.calibrate_btn.setEnabled(False)
        self.reference_combo.setEnabled(not running)
        self.confirm_camera_cb.setEnabled(not running)
        self.session_title.setEnabled(not running)
        self.source_combo.setEnabled(not running)
        self.reference_name.setEnabled(not running)
        video_source = self.source_combo.currentData() == "video"
        self.video_path.setEnabled(not running and video_source)
        self.browse_video_btn.setEnabled(not running and video_source)
        self.auto_song_cb.setEnabled(running and not calibrating)
        active = running and not calibrating
        self.score_btn.setEnabled(
            active and self._active_reference is not None and self.state.scoring.value == "off"
        )
        self.pause_btn.setEnabled(
            active and self.state.scoring.value in ("active", "paused")
        )
        self.pause_btn.setText(
            "Resume Scoring (P)" if self.state.scoring.value == "paused" else "Pause Scoring (P)"
        )
        self.stop_score_btn.setEnabled(active and self.state.scoring.value != "off")
        self.record_btn.setEnabled(active and not self._save_pending)
        self.record_btn.setText(
            "Recording..." if self._save_pending else
            "Stop Recording" if self.state.recording else "Record (R)"
        )
        self.stop_btn.setEnabled(active and self.state.recording and not self._save_pending)
        self.abort_btn.setEnabled(active and self.state.recording)
        self.close_btn.setEnabled(active)
        self.skeleton_btn.setEnabled(active)
        self.axis_btn.setEnabled(active)
