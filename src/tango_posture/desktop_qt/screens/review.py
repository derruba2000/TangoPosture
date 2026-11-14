"""Review and replay workspace screen."""

from __future__ import annotations

import queue
import re
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from typing import Any
from zipfile import BadZipFile

import cv2
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QHideEvent, QImage, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...archive import file_sha256, read_analytics, read_movement_analysis, store_analytics
from ...calibration.baseline import load_calibration_file
from ...movement import render_floor_guide, render_movement_overlay, validate_movement_analysis
from ...references.coach import (
    coach_notes_for_frame,
    compare_coach_data,
    compare_default_coach_data,
    interpolate_coach_data,
    render_coach_frame_overlay,
)
from ...references.companions import load_report_coach_data, load_report_movement_data
from ...references.constants import MAX_IMPORT_RANGE_SECONDS
from ...replay import time_label
from ..components.video_stage import VideoStage
from ..review.analysis import ReviewAnalysis, analyse_review_clip
from ..review.playback import VIDEO_SUFFIXES, ReviewPlayback
from ..router import Route, Router
from ..service_bridge import ServiceBridge
from ..theme import (
    COLOR_DANGER,
    COLOR_SUCCESS,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
    COLOR_WARNING,
)

REPLAY_ERRORS = (OSError, ValueError, RuntimeError, BadZipFile, cv2.error)


class ReviewScreen(QWidget):
    def __init__(
        self,
        bridge: ServiceBridge,
        router: Router,
        parent: QWidget | None = None,
        *,
        playback: ReviewPlayback | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.current_archive_path: Path | None = None
        self._reopen_path: Path | None = None
        self.is_playing = False
        self.current_time_sec = 0.0
        self.duration_sec = 0.0
        self._playback = playback if playback is not None else ReviewPlayback()
        self._comparison = ReviewPlayback()
        self._clock = clock
        self._started_at = 0.0
        self._playback_rate = 1.0
        self._coach_data: dict[str, Any] | None = None
        self._compared_coach_data: dict[str, Any] | None = None
        self._calibration_error = ""
        self._movement_data: dict[str, Any] | None = None
        self._analysis_events: queue.SimpleQueue[tuple[int, str, object]] = queue.SimpleQueue()
        self._analysis_cancel: Event | None = None
        self._analysis_thread: Thread | None = None
        self._analysis_job_id = 0
        self._active_analysis_job: int | None = None
        self.destroyed.connect(self._playback.close)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(self.splitter)

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
        self.stream_combo.currentTextChanged.connect(self._on_stream_changed)
        self.stream_combo.setEnabled(False)
        self.segment_combo = QComboBox()
        self.segment_combo.currentIndexChanged.connect(self._on_segment_changed)
        self.segment_combo.setEnabled(False)

        open_btn = QPushButton("Open Library Archive…")
        open_btn.clicked.connect(self._open_file)
        self.relink_btn = QPushButton("Relink Report Video…")
        self.relink_btn.setEnabled(False)
        self.relink_btn.clicked.connect(self._relink_report_video)
        self.export_btn = QPushButton("Export Video…")
        self.export_btn.clicked.connect(self._export_video)
        self.export_btn.setEnabled(False)

        self.analytics_btn = QPushButton("Analyze posture & movement")
        self.analytics_btn.setEnabled(False)
        self.analytics_btn.setToolTip(
            "Analyze the raw video in the background to add synchronized skeleton and posture insights."
        )
        self.analytics_btn.clicked.connect(self._start_posture_analysis)

        self.compare_cb = QCheckBox("Compare reference")
        self.compare_cb.setEnabled(False)
        self.compare_cb.setToolTip("Show the selected reference recording beside this practice.")
        self.compare_cb.toggled.connect(self._set_comparison_visible)
        self.compare_btn = QPushButton("Open Comparison…")
        self.compare_btn.clicked.connect(self._open_comparison)
        self.compare_btn.setEnabled(False)

        tb_layout.addWidget(QLabel("Recording:"))
        tb_layout.addWidget(self.segment_combo)
        tb_layout.addWidget(QLabel("Stream:"))
        tb_layout.addWidget(self.stream_combo)
        tb_layout.addWidget(open_btn)
        tb_layout.addWidget(self.relink_btn)
        tb_layout.addWidget(self.export_btn)
        tb_layout.addWidget(self.compare_btn)
        tb_layout.addWidget(self.compare_cb)
        tb_layout.addStretch()
        vc_layout.addWidget(top_bar)
        self.status_label = QLabel(
            "Open a local video or .tp practice archive. Video only (no audio)."
        )
        self.status_label.setWordWrap(True)
        vc_layout.addWidget(self.status_label)

        # Video Stage
        self.stage_container = QWidget()
        self.stage_layout = QHBoxLayout(self.stage_container)
        self.stage_layout.setContentsMargins(0, 0, 0, 0)

        self.primary_stage = VideoStage("Select a practice archive or open a video to replay.")
        self.stage_layout.addWidget(self.primary_stage, 1)
        self.comparison_stage = VideoStage("Open a reference video or calibration archive to compare.")
        self.comparison_stage.setVisible(False)
        self.stage_layout.addWidget(self.comparison_stage, 1)
        vc_layout.addWidget(self.stage_container, 1)

        # Transport Bar
        transport_card = QFrame()
        transport_card.setProperty("class", "card")
        tr_layout = QHBoxLayout(transport_card)
        tr_layout.setContentsMargins(12, 8, 12, 8)
        tr_layout.setSpacing(12)

        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self._toggle_playback)
        self.play_btn.setEnabled(False)

        self.step_back_btn = QPushButton("-1 frame")
        self.step_back_btn.clicked.connect(lambda: self._step_frame(-1))
        self.step_back_btn.setEnabled(False)
        self.step_forward_btn = QPushButton("+1 frame")
        self.step_forward_btn.clicked.connect(lambda: self._step_frame(1))
        self.step_forward_btn.setEnabled(False)

        self.time_label = QLabel("00:00.0 / 00:00.0")
        self.time_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")

        self.scrubber = QSlider(Qt.Orientation.Horizontal)
        self.scrubber.setRange(0, 0)
        self.scrubber.setEnabled(False)
        self.scrubber.valueChanged.connect(self._on_scrubber_changed)

        tr_layout.addWidget(self.play_btn)
        tr_layout.addWidget(self.step_back_btn)
        tr_layout.addWidget(self.step_forward_btn)
        tr_layout.addWidget(self.scrubber, 1)
        tr_layout.addWidget(self.time_label)
        tr_layout.addWidget(QLabel("Speed:"))
        self.speed_combo = QComboBox()
        for rate in (0.25, 0.5, 1.0, 1.5, 2.0):
            self.speed_combo.addItem(f"{rate:g}x", rate)
        self.speed_combo.setCurrentIndex(2)
        self.speed_combo.currentIndexChanged.connect(self._set_playback_speed)
        tr_layout.addWidget(self.speed_combo)
        vc_layout.addWidget(transport_card)

        self.splitter.addWidget(video_col)

        # Right Inspector Panel
        inspector = QFrame()
        inspector.setProperty("class", "card")
        inspector.setMinimumWidth(300)
        inspector.setMaximumWidth(420)
        self.inspector = inspector
        insp_layout = QVBoxLayout(inspector)
        insp_layout.setContentsMargins(14, 12, 14, 12)

        tabs = QTabWidget()
        self.inspector_tabs = tabs
        # Tab 1: Frame Insights
        insights_tab = QWidget()
        it_layout = QVBoxLayout(insights_tab)
        it_layout.setContentsMargins(8, 10, 8, 10)
        self.analysis_status = QLabel(
            "Analyze a raw video to add synchronized posture insights. Analysis runs in the background."
        )
        self.analysis_status.setWordWrap(True)
        self.analysis_status.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        it_layout.addWidget(self.analytics_btn)
        it_layout.addWidget(self.analysis_status)
        it_layout.addWidget(QLabel("Calibration profile:"))
        self.reference_combo = QComboBox()
        self.reference_combo.currentIndexChanged.connect(self._calibration_changed)
        it_layout.addWidget(self.reference_combo)
        self.show_coach_cb = QCheckBox("Skeleton / posture overlay")
        self.show_pivots_cb = QCheckBox("Pivot rings")
        self.show_steps_cb = QCheckBox("Step footprints")
        self.lines_only_cb = QCheckBox("Lines only on floor")
        for checkbox in (self.show_coach_cb, self.show_pivots_cb, self.show_steps_cb):
            checkbox.setChecked(True)
        for checkbox in (
            self.show_coach_cb, self.show_pivots_cb, self.show_steps_cb, self.lines_only_cb
        ):
            checkbox.toggled.connect(self._refresh_overlay)
            it_layout.addWidget(checkbox)

        self.metric_values: dict[str, QLabel] = {}
        for key, title in (
            ("head_angle", "Head alignment"),
            ("shoulder_elev_pct", "Shoulder height"),
            ("axis_curve_smoothed", "Torso / hip alignment"),
            ("torso_axis_tilt", "Torso axis (pink line)"),
            ("knee_bend_deg", "Knee bend"),
        ):
            row = QHBoxLayout()
            name_label = QLabel(title)
            name_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED};")
            value_label = QLabel("Not observed")
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(name_label, 1)
            row.addWidget(value_label)
            it_layout.addLayout(row)
            self.metric_values[key] = value_label

        self.alerts_text = QLabel("Posture alerts will appear here when a frame is analyzed.")
        self.alerts_text.setWordWrap(True)
        self.alerts_text.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-weight: 600;")
        it_layout.addWidget(self.alerts_text)

        self.insights_text = QLabel(
            "The original video remains unchanged. Analysis follows the same posture thresholds as live practice."
        )
        self.insights_text.setWordWrap(True)
        self.insights_text.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 12px;")
        it_layout.addWidget(self.insights_text)
        self.cancel_analysis_btn = QPushButton("Cancel analysis")
        self.cancel_analysis_btn.setEnabled(False)
        self.cancel_analysis_btn.clicked.connect(self._cancel_posture_analysis)
        it_layout.addWidget(self.cancel_analysis_btn)
        it_layout.addStretch()

        # Tab 2: Report Markdown
        report_tab = QWidget()
        rt_layout = QVBoxLayout(report_tab)
        rt_layout.setContentsMargins(8, 10, 8, 10)
        self.report_view = QTextBrowser()
        self.report_view.setReadOnly(True)
        self.report_view.setPlaceholderText("No archived report is loaded.")
        self.report_view.setOpenLinks(False)
        self.report_view.setOpenExternalLinks(False)
        self.report_view.anchorClicked.connect(self._on_report_link)
        report_actions = QHBoxLayout()
        self.coach_btn = QPushButton("Open Report in AI Coach")
        self.coach_btn.setEnabled(False)
        self.coach_btn.clicked.connect(self._open_report_in_coach)
        report_actions.addWidget(self.coach_btn)
        report_actions.addStretch()
        rt_layout.addLayout(report_actions)
        rt_layout.addWidget(self.report_view)

        insights_scroll = QScrollArea()
        insights_scroll.setWidgetResizable(True)
        insights_scroll.setFrameShape(QFrame.Shape.NoFrame)
        insights_scroll.setWidget(insights_tab)
        tabs.addTab(insights_scroll, "Insights")
        tabs.addTab(report_tab, "Report")
        insp_layout.addWidget(tabs)

        self.splitter.addWidget(inspector)
        self.splitter.setSizes([720, 340])

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)
        self._analysis_timer = QTimer(self)
        self._analysis_timer.setInterval(100)
        self._analysis_timer.timeout.connect(self._poll_posture_analysis)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._release)
        self.refresh()
        self.bridge.config_changed.connect(lambda _cfg: self.refresh())

    def refresh(self) -> None:
        selected = self.reference_combo.currentData()
        self.reference_combo.blockSignals(True)
        self.reference_combo.clear()
        self.reference_combo.addItem("No calibration (use Settings)", None)
        for reference in self.bridge.list_references():
            if reference.path.suffix.lower() == ".tpc" and reference.path.is_file():
                self.reference_combo.addItem(reference.name, str(reference.path))
        index = self.reference_combo.findData(selected)
        if selected is not None and index < 0:
            self.reference_combo.addItem(f"Unavailable calibration: {Path(selected).name}", selected)
            index = self.reference_combo.count() - 1
        self.reference_combo.setCurrentIndex(max(0, index))
        self.reference_combo.blockSignals(False)
        self._calibration_changed()

    def _update_calibration_comparison(self) -> None:
        self._compared_coach_data = None
        self._calibration_error = ""
        selected = self.reference_combo.currentData()
        if self._coach_data is None:
            return
        try:
            data = self._coach_data
            if isinstance(selected, str):
                path = Path(selected)
                reference = load_calibration_file(str(path), path.parent)
                if reference is None:
                    raise ValueError(f"Calibration not found: {path.name}")
                data = compare_coach_data(data, self.bridge.config, reference)
            elif self.bridge.config.calibration_defaults:
                data = compare_default_coach_data(data, self.bridge.config)
            self._compared_coach_data = interpolate_coach_data(
                data, self.bridge.config,
            )
        except (OSError, ValueError, TypeError, KeyError) as error:
            self._calibration_error = f"Calibration comparison unavailable: {error}"
            self.analysis_status.setText(self._calibration_error)
            self.bridge.status_message.emit(self._calibration_error)

    def _calibration_changed(self, *_args: object) -> None:
        self._update_calibration_comparison()
        if not self._calibration_error and self._coach_data is not None:
            self.analysis_status.setText(
                f"Comparing cached measurements with {self.reference_combo.currentText()}."
                if self.reference_combo.currentData() is not None else
                "Using Settings default ranges (no calibration selected)."
                if self.bridge.config.calibration_defaults else
                "Using Settings coaching thresholds (no calibration selected)."
            )
        self._refresh_overlay()

    def _display_coach_data(self) -> dict[str, Any] | None:
        return self._compared_coach_data

    def _using_range_targets(self) -> bool:
        return self.reference_combo.currentData() is not None or bool(
            self.bridge.config.calibration_defaults
        )

    def load_archive(
        self, path: Path, *, segment_index: int = 0, timestamp: float | None = None
    ) -> None:
        """Open either a local video or a validated practice archive."""
        self._reopen_path = None
        self._release()
        try:
            self._playback.open(path)
            self.current_archive_path = path
            if self._playback.archive is not None:
                self.segment_combo.blockSignals(True)
                self.segment_combo.addItems(
                    [f"Recording {index:02d}" for index in range(self._playback.segment_count)]
                )
                self.segment_combo.blockSignals(False)
                self.segment_combo.setEnabled(True)
                self._on_segment_changed(0)
                if 0 <= segment_index < self.segment_combo.count():
                    self.segment_combo.setCurrentIndex(segment_index)
            elif self._playback._report is not None:
                self.report_view.setMarkdown(
                    self._link_report_timestamps(self._playback.report)
                )
                self._coach_data = load_report_coach_data(self._playback._report)
                self._update_calibration_comparison()
                self._movement_data = load_report_movement_data(self._playback._report)
                self.coach_btn.setEnabled(True)
                self._set_relink_availability()
                if self._playback.variants:
                    self._populate_streams()
                else:
                    self.status_label.setText(
                        f"{path.name} — report loaded; no linked video is available. "
                        f"{self._playback.warning}"
                    )
            else:
                self._populate_streams()
            self._update_analysis_controls()
            if timestamp is not None:
                self._show_frame(self._playback.frame_for_timestamp(timestamp))
            self.compare_btn.setEnabled(self._playback.reader is not None)
        except REPLAY_ERRORS as error:
            self._release()
            self._playback_error(error)

    def _open_file(self) -> None:
        path = self.bridge.choose_library_archive(self, "Open Library archive")
        if path is not None:
            self.load_archive(path)

    def _set_relink_availability(self) -> None:
        report = self._playback._report
        archive_backed = self.current_archive_path is not None and (
            self.current_archive_path.suffix.lower() in (".tp", ".tpc", ".tpr")
        )
        self.relink_btn.setEnabled(
            report is not None
            and not archive_backed
            and any(not video.is_file() for video in report.videos.values())
        )

    def _relink_report_video(self) -> None:
        report = self._playback._report
        path = self.current_archive_path
        if report is None or path is None or path.suffix.lower() != ".md":
            self.bridge.status_message.emit("Open a standalone report before relinking its video.")
            return
        video, _ = QFileDialog.getOpenFileName(
            self,
            "Relink report to a local video",
            "",
            f"Video files ({' '.join('*' + suffix for suffix in VIDEO_SUFFIXES)})",
        )
        if not video:
            return
        from ...references.comparison import relink_report_video

        try:
            outcome = relink_report_video(report, Path(video))
        except (OSError, ValueError, RuntimeError) as error:
            self.bridge.status_message.emit(f"Could not relink report video: {error}")
            return
        self.load_archive(path)
        self.bridge.status_message.emit(outcome.message)

    def _export_video(self) -> None:
        reader = self._playback.reader
        if reader is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export selected video stream",
            reader.path.name,
            "Video files (*.mp4 *.mov *.avi *.mkv *.m4v *.webm);;All files (*)",
        )
        if not path:
            return
        destination = Path(path).expanduser().resolve()
        if destination == reader.path:
            self.bridge.status_message.emit("The selected video is already at that location.")
            return
        try:
            shutil.copy2(reader.path, destination)
        except OSError as error:
            self.bridge.status_message.emit(f"Could not export video: {error}")
            return
        self.bridge.status_message.emit(f"Exported video to {destination}")

    def _on_segment_changed(self, index: int) -> None:
        self._cancel_posture_analysis()
        self._active_analysis_job = None
        self._coach_data = None
        self._movement_data = None
        self._reset_transport()
        self.report_view.clear()
        self.stream_combo.blockSignals(True)
        self.stream_combo.clear()
        self.stream_combo.blockSignals(False)
        self.stream_combo.setEnabled(False)
        try:
            self._playback.select_segment(index)
            archive = self._playback.archive
            if archive is not None:
                analytics = read_analytics(archive.folder, index)
                if analytics is not None:
                    self._coach_data = analytics.get("coach_data")
                    self._movement_data = analytics.get("movement_data")
                movement = read_movement_analysis(archive.folder, index)
                if movement is not None and self._movement_data is None:
                    self._movement_data = validate_movement_analysis(
                        movement, expected_segment_index=index
                    )
            self._update_calibration_comparison()
            self.report_view.setMarkdown(self._link_report_timestamps(self._playback.report))
            self.coach_btn.setEnabled(self._playback._report is not None)
            self._set_relink_availability()
            self._populate_streams()
            self._update_analysis_controls()
        except REPLAY_ERRORS as error:
            self._playback_error(error)

    def _populate_streams(self) -> None:
        self.stream_combo.blockSignals(True)
        self.stream_combo.clear()
        self.stream_combo.addItems(list(self._playback.variants))
        self.stream_combo.blockSignals(False)
        self.stream_combo.setEnabled(bool(self._playback.variants))
        if not self._playback.variants:
            raise ValueError("This recording has no raw or overlay video available")
        self._on_stream_changed(self.stream_combo.currentText())

    def _on_stream_changed(self, label: str) -> None:
        self._reset_transport()
        if self._using_range_targets() and label not in (
            "Raw video", "Local video", "Source video",
        ):
            for raw in ("Raw video", "Local video", "Source video"):
                if raw in self._playback.variants:
                    self.stream_combo.setCurrentText(raw)
                    return
            self.primary_stage.clear_frame()
            self.analysis_status.setText(
                "Calibration comparison requires raw/source video; baked warnings cannot be removed."
            )
            return
        if label not in ("Raw video", "Local video", "Source video"):
            self.lines_only_cb.blockSignals(True)
            self.lines_only_cb.setChecked(False)
            self.lines_only_cb.blockSignals(False)
        try:
            reader = self._playback.select_variant(label)
            self.duration_sec = self._playback.duration()
            self.scrubber.setRange(0, reader.count - 1)
            self._show_frame(0)
            self._set_timer_interval(reader.fps)
            self.play_btn.setEnabled(True)
            self.export_btn.setEnabled(True)
            self.step_back_btn.setEnabled(True)
            self.step_forward_btn.setEnabled(True)
            self.scrubber.setEnabled(True)
            self.compare_btn.setEnabled(True)
            self._update_analysis_controls()
            name = self.current_archive_path.name if self.current_archive_path else ""
            unavailable = [
                stream
                for stream in ("Raw video", "Overlay video")
                if self._playback.archive is not None and stream not in self._playback.variants
            ]
            self.status_label.setText(
                f"{name} — {label}. Video only (no audio)."
                + (
                    f"\nUnavailable in this recording: {', '.join(unavailable)}."
                    if unavailable
                    else ""
                )
                + (
                    f"\nMetadata warning: {self._playback.warning}"
                    if self._playback.warning
                    else ""
                )
            )
        except REPLAY_ERRORS as error:
            self._playback_error(error)

    def _pause(self) -> None:
        self.is_playing = False
        self.timer.stop()
        self.play_btn.setText("▶ Play")

    def _toggle_playback(self) -> None:
        if self.is_playing:
            self._on_tick()
            self._pause()
            return
        if self._playback.reader is None:
            return
        if self.current_time_sec >= self.duration_sec:
            self._on_scrubber_changed(0)
            if self._playback.reader is None:
                return
        self._started_at = self._clock() - self.current_time_sec / self._playback_rate
        self.is_playing = True
        self.play_btn.setText("⏸ Pause")
        self.timer.start()

    def _on_tick(self) -> None:
        reader = self._playback.reader
        if not self.is_playing or reader is None:
            return
        elapsed = max(0.0, self._clock() - self._started_at) * self._playback_rate
        index = self._playback.frame_for_time(elapsed)
        try:
            if index != reader.index:
                self._show_frame(index)
            if elapsed >= self.duration_sec:
                self._pause()
                self.current_time_sec = self.duration_sec
                self._update_time_label()
        except REPLAY_ERRORS as error:
            self._playback_error(error)

    def _on_scrubber_changed(self, value: int) -> None:
        if self._playback.reader is None:
            return
        try:
            self._show_frame(value)
            self._started_at = self._clock() - self.current_time_sec / self._playback_rate
        except REPLAY_ERRORS as error:
            self._playback_error(error)

    def _step_frame(self, offset: int) -> None:
        reader = self._playback.reader
        if reader is None:
            return
        if self.is_playing:
            self._on_tick()
        if reader is not self._playback.reader:
            return
        self._pause()
        try:
            self._show_frame(reader.index + offset)
            self._started_at = self._clock() - self.current_time_sec / self._playback_rate
        except REPLAY_ERRORS as error:
            self._playback_error(error)

    def _set_playback_speed(self, index: int) -> None:
        if self.is_playing:
            self._on_tick()
        self._playback_rate = float(self.speed_combo.itemData(index) or 1.0)
        reader = self._playback.reader
        if reader is not None:
            self._set_timer_interval(reader.fps)
            if self.is_playing:
                self._started_at = self._clock() - self.current_time_sec / self._playback_rate

    def _set_timer_interval(self, fps: float) -> None:
        self.timer.setInterval(max(1, min(100, round(1000 / (fps * self._playback_rate)))))

    def _on_report_link(self, url: QUrl) -> None:
        if url.scheme() != "seek":
            return
        try:
            seconds = float(url.path())
            frame = self._playback.frame_for_report_time(seconds)
            self._pause()
            self._show_frame(frame)
        except REPLAY_ERRORS as error:
            self.status_label.setText(f"Could not seek to report timestamp: {error}")

    def _open_report_in_coach(self) -> None:
        path = self.current_archive_path
        index = self._playback.selected_segment_index
        if path is None:
            self.bridge.status_message.emit("Open a saved report first.")
            return
        if path.suffix.lower() == ".md":
            self.router.navigate(Route.COACH, path)
        elif index is not None and path.suffix.lower() in (".tp", ".tpc", ".tpr"):
            self.router.navigate(Route.COACH, {"archive_path": path, "segment_index": index})
        else:
            self.bridge.status_message.emit("Open a saved report first.")

    def _open_comparison(self) -> None:
        path = self.bridge.choose_library_archive(self, "Compare Library archive")
        if path is None:
            return
        self._load_comparison(path)

    def _load_comparison(self, path: Path) -> None:
        self._comparison.close()
        try:
            self._comparison.open(path)
            if self._comparison.archive is not None:
                index = self._playback.selected_segment_index or 0
                self._comparison.select_segment(min(index, self._comparison.segment_count - 1))
            label = "Raw video" if "Raw video" in self._comparison.variants else next(
                iter(self._comparison.variants), ""
            )
            self._comparison.select_variant(label)
            self.compare_cb.setEnabled(True)
            self.compare_cb.setChecked(True)
            self._sync_comparison_frame()
            self.compare_cb.setToolTip(f"Comparing with {Path(path).name}.")
        except REPLAY_ERRORS as error:
            self._comparison.close()
            self.comparison_stage.clear_frame()
            self.compare_cb.setChecked(False)
            self.compare_cb.setEnabled(False)
            self.status_label.setText(f"Comparison unavailable: {error}")

    def _set_comparison_visible(self, visible: bool) -> None:
        self.comparison_stage.setVisible(visible and self._comparison.reader is not None)
        if visible:
            self._sync_comparison_frame()

    def _sync_comparison_frame(self) -> None:
        reader = self._comparison.reader
        if reader is None:
            self.comparison_stage.clear_frame()
            return
        try:
            frame = reader.frame(self._comparison.frame_for_time(self.current_time_sec))
            height, width = frame.shape[:2]
            image = QImage(
                frame.data, width, height, frame.strides[0], QImage.Format.Format_BGR888
            ).copy()
            if image.isNull():
                raise ValueError("Cannot display decoded comparison frame")
            self.comparison_stage.set_frame(image)
        except REPLAY_ERRORS as error:
            self._comparison.close()
            self.comparison_stage.clear_frame()
            self.compare_cb.setChecked(False)
            self.compare_cb.setEnabled(False)
            self.status_label.setText(f"Comparison unavailable: {error}")

    @staticmethod
    def _link_report_timestamps(markdown: str) -> str:
        time_pattern = re.compile(r"`(\d{2,}:\d{2}\.\d)(?: \([^)]+\))?`")

        def link_time(match: re.Match[str]) -> str:
            label = match.group(1)
            minutes, seconds = label.split(":", 1)
            value = int(minutes) * 60 + float(seconds)
            return f"[{label}](seek:{value:.6f})"

        return time_pattern.sub(link_time, markdown)

    def _show_frame(self, index: int) -> None:
        reader = self._playback.reader
        if reader is None:
            return
        frame = reader.frame(index)
        self._update_frame_insights(reader.index)
        dynamic_stream = self.stream_combo.currentText() in (
            "Raw video", "Local video", "Source video"
        )
        if dynamic_stream:
            if self.lines_only_cb.isChecked():
                frame = render_floor_guide(frame)
            coach_data = self._display_coach_data()
            if coach_data is not None and self.show_coach_cb.isChecked():
                frame = render_coach_frame_overlay(
                    frame.copy(),
                    coach_data,
                    reader.index,
                    self.bridge.config,
                    verbose_skeleton=True,
                    show_vertical_axis=True,
                    show_footer=False,
                    lines_only=self.lines_only_cb.isChecked(),
                )
            frame = render_movement_overlay(
                frame,
                self._movement_data,
                reader.index,
                reader.fps,
                show_pivots=self.show_pivots_cb.isChecked(),
                show_steps=self.show_steps_cb.isChecked(),
                step_history_seconds=self.bridge.config.replay_step_history_seconds,
            )
        height, width = frame.shape[:2]
        image = QImage(
            frame.data, width, height, frame.strides[0], QImage.Format.Format_BGR888
        ).copy()
        if image.isNull():
            raise ValueError("Cannot display decoded video frame")
        self.primary_stage.set_frame(image)
        self.current_time_sec = self._playback.timestamp_for_frame(reader.index)
        self._sync_comparison_frame()
        self.scrubber.blockSignals(True)
        self.scrubber.setValue(reader.index)
        self.scrubber.blockSignals(False)
        self._update_time_label()

    def _refresh_overlay(self) -> None:
        reader = self._playback.reader
        if reader is None:
            return
        if (
            self.lines_only_cb.isChecked() or self._using_range_targets()
        ) and self.stream_combo.currentText() not in (
            "Raw video", "Local video", "Source video"
        ):
            for label in ("Raw video", "Local video", "Source video"):
                if label in self._playback.variants:
                    self.stream_combo.setCurrentText(label)
                    return
            self.lines_only_cb.blockSignals(True)
            self.lines_only_cb.setChecked(False)
            self.lines_only_cb.blockSignals(False)
            self.analysis_status.setText(
                "Calibration/lines-only replay requires a raw/source video; "
                "baked overlay pixels cannot be removed."
            )
            if self._using_range_targets():
                self._reset_transport()
                self.primary_stage.clear_frame()
            return
        try:
            self._show_frame(reader.index)
        except REPLAY_ERRORS as error:
            self._playback_error(error)

    def _analysis_video_path(self) -> Path | None:
        for label in ("Raw video", "Local video", "Source video"):
            path = self._playback.variants.get(label)
            if path is not None and path.is_file():
                return path
        return None

    def _update_analysis_controls(self) -> None:
        thread = self._analysis_thread
        running = thread is not None and thread.is_alive()
        self.analytics_btn.setEnabled(self._analysis_video_path() is not None and not running)
        self.cancel_analysis_btn.setEnabled(
            self._analysis_cancel is not None and not self._analysis_cancel.is_set()
        )

    def _start_posture_analysis(self) -> None:
        if self._analysis_thread is not None:
            self._poll_posture_analysis()
        reader = self._playback.reader
        video_path = self._analysis_video_path()
        if reader is None or video_path is None:
            self.analysis_status.setText("Open a raw or local video before analyzing posture.")
            return
        if self._analysis_thread is not None and self._analysis_thread.is_alive():
            return
        selected_out = min(reader.duration, MAX_IMPORT_RANGE_SECONDS)
        if selected_out <= 0:
            self.analysis_status.setText("This video has no analyzable duration.")
            return

        self._analysis_job_id += 1
        job_id = self._analysis_job_id
        cancel_event = threading.Event()
        self._analysis_cancel = cancel_event
        self._active_analysis_job = job_id
        self.analysis_status.setText("Preparing posture analysis…")
        self.cancel_analysis_btn.setEnabled(True)
        self.analytics_btn.setEnabled(False)
        self._analysis_timer.start()
        cfg = self.bridge.config.model_copy(deep=True)
        archive_path = self.current_archive_path
        segment_index = self._playback.selected_segment_index
        timestamps = list(self._playback.timestamps) or [
            frame / reader.fps for frame in range(reader.count)
        ]

        def report_progress(current: int, total: int, message: str) -> None:
            self._analysis_events.put((job_id, "progress", (current, total, message)))

        def work() -> None:
            try:
                video_sha256 = file_sha256(video_path)
                coach_data = analyse_review_clip(
                    video_path,
                    cfg,
                    selected_out=selected_out,
                    progress_callback=report_progress,
                    cancel_check=cancel_event.is_set,
                )
                if (
                    not cancel_event.is_set() and archive_path is not None
                    and archive_path.suffix.lower() in (".tp", ".tpr") and segment_index is not None
                ):
                    store_analytics(
                        archive_path, segment_index,
                        {"coach_data": coach_data.coach_data,
                         "movement_data": coach_data.movement_data, "max_gap": cfg.max_gap,
                         "model_hash": coach_data.coach_data.get("model_hash"),
                         "capture_profile": coach_data.coach_data.get("capture_profile"),
                         "frame_timestamps": timestamps},
                        expected_video_sha256=video_sha256,
                    )
                event = "cancelled" if cancel_event.is_set() else "done"
                self._analysis_events.put((job_id, event, coach_data))
            except Exception as error:  # noqa: BLE001 - surface background analysis errors in the UI
                event = "cancelled" if cancel_event.is_set() else "error"
                self._analysis_events.put(
                    (job_id, event, f"{type(error).__name__}: {error}")
                )

        self._analysis_thread = Thread(
            target=work,
            name="qt-review-posture-analysis",
            daemon=True,
        )
        self._analysis_thread.start()

    def _cancel_posture_analysis(self) -> None:
        cancel_event = self._analysis_cancel
        if cancel_event is None or cancel_event.is_set():
            return
        cancel_event.set()
        self.analysis_status.setText("Canceling posture analysis…")
        self.cancel_analysis_btn.setEnabled(False)

    def _poll_posture_analysis(self) -> None:
        thread = self._analysis_thread
        finished = thread is not None and not thread.is_alive()
        while True:
            try:
                job_id, event, payload = self._analysis_events.get_nowait()
            except queue.Empty:
                break
            if job_id != self._active_analysis_job:
                continue
            if event == "progress" and isinstance(payload, tuple):
                current, total, message = payload
                self.analysis_status.setText(f"{message} ({current}/{total}%)")
            elif event == "done" and isinstance(payload, ReviewAnalysis):
                self._coach_data = payload.coach_data
                self._movement_data = payload.movement_data
                self._update_calibration_comparison()
                frames = payload.coach_data.get("frames")
                frame_count = len(frames) if isinstance(frames, dict) else 0
                self.analysis_status.setText(
                    f"Analysis ready: {frame_count} frames, "
                    f"{len(payload.movement_data.get('pivots', []))} pivots, "
                    f"{len(payload.movement_data.get('steps', []))} steps. "
                    "The review overlay and insights follow the current playback frame."
                )
                if self._calibration_error:
                    self.analysis_status.setText(self._calibration_error)
                reader = self._playback.reader
                if reader is not None:
                    self._show_frame(reader.index)
            elif event == "cancelled":
                self.analysis_status.setText("Posture analysis canceled.")
            elif event == "error":
                self.analysis_status.setText(f"Posture analysis failed: {payload}")
                self.bridge.status_message.emit(f"Posture analysis failed: {payload}")

        if thread is not None and finished:
            thread.join()
            self._analysis_thread = None
            self._analysis_cancel = None
            self._active_analysis_job = None
            self._analysis_timer.stop()
            self._update_analysis_controls()

    def _update_frame_insights(self, frame_index: int) -> None:
        data = self._display_coach_data()
        if data is None:
            for value in self.metric_values.values():
                value.setText("Not observed")
            self.alerts_text.setText(
                self._calibration_error or "No synchronized posture analysis is loaded."
            )
            self.alerts_text.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-weight: 600;")
            self.insights_text.setText(
                "Analyze the raw video to track the skeleton and measure posture. "
                "Existing full-lines recordings retain their original overlay."
            )
            return

        frames = data.get("frames")
        entry = frames.get(str(frame_index)) if isinstance(frames, dict) else None
        if not isinstance(entry, dict):
            for value in self.metric_values.values():
                value.setText("Not observed")
            self.alerts_text.setText("No pose data is available for this frame.")
            self.alerts_text.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-weight: 600;")
            self.insights_text.setText(
                "Analyze the raw video to track the skeleton and measure posture. "
                "Existing full-lines recordings retain their original overlay."
            )
            return

        def metric(name: str, suffix: str, scale: float = 1.0, signed: bool = False) -> str:
            value = entry.get(name)
            if not isinstance(value, int | float) or isinstance(value, bool):
                return "Not observed"
            number = float(value) * scale
            format_spec = "+.1f" if signed else ".1f"
            return f"{number:{format_spec}}{suffix}"

        self.metric_values["head_angle"].setText(metric("head_angle", "°", signed=True))
        self.metric_values["shoulder_elev_pct"].setText(
            metric("shoulder_elev_pct", "%", signed=True)
        )
        self.metric_values["axis_curve_smoothed"].setText(
            metric("axis_curve_smoothed", "%", scale=100.0, signed=True)
        )
        self.metric_values["knee_bend_deg"].setText(metric("knee_bend_deg", "°"))
        self.metric_values["torso_axis_tilt"].setText(metric("torso_axis_tilt", "°"))
        if self._using_range_targets():
            rows = {row["metric"]: row for row in entry.get("calibration_rows", [])}
            for display_key, key in (
                ("head_angle", "head_pitch"), ("shoulder_elev_pct", "shoulder_clearance"),
                ("axis_curve_smoothed", "hip_offset"), ("knee_bend_deg", "knee_flexion"),
                ("torso_axis_tilt", "torso_axis_tilt"),
            ):
                row = rows.get(key)
                measured_value = row.get("smoothed") if row is not None else None
                self.metric_values[display_key].setText(
                    f"{measured_value:.3g} {row['unit']}"
                    if measured_value is not None and row is not None else "No approved / valid measurement"
                )

        message = entry.get("message")
        poses = entry.get("measurement_poses", entry.get("poses"))
        if isinstance(message, str) and message.strip():
            self.alerts_text.setText(message.strip())
            self.alerts_text.setStyleSheet(f"color: {COLOR_DANGER}; font-weight: 600;")
        elif not poses:
            self.alerts_text.setText("No dancer detected; posture alerts are unavailable.")
            self.alerts_text.setStyleSheet(f"color: {COLOR_WARNING}; font-weight: 600;")
        elif any(row.get("state") == "UNKNOWN" for row in entry.get("calibration_rows", [])):
            self.alerts_text.setText("Some approved-range measurements are unavailable (tracking/view).")
            self.alerts_text.setStyleSheet(f"color: {COLOR_WARNING}; font-weight: 600;")
        else:
            self.alerts_text.setText("No active posture alert on this frame.")
            self.alerts_text.setStyleSheet(f"color: {COLOR_SUCCESS}; font-weight: 600;")

        notes = coach_notes_for_frame(data, frame_index)
        self.insights_text.setText(
            "\n".join(notes)
            if notes
            else "Frame-level metric notes are not available for this sample."
        )

    def _update_time_label(self) -> None:
        self.time_label.setText(
            f"{time_label(self.current_time_sec)} / {time_label(self.duration_sec)}"
        )

    def _reset_transport(self) -> None:
        self._pause()
        self.current_time_sec = self.duration_sec = 0.0
        self.play_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.step_back_btn.setEnabled(False)
        self.step_forward_btn.setEnabled(False)
        self.scrubber.setEnabled(False)
        self.scrubber.blockSignals(True)
        self.scrubber.setRange(0, 0)
        self.scrubber.blockSignals(False)
        self.primary_stage.clear_frame()
        self.primary_stage.clear_badge()
        self._update_time_label()

    def _playback_error(self, error: Exception) -> None:
        self._reset_transport()
        self._playback.close_reader()
        self._comparison.close()
        self.compare_cb.setChecked(False)
        self.compare_cb.setEnabled(False)
        self.comparison_stage.clear_frame()
        self.comparison_stage.setVisible(False)
        self.status_label.setText(f"Replay unavailable: {error}")

    def _release(self) -> None:
        self._cancel_posture_analysis()
        self._active_analysis_job = None
        self._coach_data = None
        self._compared_coach_data = None
        self._calibration_error = ""
        self._movement_data = None
        self._reopen_path = None
        self._reset_transport()
        self._playback.close()
        self._comparison.close()
        self.current_archive_path = None
        self.compare_btn.setEnabled(False)
        self.compare_cb.setChecked(False)
        self.compare_cb.setEnabled(False)
        self.relink_btn.setEnabled(False)
        self.comparison_stage.clear_frame()
        self.comparison_stage.setVisible(False)
        for combo in (self.stream_combo, self.segment_combo):
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
            combo.setEnabled(False)
        self.report_view.clear()
        self.coach_btn.setEnabled(False)
        self._update_frame_insights(0)
        self.analysis_status.setText(
            "Analyze a raw video to add synchronized posture insights. Analysis runs in the background."
        )
        self._update_analysis_controls()
        self.status_label.setText("Playback closed. Open a video or .tp archive to replay.")

    def hideEvent(self, event: QHideEvent) -> None:
        path = self.current_archive_path
        self._release()
        self._reopen_path = path
        super().hideEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        path, self._reopen_path = self._reopen_path, None
        if path is not None:
            self.load_archive(path)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._release()
        super().closeEvent(event)
