from __future__ import annotations

import math
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

from tango_posture.domain import FrameObservation, Landmark
from tango_posture.movement import (
    DETECTOR_VERSION,
    MOVEMENT_SCHEMA_VERSION,
    analyse_movement_observations,
    detector_fingerprint,
    hip_heading_3d_deg,
    render_movement_overlay,
    scan_video_movement,
    step_opacity,
    unwrap_angles,
    validate_movement_analysis,
    wrapped_angle_diff,
)


def _make_pose_observation(
    timestamp: float,
    *,
    hip_center_2d: tuple[float, float] = (0.5, 0.50),
    hip_yaw_deg: float = 0.0,
    left_foot_2d: tuple[float, float] = (0.46, 0.86),
    right_foot_2d: tuple[float, float] = (0.54, 0.86),
    left_foot_angle_deg: float = 90.0,
    right_foot_angle_deg: float = 90.0,
    foot_len_norm: float = 0.05,
    include_world: bool = True,
    confidence: float = 0.95,
) -> FrameObservation:
    """Build a synthetic single-dancer FrameObservation with 2D and 3D landmarks."""
    yaw_rad = math.radians(hip_yaw_deg)
    # 2D hip projection: width shrinks as cos(yaw), with small vertical perspective offset
    hip_half_2d = 0.06 * math.cos(yaw_rad)
    cx, cy = hip_center_2d
    lx_2d = cx - hip_half_2d
    rx_2d = cx + hip_half_2d
    # Keep a minimum 2D hip span when nearly side-on if needed, or let it follow cos(yaw)
    if abs(rx_2d - lx_2d) < 0.035:
        sign = 1.0 if rx_2d >= lx_2d else -1.0
        lx_2d = cx - 0.02 * sign
        rx_2d = cx + 0.02 * sign

    l_ang = math.radians(left_foot_angle_deg)
    r_ang = math.radians(right_foot_angle_deg)
    lhx = left_foot_2d[0] - 0.4 * foot_len_norm * math.cos(l_ang)
    lhy = left_foot_2d[1] - 0.4 * foot_len_norm * math.sin(l_ang)
    ltx = left_foot_2d[0] + 0.6 * foot_len_norm * math.cos(l_ang)
    lty = left_foot_2d[1] + 0.6 * foot_len_norm * math.sin(l_ang)
    lax = left_foot_2d[0] - 0.2 * foot_len_norm * math.cos(l_ang)
    lay = left_foot_2d[1] - 0.02

    rhx = right_foot_2d[0] - 0.4 * foot_len_norm * math.cos(r_ang)
    rhy = right_foot_2d[1] - 0.4 * foot_len_norm * math.sin(r_ang)
    rtx = right_foot_2d[0] + 0.6 * foot_len_norm * math.cos(r_ang)
    rty = right_foot_2d[1] + 0.6 * foot_len_norm * math.sin(r_ang)
    rax = right_foot_2d[0] - 0.2 * foot_len_norm * math.cos(r_ang)
    ray = right_foot_2d[1] - 0.02

    def lm2(x: float, y: float) -> Landmark:
        return Landmark(x=x, y=y, visibility=confidence, presence=confidence, z=0.0)

    pose_2d = {
        "nose": lm2(cx, cy - 0.32),
        "left_ear": lm2(cx - 0.03, cy - 0.33),
        "right_ear": lm2(cx + 0.03, cy - 0.33),
        "left_shoulder": lm2(cx - 0.07, cy - 0.22),
        "right_shoulder": lm2(cx + 0.07, cy - 0.22),
        "left_hip": lm2(lx_2d, cy),
        "right_hip": lm2(rx_2d, cy),
        "left_knee": lm2(left_foot_2d[0], 0.5 * (cy + left_foot_2d[1])),
        "right_knee": lm2(right_foot_2d[0], 0.5 * (cy + right_foot_2d[1])),
        "left_ankle": lm2(lax, lay),
        "right_ankle": lm2(rax, ray),
        "left_heel": lm2(lhx, lhy),
        "right_heel": lm2(rhx, rhy),
        "left_foot_index": lm2(ltx, lty),
        "right_foot_index": lm2(rtx, rty),
    }

    world_poses: list[dict[str, Landmark]] = []
    if include_world:
        # In hip_heading_3d_deg: dx = r_hip.x - l_hip.x, dz = r_hip.z - l_hip.z, heading = atan2(dz, dx)
        r_3d = 0.14
        dx_3d = r_3d * math.cos(yaw_rad)
        dz_3d = r_3d * math.sin(yaw_rad)
        world_poses.append(
            {
                "left_hip": Landmark(
                    x=-0.5 * dx_3d,
                    y=0.0,
                    z=-0.5 * dz_3d,
                    visibility=confidence,
                    presence=confidence,
                ),
                "right_hip": Landmark(
                    x=0.5 * dx_3d,
                    y=0.0,
                    z=0.5 * dz_3d,
                    visibility=confidence,
                    presence=confidence,
                ),
            }
        )

    return FrameObservation(
        timestamp=timestamp,
        width=640,
        height=480,
        poses=[pose_2d],
        world_poses=world_poses,
    )


def test_angle_unwrap_and_heading_sign_conventions():
    assert math.isclose(wrapped_angle_diff(10.0, -10.0), 20.0)
    assert math.isclose(wrapped_angle_diff(-170.0, 170.0), 20.0)
    assert math.isclose(wrapped_angle_diff(170.0, -170.0), -20.0)

    seq = [160.0, 175.0, -170.0, -150.0]
    unwrapped = unwrap_angles(seq)
    assert np.allclose(unwrapped, [160.0, 175.0, 190.0, 210.0])

    rev = [-160.0, -175.0, 170.0, 150.0]
    assert np.allclose(unwrap_angles(rev), [-160.0, -175.0, -190.0, -210.0])

    with pytest.raises(ValueError, match="finite"):
        unwrap_angles([0.0, float("nan")])

    # Verify clockwise (+heading) vs anticlockwise (-heading) 3D hip convention
    obs_cw_0 = _make_pose_observation(0.0, hip_yaw_deg=0.0)
    obs_cw_45 = _make_pose_observation(0.5, hip_yaw_deg=45.0)
    obs_ccw_45 = _make_pose_observation(0.5, hip_yaw_deg=-45.0)
    h0 = hip_heading_3d_deg(
        obs_cw_0.world_poses[0]["left_hip"], obs_cw_0.world_poses[0]["right_hip"]
    )
    h_cw = hip_heading_3d_deg(
        obs_cw_45.world_poses[0]["left_hip"], obs_cw_45.world_poses[0]["right_hip"]
    )
    h_ccw = hip_heading_3d_deg(
        obs_ccw_45.world_poses[0]["left_hip"], obs_ccw_45.world_poses[0]["right_hip"]
    )
    assert h0 is not None and h_cw is not None and h_ccw is not None
    assert math.isclose(h0, 0.0, abs_tol=1e-4)
    assert math.isclose(h_cw, 45.0, abs_tol=1e-4)
    assert math.isclose(h_ccw, -45.0, abs_tol=1e-4)


@pytest.mark.parametrize(
    ("target_deg", "expected_direction"),
    [(55.0, "clockwise"), (-55.0, "anticlockwise")],
)
def test_pivot_detection_clockwise_and_anticlockwise_and_wraparound(target_deg, expected_direction):
    fps = 15.0
    frames = []
    # Stand still 0..10, pivot 10..20 across the 180/-180 wrap boundary, stand still 20..30
    base_yaw = 165.0 if target_deg > 0 else -165.0
    for i in range(32):
        t = i / fps
        if i < 10:
            yaw = base_yaw
        elif i <= 20:
            yaw = base_yaw + target_deg * ((i - 10) / 10.0)
        else:
            yaw = base_yaw + target_deg
        # Wrap yaw into [-180, 180] to verify unwrapping across +/-180
        wrapped_yaw = ((yaw + 180.0) % 360.0) - 180.0
        obs = _make_pose_observation(
            t,
            hip_yaw_deg=wrapped_yaw,
            left_foot_2d=(0.47, 0.86),
            right_foot_2d=(0.53, 0.86),
        )
        frames.append((i, obs))

    result = analyse_movement_observations(
        frames, width=640, height=480, fps=fps, frame_count=32, sample_stride=1
    )
    assert len(result["pivots"]) == 1
    pivot = result["pivots"][0]
    assert pivot["direction"] == expected_direction
    assert 9 <= pivot["start_frame"] <= 12
    assert 19 <= pivot["end_frame"] <= 22
    assert abs(pivot["net_rotation_deg"]) >= 35.0
    # Hip geometry frames are recorded only within the pivot interval
    hip_frame_indices = [hf["frame"] for hf in result["hip_frames"]]
    assert hip_frame_indices
    assert all(pivot["start_frame"] <= f < pivot["end_frame"] for f in hip_frame_indices)


def test_pivot_rejects_both_feet_translation_missing_3d_and_jitter():
    fps = 15.0
    # 1. Both feet translating across the floor while turning -> no pivot
    translating_frames = []
    for i in range(25):
        t = i / fps
        shift = 0.018 * i
        obs = _make_pose_observation(
            t,
            hip_center_2d=(0.30 + shift, 0.50),
            hip_yaw_deg=3.5 * i,
            left_foot_2d=(0.28 + shift, 0.86),
            right_foot_2d=(0.32 + shift, 0.86),
        )
        translating_frames.append((i, obs))
    res_trans = analyse_movement_observations(
        translating_frames, width=640, height=480, fps=fps, frame_count=25, sample_stride=1
    )
    assert res_trans["pivots"] == []

    # 2. Missing 3D world landmarks -> no pivot even if 2D hips move
    no_world_frames = [
        (i, _make_pose_observation(i / fps, hip_yaw_deg=4.0 * i, include_world=False))
        for i in range(25)
    ]
    res_no_world = analyse_movement_observations(
        no_world_frames, width=640, height=480, fps=fps, frame_count=25, sample_stride=1
    )
    assert res_no_world["pivots"] == []

    # 3. Wraparound jitter (+/- 6 deg across 180/-180) -> no pivot
    jitter_frames = [
        (
            i,
            _make_pose_observation(
                i / fps, hip_yaw_deg=(177.0 if i % 2 == 0 else -177.0)
            ),
        )
        for i in range(30)
    ]
    res_jitter = analyse_movement_observations(
        jitter_frames, width=640, height=480, fps=fps, frame_count=30, sample_stride=1
    )
    assert res_jitter["pivots"] == []


def test_pivot_splits_on_reversal_and_never_interpolates_across_tracking_gap():
    fps = 30.0
    # Sample at stride=2 (every 2nd frame) with a 0.5s tracking gap in the middle
    sampled_frames = []
    for i in range(0, 90, 2):
        if 36 <= i <= 52:
            # Tracking gap of >0.5s (no pose)
            sampled_frames.append(
                (
                    i,
                    FrameObservation(
                        timestamp=i / fps, width=640, height=480, poses=[], world_poses=[]
                    ),
                )
            )
            continue
        if i < 36:
            yaw = min(50.0, max(0.0, (i - 6) * 2.2))
        else:
            yaw = 50.0 - min(50.0, max(0.0, (i - 56) * 2.2))
        sampled_frames.append((i, _make_pose_observation(i / fps, hip_yaw_deg=yaw)))

    res = analyse_movement_observations(
        sampled_frames, width=640, height=480, fps=fps, frame_count=90, sample_stride=2
    )
    assert len(res["pivots"]) == 2
    assert res["pivots"][0]["direction"] == "clockwise"
    assert res["pivots"][1]["direction"] == "anticlockwise"
    assert res["pivots"][0]["end_frame"] <= 36
    assert res["pivots"][1]["start_frame"] >= 54
    # Within the first pivot, odd frames between stride=2 samples are interpolated,
    # while no frames in the gap [36..53] exist in hip_frames.
    hip_frames_set = {hf["frame"] for hf in res["hip_frames"]}
    assert any(f % 2 == 1 for f in hip_frames_set)
    assert all(not (36 <= f <= 53) for f in hip_frames_set)


def test_step_placements_detect_real_steps_and_debounce():
    fps = 15.0
    frames = []
    # Left foot starts at x=0.35 (frames 0..5), moves to x=0.52 (frames 6..11), settles (frames 12..20)
    # Right foot stays anchored at (0.58, 0.86) until frame 20, then steps to (0.72, 0.86) at 21..26 and settles 27..35
    for i in range(36):
        t = i / fps
        if i <= 5:
            lx = 0.35
        elif i <= 11:
            lx = 0.35 + (0.52 - 0.35) * ((i - 5) / 6.0)
        else:
            lx = 0.52

        if i <= 20:
            rx = 0.58
        elif i <= 26:
            rx = 0.58 + (0.72 - 0.58) * ((i - 20) / 6.0)
        else:
            rx = 0.72

        frames.append(
            (
                i,
                _make_pose_observation(
                    t,
                    left_foot_2d=(lx, 0.86),
                    right_foot_2d=(rx, 0.86),
                ),
            )
        )

    res = analyse_movement_observations(
        frames, width=640, height=480, fps=fps, frame_count=36, sample_stride=1
    )
    steps = res["steps"]
    assert len(steps) == 2
    assert steps[0]["side"] == "left"
    assert 11 <= steps[0]["frame"] <= 14
    assert math.isclose(steps[0]["x"], 0.52 * 640, rel_tol=0.05)
    assert steps[1]["side"] == "right"
    assert 26 <= steps[1]["frame"] <= 29
    assert math.isclose(steps[1]["x"], 0.72 * 640, rel_tol=0.05)


def test_step_placements_detect_short_gliding_steps():
    fps = 15.0
    frames = []
    for i in range(20):
        if i <= 3:
            left_x = 0.45
        elif i <= 9:
            left_x = 0.45 + (0.468 - 0.45) * ((i - 3) / 6.0)
        else:
            left_x = 0.468 + (0.002 if i % 2 else -0.002)
        frames.append(
            (
                i,
                _make_pose_observation(
                    i / fps,
                    left_foot_2d=(left_x, 0.86),
                    right_foot_2d=(0.56, 0.86),
                ),
            )
        )

    result = analyse_movement_observations(
        frames, width=640, height=480, fps=fps, frame_count=len(frames), sample_stride=1
    )

    assert len(result["steps"]) == 1
    assert result["steps"][0]["side"] == "left"
    assert math.isclose(result["steps"][0]["x"], 0.468 * 640, rel_tol=0.03)


def test_step_placements_accept_moderate_foot_visibility():
    fps = 15.0
    frames = []
    for i in range(24):
        if i <= 4:
            right_x = 0.56
        elif i <= 10:
            right_x = 0.56 + (0.70 - 0.56) * ((i - 4) / 6.0)
        else:
            right_x = 0.70
        observation = _make_pose_observation(
            i / fps,
            left_foot_2d=(0.42, 0.86),
            right_foot_2d=(right_x, 0.86),
        )
        for landmark_name in ("right_ankle", "right_heel", "right_foot_index"):
            landmark = observation.poses[0][landmark_name]
            observation.poses[0][landmark_name] = Landmark(
                x=landmark.x,
                y=landmark.y,
                z=landmark.z,
                visibility=0.50,
                presence=0.50,
            )
        frames.append((i, observation))

    result = analyse_movement_observations(
        frames, width=640, height=480, fps=fps, frame_count=len(frames), sample_stride=1
    )

    assert len(result["steps"]) == 1
    assert result["steps"][0]["side"] == "right"


def test_step_detector_rejects_swivel_airborne_and_post_occlusion():
    fps = 15.0
    # 1. In-place foot swivel: left heel stays fixed while toe rotates 75 deg and settles
    swivel_frames = []
    for i in range(25):
        t = i / fps
        ang = 90.0 if i < 5 else (90.0 + 75.0 * min(1.0, (i - 5) / 5.0))
        # Keep heel fixed at (0.45, 0.85) by shifting midpoint slightly as angle changes
        rad = math.radians(ang)
        foot_len = 0.05
        heel_x, heel_y = 0.45, 0.85
        mid_x = heel_x + 0.4 * foot_len * math.cos(rad)
        mid_y = heel_y + 0.4 * foot_len * math.sin(rad)
        swivel_frames.append(
            (
                i,
                _make_pose_observation(
                    t,
                    left_foot_2d=(mid_x, mid_y),
                    left_foot_angle_deg=ang,
                ),
            )
        )
    res_swivel = analyse_movement_observations(
        swivel_frames, width=640, height=480, fps=fps, frame_count=25, sample_stride=1
    )
    assert res_swivel["steps"] == []

    # 2. Suspended / airborne foot: left foot lifts high into the air (y=0.68 vs right foot y=0.86) and holds still
    airborne_frames = []
    for i in range(25):
        t = i / fps
        if i <= 5:
            lx, ly = 0.45, 0.86
        elif i <= 11:
            frac = (i - 5) / 6.0
            lx = 0.45 + 0.14 * frac
            ly = 0.86 - 0.18 * frac
        else:
            lx, ly = 0.59, 0.68
        airborne_frames.append(
            (i, _make_pose_observation(t, left_foot_2d=(lx, ly), right_foot_2d=(0.54, 0.86)))
        )
    res_airborne = analyse_movement_observations(
        airborne_frames, width=640, height=480, fps=fps, frame_count=25, sample_stride=1
    )
    assert res_airborne["steps"] == []

    # 3. Post-occlusion reappearance: left foot is occluded for 0.6s and reappears settled at a new position
    occluded_frames = []
    for i in range(30):
        t = i / fps
        if i < 6:
            obs = _make_pose_observation(t, left_foot_2d=(0.40, 0.86))
        elif i < 16:
            obs = _make_pose_observation(t, left_foot_2d=(0.40, 0.86))
            # Mark left foot landmarks low confidence (occluded)
            for k in ("left_ankle", "left_heel", "left_foot_index"):
                obs.poses[0][k] = Landmark(x=0.40, y=0.86, visibility=0.1, presence=0.1)
        else:
            # Reappears already settled at x=0.60
            obs = _make_pose_observation(t, left_foot_2d=(0.60, 0.86))
        occluded_frames.append((i, obs))
    res_occ = analyse_movement_observations(
        occluded_frames, width=640, height=480, fps=fps, frame_count=30, sample_stride=1
    )
    assert res_occ["steps"] == []


def test_step_opacity_ramp_and_fade_window():
    # Default 5.0s window: ramp = min(0.15, 0.15 * 5.0) = 0.15s
    assert step_opacity(-0.01, 5.0) == 0.0
    assert step_opacity(0.0, 5.0) == 0.0
    half_ramp = step_opacity(0.075, 5.0, peak_opacity=0.88)
    peak = step_opacity(0.15, 5.0, peak_opacity=0.88)
    mid_fade = step_opacity(2.575, 5.0, peak_opacity=0.88)
    assert math.isclose(half_ramp, 0.44, rel_tol=1e-3)
    assert math.isclose(peak, 0.88, rel_tol=1e-3)
    assert math.isclose(mid_fade, 0.44, rel_tol=1e-3)
    assert step_opacity(5.0, 5.0) == 0.0
    assert step_opacity(5.5, 5.0) == 0.0


def test_renderer_immutability_deterministic_seek_and_mirror_reflection():
    analysis = {
        "schema_version": MOVEMENT_SCHEMA_VERSION,
        "detector_version": DETECTOR_VERSION,
        "detector_fingerprint": detector_fingerprint(),
        "segment_index": 0,
        "source_member": "video_00.mp4",
        "source_sha256": "a" * 64,
        "model_hash": "b" * 64,
        "width": 320,
        "height": 240,
        "fps": 10.0,
        "frame_count": 100,
        "duration": 10.0,
        "sample_stride": 1,
        "timing_basis": "frame_index_over_fps",
        "coverage": {
            "sampled_frames": 100,
            "valid_pose_frames": 100,
            "pivot_valid_frames": 100,
            "left_foot_valid_frames": 100,
            "right_foot_valid_frames": 100,
            "pose_coverage": 1.0,
            "pivot_coverage": 1.0,
            "left_foot_coverage": 1.0,
            "right_foot_coverage": 1.0,
        },
        "cut_frames": [],
        "pivots": [
            {
                "start_frame": 10,
                "end_frame": 20,
                "start_time": 1.0,
                "end_time": 2.0,
                "direction": "clockwise",
                "net_rotation_deg": 45.0,
                "mean_rate_deg_s": 45.0,
                "support_side": "left",
                "quality": 0.95,
            }
        ],
        "hip_frames": [
            {
                "frame": 15,
                "cx": 100.0,
                "cy": 120.0,
                "lx": 75.0,
                "ly": 120.0,
                "rx": 125.0,
                "ry": 120.0,
                "scale": 110.0,
                "hip_span": 50.0,
            }
        ],
        "steps": [
            {
                "frame": 10,
                "time": 1.0,
                "side": "left",
                "hx": 80.0,
                "hy": 200.0,
                "tx": 80.0,
                "ty": 222.0,
                "ax": 80.0,
                "ay": 195.0,
                "x": 80.0,
                "y": 210.0,
                "length_px": 22.0,
                "angle_deg": 90.0,
                "confidence": 0.92,
            }
        ],
    }
    validate_movement_analysis(analysis, expected_segment_index=0, expected_source_sha256="a" * 64)

    raw_frame = np.full((240, 320, 3), 40, dtype=np.uint8)
    raw_backup = raw_frame.copy()

    # Render at frame 15 (t=1.5s, inside pivot and 0.5s after step)
    out_15_a = render_movement_overlay(
        raw_frame,
        analysis,
        15,
        10.0,
        show_pivots=True,
        show_steps=True,
        step_history_seconds=5.0,
        mirror=False,
    )
    # Input image is never mutated
    assert np.array_equal(raw_frame, raw_backup)
    assert not np.array_equal(out_15_a, raw_frame)

    # Render at frame 60 (t=6.0s, 5.0s after step at t=1.0s -> step fully faded & outside pivot)
    out_60 = render_movement_overlay(
        raw_frame,
        analysis,
        60,
        10.0,
        show_pivots=True,
        show_steps=True,
        step_history_seconds=5.0,
        mirror=False,
    )
    assert np.array_equal(out_60, raw_frame)

    # Seeking backwards to frame 15 produces identical pixels
    out_15_b = render_movement_overlay(
        raw_frame,
        analysis,
        15,
        10.0,
        show_pivots=True,
        show_steps=True,
        step_history_seconds=5.0,
        mirror=False,
    )
    assert np.array_equal(out_15_a, out_15_b)

    # Mirrored render reflects geometry to the right half of the image without mutating analysis
    out_15_mirror = render_movement_overlay(
        raw_frame,
        analysis,
        15,
        10.0,
        show_pivots=True,
        show_steps=True,
        step_history_seconds=5.0,
        mirror=True,
    )
    assert analysis["pivots"][0]["direction"] == "clockwise"
    assert analysis["steps"][0]["hx"] == 80.0
    # Unmirrored modifications are on the left side (x < 160); mirrored are on the right side (x > 160)
    diff_unmirrored = np.abs(out_15_a.astype(int) - raw_frame.astype(int)).sum(axis=2)
    diff_mirrored = np.abs(out_15_mirror.astype(int) - raw_frame.astype(int)).sum(axis=2)
    assert diff_unmirrored[:, :160].sum() > 0 and diff_unmirrored[:, 180:].sum() == 0
    assert diff_mirrored[:, 160:].sum() > 0 and diff_mirrored[:, :140].sum() == 0


def test_validation_rejects_corrupt_or_mismatched_analysis():
    valid = analyse_movement_observations(
        [],
        width=320,
        height=240,
        fps=15.0,
        frame_count=30,
        sample_stride=1,
        source_sha256="c" * 64,
        segment_index=1,
    )
    validate_movement_analysis(valid, expected_segment_index=1, expected_source_sha256="c" * 64)

    with pytest.raises(ValueError, match="segment index"):
        validate_movement_analysis(valid, expected_segment_index=2)
    with pytest.raises(ValueError, match="video stream"):
        validate_movement_analysis(valid, expected_source_sha256="d" * 64)
    with pytest.raises(ValueError, match="width"):
        validate_movement_analysis(valid, expected_width=640)

    bad_fp = {**valid, "detector_fingerprint": "stale"}
    with pytest.raises(ValueError, match="configuration changed"):
        validate_movement_analysis(bad_fp)


def test_scan_video_movement_streams_and_supports_cancellation(tmp_path: Path):
    video_path = tmp_path / "synthetic.mp4"
    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), 15, (320, 240))
    assert writer.isOpened()
    for _ in range(30):
        writer.write(np.zeros((240, 320, 3), dtype=np.uint8))
    writer.release()

    class FakeEstimator:
        model_hash = "fake-hash"

        def __init__(self):
            self.calls = 0

        def estimate(self, _frame_bgr: np.ndarray, timestamp: float) -> FrameObservation:
            idx = self.calls
            self.calls += 1
            yaw = 5.0 * idx if 5 <= idx <= 18 else (0.0 if idx < 5 else 65.0)
            return _make_pose_observation(timestamp, hip_yaw_deg=yaw)

        def close(self) -> None:
            pass

    progress_values: list[int] = []
    result = scan_video_movement(
        video_path,
        FakeEstimator(),
        model_hash="fake-hash",
        segment_index=0,
        on_progress=progress_values.append,
    )
    assert progress_values and progress_values[-1] == 100
    assert len(result["pivots"]) == 1
    assert result["pivots"][0]["direction"] == "clockwise"

    # Cancellation during scan raises RuntimeError("Movement analysis cancelled")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises((RuntimeError, ValueError), match="cancelled"):
        scan_video_movement(video_path, FakeEstimator(), cancel=cancel)
