"""Library screen listing all saved practices and archives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...reference_import import (
    MAX_IMPORT_RANGE_SECONDS,
    ImportedClipResult,
    ReferenceImportWorker,
    probe_import_video,
)
from ...review import report_directory
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
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._import_worker.close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Practice Library")
        title.setProperty("class", "title")
        subtitle = QLabel(
            "Browse saved practice archives or open local video files without copying or changing them."
        )
        subtitle.setProperty("class", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.import_btn = QPushButton("Import & Analyze Video…")
        self.import_btn.clicked.connect(self._analyze_import_video)
        self.open_btn = QPushButton("Open Local Video…")
        self.open_btn.clicked.connect(self._import_video)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)

        header.addWidget(self.import_btn)
        header.addWidget(self.open_btn)
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        # Search Bar
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search practices by name…")
        self.search_input.textChanged.connect(self._filter_table)
        search_row.addWidget(self.search_input)
        layout.addLayout(search_row)

        # Table
        table_frame = QFrame()
        table_frame.setProperty("class", "card")
        tf_layout = QVBoxLayout(table_frame)
        tf_layout.setContentsMargins(12, 12, 12, 12)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Segments", "Date / Created", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        tf_layout.addWidget(self.table)
        layout.addWidget(table_frame, 1)

        import_status = QHBoxLayout()
        self.import_status = QLabel("Local video analysis creates a report and provenance metadata.")
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
        self.practices = self.bridge.list_practices()
        for path in self.bridge.list_reports():
            self.practices.append(
                {
                    "name": path.stem,
                    "path": path.resolve(),
                    "created_at": "",
                    "segments_count": 0,
                    "kind": "report",
                }
            )
        self._filter_table(self.search_input.text())

    def _filter_table(self, query: str) -> None:
        q = query.strip().lower()
        filtered = [p for p in self.practices if q in p["name"].lower()] if q else self.practices

        self.table.setRowCount(0)
        for row_idx, p in enumerate(filtered):
            self.table.insertRow(row_idx)
            self.table.setItem(row_idx, 0, QTableWidgetItem(p["name"]))
            segments = "Report" if p.get("kind") == "report" else str(p["segments_count"])
            self.table.setItem(row_idx, 1, QTableWidgetItem(segments))
            self.table.setItem(row_idx, 2, QTableWidgetItem(p["created_at"] or "Recent"))

            btn = QPushButton("Open in Review")
            btn.clicked.connect(lambda _, path=p["path"]: self.router.navigate(Route.REVIEW, path))
            self.table.setCellWidget(row_idx, 3, btn)

    def _import_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a video to import",
            "",
            "Video files (*.mp4 *.mov *.avi *.mkv);;All files (*)",
        )
        if path:
            self.router.navigate(Route.REVIEW, Path(path))

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
        self, path: Path, selected_in: float, selected_out: float, title: str
    ) -> None:
        try:
            self._import_job_id = self._import_worker.start(
                video_path=path,
                cfg=self.bridge.config,
                report_dir=report_directory(self.bridge.config.data_dir),
                selected_in=selected_in,
                selected_out=selected_out,
                title=title,
                analysis_mode="movement",
                analyse_soundtrack=False,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self.import_status.setText(f"Could not start import: {error}")
            self._finish_import()
            return
        self._set_import_busy(True)
        self.import_status.setText("Analyzing local video…")
        self.import_progress.setRange(0, 0)
        self.import_progress.show()
        self.cancel_import_btn.setEnabled(True)

    def _cancel_import(self) -> None:
        if self._import_job_id is None:
            return
        self._import_worker.cancel()
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
            self._finish_import()
            self.import_status.setText(
                f"Imported report saved with source SHA-256 {result.source_sha256}."
            )
            self.bridge.status_message.emit("Local video analysis completed.")
            self.router.navigate(Route.REVIEW, result.report_path)
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
        self._import_job_id = None
        self.import_progress.hide()
        self.import_progress.setRange(0, 100)
        self.import_progress.setValue(0)
        self.cancel_import_btn.setEnabled(False)
        self._set_import_busy(False)

    def _set_import_busy(self, busy: bool) -> None:
        self.import_btn.setEnabled(not busy)
        self.open_btn.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
