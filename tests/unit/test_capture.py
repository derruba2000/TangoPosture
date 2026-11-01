import itertools

import numpy as np
import pytest

from tango_posture.capture import Capture


class FakeCV:
    def __init__(self, timestamps, fps=30):
        self.timestamps = timestamps
        self.index = -1
        self.fps = fps
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        self.index += 1
        return (True, np.zeros((2, 2, 3))) if self.index < len(self.timestamps) else (False, None)

    def get(self, key):
        import cv2

        if key == cv2.CAP_PROP_FPS:
            return self.fps
        if key == cv2.CAP_PROP_FRAME_COUNT:
            return len(self.timestamps)
        return self.timestamps[self.index]

    def set(self, key, value):
        return True

    def release(self):
        self.released = True


def test_timestamp_fallback(cfg, monkeypatch):
    cv = FakeCV([0, 40, 40, 0, 0])
    monkeypatch.setattr("cv2.VideoCapture", lambda _: cv)
    c = Capture(cfg, "file")
    values = [c.read().timestamp for _ in range(5)]
    assert values[:2] == [0, 0.04]
    assert all(b > a for a, b in itertools.pairwise(values))
    assert c.fallback
    assert c.read() is None
    c.close()
    assert cv.released


def test_bad_timing(cfg, monkeypatch):
    cv = FakeCV([0, 0], fps=0)
    monkeypatch.setattr("cv2.VideoCapture", lambda _: cv)
    c = Capture(cfg, "file")
    c.read()
    with pytest.raises(ValueError, match="timestamps"):
        c.read()
    c.close()


def test_mac_permission_prompt_retry(cfg, monkeypatch):
    class ClosedCV(FakeCV):
        def isOpened(self):
            return False

    first, second = ClosedCV([]), FakeCV([0])
    calls = iter([first, second])
    statuses = iter([0, 3, 3])
    monkeypatch.setattr("cv2.VideoCapture", lambda _: next(calls))
    monkeypatch.setattr("tango_posture.capture._mac_camera_status", lambda: next(statuses))
    monkeypatch.setattr("time.sleep", lambda _: None)
    c = Capture(cfg, None)
    c.close()
    assert first.released and second.released
