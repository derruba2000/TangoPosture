import socket
from pathlib import Path

import cv2
import numpy as np
import pytest
from conftest import frame

from tango_posture.app import Application
from tango_posture.archive import PracticeArchive, read_manifest
from tango_posture.calibration import load_calibration_file, profile
from tango_posture.capture import Captured
from tango_posture.models import MANIFEST, fetch, verify
from tango_posture.practice import GuiDisplay
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
    keys = iter("spvvyyeq")
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
        assert app.verbose_skeleton is True
        assert app.show_vertical_axis is True
        assert capture.closed and estimator.closed
    finally:
        storage.close()


def test_native_display_buttons_use_the_existing_session_pipeline(tmp_path, reference, cfg, monkeypatch):
    monkeypatch.setattr("tango_posture.app.verify", lambda _: MANIFEST["sha256"])

    def no_opencv_window(*args):
        raise AssertionError("Native display must not use OpenCV windows or keyboard polling")

    monkeypatch.setattr(cv2, "imshow", no_opencv_window)
    monkeypatch.setattr(cv2, "waitKey", no_opencv_window)
    monkeypatch.setattr(cv2, "destroyAllWindows", no_opencv_window)
    keys = iter("sppeq")

    class Buttons(GuiDisplay):
        def show(self, image):
            super().show(image)
            self.request(next(keys))

    display = Buttons()
    cfg.data_dir = str(tmp_path)
    storage = Storage(tmp_path)
    storage.save_calibration(reference)
    capture, estimator = FakeCapture(), FakeEstimator()
    app = Application(cfg, storage, video="fixture", reference=reference, capture=capture, estimator=estimator, display=display)
    try:
        app.run()
        assert app.state == "FINISHED"
        assert storage.list("sessions")[0]["status"] == "completed"
        assert capture.closed and estimator.closed and display.stopped.is_set()
        assert app.practice_file.is_file()
        assert not (tmp_path / "reports").exists()
    finally:
        storage.close()


@pytest.mark.parametrize("start_recording", [False, True])
def test_native_recording_waits_for_explicit_start(tmp_path, reference, cfg, monkeypatch, start_recording):
    monkeypatch.setattr("tango_posture.app.verify", lambda _: MANIFEST["sha256"])
    written = {}

    class Writer:
        def __init__(self, path):
            self.path = path
            self.frames = written.setdefault(path.name, [])

        def write(self, image):
            self.frames.append(image)

        def release(self):
            self.path.write_bytes(b"fake video")

    monkeypatch.setattr("tango_posture.app._open_writer", lambda path, *args: Writer(path))

    class Buttons(GuiDisplay):
        shown = 0

        def show(self, image):
            self.shown += 1
            if self.shown == 3 and start_recording:
                assert not written
                self.recording.set()
            if self.shown == 6:
                self.request("q")

    cfg.data_dir = str(tmp_path)
    cfg.beat_tracking = False
    storage = Storage(tmp_path)
    app = Application(
        cfg, storage, video="fixture", reference=reference, capture=FakeCapture(),
        estimator=FakeEstimator(), display=Buttons(),
        record_video=tmp_path / "raw.mp4", record_overlay=tmp_path / "overlay.mp4",
    )
    try:
        app.run()
        if start_recording:
            assert {name: len(frames) for name, frames in written.items()} == {
                "video_00.mp4": 3, "overlay_00.mp4": 3, "overlay_00_lines.mp4": 3,
            }
        else:
            assert not written
            assert app.record_video is None and app.record_overlay is None
    finally:
        storage.close()


@pytest.mark.parametrize("stop", ["e", "q"])
def test_record_key_starts_video_and_stop_saves_without_calibrating(tmp_path, cfg, monkeypatch, stop):
    monkeypatch.setattr("tango_posture.app.verify", lambda _: MANIFEST["sha256"])
    written = {}
    released = []

    class Writer:
        def __init__(self, path):
            self.path = path
            self.name = path.name
            written[self.name] = 0

        def write(self, image):
            assert self.name not in released
            written[self.name] += 1

        def release(self):
            released.append(self.name)
            self.path.write_bytes(b"fake video")

    monkeypatch.setattr("tango_posture.app._open_writer", lambda path, *args: Writer(path))

    class Buttons(GuiDisplay):
        shown = 0

        def show(self, image):
            self.shown += 1
            if self.shown == 2:
                assert not written
                self.request("r")
            if self.shown == 5:
                self.request(stop)
            if self.shown == 7:
                assert len(released) == 3
            if self.shown == 8:
                self.request("q")

    cfg.data_dir = str(tmp_path)
    cfg.beat_tracking = False
    storage = Storage(tmp_path)
    app = Application(
        cfg, storage, video="fixture", capture=FakeCapture(),
        estimator=FakeEstimator(), display=Buttons(),
        record_video=tmp_path / "raw.mp4", record_overlay=tmp_path / "overlay.mp4",
    )
    try:
        app.run()
        assert written == {"video_00.mp4": 3, "overlay_00.mp4": 3, "overlay_00_lines.mp4": 3}
        assert sorted(released) == ["overlay_00.mp4", "overlay_00_lines.mp4", "video_00.mp4"]
        assert app.cal_start is None and app.session is None
        assert not app.recording
    finally:
        storage.close()


def test_three_recordings_share_one_practice_archive(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr("tango_posture.app.verify", lambda _: MANIFEST["sha256"])

    class Buttons(GuiDisplay):
        shown = 0

        def show(self, image):
            self.shown += 1
            if self.shown in (2, 8, 14):
                self.request("r")
            if self.shown in (5, 11, 17):
                self.request("e")
            if self.shown == 19:
                self.request("q")

    cfg.data_dir = str(tmp_path)
    cfg.beat_tracking = False
    storage = Storage(tmp_path)
    app = Application(
        cfg, storage, video="fixture", capture=FakeCapture(),
        estimator=FakeEstimator(), display=Buttons(), practice_name="My dance",
    )
    try:
        app.run()
        assert app.practice_file == tmp_path / "practice" / "My dance.tp"
        assert len(read_manifest(app.practice_file)["segments"]) == 3
        assert not app.workspace_path.exists()
        assert not (tmp_path / "reports").exists()
        assert not list(tmp_path.glob("*.mp4"))
        replay = PracticeArchive(app.practice_file)
        try:
            for index in range(3):
                for prefix in ("video", "overlay"):
                    capture = cv2.VideoCapture(str(replay.folder / f"{prefix}_{index:02d}.mp4"))
                    assert capture.isOpened()
                    assert capture.get(cv2.CAP_PROP_FRAME_COUNT) == 3
                    capture.release()
                from tango_posture.review import read_report

                record = read_report(replay.report(index))
                assert record.timestamps == pytest.approx([
                    (2 + index * 6) / 10, (3 + index * 6) / 10, (4 + index * 6) / 10,
                ])
        finally:
            replay.close()
    finally:
        storage.close()


def test_archive_save_failure_preserves_recordings_and_closes_capture(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr("tango_posture.app.verify", lambda _: MANIFEST["sha256"])
    workspace = tmp_path / "recovery"
    workspace.mkdir()
    monkeypatch.setattr("tango_posture.app.mkdtemp", lambda **_: str(workspace))

    def fail_save(*args):
        raise OSError("archive disk full")

    monkeypatch.setattr("tango_posture.archive.append_segment", fail_save)
    cfg.data_dir = str(tmp_path)
    cfg.beat_tracking = False
    storage = Storage(tmp_path)
    capture, estimator = FakeCapture(count=3), FakeEstimator()
    app = Application(
        cfg, storage, video="fixture", headless=True, capture=capture,
        estimator=estimator, record_video=tmp_path / "raw.mp4",
    )
    try:
        with pytest.raises(OSError, match="disk full"):
            app.run()
        assert capture.closed and estimator.closed
        assert (workspace / "video_00.mp4").stat().st_size > 0
        assert (workspace / "overlay_00.mp4").stat().st_size > 0
        assert list(workspace.glob("report_*.md"))
    finally:
        storage.close()


def test_calibration_window_capture_replaces_one_archive_on_retry(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr("tango_posture.app.verify", lambda _: MANIFEST["sha256"])

    class Buttons(GuiDisplay):
        shown = 0

        def __init__(self):
            super().__init__()
            self.saved = []

        def show(self, image):
            self.shown += 1
            if self.shown in (1, 158):
                self.request("c")
            if app.saved_calibration_path and app.reference.id not in self.saved:
                self.saved.append(app.reference.id)
            if self.shown == 316:
                self.request("q")

    cfg.data_dir = str(tmp_path)
    cfg.beat_tracking = False
    storage = Storage(tmp_path)
    display = Buttons()
    app = Application(
        cfg, storage, video="fixture", capture=FakeCapture(count=320),
        estimator=FakeEstimator(), display=display, calibration_only=True, name="Reference",
    )
    try:
        app.run()
        assert len(display.saved) == 2
        assert list((tmp_path / "calibrations").glob("*.tpc")) == [app.calibration_file]
        assert not list((tmp_path / "calibrations").glob("*.json"))
        assert not (tmp_path / "practice").exists()
        loaded = load_calibration_file(str(app.calibration_file))
        assert loaded.id == display.saved[-1]
        replay = PracticeArchive(app.calibration_file)
        try:
            for name in ("video_00.mp4", "overlay_00.mp4"):
                video = cv2.VideoCapture(str(replay.folder / name))
                assert video.isOpened()
                assert video.get(cv2.CAP_PROP_FRAME_COUNT) == 120
                video.release()
        finally:
            replay.close()
    finally:
        storage.close()


@pytest.mark.parametrize("save_first", [False, True])
def test_abort_discards_unsaved_video_but_keeps_saved_segments(tmp_path, cfg, reference, monkeypatch, save_first):
    monkeypatch.setattr("tango_posture.app.verify", lambda _: MANIFEST["sha256"])

    class Buttons(GuiDisplay):
        shown = 0

        def show(self, image):
            self.shown += 1
            if self.shown == 1 or (save_first and self.shown == 5):
                self.request("r")
            if self.shown == 2 or (save_first and self.shown == 6):
                self.request("s")
            if save_first and self.shown == 3:
                self.request("e")
            if self.shown == 75:
                self.request("x")

    cfg.data_dir = str(tmp_path)
    cfg.beat_tracking = False
    storage = Storage(tmp_path)
    storage.save_calibration(reference)
    capture, estimator = FakeCapture(count=80), FakeEstimator()
    app = Application(
        cfg, storage, video="fixture", reference=reference, capture=capture,
        estimator=estimator, display=Buttons(),
    )
    try:
        app.run()
        assert app.aborted and capture.closed and estimator.closed
        assert not app.workspace_path.exists()
        if save_first:
            assert len(read_manifest(app.practice_file)["segments"]) == 1
            assert len(storage.list("sessions")) == 1
            assert storage.list("sessions")[0]["status"] == "completed"
        else:
            assert not app.practice_file.exists()
            assert not storage.list("sessions")
        assert not (tmp_path / "reports").exists()
    finally:
        storage.close()


def test_auto_recording_repeats_songs_with_delays_and_limit(tmp_path, cfg, monkeypatch):
    from tango_posture.beat import BeatTracker

    cfg.data_dir = str(tmp_path)
    cfg.auto_record_start_delay = 1
    cfg.auto_record_stop_delay = 2
    cfg.max_recordings = 2
    storage = Storage(tmp_path)
    display = GuiDisplay()
    display.auto_recording.set()
    app = Application(cfg, storage, display=display)
    app.beat_tracker = BeatTracker(enable_audio=False)
    app.beat_tracker.using_live_stream = True
    stops = []
    monkeypatch.setattr(app, "finish", lambda timestamp, status: stops.append(timestamp))
    try:
        app.update_auto_recording(True, 0)
        app.update_auto_recording(True, 0.9)
        assert not app.recording
        app.update_auto_recording(True, 1)
        assert app.recording
        app.update_auto_recording(False, 4)
        app.update_auto_recording(True, 5)
        assert app.recording and not stops
        app.update_auto_recording(False, 7)
        app.update_auto_recording(False, 9)
        assert not app.recording and stops == [9]
        app.update_auto_recording(True, 10)
        app.update_auto_recording(True, 11)
        assert app.recording and app.recordings_started == 2
        app.update_auto_recording(False, 12)
        app.update_auto_recording(False, 14)
        app.update_auto_recording(True, 15)
        app.update_auto_recording(True, 16)
        assert not app.recording and app.recordings_started == 2
        assert stops == [9, 14]
    finally:
        import shutil

        shutil.rmtree(app.workspace_path)
        storage.close()


def test_auto_recording_requires_live_microphone_and_stops_when_unchecked(tmp_path, cfg, monkeypatch):
    from tango_posture.beat import BeatTracker

    cfg.data_dir = str(tmp_path)
    cfg.auto_record_start_delay = 0
    storage = Storage(tmp_path)
    display = GuiDisplay()
    display.auto_recording.set()
    app = Application(cfg, storage, display=display)
    app.beat_tracker = BeatTracker(enable_audio=False)
    stops = []
    monkeypatch.setattr(app, "finish", lambda *args: stops.append(True))
    try:
        app.update_auto_recording(True, 0)
        assert not app.recording
        app.beat_tracker.using_live_stream = True
        app.update_auto_recording(True, 1)
        assert app.recording
        display.auto_recording.clear()
        app.update_auto_recording(True, 2)
        assert not app.recording and stops == [True]
    finally:
        import shutil

        shutil.rmtree(app.workspace_path)
        storage.close()


def test_auto_songs_save_matching_video_overlay_and_report_segments(tmp_path, cfg, monkeypatch):
    from tango_posture.beat import BeatTracker
    from tango_posture.review import read_report

    monkeypatch.setattr("tango_posture.app.verify", lambda _: MANIFEST["sha256"])
    cfg.data_dir = str(tmp_path)
    cfg.auto_record_start_delay = 0.1
    cfg.auto_record_stop_delay = 0.2
    cfg.max_recordings = 2
    storage = Storage(tmp_path)

    class Buttons(GuiDisplay):
        shown = 0

        def show(self, image):
            self.shown += 1
            if self.shown == 40:
                self.request("q")

    display = Buttons()
    display.auto_recording.set()
    tracker = BeatTracker(enable_audio=False)
    tracker.using_live_stream = True

    def beat_state(timestamp):
        playing = any(start <= timestamp < end for start, end in ((0.2, 0.8), (1.5, 2.1), (2.8, 3.4)))
        return {
            "playing": playing, "beat": 1 if playing else 0, "phrase": 1, "phase": 0.0,
            "bpm": 120, "analyzing": False, "analyzed_seconds": 30,
            "analysis_target_seconds": 30, "phrase_locked": True, "source": "test-microphone",
        }

    monkeypatch.setattr(tracker, "state_at", beat_state)
    app = Application(
        cfg, storage, video="fixture", capture=FakeCapture(count=40),
        estimator=FakeEstimator(), display=display,
    )
    app.beat_tracker = tracker
    try:
        app.run()
        assert app.recordings_started == 2
        assert len(read_manifest(app.practice_file)["segments"]) == 2
        replay = PracticeArchive(app.practice_file)
        try:
            for index in range(2):
                record = read_report(replay.report(index))
                assert len(record.timestamps) == 7
                for label in ("Raw video", "Overlay"):
                    path = record.videos[label]
                    capture = cv2.VideoCapture(str(path))
                    assert capture.isOpened()
                    assert capture.get(cv2.CAP_PROP_FRAME_COUNT) == len(record.timestamps)
                    capture.release()
        finally:
            replay.close()
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
        assert app.saved_calibration_path is not None and app.saved_calibration_path.is_file()
        loaded_cal = load_calibration_file(str(app.saved_calibration_path), tmp_path / "calibrations")
        assert loaded_cal is not None and loaded_cal.id == app.reference.id
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
        assert not raw_path.exists() and not overlay_path.exists()
        assert not (tmp_path / "reports").exists()
        replay = PracticeArchive(app.practice_file)
        try:
            assert (replay.folder / "video_00.mp4").stat().st_size > 0
            assert (replay.folder / "overlay_00.mp4").stat().st_size > 0
            report_text = replay.report(0).read_text(encoding="utf-8")
            assert "## 1. What This Report Is & Its Purpose" in report_text
            assert "## 4. What Went Wrong & Where in Time" in report_text
            assert "### Saved Video & Overlay Files (For Consultation & Analysis)" in report_text
            assert "video_00.mp4" in report_text
            assert "overlay_00.mp4" in report_text
            assert str(app.workspace_path) not in report_text
        finally:
            replay.close()
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
