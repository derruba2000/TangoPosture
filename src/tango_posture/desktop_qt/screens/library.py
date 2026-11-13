"""Library screen listing all saved practices and archives."""

from __future__ import annotations

from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any
from urllib.parse import urlsplit

import cv2
from PySide6.QtCore import QFile, QObject, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...archive import PracticeArchive, _archive_lock, rename_archive
from ...calibration.archives import read_range_profile
from ...references.archives import promote_reference, reference_directory, reference_path
from ...references.constants import MAX_IMPORT_RANGE_SECONDS
from ...references.links import parse_facebook_reel_url, parse_youtube_url
from ...references.results import ImportedClipResult
from ...references.video import probe_import_video
from ...references.worker import ReferenceImportWorker
from ...replay import VideoReader
from ...review import report_directory
from ..library.archives import archive_report
from ..router import Route, Router
from ..service_bridge import ServiceBridge


class _ImportEvents(QObject):
    worker_event = Signal(object)


class LibraryScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.practices: list[dict[str, Any]] = []
        self._import_events = _ImportEvents(self)
        self._import_events.worker_event.connect(self._handle_import_event)
        self._import_worker = ReferenceImportWorker(self._import_events.worker_event.emit)
        self._import_job_id: str | None = None
        self._import_cfg = bridge.config.model_copy(deep=True)
        self._import_as_reference = True
        self._package_cancel = Event()
        self._package_thread: Thread | None = None
        self._package_events: Queue[tuple[Any, ...]] = Queue()
        self._package_timer = QTimer(self)
        self._package_timer.setInterval(100)
        self._package_timer.timeout.connect(self._poll_package_events)
        self.destroyed.connect(lambda: self._shutdown_import())
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_import)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Library")
        title.setProperty("class", "title")
        subtitle = QLabel(
            "Practice sessions, replayable references and approved calibration profiles."
        )
        subtitle.setProperty("class", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.import_btn = QPushButton("Import & Analyze Video…")
        self.import_btn.clicked.connect(self._analyze_import_video)
        self.url_import_btn = QPushButton("Import YouTube / Facebook…")
        self.url_import_btn.clicked.connect(self.import_online_video)
        self.open_btn = QPushButton("Import Local Video…")
        self.open_btn.clicked.connect(self._import_video)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.import_btn)
        header.addWidget(self.url_import_btn)
        self.open_btn.hide()
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        # Search Bar
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search practices by name…")
        self.search_input.textChanged.connect(self._filter_table)
        search_row.addWidget(self.search_input)
        search_row.addWidget(QLabel("File type:"))
        self.type_filter = QComboBox()
        self.type_filter.addItem("All types", "")
        self.type_filter.addItem("References (.tpr)", ".tpr")
        self.type_filter.addItem("Calibrations (.tpc)", ".tpc")
        self.type_filter.addItem("Practices (.tp)", ".tp")
        self.type_filter.currentIndexChanged.connect(
            lambda _: self._filter_table(self.search_input.text())
        )
        search_row.addWidget(self.type_filter)
        self.import_reference_cb = QCheckBox("Import as reference (.tpr)")
        self.import_reference_cb.setChecked(True)
        search_row.addWidget(self.import_reference_cb)
        self.calibration_btn = QPushButton("Guided camera calibration…")
        self.calibration_btn.clicked.connect(lambda: self._camera_calibration())
        search_row.addWidget(self.calibration_btn)
        layout.addLayout(search_row)

        # Table
        table_frame = QFrame()
        table_frame.setProperty("class", "card")
        tf_layout = QVBoxLayout(table_frame)
        tf_layout.setContentsMargins(12, 12, 12, 12)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Recordings", "Date / Created", "Actions", "Type"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tf_layout.addWidget(self.table)
        layout.addWidget(table_frame, 1)

        import_status = QHBoxLayout()
        self.import_status = QLabel(
            ".tp practice · .tpr replayable reference · .tpc approved calibration profile. "
            "Legacy .tpc recordings remain readable."
        )
        self.import_status.setWordWrap(True)
        self.import_progress = QProgressBar()
        self.import_progress.setRange(0, 100)
        self.import_progress.setValue(0)
        self.import_progress.setMaximumWidth(180)
        self.import_progress.hide()
        self.cancel_import_btn = QPushButton("Cancel Import")
        self.cancel_import_btn.setEnabled(False)
        self.cancel_import_btn.clicked.connect(self._cancel_import)
        import_status.addWidget(self.import_status, 1)
        import_status.addWidget(self.import_progress)
        import_status.addWidget(self.cancel_import_btn)
        layout.addLayout(import_status)

        self.refresh()

    def refresh(self) -> None:
        self.practices = self.bridge.list_library_archives()
        self._filter_table(self.search_input.text())

    def _filter_table(self, query: str) -> None:
        q = query.strip().lower()
        suffix = self.type_filter.currentData()
        filtered = [
            p for p in self.practices
            if q in p["name"].lower() and (not suffix or p["path"].suffix.lower() == suffix)
        ]

        self.table.setRowCount(0)
        for row_idx, p in enumerate(filtered):
            self.table.insertRow(row_idx)
            self.table.setItem(row_idx, 0, QTableWidgetItem(p["name"]))
            segments = "Report" if p.get("kind") == "report" else str(p["segments_count"])
            self.table.setItem(row_idx, 1, QTableWidgetItem(segments))
            self.table.setItem(row_idx, 2, QTableWidgetItem(p["created_at"] or "Recent"))
            self.table.setItem(row_idx, 4, QTableWidgetItem(p["path"].suffix))

            profile_only = p["segments_count"] == 0
            btn = QPushButton("Profile details" if profile_only else "Open in Review")
            btn.clicked.connect(
                lambda _, path=p["path"], profile_only=profile_only:
                self._profile_details(path) if profile_only else self.router.navigate(Route.REVIEW, path)
            )
            rename_btn = QPushButton("Rename")
            rename_btn.clicked.connect(
                lambda _, path=p["path"], name=p["name"]: self._rename(path, name)
            )
            delete_btn = QPushButton("Delete")
            delete_btn.clicked.connect(lambda _, path=p["path"]: self._delete(path))
            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            for button in (btn, rename_btn, delete_btn):
                actions_layout.addWidget(button)
            if p["path"].suffix.lower() == ".tpr":
                calibrate_btn = QPushButton("Create calibration")
                calibrate_btn.clicked.connect(lambda _, path=p["path"]: self._calibrate_reference(path))
                actions_layout.addWidget(calibrate_btn)
            elif p["path"].suffix.lower() == ".tp":
                promote_btn = QPushButton("Promote to Reference")
                promote_btn.clicked.connect(lambda _, path=p["path"]: self._promote(path))
                actions_layout.addWidget(promote_btn)
            elif profile_only:
                check_btn = QPushButton("Check camera")
                check_btn.clicked.connect(lambda _, path=p["path"]: self._camera_calibration(path))
                actions_layout.addWidget(check_btn)
                defaults_btn = QPushButton("Use as Settings defaults")
                defaults_btn.clicked.connect(lambda _, path=p["path"]: self._use_defaults(path))
                actions_layout.addWidget(defaults_btn)
            self.table.setCellWidget(row_idx, 3, actions)

    def _calibrate_reference(self, path: Path) -> None:
        from ..calibration.editor import CalibrationEditor

        try:
            editor = CalibrationEditor(path, self.bridge.config, self, bridge=self.bridge)
            if editor.exec() == QDialog.DialogCode.Accepted:
                self.refresh()
                self.import_status.setText(f"Approved calibration saved: {editor.saved_path}")
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            self.import_status.setText(f"Cannot create calibration: {error}")

    def _use_defaults(self, path: Path) -> None:
        try:
            self.bridge.use_calibration_defaults(read_range_profile(path), self)
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.import_status.setText(f"Cannot use calibration defaults: {error}")

    def _profile_details(self, path: Path) -> None:
        try:
            profile = read_range_profile(path)
            metrics = "\n".join(
                f"• {key}: {value.lower:.4g} … {value.upper:.4g} {value.unit} · {value.view} · "
                f"{'camera checked' if value.camera_check_passed else 'comparison only'}"
                for key, value in profile.metrics.items()
            )
            QMessageBox.information(
                self, profile.name,
                f"{profile.context}\n{profile.measurement_version}\n\n{metrics}\n\n"
                "Scoring uses these saved ranges; the original reference video is not required.",
            )
        except (OSError, ValueError, KeyError) as error:
            self.import_status.setText(f"Cannot read profile: {error}")

    def _camera_calibration(self, path: Path | None = None) -> None:
        from ..calibration.guided import GuidedCalibration

        try:
            dialog = GuidedCalibration(self.bridge, self, profile_path=path)
            dialog.exec()
            self.refresh()
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            self.import_status.setText(f"Cannot open camera calibration: {error}")

    def _promote(self, path: Path) -> None:
        try:
            replay = PracticeArchive(path)
            try:
                dialog = QDialog(self)
                dialog.setWindowTitle("Promote recording / excerpt to Reference")
                form = QFormLayout(dialog)
                name_input = QLineEdit(f"{path.stem} reference")
                recording = QComboBox()
                recording.addItems([str(i) for i in range(len(replay.manifest["segments"]))])
                start, end = QDoubleSpinBox(), QDoubleSpinBox()
                rates: dict[str, float] = {}
                def select_recording() -> None:
                    segment = replay.manifest["segments"][recording.currentIndex()]
                    if not segment.get("video"):
                        raise ValueError("This recording has no original video")
                    reader = VideoReader(replay.folder / segment["video"])
                    try:
                        rates["fps"], rates["count"] = reader.fps, reader.count
                        for field in (start, end):
                            field.setRange(0, reader.duration)
                            field.setDecimals(3)
                        start.setValue(0)
                        end.setValue(reader.duration)
                    finally:
                        reader.close()
                recording.currentIndexChanged.connect(select_recording)
                select_recording()
                form.addRow("Reference name:", name_input)
                form.addRow("Recording index:", recording)
                form.addRow("Start seconds:", start)
                form.addRow("End seconds:", end)
                buttons = QDialogButtonBox(
                    QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                )
                buttons.accepted.connect(dialog.accept)
                buttons.rejected.connect(dialog.reject)
                form.addRow(buttons)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                destination = reference_path(reference_directory(self.bridge.config.data_dir),
                                             name_input.text().strip())
                first = round(start.value() * rates["fps"])
                last = round(end.value() * rates["fps"])
                if last <= first:
                    raise ValueError("Choose a nonempty excerpt")
                index = recording.currentIndex()
                stop = None if last == rates["count"] else last
                name = name_input.text().strip()
            finally:
                replay.close()
            self._package_cancel.clear()
            self._import_job_id = "promotion"
            self._set_import_busy(True)
            self.import_status.setText("Creating a separate reference; practice original retained…")
            def work() -> None:
                try:
                    result = promote_reference(
                        path, destination, name, index,
                        start_frame=first, end_frame=stop, cancel_check=self._package_cancel.is_set,
                    )
                    self._package_events.put(("archive_done", "promotion", result))
                except Exception as error:  # noqa: BLE001 - worker boundary reports failures
                    self._package_events.put(("import_error", "promotion", str(error)))
            self._package_thread = Thread(target=work, name="qt-reference-promotion", daemon=True)
            self._package_thread.start()
            self._package_timer.start()
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            self.import_status.setText(f"Cannot promote reference: {error}")

    def _rename(self, path: Path, current_name: str) -> None:
        name, accepted = QInputDialog.getText(self, "Rename Library entry", "Name:", text=current_name)
        if not accepted:
            return
        try:
            rename_archive(path, name)
        except (OSError, ValueError) as error:
            self.import_status.setText(f"Could not rename archive: {error}")
            return
        self.refresh()
        self.import_status.setText("Library name saved inside the archive.")

    def _delete(self, path: Path) -> None:
        decision = QMessageBox.question(
            self, "Move archive to Trash?",
            f"Move {path.name} and all its recordings and analytics to Trash?\n"
            "The original imported video and legacy reports will not be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if decision != QMessageBox.StandardButton.Yes:
            return
        archive = QFile(str(path))
        try:
            with _archive_lock(path):
                success = archive.moveToTrash()
        except OSError as error:
            self.import_status.setText(f"Could not move {path.name} to Trash: {error}")
            return
        if not success:
            self.import_status.setText(
                f"Could not move {path.name} to Trash: {archive.errorString()}. Nothing was deleted."
            )
            return
        self.refresh()
        self.import_status.setText(f"{path.name} moved to Trash.")

    def _shutdown_import(self) -> None:
        self._package_cancel.set()
        self._import_worker.close()
        if self._package_thread is not None:
            self._package_thread.join(timeout=1.5)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._package_timer.stop()
        self._shutdown_import()
        super().closeEvent(event)

    def _poll_package_events(self) -> None:
        while True:
            try:
                event = self._package_events.get_nowait()
            except Empty:
                break
            self._handle_import_event(event)

    def _import_video(self) -> None:
        self._analyze_import_video()

    def _analyze_import_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a local video to analyze",
            "",
            "Video files (*.mp4 *.mov *.avi *.mkv *.m4v *.webm);;All files (*)",
        )
        if not path:
            return
        try:
            probe = probe_import_video(Path(path))
        except (cv2.error, OSError, ValueError, RuntimeError) as error:
            self.import_status.setText(f"Could not inspect video: {error}")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Import and analyze local video")
        form = QFormLayout(dialog)
        title_input = QLineEdit(probe.path.stem)
        start_input = QDoubleSpinBox()
        end_input = QDoubleSpinBox()
        for field in (start_input, end_input):
            field.setDecimals(2)
            field.setSingleStep(0.5)
            field.setRange(0.0, probe.duration_seconds)
        end_input.setValue(min(probe.duration_seconds, MAX_IMPORT_RANGE_SECONDS))
        start_input.valueChanged.connect(
            lambda value: end_input.setRange(
                value, min(probe.duration_seconds, value + MAX_IMPORT_RANGE_SECONDS)
            )
        )
        form.addRow("Title:", title_input)
        form.addRow("Clip start (seconds):", start_input)
        form.addRow("Clip end (seconds):", end_input)
        form.addRow(
            QLabel(
                f"Movement review · maximum clip length {MAX_IMPORT_RANGE_SECONDS:.0f} seconds. "
                "The source video is not modified."
            )
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Analyze")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        selected_in = start_input.value()
        selected_out = end_input.value()
        if selected_out <= selected_in:
            self.import_status.setText("Clip end must be later than clip start.")
            return
        if selected_out - selected_in > MAX_IMPORT_RANGE_SECONDS:
            self.import_status.setText(
                f"Choose a clip no longer than {MAX_IMPORT_RANGE_SECONDS:.0f} seconds."
            )
            return
        self._start_import(
            probe.path,
            selected_in,
            selected_out,
            title_input.text().strip() or probe.path.stem,
        )

    def _start_import(
        self, path: Path | str, selected_in: float, selected_out: float | None, title: str,
        *, youtube_url: str | None = None, facebook_url: str | None = None,
    ) -> None:
        self._package_cancel.clear()
        self._import_cfg = self.bridge.config.model_copy(deep=True)
        self._import_as_reference = self.import_reference_cb.isChecked()
        try:
            self._import_job_id = self._import_worker.start(
                video_path=path,
                cfg=self._import_cfg,
                report_dir=report_directory(self._import_cfg.data_dir),
                selected_in=selected_in,
                selected_out=selected_out,
                title=title,
                analysis_mode="movement",
                analyse_soundtrack=False,
                youtube_url=youtube_url,
                facebook_url=facebook_url,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.import_status.setText(f"Could not start import: {error}")
            self._finish_import()
            return
        self._set_import_busy(True)
        self.import_status.setText(
            "Downloading and analyzing video…" if youtube_url or facebook_url
            else "Analyzing local video…"
        )
        self.import_progress.setRange(0, 0)
        self.import_progress.show()
        self.cancel_import_btn.setEnabled(True)

    def import_online_video(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Import YouTube video or Facebook Reel")
        form = QFormLayout(dialog)
        url_input = QLineEdit()
        url_input.setPlaceholderText("HTTPS YouTube or Facebook Reel URL")
        title_input = QLineEdit()
        start_input = QDoubleSpinBox()
        end_input = QDoubleSpinBox()
        for field in (start_input, end_input):
            field.setDecimals(2)
            field.setRange(0.0, 86400.0)
        end_input.setValue(MAX_IMPORT_RANGE_SECONDS)
        automatic_end = QCheckBox("Use available duration (up to 120 seconds from start)")
        automatic_end.setChecked(True)
        end_input.setEnabled(False)
        automatic_end.toggled.connect(lambda checked: end_input.setEnabled(not checked))
        form.addRow("Video URL:", url_input)
        form.addRow("Title (optional):", title_input)
        form.addRow("Clip start (seconds):", start_input)
        form.addRow("Clip end (seconds):", end_input)
        form.addRow(automatic_end)
        note = QLabel(
            "Downloads require network access and yt-dlp. Import only videos you have "
            "permission to use. Maximum analysis range: 120 seconds."
        )
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Download & Analyze")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._start_online_import(
            url_input.text(), start_input.value(),
            None if automatic_end.isChecked() else end_input.value(), title_input.text()
        )

    def _start_online_import(
        self, url: str, selected_in: float, selected_out: float | None, title: str
    ) -> None:
        youtube = None
        facebook = None
        try:
            host = (urlsplit(url.strip()).hostname or "").lower()
            if host in ("facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"):
                facebook = parse_facebook_reel_url(url.strip())
            else:
                youtube = parse_youtube_url(url.strip())
        except ValueError as error:
            self.import_status.setText(
                f"Enter a valid HTTPS YouTube video or Facebook Reel URL: {error}"
            )
            return
        if youtube is None and facebook is None:
            self.import_status.setText("Enter a valid HTTPS YouTube video or Facebook Reel URL.")
            return
        if selected_out is not None and not 0 < selected_out - selected_in <= MAX_IMPORT_RANGE_SECONDS:
            self.import_status.setText("Choose a clip range longer than zero and at most 120 seconds.")
            return
        self._start_import(
            "", selected_in, selected_out, title.strip(),
            youtube_url=youtube.canonical_url if youtube is not None else None,
            facebook_url=facebook.canonical_url if facebook is not None else None,
        )

    def _cancel_import(self) -> None:
        if self._import_job_id is None:
            return
        self._import_worker.cancel()
        self._package_cancel.set()
        self.cancel_import_btn.setEnabled(False)
        self.import_status.setText("Cancellation requested; waiting for the current frame to finish.")

    def _handle_import_event(self, event: object) -> None:
        if not isinstance(event, tuple) or len(event) < 2:
            self.import_status.setText("Import worker returned an invalid status event.")
            self._finish_import()
            return
        kind, job_id, *values = event
        if job_id != self._import_job_id:
            return
        if kind == "import_progress" and len(values) == 3:
            current, total, message = values
            if isinstance(current, int) and isinstance(total, int) and total > 0:
                self.import_progress.setRange(0, total)
                self.import_progress.setValue(current)
            if isinstance(message, str):
                self.import_status.setText(message)
        elif kind == "import_done" and len(values) == 1 and isinstance(
            values[0], ImportedClipResult
        ):
            result = values[0]
            job = job_id
            cfg = self._import_cfg
            as_reference = self._import_as_reference
            events = self._package_events
            self.import_status.setText(
                f"Saving indexed raw, overlay, lines and analytics in a "
                f"{'.tpr' if as_reference else '.tp'} archive…"
            )

            def package() -> None:
                try:
                    path = archive_report(
                        result.report_path, cfg, self._package_cancel.is_set,
                        as_reference=as_reference,
                    )
                    events.put(("archive_done", job, path))
                except Exception as error:  # noqa: BLE001 - worker boundary reports failures
                    kind = "import_cancelled" if self._package_cancel.is_set() else "import_error"
                    events.put((kind, job, str(error)))

            self._package_thread = Thread(target=package, name="qt-library-packaging", daemon=True)
            self._package_thread.start()
            self._package_timer.start()
        elif kind == "archive_done" and values and isinstance(values[0], Path):
            self._finish_import()
            self.import_status.setText("Portable Library archive saved with indexed analytics.")
            self.bridge.status_message.emit("Local video analysis completed.")
            self.refresh()
            self.router.navigate(Route.REVIEW, values[0])
        elif kind == "import_cancelled":
            self.import_status.setText("Import cancelled. You can select another video.")
            self._finish_import()
        elif kind == "import_error" and values:
            self.import_status.setText(f"Import failed: {values[0]}")
            self._finish_import()
        else:
            self.import_status.setText(f"Import worker returned an unexpected event: {kind!r}.")
            self._finish_import()

    def _finish_import(self) -> None:
        self._package_timer.stop()
        self._import_job_id = None
        self.import_progress.hide()
        self.import_progress.setRange(0, 100)
        self.import_progress.setValue(0)
        self.cancel_import_btn.setEnabled(False)
        self._set_import_busy(False)

    def _set_import_busy(self, busy: bool) -> None:
        self.table.setEnabled(not busy)
        self.import_btn.setEnabled(not busy)
        self.url_import_btn.setEnabled(not busy)
        self.open_btn.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
        self.import_reference_cb.setEnabled(not busy)
        self.calibration_btn.setEnabled(not busy)
