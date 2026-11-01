import pytest
from conftest import frame

from tango_posture.calibration import calibrate, compatible
from tango_posture.config import Config
from tango_posture.metrics import measure
from tango_posture.temporal import Evaluator


@pytest.mark.parametrize("fps", [10, 15, 30, 60])
def test_persistence_and_onset(fps):
    e = Evaluator("head_forward", 0, Config())
    for i in range(fps + 1):
        assert e.update(i / fps, 0.2)["state"] == "PENDING"
    assert not e.events
    for i in range(fps + 1, 2 * fps + 1):
        e.update(i / fps, 0.2)
    assert len(e.events) == 1
    assert e.events[0].onset == 0
    assert e.events[0].detection == pytest.approx(1.5, abs=1 / fps)


def test_transient_hysteresis_and_clear():
    e = Evaluator("head_forward", 0, Config(smoothing=0.0001))
    for i in range(11):
        e.update(i / 10, 0.1)
    assert e.update(1.1, 0.07)["state"] == "WITHIN_REFERENCE"
    for i in range(12, 33):
        e.update(i / 10, 0.1)
    assert e.events[0].onset == 1.2
    for i in range(33, 43):
        assert e.update(i / 10, 0.06)["state"] == "DEVIATION"
    e.update(4.3, 0.01)
    e.update(4.7, 0.01)
    assert e.update(5.1, 0.01)["state"] == "WITHIN_REFERENCE"
    assert e.events[0].end_reason == "cleared"


def test_irregular_gap_unknown_pause():
    e = Evaluator("head_forward", 0, Config())
    for t in [0, 0.13, 0.45, 0.8, 1.1, 1.49, 1.6]:
        e.update(t, 0.2)
    assert e.events[0].detection == 1.6
    assert e.update(1.8, None)["state"] == "UNKNOWN"
    assert e.events[0].end == 1.6
    assert e.events[0].end_reason == "tracking_lost"
    assert e.update(2, 0.2)["state"] == "PENDING"
    assert e.update(5, 0.2)["state"] == "PENDING"
    for t in [5.4, 5.8, 6.2, 6.6]:
        e.update(t, 0.2)
    e.close("paused", reset_filter=True)
    assert e.events[-1].end_reason == "paused"
    assert e.update(20, 0)["smoothed"] == 0
    with pytest.raises(ValueError):
        e.update(20, 0)


def test_angle_wrapping_and_time_smoothing():
    e = Evaluator("head_inclination", 179, Config())
    e.update(0, 179)
    row = e.update(0.25, -179)
    assert row["delta"] == pytest.approx(2 * (1 - __import__("math").exp(-1)))
    assert row["state"] == "WITHIN_REFERENCE"


def test_calibration(reference, cfg):
    assert reference.quality["valid_duration"] == pytest.approx(12)
    assert reference.quality["mad"]["head_forward"] == pytest.approx(0)
    compatible(reference, reference.profile, reference.model_hash)
    with pytest.raises(ValueError, match="changed"):
        compatible(reference, {**reference.profile, "width": 1280}, reference.model_hash)
    with pytest.raises(ValueError):
        compatible(reference, reference.profile, "other")
    with pytest.raises(ValueError, match="valid"):
        calibrate([measure(frame(i / 10), cfg) for i in range(80)], 0, cfg, {}, "hash", "short")
    samples = [
        measure(frame(i / 10, forward=0.2 * __import__("math").sin(i)), cfg) for i in range(121)
    ]
    with pytest.raises(ValueError, match="unstable"):
        calibrate(samples, 0, cfg, {}, "hash", "unstable")
    samples = [measure(frame(i / 1000), cfg) for i in range(121)]
    with pytest.raises(ValueError, match="valid"):
        calibrate(samples, 0, cfg, {}, "hash", "too fast")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"confidence": 1.1},
        {"smoothing": -1},
        {"head_forward": {"entry": 0.05, "clear": 0.08}},
        {"entry_duration": float("nan")},
        {"calibration_duration": 3},
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)
