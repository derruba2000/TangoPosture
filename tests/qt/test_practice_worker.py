"""Qt practice worker tests; no camera is opened."""

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QApplication

from tango_posture.config import Config
from tango_posture.desktop_qt.practice.worker import QtPracticeDisplay, QtPracticeWorker
from tango_posture.desktop_qt.router import Router
from tango_posture.desktop_qt.screens.practice import PracticeScreen
from tango_posture.desktop_qt.service_bridge import ServiceBridge
from tango_posture.domain import Calibration


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication(["-platform", "offscreen"])


def _reference() -> Calibration:
    return Calibration(
        id="reference-id",
        created_at_utc="2026-01-01T00:00:00+00:00",
        name="Side reference",
        profile={
            "side": "right",
            "facing": "left",
            "width": 1280,
            "height": 720,
            "aspect_ratio": 1280 / 720,
            "capture_profile": "custom",
            "source": "camera:2",
        },
        baseline={"head_forward": 0.0, "head_inclination": 0.0, "trunk_inclination": 0.0},
        quality={},
        model_hash="model-hash",
    )


def test_worker_uses_stored_reference_profile_and_closes_storage(monkeypatch, tmp_path):
    import tango_posture.desktop_qt.practice.worker as worker_module

    reference = _reference()

    class FakeStorage:
        closed = False

        def __init__(self, path: Path):
            assert path == tmp_path

        def calibration(self, identifier):
            assert identifier == reference.id
            return reference

        def close(self):
            self.closed = True

    storage = FakeStorage(tmp_path)
    monkeypatch.setattr(worker_module, "Storage", lambda _: storage)
    captured = {}

    class FakeApplication:
        def __init__(self, cfg, active_storage, *args, **kwargs):
            captured["cfg"] = cfg
            captured["storage"] = active_storage
            captured["kwargs"] = kwargs

        def run(self):
            return []

    monkeypatch.setattr(worker_module, "Application", FakeApplication)
    cfg = Config(data_dir=str(tmp_path))
    display = QtPracticeDisplay()
    errors = []
    completed = []
    worker = QtPracticeWorker(
        cfg,
        display,
        reference_id=reference.id,
        reference_path=tmp_path / "reference.tpc",
        practice_name="practice",
    )
    worker.failed.connect(errors.append)
    worker.finished.connect(lambda: completed.append(True))
    worker.run()

    active_cfg = captured["cfg"]
    assert active_cfg.camera == 2
    assert active_cfg.width == 1280
    assert active_cfg.height == 720
    assert active_cfg.side == "right"
    assert active_cfg.facing == "left"
    assert captured["kwargs"]["reference"] is reference
    assert captured["kwargs"]["display"] is display
    assert storage.closed
    assert completed == [True]
    assert errors == []


def test_worker_surfaces_runtime_error_and_closes_storage(monkeypatch, tmp_path):
    import tango_posture.desktop_qt.practice.worker as worker_module

    class FakeStorage:
        closed = False

        def __init__(self, path: Path):
            pass

        def close(self):
            self.closed = True

    storage = FakeStorage(tmp_path)
    monkeypatch.setattr(worker_module, "Storage", lambda _: storage)

    class FakeApplication:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            raise RuntimeError("camera permission denied")

    monkeypatch.setattr(worker_module, "Application", FakeApplication)
    errors = []
    completed = []
    worker = QtPracticeWorker(Config(data_dir=str(tmp_path)), QtPracticeDisplay())
    worker.failed.connect(errors.append)
    worker.finished.connect(lambda: completed.append(True))
    worker.run()

    assert errors == ["RuntimeError: camera permission denied"]
    assert completed == [True]
    assert storage.closed


def test_practice_screen_is_idle_until_user_starts_preview(qapp, tmp_path):
    screen = PracticeScreen(
        ServiceBridge(Config(data_dir=str(tmp_path))),
        Router(),
    )
    assert not screen.worker_running
    assert not screen.record_btn.isEnabled()
    assert not screen.score_btn.isEnabled()
    assert "only after you start preview" in screen.status_label.text()
    screen.close()


def test_practice_screen_starts_and_releases_worker_without_blocking(
    qapp, tmp_path, monkeypatch
):
    import tango_posture.desktop_qt.screens.practice as practice_screen

    class FastWorker(QObject):
        failed = Signal(str)
        finished = Signal()

        def __init__(self, cfg, display, **kwargs):
            super().__init__()
            self.display = display

        @Slot()
        def run(self):
            self.display.notes = "Preview ready"
            self.finished.emit()

    monkeypatch.setattr(practice_screen, "QtPracticeWorker", FastWorker)
    screen = PracticeScreen(
        ServiceBridge(Config(data_dir=str(tmp_path))),
        Router(),
    )
    screen._start_preview()
    assert screen.worker_running
    thread_finished = []
    assert screen._thread is not None
    screen._thread.finished.connect(lambda: thread_finished.append(True))
    deadline = time.monotonic() + 2
    while screen.worker_running and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert not screen.worker_running, (
        screen._thread.isRunning() if screen._thread is not None else "thread already cleared",
        thread_finished,
    )
    assert screen.status_label.text() == "Practice preview closed."
    screen.close()

