import socket
from pathlib import Path

import cv2
import numpy as np
import pytest
from conftest import frame

from tango_posture.app import Application
from tango_posture.calibration import profile
from tango_posture.capture import Captured
from tango_posture.models import MANIFEST, fetch, verify
from tango_posture.storage import Storage


class FakeCapture:
    dropped = 0
    fallback = False
    fps = 10
    last = -1
    closed = False
    origin = 0

    def __init__(self, ending=None, count=30):
        self.index = 0
        self.ending = ending
        self.count = count

    def read(self):
        if self.index == self.count:
            if self.ending:
                raise self.ending
            return None
        t = self.index / 10
        self.index += 1
        return Captured(np.zeros((480, 640, 3), np.uint8), t, __import__("time").monotonic())

    def close(self):
        self.closed = True


class FakeEstimator:
    closed = False

    def estimate(self, image, timestamp):
        return frame(timestamp, forward=0.2)

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "ending,status",
    [
        (None, "completed"),
        (KeyboardInterrupt(), "stopped"),
        (OSError("camera disconnected"), "interrupted"),
    ],
)
def test_shutdown(tmp_path, reference, cfg, monkeypatch, ending, status):
    monkeypatch.setattr("tango_posture.app.verify", lambda p: MANIFEST["sha256"])
    cfg.data_dir = str(tmp_path)
    storage = Storage(tmp_path)
    storage.save_calibration(reference)
    capture, estimator = FakeCapture(ending), FakeEstimator()
    app = Application(
        cfg,
        storage,
        video="fixture",
        headless=True,
        reference=reference,
        capture=capture,
        estimator=estimator,
    )
    try:
        if isinstance(ending, OSError):
            with pytest.raises(OSError):
                app.run()
        else:
            app.run()
        assert capture.closed and estimator.closed
        assert storage.list("sessions")[0]["status"] == status
        assert (app.outputs[0] / "summary.json").exists()
    finally:
        storage.close()


def test_gui_controls(tmp_path, reference, cfg, monkeypatch):
    monkeypatch.setattr("tango_posture.app.verify", lambda p: MANIFEST["sha256"])
    monkeypatch.setattr(cv2, "imshow", lambda *args: None)
    monkeypatch.setattr(cv2, "getWindowProperty", lambda *args: 1)
    monkeypatch.setattr(cv2, "destroyAllWindows", lambda: None)
    keys = iter("sp peq")
    monkeypatch.setattr(cv2, "waitKey", lambda _: ord(next(keys)))
    cfg.data_dir = str(tmp_path)
    capture, estimator = FakeCapture(), FakeEstimator()
    storage = Storage(tmp_path)
    storage.save_calibration(reference)
    try:
        app = Application(
            cfg, storage, video="fixture", reference=reference, capture=capture, estimator=estimator
        )
        app.run()
        assert storage.list("sessions")[0]["status"] == "completed"
        assert capture.closed and estimator.closed
    finally:
        storage.close()


def test_headless_interval(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr("tango_posture.app.verify", lambda p: MANIFEST["sha256"])
    cfg.data_dir = str(tmp_path)
    storage = Storage(tmp_path)
    try:
        app = Application(
            cfg,
            storage,
            video="fixture",
            headless=True,
            interval=(0, 12),
            capture=FakeCapture(count=150),
            estimator=FakeEstimator(),
        )
        app.run()
        assert len(storage.list("calibrations")) == 1
        assert storage.list("sessions")[0]["active_duration"] == pytest.approx(3)
        assert min(r["timestamp"] for r in app.session.rows) > 12
    finally:
        storage.close()


def test_model_gate(tmp_path, monkeypatch):
    bad = tmp_path / "bad.task"
    bad.write_bytes(b"not a model")
    with pytest.raises(ValueError, match="checksum"):
        verify(bad)
    with pytest.raises(ValueError, match="missing"):
        verify(tmp_path / "absent.task")
    import io

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(b"wrong download"))
    with pytest.raises(ValueError, match="checksum"):
        fetch(tmp_path / "download.task")
    assert not (tmp_path / "download.task").exists()
    assert not list(tmp_path.glob("*.download"))


@pytest.mark.skipif(
    not Path("models/pose_landmarker_lite.task").exists(),
    reason="Explicit model installation required",
)
def test_real_backend_offline_blank_video(tmp_path, reference, cfg, monkeypatch):
    # Real codec + real inference. Blank synthetic frames are NOT real-person validation.
    path = tmp_path / "blank.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (640, 480))
    assert writer.isOpened()
    for _ in range(5):
        writer.write(np.zeros((480, 640, 3), np.uint8))
    writer.release()

    def deny(*args, **kwargs):
        raise AssertionError("Network access attempted")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr("urllib.request.urlopen", deny)
    cfg.data_dir = str(tmp_path / "data")
    storage = Storage(Path(cfg.data_dir))
    storage.save_calibration(reference)
    try:
        app = Application(cfg, storage, video=str(path), headless=True, reference=reference)
        app.run()
        assert len(app.session.rows) == 15
        assert all(row["state"] == "UNKNOWN" for row in app.session.rows)
        assert storage.list("sessions")[0]["status"] == "completed"
    finally:
        storage.close()


def test_camera_disconnect_status(tmp_path, reference, cfg, monkeypatch):
    monkeypatch.setattr("tango_posture.app.verify", lambda p: MANIFEST["sha256"])
    monkeypatch.setattr(cv2, "imshow", lambda *args: None)
    monkeypatch.setattr(cv2, "getWindowProperty", lambda *args: 1)
    monkeypatch.setattr(cv2, "destroyAllWindows", lambda: None)
    keys = iter([ord("s")] + [0] * 40)
    monkeypatch.setattr(cv2, "waitKey", lambda _: next(keys))
    cfg.data_dir = str(tmp_path)
    reference.profile = profile(cfg, 640, 480, "camera:0")
    capture = FakeCapture(OSError("Camera disconnected"))
    capture.origin = __import__("time").monotonic()
    estimator = FakeEstimator()
    storage = Storage(tmp_path)
    storage.save_calibration(reference)
    try:
        app = Application(cfg, storage, reference=reference, capture=capture, estimator=estimator)
        with pytest.raises(OSError):
            app.run()
        assert storage.list("sessions")[0]["status"] == "interrupted"
        assert capture.closed and estimator.closed
    finally:
        storage.close()


def test_opt_in_video_recording(tmp_path, reference, cfg, monkeypatch):
    monkeypatch.setattr("tango_posture.app.verify", lambda p: MANIFEST["sha256"])
    cfg.data_dir = str(tmp_path)
    raw_path = tmp_path / "raw.mp4"
    overlay_path = tmp_path / "overlay.mp4"
    storage = Storage(tmp_path)
    storage.save_calibration(reference)
    try:
        app = Application(
            cfg,
            storage,
            video="fixture",
            headless=True,
            reference=reference,
            capture=FakeCapture(count=5),
            estimator=FakeEstimator(),
            record_video=raw_path,
            record_overlay=overlay_path,
        )
        app.run()
        assert raw_path.is_file() and raw_path.stat().st_size > 0
        assert overlay_path.is_file() and overlay_path.stat().st_size > 0
        latest_report = tmp_path / "reports" / "latest_report.md"
        assert latest_report.is_file()
        report_text = latest_report.read_text(encoding="utf-8")
        assert "## 1. What This Report Is & Its Purpose" in report_text
        assert "## 4. What Went Wrong & Where in Time" in report_text
        rel_app = Application(
            cfg,
            storage,
            record_video=Path("practice.mp4"),
            record_overlay=Path("./overlay.mp4"),
        )
        assert rel_app.record_video == Path("videos/practice.mp4")
        assert rel_app.record_overlay == Path("videos/overlay.mp4")
    finally:
        storage.close()
