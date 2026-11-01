from dataclasses import replace

import numpy as np
import pytest
from conftest import frame

from tango_posture.beat import BeatTracker
from tango_posture.config import load
from tango_posture.domain import METRICS, Landmark
from tango_posture.geometry import calculate, detect_role, head_horizontal_alignment, wrapped
from tango_posture.metrics import measure
from tango_posture.overlay import screen_point


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("facing", ["left", "right"])
@pytest.mark.parametrize("size", [(640, 480), (1280, 720)])
def test_geometry(cfg, side, facing, size):
    cfg.side, cfg.facing = side, facing
    base = measure(frame(width=size[0], height=size[1], side=side), cfg)
    assert base.values == pytest.approx(dict.fromkeys(METRICS, 0))
    positive = measure(
        frame(forward=0.2, width=size[0], height=size[1], side=side, facing=facing), cfg
    )
    assert positive.values["head_forward"] == pytest.approx(0.2)
    assert positive.values["head_inclination"] > 0
    assert measure(frame(forward=-0.2, side=side, facing=facing), cfg).values["head_forward"] < 0


def test_translation_scale_and_rotation():
    ear, shoulder, hip = np.array([30.0, 80.0]), np.array([0.0, 0.0]), np.array([0.0, -200.0])
    expected = calculate(ear, shoulder, hip, "right")
    for scale in (0.5, 2, 8):
        transformed = [p * scale + np.array([500, -200]) for p in (ear, shoulder, hip)]
        assert calculate(*transformed, "right") == pytest.approx(expected)
    angle = np.deg2rad(-20)
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    rotated = calculate(*(rotation @ p for p in (ear, shoulder, hip)), "right")
    assert rotated["head_inclination"] == pytest.approx(expected["head_inclination"])
    assert rotated["head_forward"] == pytest.approx(expected["head_forward"])
    assert rotated["trunk_inclination"] == pytest.approx(20)
    assert wrapped(359) == -1


def test_mirror_only_display(cfg):
    f = frame(forward=0.1)
    before = measure(f, cfg)
    cfg.mirror = True
    assert measure(f, cfg) == before
    assert screen_point(0.25, 0.5, 641, 481, True) == (480, 240)
    assert screen_point(0.25, 0.5, 641, 481, False) == (160, 240)


@pytest.mark.parametrize("fault", ["missing", "low", "presence", "nan", "out", "short"])
def test_head_unknown_independently(cfg, fault):
    f = frame()
    p = f.poses[0]["left_ear"]
    if fault == "missing":
        del f.poses[0]["left_ear"]
    else:
        replacements = {
            "low": replace(p, visibility=0.2),
            "presence": replace(p, presence=None),
            "nan": replace(p, x=float("nan")),
            "out": replace(p, x=1.2),
            "short": f.poses[0]["left_shoulder"],
        }
        f.poses[0]["left_ear"] = replacements[fault]
    result = measure(f, cfg)
    assert result.values["head_forward"] is None
    assert result.values["head_inclination"] is None
    assert result.quality["trunk_inclination"].valid


@pytest.mark.parametrize(
    "fault", ["none", "two", "frontal", "far", "degenerate", "ankles", "nose", "confidence"]
)
def test_overall_unknown(cfg, fault):
    f = frame()
    p = f.poses[0]
    if fault == "none":
        f.poses.clear()
    elif fault == "two":
        f.poses.append(p.copy())
    elif fault == "frontal":
        p["right_shoulder"] = replace(p["right_shoulder"], x=0.9)
    elif fault in ("far", "degenerate"):
        p["left_shoulder"] = replace(
            p["left_hip"], y=p["left_hip"].y - (0.01 if fault == "far" else 0)
        )
    elif fault == "ankles":
        del p["left_ankle"], p["right_ankle"]
    elif fault == "nose":
        del p["nose"]
    else:
        p["right_hip"] = Landmark(0.5, 0.5)
    result = measure(f, cfg)
    assert all(v is None for v in result.values.values())
    assert not any(q.valid for q in result.quality.values())


def test_view_baseline_and_far_ankle(cfg):
    f = frame()
    del f.poses[0]["right_ankle"]
    assert all(q.valid for q in measure(f, cfg).quality.values())
    assert not any(q.valid for q in measure(f, cfg, (0.3, 0.4)).quality.values())


def test_head_horizontal_alignment_and_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("HEAD_DOWN_DURATION_SECONDS=4.5\nHEAD_DOWN_ANGLE_DEGREES=-18.0\n")
    loaded = load(None, {}, env_path=env_file)
    assert loaded.head_down_duration == pytest.approx(4.5)
    assert loaded.head_down_angle == pytest.approx(-18.0)

    level_pose = {
        "left_ear": Landmark(0.4, 0.2, 0.99, 0.99),
        "nose": Landmark(0.5, 0.2, 0.99, 0.99),
    }
    res_level = head_horizontal_alignment(level_pose, 640, 480)
    assert res_level is not None and res_level[0] == pytest.approx(0.0)

    down_pose = {
        "left_ear": Landmark(0.4, 0.2, 0.99, 0.99),
        "nose": Landmark(0.5, 0.2 + 64 / 480, 0.99, 0.99),
    }
    res_down = head_horizontal_alignment(down_pose, 640, 480)
    assert res_down is not None and res_down[0] == pytest.approx(-45.0)


def test_leader_and_follower_role_detection():
    leader_pose = {
        "left_shoulder": Landmark(0.80, 0.36, 0.99, 0.99),
        "left_elbow": Landmark(1.00, 0.42, 0.99, 0.99),
        "left_wrist": Landmark(1.01, 0.19, 0.99, 0.99),
        "right_shoulder": Landmark(0.54, 0.37, 0.99, 0.99),
        "right_elbow": Landmark(0.47, 0.64, 0.99, 0.99),
        "right_wrist": Landmark(0.67, 0.59, 0.99, 0.99),
    }
    assert detect_role(leader_pose) == "LEADER"

    follower_pose = {
        "left_shoulder": Landmark(0.84, 0.36, 0.99, 0.99),
        "left_elbow": Landmark(1.00, 0.54, 0.99, 0.99),
        "left_wrist": Landmark(0.81, 0.51, 0.99, 0.99),
        "right_shoulder": Landmark(0.56, 0.36, 0.99, 0.99),
        "right_elbow": Landmark(0.38, 0.52, 0.99, 0.99),
        "right_wrist": Landmark(0.40, 0.26, 0.99, 0.99),
    }
    assert detect_role(follower_pose) == "FOLLOWER"

    neutral_pose = {
        "left_shoulder": Landmark(0.6, 0.35, 0.99, 0.99),
        "left_elbow": Landmark(0.6, 0.55, 0.99, 0.99),
        "left_wrist": Landmark(0.6, 0.75, 0.99, 0.99),
        "right_shoulder": Landmark(0.4, 0.35, 0.99, 0.99),
        "right_elbow": Landmark(0.4, 0.55, 0.99, 0.99),
        "right_wrist": Landmark(0.4, 0.75, 0.99, 0.99),
    }
    assert detect_role(neutral_pose) == "NONE"


def test_tango_8_beat_phrase_tracker():
    tracker = BeatTracker(bpm=120.0, enable_audio=False)
    # Initialize at t=0.0; at 120 BPM, each beat is 0.5 seconds
    tracker.tap(0.0)
    expected_beats = [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3]
    for i, expected in enumerate(expected_beats):
        st = tracker.state_at(i * 0.5 + 0.05)
        assert st["beat"] == expected
        assert st["phrase"] == (1 if i < 8 else 2)

    # Tapping Beat 1 / tempo at 10.0s and 10.6s (100 BPM) resets beat to 1 and updates BPM
    tracker.tap(10.0)
    assert tracker.state_at(10.05)["beat"] == 1
    tracker.tap(10.6)
    st_after = tracker.state_at(10.61)
    assert st_after["beat"] == 1
    assert 95.0 <= st_after["bpm"] <= 120.0

    # Audio spectral flux onset shifts phase smoothly and marks source as mic-pll
    for k in range(25):
        tracker.process_flux(1.0 if k % 5 == 0 else 0.05, 11.0 + k * 0.1)
    st_audio = tracker.state_at(13.5)
    assert st_audio["source"] == "mic-pll"

    # In microphone mode (require_audio=True), beats MUST pause when music stops
    mic_tracker = BeatTracker(bpm=120.0, enable_audio=False, require_audio=True)
    assert mic_tracker.state_at(0.0)["playing"] is False
    assert mic_tracker.state_at(2.0)["beat"] == 1  # Still beat 1 after 2s of silence

    # Feed active music with 120 BPM onsets (every 0.5s) from t=2.0s to t=4.5s
    t = 2.0
    while t <= 4.5:
        is_beat = abs((t - 2.0) % 0.5) < 1e-6 or abs(((t - 2.0) % 0.5) - 0.5) < 1e-6
        mic_tracker.process_flux(2.0 if is_beat else 0.1, round(t, 2), rms=0.05)
        mic_tracker.state_at(round(t, 2))
        t += 0.05
    playing_state = mic_tracker.state_at(4.5)
    assert playing_state["playing"] is True
    assert playing_state["beat"] > 1

    # Music is paused at t=4.55s (RMS drops to silence) -> beat counter must freeze and stop progressing
    while t <= 8.0:
        mic_tracker.process_flux(0.0, round(t, 2), rms=0.0)
        t += 0.05
    paused_at_5s = mic_tracker.state_at(5.0)
    paused_at_8s = mic_tracker.state_at(8.0)
    assert paused_at_5s["playing"] is False
    assert paused_at_8s["playing"] is False
    assert paused_at_5s["beat"] == paused_at_8s["beat"]
    assert paused_at_8s["source"] == "mic-paused"
