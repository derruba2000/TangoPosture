from dataclasses import replace

import numpy as np
import pytest
from conftest import frame

from tango_posture.beat import BeatTracker
from tango_posture.config import load
from tango_posture.domain import METRICS, Landmark
from tango_posture.geometry import (
    AnkleStepTracker,
    AxisCurveTracker,
    EmbraceTracker,
    PoseSmoother,
    RoleTracker,
    ankle_lift_alignment,
    axis_curve_alignment,
    calculate,
    detect_embrace_type,
    detect_role,
    head_horizontal_alignment,
    knee_bend_alignment,
    shoulder_tension_alignment,
    wrapped,
)
from tango_posture.metrics import measure
from tango_posture.overlay import draw, screen_point


@pytest.mark.parametrize("settings_coaching", [False, True])
def test_overlay_does_not_apply_settings_judgements_with_calibration(monkeypatch, settings_coaching):
    observation = frame()
    observation.poses[0]["left_knee"] = Landmark(
        x=0.5, y=0.8, visibility=0.99, presence=0.99,
    )
    labels = []
    monkeypatch.setattr(
        "tango_posture.overlay.cv2.putText",
        lambda image, text, *args, **kwargs: labels.append(text) or image,
    )
    draw(
        np.zeros((480, 640, 3), np.uint8), observation, False, [],
        "Head pitch: outside approved range" if not settings_coaching else "",
        0.7, head_down_elapsed=10, shoulder_tension_elapsed=10, knee_straight_elapsed=10,
        axis_curve_elapsed=10, settings_coaching=settings_coaching, show_footer=False,
    )
    legacy = [
        label for label in labels
        if label.startswith(("Head horiz", "Shoulders:", "LOWER", "BEND", "BAD HEAD", "HIP OFFSET"))
    ]
    assert bool(legacy) is settings_coaching
    if not settings_coaching:
        assert "Head pitch: outside approved range" in labels


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

    # Hysteresis: 300ms to assign from NONE, 5000ms hold before returning to NONE
    rt = RoleTracker(assign_ms=300.0, hold_ms=5000.0)
    assert rt.update("LEADER", 1.00) == "NONE"
    assert rt.update("LEADER", 1.20) == "NONE"  # Only 200ms elapsed (< 300ms)
    assert rt.update("LEADER", 1.30) == "LEADER"  # 300ms reached -> assigns LEADER
    assert rt.update("NONE", 3.00) == "LEADER"  # Held at +1.7s (< 5000ms)
    assert rt.update("NONE", 6.29) == "LEADER"  # Held at +4.99s (< 5000ms)
    assert rt.update("NONE", 6.31) == "NONE"  # 5000ms expired -> returns to NONE


def test_shoulder_tension_detection():
    # Relaxed shoulders: ear at y=0.20, shoulder at y=0.36, hip at y=0.80 -> clearance ~ 1.02
    relaxed_pose = {
        "nose": Landmark(0.58, 0.22, 0.99, 0.99),
        "left_ear": Landmark(0.50, 0.20, 0.99, 0.99),
        "left_shoulder": Landmark(0.50, 0.36, 0.99, 0.99),
        "left_hip": Landmark(0.50, 0.80, 0.99, 0.99),
    }
    relaxed_res = shoulder_tension_alignment(relaxed_pose, 640, 480, neutral_clearance=1.05)
    assert relaxed_res is not None
    assert relaxed_res[0] < 10.0  # Below 10% tension threshold

    # Shrugged / tense shoulders: shoulder rises up to y=0.28 toward the ear (> 10% elevation)
    tense_pose = {
        "nose": Landmark(0.58, 0.22, 0.99, 0.99),
        "left_ear": Landmark(0.50, 0.20, 0.99, 0.99),
        "left_shoulder": Landmark(0.50, 0.28, 0.99, 0.99),
        "left_hip": Landmark(0.50, 0.80, 0.99, 0.99),
    }
    tense_res = shoulder_tension_alignment(tense_pose, 640, 480, neutral_clearance=1.05)
    assert tense_res is not None
    assert tense_res[0] >= 10.0  # Above 10% is no longer OK -> flags LOWER YOUR SHOULDERS

    # Even if nose moves horizontally away from ear (side profile), vertical ear-to-hip reference
    # scale prevents head/nose horizontal distance from masking high shoulders
    side_shrug_pose = {
        "nose": Landmark(0.66, 0.22, 0.99, 0.99),
        "left_ear": Landmark(0.50, 0.20, 0.99, 0.99),
        "left_shoulder": Landmark(0.50, 0.28, 0.99, 0.99),
        "left_hip": Landmark(0.50, 0.80, 0.99, 0.99),
    }
    side_shrug_res = shoulder_tension_alignment(side_shrug_pose, 640, 480, neutral_clearance=1.05)
    assert side_shrug_res is not None
    assert side_shrug_res[0] >= 10.0


def test_frontal_calibration_side_steps_and_pivots(cfg):
    # Enable all-angle mode (front, side, 360 pivots)
    cfg.allow_all_angles = True
    f_front = frame()
    p = f_front.poses[0]
    # Wide frontal stance (shoulders & hips separated horizontally, facing the camera)
    p["left_ear"] = replace(p["left_ear"], x=0.44, y=0.20)
    p["right_ear"] = replace(p["right_ear"], x=0.56, y=0.20)
    p["nose"] = replace(p["nose"], x=0.50, y=0.20)
    p["left_shoulder"] = replace(p["left_shoulder"], x=0.35, y=0.33)
    p["right_shoulder"] = replace(p["right_shoulder"], x=0.65, y=0.33)
    p["left_hip"] = replace(p["left_hip"], x=0.38, y=0.66)
    p["right_hip"] = replace(p["right_hip"], x=0.62, y=0.66)
    # Even if ankles are off-screen during close frontal side steps, all metrics remain valid
    del p["left_ankle"], p["right_ankle"]

    obs = measure(f_front, cfg)
    assert all(q.valid for q in obs.quality.values())
    assert obs.values["trunk_inclination"] == pytest.approx(0.0)
    assert obs.values["head_inclination"] == pytest.approx(0.0)

    # Head horizontal alignment works accurately when facing the camera directly
    head_front = head_horizontal_alignment(p, 640, 480, neutral_offset=0.0)
    assert head_front is not None
    assert head_front[0] == pytest.approx(0.0)


def test_closed_vs_open_embrace_and_knee_bend():
    closed_leader = {
        "left_shoulder": Landmark(0.80, 0.36, 0.99, 0.99),
        "left_elbow": Landmark(0.92, 0.44, 0.99, 0.99),
        "left_wrist": Landmark(0.84, 0.25, 0.99, 0.99),
        "right_shoulder": Landmark(0.54, 0.37, 0.99, 0.99),
        "right_elbow": Landmark(0.47, 0.64, 0.99, 0.99),
        "right_wrist": Landmark(0.67, 0.59, 0.99, 0.99),
        "left_hip": Landmark(0.50, 0.62, 0.99, 0.99),
        "left_knee": Landmark(0.50, 0.80, 0.99, 0.99),
        "left_ankle": Landmark(0.50, 0.98, 0.99, 0.99),
    }
    assert detect_embrace_type(closed_leader, "LEADER", closed_threshold=0.72) == "CLOSED"

    open_leader = {
        "left_shoulder": Landmark(0.80, 0.36, 0.99, 0.99),
        "left_elbow": Landmark(0.95, 0.42, 0.99, 0.99),
        "left_wrist": Landmark(1.12, 0.22, 0.99, 0.99),
        "right_shoulder": Landmark(0.54, 0.37, 0.99, 0.99),
        "right_elbow": Landmark(0.38, 0.56, 0.99, 0.99),
        "right_wrist": Landmark(0.20, 0.56, 0.99, 0.99),
    }
    assert detect_embrace_type(open_leader, "LEADER", closed_threshold=0.72) == "OPEN"

    # EmbraceTracker smoothly transitions CLOSED -> OPEN -> CLOSED even when role is held
    # across frames where raw_role would otherwise flicker, and works in side-profile occlusion
    et = EmbraceTracker(closed_threshold=0.72, switch_ms=200.0, smoothing_sec=0.05)
    assert et.update(closed_leader, "LEADER", 0.0) == "CLOSED"
    assert et.update(open_leader, "LEADER", 0.1) == "CLOSED"  # < 200ms transition hold
    assert et.update(open_leader, "LEADER", 0.35) == "OPEN"  # >= 200ms -> transitions to OPEN
    assert et.update(closed_leader, "LEADER", 0.45) == "OPEN"  # < 200ms transition hold
    assert et.update(closed_leader, "LEADER", 0.70) == "CLOSED"  # >= 200ms -> transitions back to CLOSED

    # Side-profile CLOSED vs OPEN embrace (even when the far embracing arm is occluded behind the torso):
    side_closed_leader = {
        "nose": Landmark(0.45, 0.22, 0.99, 0.99),
        "left_ear": Landmark(0.52, 0.22, 0.99, 0.99),
        "left_shoulder": Landmark(0.58, 0.37, 0.99, 0.99),
        "left_elbow": Landmark(0.62, 0.38, 0.99, 0.99),
        "left_wrist": Landmark(0.58, 0.32, 0.99, 0.99),
        "right_shoulder": Landmark(0.53, 0.40, 0.99, 0.99),
        "right_elbow": Landmark(0.49, 0.47, 0.15, 0.15),  # Far arm occluded
        "right_wrist": Landmark(0.50, 0.44, 0.20, 0.20),
        "left_hip": Landmark(0.55, 0.60, 0.99, 0.99),
        "right_hip": Landmark(0.52, 0.60, 0.99, 0.99),
    }
    assert detect_role(side_closed_leader) == "LEADER"
    assert detect_embrace_type(side_closed_leader, "LEADER", closed_threshold=0.72) == "CLOSED"

    side_open_leader = dict(side_closed_leader)
    side_open_leader["left_elbow"] = Landmark(0.56, 0.42, 0.99, 0.99)
    side_open_leader["left_wrist"] = Landmark(0.52, 0.40, 0.99, 0.99)  # Extended forward (-x)
    assert detect_role(side_open_leader) == "LEADER"
    assert detect_embrace_type(side_open_leader, "LEADER", closed_threshold=0.72) == "OPEN"

    # Straight knee (0 deg bend < 6.0 deg threshold)
    straight_knee = knee_bend_alignment(closed_leader, 640, 480)
    assert straight_knee is not None
    assert straight_knee[0] == pytest.approx(0.0, abs=0.5)

    # Slightly bent knee (knee shifted forward -> bend >= 6.0 deg)
    bent_leader = dict(closed_leader)
    bent_leader["left_knee"] = Landmark(0.54, 0.80, 0.99, 0.99)
    bent_knee = knee_bend_alignment(bent_leader, 640, 480)
    assert bent_knee is not None
    assert bent_knee[0] >= 6.0

    # Even if the far (occluded) leg appears slightly bent due to MediaPipe jitter,
    # a locked/straight supporting leg on the near side is still detected via min(...)
    both_legs_one_straight = dict(closed_leader)
    both_legs_one_straight["right_hip"] = Landmark(0.50, 0.62, 0.99, 0.99)
    both_legs_one_straight["right_knee"] = Landmark(0.55, 0.80, 0.99, 0.99)  # ~15 deg bend on far leg
    both_legs_one_straight["right_ankle"] = Landmark(0.50, 0.98, 0.99, 0.99)
    one_straight_res = knee_bend_alignment(both_legs_one_straight, 640, 480, side="left")
    assert one_straight_res is not None
    assert one_straight_res[0] < 6.0  # Catches the straight left supporting leg


def test_axis_curve_alignment_and_hysteresis(tmp_path):
    # Facing right (+x): posterior ('back / bump') direction is -x (to the left)
    aligned_pose = {
        "nose": Landmark(0.54, 0.20, 0.99, 0.99),
        "left_ear": Landmark(0.50, 0.20, 0.99, 0.99),
        "left_hip": Landmark(0.486, 0.60, 0.99, 0.99),
        "left_ankle": Landmark(0.486, 0.90, 0.99, 0.99),
    }
    res_aligned = axis_curve_alignment(aligned_pose, 640, 480, facing="right", side="left")
    assert res_aligned is not None
    dev_aligned, raw_aligned, head_back, _hip_lm, _ank_lm, _proj_lm = res_aligned
    assert abs(dev_aligned) < 0.01
    assert head_back.x < aligned_pose["left_ear"].x  # Back of head is behind ear when facing right

    # Curving too much: hip/bump pushed backward (-x when facing right) past the ankle->back-of-head line
    curved_pose = dict(aligned_pose)
    curved_pose["left_hip"] = Landmark(0.42, 0.60, 0.99, 0.99)
    res_curved = axis_curve_alignment(
        curved_pose, 640, 480, facing="right", side="left", neutral_offset=raw_aligned
    )
    assert res_curved is not None
    assert res_curved[0] > 0.08  # > 6% entry threshold

    # Facing left (-x): posterior ('back / bump') direction is +x (to the right)
    curved_left_pose = {
        "nose": Landmark(0.46, 0.20, 0.99, 0.99),
        "right_ear": Landmark(0.50, 0.20, 0.99, 0.99),
        "right_hip": Landmark(0.58, 0.60, 0.99, 0.99),
        "right_ankle": Landmark(0.514, 0.90, 0.99, 0.99),
    }
    res_left = axis_curve_alignment(curved_left_pose, 640, 480, facing="left", side="right")
    assert res_left is not None
    assert res_left[0] > 0.08

    # Hysteresis & persistence tracker test:
    # entry=0.06, clear=0.035, entry_duration=1.5s, clear_duration=0.75s
    tracker = AxisCurveTracker(
        entry=0.06,
        clear=0.035,
        entry_duration=1.5,
        clear_duration=0.75,
        smoothing=0.001,
    )
    assert tracker.update(0.02, 0.0)["state"] == "WITHIN_LIMIT"
    assert tracker.update(0.09, 0.2)["state"] == "PENDING"
    assert tracker.update(0.09, 0.6)["state"] == "PENDING"
    assert tracker.update(0.09, 1.0)["state"] == "PENDING"  # 0.8s elapsed (< 1.5s)
    assert tracker.update(0.09, 1.4)["state"] == "PENDING"  # 1.2s elapsed (< 1.5s)
    st_dev = tracker.update(0.09, 1.7)  # 1.5s elapsed -> DEVIATION confirmed
    assert st_dev["state"] == "DEVIATION"
    assert st_dev["alert"] is True

    # Drops to 0.045 (below entry=0.06, but still above clear=0.035) -> must stay in DEVIATION (hysteresis)
    assert tracker.update(0.045, 2.0)["state"] == "DEVIATION"
    assert tracker.update(0.045, 2.4)["state"] == "DEVIATION"

    # Drops below clear=0.035 (to 0.02), but for < 0.75s -> still in DEVIATION until clear_duration met
    assert tracker.update(0.02, 2.7)["state"] == "DEVIATION"
    assert tracker.update(0.02, 3.1)["state"] == "DEVIATION"  # 0.4s < 0.75s
    st_cleared = tracker.update(0.02, 3.46)  # 0.76s >= 0.75s -> cleared
    assert st_cleared["state"] == "WITHIN_LIMIT"
    assert st_cleared["alert"] is False

    # Config env and TOML loading for axis_curve and smoothing parameters
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AXIS_CURVE_ENTRY=0.07\n"
        "AXIS_CURVE_CLEAR=0.04\n"
        "AXIS_CURVE_DURATION_SECONDS=2.0\n"
        "SMOOTHING_SECONDS=0.40\n"
        "OVERLAY_SMOOTHING_SECONDS=0.35\n"
    )
    cfg_loaded = load(None, {}, env_path=env_file)
    assert cfg_loaded.axis_curve_entry == pytest.approx(0.07)
    assert cfg_loaded.axis_curve_clear == pytest.approx(0.04)
    assert cfg_loaded.axis_curve_duration == pytest.approx(2.0)
    assert cfg_loaded.smoothing == pytest.approx(0.40)
    assert cfg_loaded.overlay_smoothing == pytest.approx(0.35)


def test_pose_smoother_reduces_landmark_jitter_and_blinking():
    smoother = PoseSmoother(smoothing_sec=0.30, max_gap=0.5)
    p0 = [{"left_shoulder": Landmark(0.50, 0.40, 0.90, 0.90)}]
    p1 = [{"left_shoulder": Landmark(0.60, 0.50, 0.65, 0.65)}]

    out0 = smoother.update(p0, 0.0)
    assert out0[0]["left_shoulder"].x == pytest.approx(0.50)

    out1 = smoother.update(p1, 0.033)
    # Smoothed coordinate moves only a fraction of the step (avoids sudden jumps)
    assert 0.50 < out1[0]["left_shoulder"].x < 0.53
    assert 0.40 < out1[0]["left_shoulder"].y < 0.43
    # Smoothed visibility stays above 0.70 threshold across a 1-frame dip to 0.65
    assert out1[0]["left_shoulder"].visibility is not None
    assert out1[0]["left_shoulder"].visibility > 0.75

    # Disabled when smoothing_sec == 0.0
    passthrough = PoseSmoother(smoothing_sec=0.0).update(p1, 0.033)
    assert passthrough[0]["left_shoulder"].x == pytest.approx(0.60)


def test_embrace_hysteresis_seconds_and_slow_tango_ankle_steps(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EMBRACE_HYSTERESIS_SECONDS=1.2\n"
        "SLOW_TANGO_MAX_BPM=115.0\n"
        "ANKLE_HIGH_LIFT_RATIO=0.06\n"
        "MAX_CONSECUTIVE_HIGH_ANKLE_STEPS=2\n"
    )
    cfg_loaded = load(None, {}, env_path=env_file)
    assert cfg_loaded.embrace_hysteresis_seconds == pytest.approx(1.2)
    assert cfg_loaded.slow_tango_max_bpm == pytest.approx(115.0)
    assert cfg_loaded.ankle_high_lift_ratio == pytest.approx(0.06)
    assert cfg_loaded.max_consecutive_high_ankle_steps == 2

    # 1. Embrace hysteresis in seconds (e.g. 1.2s required before switching CLOSED <-> OPEN)
    closed_pose = {
        "left_shoulder": Landmark(0.73, 0.29, 0.99, 0.99),
        "left_elbow": Landmark(0.94, 0.33, 0.99, 0.99),
        "left_wrist": Landmark(0.84, 0.08, 0.99, 0.99),
        "right_shoulder": Landmark(0.46, 0.30, 0.99, 0.99),
        "right_elbow": Landmark(0.36, 0.49, 0.99, 0.99),
        "right_wrist": Landmark(0.60, 0.41, 0.99, 0.99),
        "left_hip": Landmark(0.68, 0.62, 0.99, 0.99),
        "right_hip": Landmark(0.48, 0.62, 0.99, 0.99),
    }
    open_pose = dict(closed_pose)
    open_pose["right_elbow"] = Landmark(0.33, 0.43, 0.99, 0.99)
    open_pose["right_wrist"] = Landmark(0.14, 0.45, 0.99, 0.99)

    et = EmbraceTracker(closed_threshold=0.72, switch_sec=1.2, smoothing_sec=0.05)
    assert et.update(closed_pose, "LEADER", 0.0) == "CLOSED"
    assert et.update(open_pose, "LEADER", 0.5) == "CLOSED"  # 0.5s < 1.2s hysteresis
    assert et.update(open_pose, "LEADER", 1.1) == "CLOSED"  # 1.1s < 1.2s hysteresis
    assert et.update(open_pose, "LEADER", 1.8) == "OPEN"  # >= 1.2s -> switches to OPEN

    # 2. Slow-tango (< 115 BPM, e.g. Di Sarli at 108 BPM) consecutive high-ankle step counter
    def step_pose(left_ankle_y: float, right_ankle_y: float = 0.90) -> dict[str, Landmark]:
        return {
            "left_hip": Landmark(0.50, 0.50, 0.99, 0.99),
            "right_hip": Landmark(0.50, 0.50, 0.99, 0.99),
            "left_knee": Landmark(0.50, 0.70, 0.99, 0.99),
            "right_knee": Landmark(0.50, 0.70, 0.99, 0.99),
            "left_ankle": Landmark(0.50, left_ankle_y, 0.99, 0.99),
            "right_ankle": Landmark(0.50, right_ankle_y, 0.99, 0.99),
        }

    # Leg length is 0.40; 6% high lift threshold corresponds to dy >= 0.024 (ankle y <= 0.876)
    grounded = step_pose(0.90, 0.90)
    high_lift = step_pose(0.85, 0.90)  # dy = 0.05 -> 12.5% lift (> 6%)
    low_glide = step_pose(0.89, 0.90)  # dy = 0.01 -> 2.5% lift (<= 6%)

    assert ankle_lift_alignment(high_lift)[0] == pytest.approx(0.125)

    at = AnkleStepTracker(
        high_lift_ratio=0.06,
        max_consecutive_steps=2,
        slow_tango_max_bpm=115.0,
        smoothing_sec=0.01,
    )
    # Start grounded at 108 BPM (Di Sarli slow tango <= 115 BPM)
    st0 = at.update(grounded, 0.0, bpm=108.0)
    assert st0["consecutive_high_steps"] == 0 and st0["alert"] is False

    # Step 1: high ankle lift -> consecutive_high_steps becomes 1 (no alert yet, need 2)
    st1 = at.update(high_lift, 0.2, bpm=108.0)
    assert st1["consecutive_high_steps"] == 1
    assert st1["alert"] is False
    # Land step 1 back on the floor
    at.update(grounded, 0.5, bpm=108.0)
    assert at.consecutive_high_steps == 1

    # Step 2: second consecutive high ankle lift -> consecutive_high_steps becomes 2 -> ALERT!
    st2 = at.update(high_lift, 0.8, bpm=108.0)
    assert st2["consecutive_high_steps"] == 2
    assert st2["alert"] is True
    at.update(grounded, 1.1, bpm=108.0)

    # Step 3: low gliding step close to the floor (<= 6%) -> resets consecutive high steps & clears alert
    at.update(low_glide, 1.3, bpm=108.0)
    at.update(low_glide, 1.5, bpm=108.0)
    st_reset = at.update(grounded, 1.7, bpm=108.0)
    assert st_reset["consecutive_high_steps"] == 0
    assert st_reset["alert"] is False

    # At fast tempo (> 115 BPM, e.g. D'Arienzo at 128 BPM), slow-tango high-ankle alert is inactive
    st_fast = at.update(high_lift, 2.0, bpm=128.0)
    assert st_fast["slow_tango_active"] is False
    assert st_fast["alert"] is False


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

    # In microphone mode (require_audio=True), beats MUST NOT be counted before a song begins
    # or when music stops
    mic_tracker = BeatTracker(
        bpm=120.0, enable_audio=False, require_audio=True, analysis_seconds=0.0
    )
    st_waiting_0 = mic_tracker.state_at(0.0)
    st_waiting_2 = mic_tracker.state_at(2.0)
    assert st_waiting_0["playing"] is False
    assert st_waiting_2["playing"] is False
    assert st_waiting_0["beat"] == 0
    assert st_waiting_0["phrase"] == 0
    assert st_waiting_2["beat"] == 0  # Still 0 (not counting) after 2s of silence
    assert st_waiting_2["source"] == "waiting-for-song"

    # Feed active music with 120 BPM onsets (every 0.5s) from t=2.0s to t=4.5s
    t = 2.0
    while t <= 4.5:
        is_beat = abs((t - 2.0) % 0.5) < 1e-6 or abs(((t - 2.0) % 0.5) - 0.5) < 1e-6
        mic_tracker.process_flux(2.0 if is_beat else 0.1, round(t, 2), rms=0.05)
        mic_tracker.state_at(round(t, 2))
        t += 0.05
    playing_state = mic_tracker.state_at(4.5)
    assert playing_state["playing"] is True
    assert playing_state["beat"] >= 1
    assert playing_state["phrase"] >= 1

    # Music is paused at t=4.55s (RMS drops to silence) -> must stop counting beats/phrases
    while t <= 8.0:
        mic_tracker.process_flux(0.0, round(t, 2), rms=0.0)
        t += 0.05
    paused_at_5s = mic_tracker.state_at(5.0)
    paused_at_8s = mic_tracker.state_at(8.0)
    assert paused_at_5s["playing"] is False
    assert paused_at_8s["playing"] is False
    assert paused_at_5s["beat"] == 0
    assert paused_at_8s["beat"] == 0
    assert paused_at_8s["phrase"] == 0
    assert paused_at_8s["source"] == "mic-paused"


def test_tango_30s_phrase_start_analysis_and_audio_recording(tmp_path):
    sr = 16000
    beat_sec = 0.5  # 120 BPM (8-beat phrase = 4.0s)
    total_beats = 64  # 32.0 seconds (8 full 8-beat phrases)
    # Start the recording mid-phrase so the true Beat 1 falls at beat index k = 3, 11, 19, ...
    true_phrase_offset = 3
    total_samples = int(total_beats * beat_sec * sr)
    audio = np.zeros(total_samples, dtype=np.float32)
    t_arr = np.arange(int(beat_sec * sr), dtype=np.float32) / sr

    for k in range(total_beats):
        phrase_beat = ((k - true_phrase_offset) % 8) + 1  # 1..8
        phrase_num = (k - true_phrase_offset) // 8
        start = int(k * beat_sec * sr)
        end = start + len(t_arr)
        # Decay envelope for marcato attack
        env = np.exp(-t_arr / (0.28 if phrase_beat == 1 else 0.14 if phrase_beat == 5 else 0.09))
        if phrase_beat == 8:
            # Phrase cadence / breath before Beat 1
            env *= 0.35
        # Contrabajo + piano bass marcato (90 Hz + 180 Hz)
        amp = 0.45 if phrase_beat == 1 else 0.28 if phrase_beat in (3, 5, 7) else 0.14
        sig = amp * env * (np.sin(2 * np.pi * 95.0 * t_arr) + 0.6 * np.sin(2 * np.pi * 190.0 * t_arr))
        # Orchestration / harmonic shift at the start of every 8-beat phrase (Beat 1)
        chord_freq = 520.0 if (phrase_num % 2 == 0) else 1180.0
        chord_amp = 0.35 if phrase_beat == 1 else 0.18 if phrase_beat <= 4 else 0.10
        sig += chord_amp * env * np.sin(2 * np.pi * chord_freq * t_arr)
        audio[start:end] = sig.astype(np.float32)

    wav_out = tmp_path / "videos" / "cara_sucia_test.wav"
    tracker = BeatTracker(
        bpm=120.0,
        enable_audio=False,
        require_audio=True,
        analysis_seconds=30.0,
        audio_save_path=wav_out,
    )
    block = 1024
    # First feed 4 seconds of ambient room noise (RMS ~0.003) before any song begins
    rng = np.random.default_rng(42)
    ambient = rng.normal(0.0, 0.003, size=4 * sr).astype(np.float32)
    for idx in range(0, len(ambient), block):
        chunk = ambient[idx : idx + block]
        ts = (idx + len(chunk)) / sr
        tracker.feed_samples(chunk, ts)

    st_before_song = tracker.state_at(4.0)
    assert tracker.song_detected is False
    assert st_before_song["playing"] is False
    assert st_before_song["beat"] == 0
    assert st_before_song["phrase"] == 0
    assert st_before_song["analyzed_seconds"] == 0.0
    assert st_before_song["source"] == "waiting-for-song"

    # Now start the song at t = 4.0s and track all beats observed across the phrases
    observed_beats: set[int] = set()
    for idx in range(0, len(audio), block):
        chunk = audio[idx : idx + block]
        ts = 4.0 + (idx + len(chunk)) / sr
        tracker.feed_samples(chunk, ts)
        st_cur = tracker.state_at(ts)
        if st_cur["playing"] and st_cur["beat"] > 0:
            observed_beats.add(int(st_cur["beat"]))

    # Must progress through all 8 beats (1, 2, 3, 4, 5, 6, 7, 8) of the phrase
    assert observed_beats == {1, 2, 3, 4, 5, 6, 7, 8}
    assert tracker.song_detected is True
    assert tracker.phrase_locked is True
    anchor_in_beats = round(tracker.phrase_anchor_frame / tracker.tau_frames) % 8
    assert anchor_in_beats == true_phrase_offset
    assert 118.0 <= tracker.bpm <= 122.0

    # At beat k = 64 (after 4.0s silence + 32.0s song = 36.0s):
    # After 64 beats starting at offset 3, 64 - 3 = 61 beats after first Beat 1 -> 61 % 8 = 5 (Beat 6)
    st_end = tracker.state_at(36.01)
    assert st_end["beat"] in (5, 6)
    assert st_end["phrase_locked"] is True

    saved = tracker.save_audio(wav_out)
    assert wav_out.is_file()
    assert len(saved) >= 1
