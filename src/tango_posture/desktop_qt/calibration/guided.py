"""Guided camera evidence capture, ending in the shared range approval editor."""

from __future__ import annotations

import queue
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...calibration.archives import read_range_profile
from ...calibration.measurements import metric_samples
from ...calibration.models import METRIC_DETAILS, STEPS, Passage
from ...calibration.statistics import repeat_check
from ...domain import FrameObservation
from ...references.archives import promote_reference, reference_directory
from ..components.video_stage import VideoStage
from ..practice.worker import PracticeThread, QtPracticeDisplay, QtPracticeWorker
from ..service_bridge import ServiceBridge, numpy_to_qimage
from .editor import CalibrationEditor

INSTRUCTIONS = {
    "Head": ("Look comfortably ahead", "Lower your gaze slightly, without forcing your neck"),
    "Shoulders": ("Relax your shoulders", "Gently raise your shoulders"),
    "Hips": ("Stand comfortably with your hips balanced", "Gently shift your hips a little forward"),
    "Torso": ("Stand comfortably upright", "Incline your torso a little, without forcing"),
    "Knees": ("Keep your knees comfortably straighter, not locked", "Gently soften and bend your knees"),
}
FRAMING = {
    "Head": "Keep the camera side-on with ear, nose, shoulder and hip visible.",
    "Shoulders": "Keep your ear, shoulder and hip visible; avoid obscuring the shoulders.",
    "Hips": "Move back until the ankles, hip and head are visible.",
    "Torso": "Turn side-on so the camera can see shoulder and hip clearly.",
    "Knees": "Move back until both ankles and knees are visible.",
}


class GuidedCalibration(QDialog):
    def __init__(
        self, bridge: ServiceBridge, parent: QWidget | None = None, *,
        profile_path: Path | None = None,
        profile_name: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self._profile_name = profile_name
        self.base_profile = read_range_profile(profile_path) if profile_path else None
        self.setWindowTitle("Guided camera calibration & repeat check")
        self.resize(1000, 780)
        self._temporary = TemporaryDirectory(prefix="tango-guided-calibration-")
        self._display = QtPracticeDisplay(capture_evidence=True)
        self._thread: PracticeThread | None = None
        self._frames: list[FrameObservation] = []
        self._holds: dict[str, list[Passage]] = {}
        self._step = 0
        self._phase = -1
        self._phase_start: float | None = None
        self._latest: float | None = None
        self._countdown = False
        self._walking = False
        self._review_requested = False
        self._begun = False
        self._closing = False
        self._error: str | None = None
        self.saved_path: Path | None = None
        self._stamp = time.time_ns()
        layout = QVBoxLayout(self)
        self.instructions = QLabel(
            "One dancer only. Frame your full body, then start the camera. "
            "No pose should hurt; retry or skip any measurement."
        )
        self.instructions.setWordWrap(True)
        layout.addWidget(self.instructions)
        self.stage = VideoStage()
        layout.addWidget(self.stage, 1)
        row = QHBoxLayout()
        self.context = QComboBox()
        self.context.addItems(["Neutral posture", "Solo walking", "Close embrace"])
        if self.base_profile:
            self.context.setEditable(True)
            self.context.setCurrentText(self.base_profile.context)
        row.addWidget(self.context)
        self.preview = QPushButton("Start camera preview")
        self.preview.clicked.connect(self._start_camera)
        row.addWidget(self.preview)
        self.start = QPushButton("Begin guided checks")
        self.start.setEnabled(False)
        self.start.clicked.connect(self._begin)
        row.addWidget(self.start)
        self.retry = QPushButton("Retry step")
        self.retry.setEnabled(False)
        self.retry.clicked.connect(self._retry)
        row.addWidget(self.retry)
        self.skip = QPushButton("Skip step")
        self.skip.setEnabled(False)
        self.skip.clicked.connect(self._skip)
        row.addWidget(self.skip)
        self.next = QPushButton("Continue")
        self.next.setEnabled(False)
        self.next.clicked.connect(self._next)
        row.addWidget(self.next)
        layout.addLayout(row)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        self.timer = QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._poll)
        self.finished.connect(lambda: self._temporary.cleanup())
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown)

    def _shutdown(self) -> None:
        self._closing = True
        self._display.request("q")
        if self._thread is not None:
            self._thread.wait()

    def _start_camera(self) -> None:
        cfg = self.bridge.config.model_copy(update={
            "data_dir": self._temporary.name, "beat_tracking": False,
        })
        worker = QtPracticeWorker(cfg, self._display, practice_name=f"Camera_evidence_{self._stamp}")
        worker.setParent(self)
        self._thread = PracticeThread(worker, self)
        worker.failed.connect(self._failed)
        worker.finished.connect(worker.deleteLater)
        self._thread.finished.connect(self._finished_capture)
        self.preview.setEnabled(False)
        self.context.setEnabled(False)
        self.timer.start()
        self._thread.start()

    def _failed(self, message: str) -> None:
        self._error = message
        self.result_label.setText(f"Camera calibration failed: {message}")

    def _speak(self, text: str) -> None:
        self.instructions.setText(text)
        self._display.prompts.put(text)

    def _begin(self) -> None:
        if self._latest is None:
            return
        self._display.recording.set()
        self._begun = True
        self.start.setEnabled(False)
        self._countdown = True
        self._phase_start = self._latest
        self._speak("Calibration starts in three seconds. Stand comfortably.")

    def _start_step(self) -> None:
        self._phase = 0
        self._holds[STEPS[self._step]] = []
        self._start_phase()

    def _start_phase(self) -> None:
        self.retry.setEnabled(False)
        self.skip.setEnabled(False)
        self.next.setEnabled(False)
        self._phase_start = self._latest
        group = STEPS[self._step]
        neutral, changed = INSTRUCTIONS[group]
        prompt = (
            neutral, changed, f"Return to your starting posture. {neutral}",
            f"Fresh repetition. {changed}",
        )[self._phase]
        self._speak(f"{group}. {prompt}. Settle for two seconds, then hold for three.")

    def _poll(self) -> None:
        try:
            image = self._display.frames.get_nowait()
        except queue.Empty:
            pass
        else:
            self.stage.set_frame(numpy_to_qimage(image))
        while True:
            try:
                frame = self._display.observations.get_nowait()
            except queue.Empty:
                break
            self._frames.append(frame)
            self._latest = frame.timestamp
        if self._latest is None:
            return
        if self._phase_start is None:
            self.start.setEnabled(not self._begun)
            return
        elapsed = self._latest - self._phase_start
        if self._countdown:
            self.result_label.setText(f"Countdown: {max(1, 3 - int(elapsed))}")
            if elapsed >= 3:
                self._countdown = False
                self._start_step()
            return
        duration = 10 if self._walking else 5
        self.result_label.setText(f"{'Settle' if elapsed < 2 else 'Capture'} · {max(0, duration - elapsed):.1f}s")
        if elapsed < duration:
            return
        passage = Passage(start=self._phase_start + 2, end=self._latest)
        if self._walking:
            self._holds["Walking"] = [passage]
            self._finish_checks()
            return
        group = STEPS[self._step]
        self._holds[group].append(passage)
        if self._phase < 3:
            self._phase += 1
            self._start_phase()
            return
        self._phase_start = None
        cfg = self.bridge.config
        single = [FrameObservation(
            frame.timestamp, frame.width, frame.height,
            frame.poses if len(frame.poses) == 1 else [],
        ) for frame in self._frames]
        samples = metric_samples(single, cfg)
        messages = []
        passed_any = False
        for key, (step, label, _unit) in METRIC_DETAILS.items():
            if step == group:
                passed, reason = repeat_check(samples[key], self._holds[group], cfg.max_gap)
                passed_any |= passed
                messages.append(f"{label}: {'repeatable' if passed else reason}")
        self.result_label.setText(
            "\n".join(messages) + "\n" + FRAMING[group] +
            "\nThis is a repeatability check, not validation of dance technique."
        )
        self.retry.setEnabled(True)
        self.skip.setEnabled(True)
        self.next.setEnabled(passed_any)

    def _retry(self) -> None:
        self._start_step()

    def _skip(self) -> None:
        self._holds.pop(STEPS[self._step], None)
        self._next()

    def _next(self) -> None:
        self.retry.setEnabled(False)
        self.skip.setEnabled(False)
        self.next.setEnabled(False)
        if self._step < len(STEPS) - 1:
            self._step += 1
            self._start_step()
        elif self.context.currentText() == "Solo walking":
            self._walking = True
            self._phase_start = self._latest
            self._speak("Walk naturally in view of the camera for eight seconds. Keep both feet visible.")
        else:
            self._finish_checks()

    def _finish_checks(self) -> None:
        self._phase_start = None
        self._review_requested = True
        self._display.request("q")
        self.instructions.setText("Saving cached camera evidence and releasing the camera…")

    def _finished_capture(self) -> None:
        self.timer.stop()
        if self._closing:
            super().reject()
            return
        if self._error is not None or not self._review_requested:
            self.result_label.setText(self._error or "Camera stopped before the guided checks completed.")
            return
        try:
            source, _index = self._display.saved_segments.get_nowait()
            destination = reference_directory(self.bridge.config.data_dir) / f"Camera_{self._stamp}.tpr"
            promote_reference(source, destination, f"Camera evidence {self._stamp}")
            editor = CalibrationEditor(
                destination, self.bridge.config, self, holds=self._holds,
                base_profile=self.base_profile,
                bridge=self.bridge,
            )
            editor.context_combo.setCurrentText(self.context.currentText())
            if self._profile_name is not None:
                editor.name_input.setText(self._profile_name)
            if self.context.currentText() == "Solo walking":
                editor.pose_combo.setCurrentIndex(4)
                if self.base_profile is None:
                    editor._update_stats(suggest=True)
            if editor.exec() == QDialog.DialogCode.Accepted:
                self.saved_path = editor.saved_path
                self.accept()
            else:
                self.result_label.setText(
                    "No calibration profile approved. The camera reference remains in Library."
                )
        except (queue.Empty, OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            self.result_label.setText(f"Cannot review calibration evidence: {error}")

    def reject(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._closing = True
            self._display.request("q")
            self.instructions.setText("Stopping camera safely…")
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
