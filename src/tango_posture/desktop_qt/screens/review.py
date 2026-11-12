"""Review and replay workspace screen."""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
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
    QSlider,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...replay import time_label
from ..components.video_stage import VideoStage
from ..review_playback import VIDEO_SUFFIXES, ReviewPlayback
from ..router import Route, Router
from ..service_bridge import ServiceBridge
from ..theme import COLOR_TEXT_MUTED, COLOR_TEXT_PRIMARY

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
        self.destroyed.connect(self._playback.close)

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
        self.stream_combo.currentTextChanged.connect(self._on_stream_changed)
        self.stream_combo.setEnabled(False)
        self.segment_combo = QComboBox()
        self.segment_combo.currentIndexChanged.connect(self._on_segment_changed)
        self.segment_combo.setEnabled(False)

        open_btn = QPushButton("Open Video / Archive…")
        open_btn.clicked.connect(self._open_file)
        self.relink_btn = QPushButton("Relink Report Video…")
        self.relink_btn.setEnabled(False)
        self.relink_btn.clicked.connect(self._relink_report_video)
        self.export_btn = QPushButton("Export Video…")
        self.export_btn.clicked.connect(self._export_video)
        self.export_btn.setEnabled(False)

        self.analytics_btn = QPushButton("View Analytics")
        self.analytics_btn.setEnabled(False)
        self.analytics_btn.setToolTip("Analytics are not available in this replay player.")

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
        tb_layout.addWidget(self.analytics_btn)
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
        self.insights_text = QLabel("Synchronized frame insights are not available in this player.")
        self.insights_text.setWordWrap(True)
        self.insights_text.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 12px;")
        it_layout.addWidget(self.insights_text)
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

        tabs.addTab(insights_tab, "Insights")
        tabs.addTab(report_tab, "Report")
        insp_layout.addWidget(tabs)

        splitter.addWidget(inspector)
        splitter.setSizes([720, 340])

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._release)

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
            if timestamp is not None:
                self._show_frame(self._playback.frame_for_timestamp(timestamp))
            self.compare_btn.setEnabled(self._playback.reader is not None)
        except REPLAY_ERRORS as error:
            self._release()
            self._playback_error(error)

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open local video or practice archive",
            "",
            f"Video / report files ({' '.join('*' + s for s in VIDEO_SUFFIXES)} *.tp *.tpc *.md)"
            ";;All files (*)",
        )
        if path:
            self.load_archive(Path(path))

    def _set_relink_availability(self) -> None:
        report = self._playback._report
        archive_backed = self.current_archive_path is not None and (
            self.current_archive_path.suffix.lower() in (".tp", ".tpc")
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
        from ...reference_import import relink_report_video

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
        self._reset_transport()
        self.report_view.clear()
        self.stream_combo.blockSignals(True)
        self.stream_combo.clear()
        self.stream_combo.blockSignals(False)
        self.stream_combo.setEnabled(False)
        try:
            self._playback.select_segment(index)
            self.report_view.setMarkdown(self._link_report_timestamps(self._playback.report))
            self.coach_btn.setEnabled(self._playback._report is not None)
            self._set_relink_availability()
            self._populate_streams()
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
        elif index is not None and path.suffix.lower() in (".tp", ".tpc"):
            self.router.navigate(Route.COACH, {"archive_path": path, "segment_index": index})
        else:
            self.bridge.status_message.emit("Open a saved report first.")

    def _open_comparison(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a reference video or archive",
            "",
            f"Video / practice files ({' '.join('*' + s for s in VIDEO_SUFFIXES)} *.tp *.tpc)"
            ";;All files (*)",
        )
        if not path:
            return
        self._comparison.close()
        try:
            self._comparison.open(Path(path))
            if self._comparison.archive is not None:
                self._comparison.select_segment(0)
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
