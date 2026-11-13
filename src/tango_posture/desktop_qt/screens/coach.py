"""Explicit, local Ollama analysis for selected saved practice reports."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4
from zipfile import BadZipFile

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...archive import PracticeArchive
from ...config import Config
from ...ollama import OllamaClient
from ...review import ReportRecord, read_report, save_analysis, saved_analyses
from ..router import Router
from ..service_bridge import ServiceBridge
from ..theme import COLOR_TEXT_MUTED


class _CoachEvents(QObject):
    notification = Signal(str, str, object)


class CoachScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self._report: ReportRecord | None = None
        self._report_archive: PracticeArchive | None = None
        self._report_segment: int | None = None
        self._analysis_text = ""
        self._analysis_model = ""
        self._analysis_created_at = ""
        self._job_token = ""
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._events = _CoachEvents(self)
        self._events.notification.connect(self._handle_event)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        title = QLabel("Local AI Coach")
        title.setProperty("class", "title")
        subtitle = QLabel(
            "Qualitative suggestions from a selected saved report. No Ollama request is made "
            "until you explicitly start analysis."
        )
        subtitle.setProperty("class", "subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        config_card = QFrame()
        config_card.setProperty("class", "card")
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(18, 14, 18, 14)

        report_row = QHBoxLayout()
        self.open_report_btn = QPushButton("Open Report…")
        self.open_report_btn.clicked.connect(self._open_report)
        self.report_label = QLabel("No report selected")
        self.report_label.setWordWrap(True)
        report_row.addWidget(self.open_report_btn)
        report_row.addWidget(self.report_label, 1)
        config_layout.addLayout(report_row)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Ollama model:"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItem(self.bridge.config.ollama_model)
        self.model_combo.setAccessibleName("Ollama model")
        model_row.addWidget(self.model_combo, 1)
        self.models_btn = QPushButton("Check Ollama")
        self.models_btn.clicked.connect(self._check_ollama)
        model_row.addWidget(self.models_btn)
        config_layout.addLayout(model_row)

        focus_row = QHBoxLayout()
        focus_row.addWidget(QLabel("Analysis focus:"))
        self.focus_input = QLineEdit(self.bridge.config.ollama_instruction)
        self.focus_input.setAccessibleName("AI analysis focus")
        focus_row.addWidget(self.focus_input, 1)
        config_layout.addLayout(focus_row)

        actions = QHBoxLayout()
        self.analyze_btn = QPushButton("Analyze Selected Report")
        self.analyze_btn.setProperty("class", "primary")
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.clicked.connect(self._run_analysis)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_analysis)
        self.export_btn = QPushButton("Export Analysis…")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_analysis)
        actions.addWidget(self.analyze_btn)
        actions.addWidget(self.cancel_btn)
        actions.addWidget(self.export_btn)
        actions.addStretch()
        config_layout.addLayout(actions)

        saved_row = QHBoxLayout()
        saved_row.addWidget(QLabel("Saved analysis:"))
        self.saved_combo = QComboBox()
        self.saved_combo.addItem("Select a saved analysis…", None)
        self.saved_combo.currentIndexChanged.connect(self._show_saved_analysis)
        saved_row.addWidget(self.saved_combo, 1)
        config_layout.addLayout(saved_row)
        layout.addWidget(config_card)

        result_card = QFrame()
        result_card.setProperty("class", "card")
        result_layout = QVBoxLayout(result_card)
        result_layout.setContentsMargins(16, 14, 16, 14)
        self.status_label = QLabel(
            "Select a report. Analysis is manual and uses the configured local Ollama endpoint."
        )
        self.status_label.setWordWrap(True)
        result_layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        result_layout.addWidget(self.progress)
        self.response_text = QTextBrowser()
        self.response_text.setOpenLinks(False)
        self.response_text.setOpenExternalLinks(False)
        self.response_text.setPlaceholderText(
            "Your streamed coaching response and saved analyses will appear here."
        )
        result_layout.addWidget(self.response_text, 1)
        layout.addWidget(result_card, 1)

        disclaimer = QLabel(
            "Experimental qualitative guidance only. The model receives report text, not video; "
            "it does not replace a tango teacher or provide medical advice."
        )
        disclaimer.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        disclaimer.setWordWrap(True)
        layout.addWidget(disclaimer)

    def _open_report(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a saved report or practice archive",
            "",
            "Reports and archives (*.md *.tp *.tpc *.tpr);;All files (*)",
        )
        if path:
            self.load_report(Path(path))

    def load_report(self, source: object) -> None:
        if self._is_busy():
            self.bridge.status_message.emit("Wait for the current Ollama request to finish.")
            return
        archive: PracticeArchive | None = None
        segment_index: int | None = None
        try:
            if isinstance(source, dict):
                path = source.get("archive_path")
                index = source.get("segment_index")
                if not isinstance(path, Path) or not isinstance(index, int):
                    raise TypeError("Invalid archive report selection.")
                archive = PracticeArchive(path)
                segment_index = index
            elif isinstance(source, Path):
                path = source
                if path.suffix.lower() in (".tp", ".tpc", ".tpr"):
                    archive = PracticeArchive(path)
                    labels = [
                        f"Recording {index + 1:02d}"
                        for index in range(len(archive.manifest["segments"]))
                    ]
                    if not labels:
                        raise ValueError("This archive contains no reports.")
                    if len(labels) == 1:
                        segment_index = 0
                    else:
                        label, accepted = QInputDialog.getItem(
                            self, "Select report", "Recording:", labels, 0, False
                        )
                        if not accepted:
                            archive.close()
                            return
                        segment_index = labels.index(label)
                elif path.suffix.lower() != ".md":
                    raise ValueError("Choose a Markdown report or a .tp/.tpc archive.")
            else:
                raise TypeError("Invalid report selection.")

            if archive is not None:
                if segment_index is None or not 0 <= segment_index < len(
                    archive.manifest["segments"]
                ):
                    raise ValueError("The selected recording is not present in this archive.")
                report_path = archive.report(segment_index)
            else:
                report_path = path
            report = read_report(report_path)
            if archive is not None:
                report.archive_path = archive.path
        except (OSError, ValueError, TypeError, KeyError, BadZipFile) as error:
            if archive is not None:
                archive.close()
            self.status_label.setText(f"Could not open report: {error}")
            return

        previous, self._report_archive = self._report_archive, archive
        if previous is not None:
            previous.close()
        self._report = report
        self._report_segment = segment_index
        self._analysis_text = ""
        self._analysis_model = ""
        self._analysis_created_at = ""
        self.response_text.clear()
        self.report_label.setText(
            f"{report.path.name} · SHA-256 {report.digest} · {report.path}"
        )
        self.status_label.setText(
            "Report loaded. Ollama will receive this report only after you click Analyze."
        )
        self.analyze_btn.setEnabled(True)
        self.export_btn.setEnabled(False)
        self._refresh_saved_analyses()

    def _client_config(self) -> Config:
        model = self.model_combo.currentText().strip()
        if not model:
            raise ValueError("Enter an Ollama model name.")
        return Config.model_validate(
            {
                **self.bridge.config.model_dump(),
                "ollama_model": model,
                "ollama_instruction": self.focus_input.text(),
            }
        )

    def _check_ollama(self) -> None:
        try:
            client = OllamaClient(self._client_config())
        except (ValueError, TypeError) as error:
            self.status_label.setText(f"Ollama configuration error: {error}")
            return
        self._start_worker(
            "models",
            lambda token: self._run_models(client, token),
        )

    def _run_models(self, client: OllamaClient, token: str) -> None:
        try:
            models = client.models()
            self._events.notification.emit(token, "models", models)
        except Exception as error:  # noqa: BLE001 - worker boundary reports failures to the UI
            self._events.notification.emit(token, "error", str(error))

    def _run_analysis(self) -> None:
        report = self._report
        if report is None:
            self.status_label.setText("Open a practice report first.")
            return
        try:
            client = OllamaClient(self._client_config())
        except (ValueError, TypeError) as error:
            self.status_label.setText(f"Ollama configuration error: {error}")
            return
        report_digest = report.digest
        archive_hold = self._report_archive
        self._analysis_text = ""
        self._analysis_model = client.cfg.ollama_model
        self._analysis_created_at = ""
        self.response_text.clear()
        self._start_worker(
            "analysis",
            lambda token: self._run_analysis_worker(
                client, report, report_digest, token, archive_hold
            ),
        )

    def _run_analysis_worker(
        self,
        client: OllamaClient,
        report: ReportRecord,
        digest: str,
        token: str,
        _archive_hold: PracticeArchive | None,
    ) -> None:
        cancel = self._cancel
        if cancel is None:
            self._events.notification.emit(token, "error", "Analysis was not initialized.")
            return
        try:
            text, complete = client.analyze(
                report.markdown,
                lambda chunk: self._events.notification.emit(token, "chunk", chunk),
                cancel,
            )
            if cancel.is_set():
                self._events.notification.emit(token, "cancelled", None)
                return
            if not self._report_is_current(report, digest):
                self._events.notification.emit(token, "stale", None)
                return
            path = save_analysis(
                report,
                client.cfg.ollama_model,
                client.url,
                client.cfg.ollama_instruction,
                text,
                complete,
            )
            self._events.notification.emit(token, "done", (text, complete, path))
        except InterruptedError:
            self._events.notification.emit(token, "cancelled", None)
        except Exception as error:  # noqa: BLE001 - worker boundary reports failures to the UI
            self._events.notification.emit(token, "error", str(error))

    @staticmethod
    def _report_is_current(report: ReportRecord, digest: str) -> bool:
        if report.archive_path is not None:
            archive = PracticeArchive(report.archive_path)
            try:
                if archive.manifest.get("kind") == "calibration":
                    index: int | None = 0
                else:
                    index = next(
                        (
                            i
                            for i, segment in enumerate(archive.manifest["segments"])
                            if segment.get("report") == report.path.name
                        ),
                        None,
                    )
                if index is None:
                    return False
                current = read_report(archive.report(index))
                return current.digest == digest
            finally:
                archive.close()
        return read_report(report.path).digest == digest

    def _start_worker(self, kind: str, run: Callable[[str], None]) -> None:
        if self._is_busy():
            self.status_label.setText("An Ollama request is already running.")
            return
        token = uuid4().hex
        self._job_token = token
        self._cancel = threading.Event()
        self._set_busy(True)
        self.status_label.setText(
            "Connecting to local Ollama…" if kind == "models" else "Waiting for Ollama; streaming analysis…"
        )
        thread = threading.Thread(
            target=run,
            args=(token,),
            name=f"qt-ollama-{kind}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _is_busy(self) -> bool:
        return bool(self._job_token and self._thread and self._thread.is_alive())

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        self.open_report_btn.setEnabled(not busy)
        self.models_btn.setEnabled(not busy)
        self.model_combo.setEnabled(not busy)
        self.focus_input.setEnabled(not busy)
        self.analyze_btn.setEnabled(not busy and self._report is not None)
        self.cancel_btn.setEnabled(busy)
        self.saved_combo.setEnabled(not busy)

    def _cancel_analysis(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self.cancel_btn.setEnabled(False)
            self.status_label.setText("Cancellation requested; waiting for Ollama to stop.")

    def _handle_event(self, token: str, kind: str, value: object) -> None:
        if token != self._job_token:
            return
        if kind == "chunk" and isinstance(value, str):
            self._analysis_text += value
            self.response_text.setMarkdown(self._analysis_text)
            self.export_btn.setEnabled(bool(self._analysis_text.strip()))
        elif kind == "models" and isinstance(value, list):
            models = [name for name in value if isinstance(name, str)]
            current = self.model_combo.currentText()
            self.model_combo.clear()
            self.model_combo.addItems(models)
            if current and self.model_combo.findText(current) < 0:
                self.model_combo.setEditText(current)
            self.status_label.setText(f"Connected to Ollama. {len(models)} models available.")
            self._finish_job()
        elif kind == "done" and isinstance(value, tuple):
            text, complete, path = value
            if isinstance(text, str) and isinstance(complete, bool) and isinstance(path, Path):
                self._analysis_text = text
                self._analysis_model = self.model_combo.currentText()
                self.response_text.setMarkdown(text)
                self.export_btn.setEnabled(True)
                self.status_label.setText(
                    f"Analysis saved to {path.name}."
                    + (" Output may be incomplete (generation limit reached)." if not complete else "")
                )
                self._refresh_saved_analyses()
                self._finish_job()
        elif kind == "cancelled":
            self.status_label.setText("Analysis cancelled; no response was saved.")
            self._finish_job()
        elif kind == "stale":
            self._analysis_text = ""
            self.response_text.clear()
            self.export_btn.setEnabled(False)
            self.status_label.setText(
                "The selected report changed during analysis; the response was discarded and not saved."
            )
            self._finish_job()
        elif kind == "error":
            self.status_label.setText(f"Ollama request failed: {value}")
            self._finish_job()

    def _finish_job(self) -> None:
        self._job_token = ""
        self._cancel = None
        self._set_busy(False)

    def _refresh_saved_analyses(self) -> None:
        self.saved_combo.blockSignals(True)
        self.saved_combo.clear()
        self.saved_combo.addItem("Select a saved analysis…", None)
        if self._report is not None:
            try:
                records = saved_analyses(self._report)
                for path, data in records:
                    self.saved_combo.addItem(
                        f"{data['created_at_utc']} · {data['model']}",
                        (path, data),
                    )
            except (OSError, ValueError, TypeError) as error:
                self.status_label.setText(f"Saved analyses unavailable: {error}")
        self.saved_combo.setCurrentIndex(0)
        self.saved_combo.blockSignals(False)

    def _show_saved_analysis(self, index: int) -> None:
        saved = self.saved_combo.itemData(index)
        if not isinstance(saved, tuple) or len(saved) != 2 or not isinstance(saved[1], dict):
            return
        data = saved[1]
        analysis = data.get("analysis")
        if not isinstance(analysis, str):
            self.status_label.setText("Saved analysis has invalid content.")
            return
        self._analysis_text = analysis
        self._analysis_model = str(data.get("model", "Unknown model"))
        self._analysis_created_at = str(data.get("created_at_utc", "Unknown date"))
        self.response_text.setMarkdown(analysis)
        self.export_btn.setEnabled(True)
        self.status_label.setText(
            f"Saved analysis · {data.get('model', 'Unknown model')} · "
            f"{data.get('created_at_utc', 'Unknown date')}"
        )

    def _export_analysis(self) -> None:
        report = self._report
        if report is None or not self._analysis_text.strip():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export AI analysis", f"{report.path.stem}-coach.md", "Markdown (*.md)"
        )
        if not path:
            return
        content = (
            "# AI practice analysis\n\n"
            f"- Model: `{self._analysis_model or self.model_combo.currentText()}`\n"
            f"- Report: `{report.path.name}`\n"
            f"- Report SHA-256: `{report.digest}`\n"
            + (f"- Generated: `{self._analysis_created_at}`\n" if self._analysis_created_at else "")
            + "\n"
            f"{self._analysis_text.rstrip()}\n"
        )
        try:
            Path(path).write_text(content, encoding="utf-8")
        except OSError as error:
            self.status_label.setText(f"Could not export analysis: {error}")
            return
        self.status_label.setText(f"Exported analysis to {Path(path).name}.")

    def _release_archive(self) -> None:
        if self._report_archive is not None:
            self._report_archive.close()
            self._report_archive = None

    def closeEvent(self, event: QCloseEvent) -> None:
        self._cancel_analysis()
        if not self._is_busy():
            self._release_archive()
        super().closeEvent(event)
