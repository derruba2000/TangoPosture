"""Qt worker adapter for the existing local practice pipeline."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from queue import SimpleQueue

import cv2
from PySide6.QtCore import QObject, QThread, Signal

from ..app import Application
from ..calibration import load_calibration_file
from ..config import Config
from ..domain import FrameObservation
from ..practice import GuiDisplay
from ..storage import Storage

PRACTICE_ERRORS = (OSError, ValueError, RuntimeError, ImportError, sqlite3.Error, cv2.error)


class QtPracticeDisplay(GuiDisplay):
    """Thread-safe mailbox and explicit calibration handoff for Qt."""

    requires_reference_acceptance = True

    def __init__(self, *, capture_evidence: bool = False) -> None:
        super().__init__()
        self.calibration_candidates: SimpleQueue[str] = SimpleQueue()
        self.calibration_archives: SimpleQueue[Path] = SimpleQueue()
        self.capture_evidence = capture_evidence
        self.observations: SimpleQueue[FrameObservation] = SimpleQueue()
        self.prompts: SimpleQueue[str] = SimpleQueue()

    def observe(self, frame: FrameObservation) -> None:
        if self.capture_evidence:
            self.observations.put(frame)

    def candidate_ready(self, name: str) -> None:
        self.calibration_candidates.put(name)

    def calibration_saved(self, path: Path) -> None:
        self.calibration_archives.put(path.resolve())


class QtPracticeWorker(QObject):
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        cfg: Config,
        display: QtPracticeDisplay,
        *,
        reference_id: str | None = None,
        reference_path: Path | None = None,
        video: Path | None = None,
        practice_name: str | None = None,
        calibration_only: bool = False,
        reference_name: str = "My reference",
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.display = display
        self.reference_id = reference_id
        self.reference_path = reference_path
        self.video = video
        self.practice_name = practice_name
        self.calibration_only = calibration_only
        self.reference_name = reference_name

    def run(self) -> None:
        storage: Storage | None = None
        try:
            storage = Storage(Path(self.cfg.data_dir))
            reference = None
            cfg = self.cfg
            if self.reference_id:
                try:
                    reference = (
                        load_calibration_file(str(self.reference_path), self.reference_path.parent)
                        if self.reference_path is not None and self.reference_path.is_file()
                        else storage.calibration(self.reference_id)
                    )
                except ValueError:
                    if self.reference_path is None:
                        raise
                    reference = load_calibration_file(
                        str(self.reference_path), self.reference_path.parent
                    )
                    if reference is None:
                        raise ValueError(f"Reference not found: {self.reference_path.name}")
                if reference is None:
                    raise ValueError("Selected calibration could not be loaded")
                cfg = self._config_for_reference(cfg, reference.profile)

            Application(
                cfg,
                storage,
                str(self.video) if self.video is not None else None,
                reference=reference,
                display=self.display,
                practice_name=self.practice_name,
                calibration_only=self.calibration_only,
                name=self.reference_name,
            ).run()
        except PRACTICE_ERRORS as error:
            self.failed.emit(f"{type(error).__name__}: {error}")
        finally:
            if storage is not None:
                storage.close()
            self.finished.emit()

    @staticmethod
    def _config_for_reference(cfg: Config, profile: dict[str, object]) -> Config:
        updates: dict[str, object] = {
            "side": profile["side"],
            "facing": profile["facing"],
            "width": profile["width"],
            "height": profile["height"],
            "capture_profile": profile["capture_profile"],
        }
        source = profile["source"]
        if isinstance(source, str) and source.startswith("camera:"):
            camera_index = source.partition(":")[2]
            try:
                updates["camera"] = int(camera_index)
            except ValueError as error:
                raise ValueError("Reference contains an invalid camera source") from error
        return Config.model_validate({**cfg.model_dump(), **updates})


class PracticeThread(QThread):
    def __init__(self, worker: QtPracticeWorker, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.worker = worker

    def run(self) -> None:
        self.worker.run()
