"""Offline replay movement analysis: pivot estimation, step placements, rendering and worker queue.

Sign and coordinate conventions
-------------------------------
- Image-space geometry is stored in original unmirrored source pixel coordinates
  (`0 <= x <= width`, `0 <= y <= height`, `+x` right, `+y` down).
- 3D hip yaw is computed from the directed vector from anatomical `left_hip` to
  anatomical `right_hip` projected into the camera-relative horizontal `(X, Z)` plane:
  `dx = right_hip.x - left_hip.x`, `dz = right_hip.z - left_hip.z`,
  `heading_deg = degrees(atan2(dz, dx))` in `(-180, 180]`.
- Heading angles are unwrapped across the `±180°` boundary and smoothed over a short
  time window. Positive angular velocity (`d(heading_deg)/dt > 0`, top-down `+X -> +Z`
  rotation) maps to `"clockwise"`; negative angular velocity (`d(heading_deg)/dt < 0`)
  maps to `"anticlockwise"`.
- When rendering with `mirror=True`, all image-space x-coordinates (`x -> width - 1 - x`),
  hip-line angles, and the apparent rotation arrow direction are reflected exactly once
  while keeping the stored event data unchanged.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from .archive import (
    extract_segment_video,
    file_sha256,
    movement_member_name,
    read_movement_analysis,
    store_movement_analysis,
)
from .domain import FrameObservation, Landmark, PoseEstimator
from .models import verify
from .pose.mediapipe_backend import MediaPipeEstimator

MOVEMENT_SCHEMA_VERSION = 1
DETECTOR_VERSION = "1.0"

MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
MAX_VIDEO_DURATION_SECONDS = 45.0 * 60.0  # 45 minutes
MAX_ANALYSIS_JSON_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_FRAME_COUNT = 200_000
MAX_PIVOT_EVENTS = 5_000
MAX_STEP_EVENTS = 20_000
MAX_HIP_FRAMES = 200_000

DETECTOR_CONFIG: dict[str, Any] = {
    "target_sample_fps": 15.0,
    "min_landmark_confidence": 0.55,
    "frame_margin": 0.01,
    "max_tracking_gap_s": 0.35,
    "max_interp_gap_s": 0.22,
    "heading_smooth_tau_s": 0.10,
    "min_3d_hip_span": 0.035,
    "min_2d_hip_span_ratio": 0.08,
    "pivot_window_min_s": 0.35,
    "pivot_window_max_s": 0.80,
    "pivot_min_net_deg": 20.0,
    "pivot_entry_rate_deg_s": 30.0,
    "pivot_exit_rate_deg_s": 14.0,
    "pivot_trim_rate_deg_s": 12.0,
    "pivot_max_opposite_fraction": 0.22,
    "pivot_foot_anchor_radius_scale": 0.18,
    "step_smooth_tau_s": 0.04,
    "step_move_speed_scale_s": 0.30,
    "step_settle_speed_scale_s": 0.20,
    "step_settle_radius_scale": 0.045,
    "step_settle_min_duration_s": 0.10,
    "step_min_center_travel_scale": 0.14,
    "step_min_both_ends_travel_scale": 0.065,
    "step_swivel_override_travel_scale": 0.20,
    "step_min_leg_extension_ratio": 0.78,
    "step_max_airborne_lift_scale": 0.14,
    "step_debounce_s": 0.25,
}


def detector_fingerprint(config: dict[str, Any] | None = None) -> str:
    payload = json.dumps(config or DETECTOR_CONFIG, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def wrapped_angle_diff(current_deg: float, previous_deg: float) -> float:
    """Return signed shortest angular difference `current_deg - previous_deg` in `(-180, 180]`."""
    diff = (current_deg - previous_deg + 180.0) % 360.0 - 180.0
    return 180.0 if diff == -180.0 and current_deg > previous_deg else diff


def unwrap_angles(angles_deg: Iterable[float]) -> list[float]:
    """Unwrap a sequence of angles in degrees across the `±180°` boundary."""
    result: list[float] = []
    for angle in angles_deg:
        if not math.isfinite(angle):
            raise ValueError("Angle must be finite")
        if not result:
            result.append(float(angle))
        else:
            prev_wrapped = ((result[-1] + 180.0) % 360.0) - 180.0
            result.append(result[-1] + wrapped_angle_diff(float(angle), prev_wrapped))
    return result


def step_opacity(age: float, history_seconds: float, peak_opacity: float = 0.88) -> float:
    """Compute step footprint opacity at `age = t - step_time` over `T = history_seconds`."""
    if (
        not math.isfinite(age)
        or not math.isfinite(history_seconds)
        or history_seconds <= 0.0
        or age < 0.0
        or age >= history_seconds
    ):
        return 0.0
    ramp = min(0.15, 0.15 * history_seconds)
    if ramp > 0.0 and age < ramp:
        return float(peak_opacity * (age / ramp))
    denom = max(1e-9, history_seconds - ramp)
    return float(peak_opacity * max(0.0, min(1.0, (history_seconds - age) / denom)))


def _usable_landmark(
    lm: Landmark | None,
    confidence: float,
    margin: float = 0.01,
    *,
    check_bounds: bool = True,
) -> bool:
    if lm is None:
        return False
    if not (math.isfinite(lm.x) and math.isfinite(lm.y)):
        return False
    if lm.visibility is not None and (not math.isfinite(lm.visibility) or lm.visibility < confidence):
        return False
    if lm.presence is not None and (not math.isfinite(lm.presence) or lm.presence < confidence):
        return False
    return not (
        check_bounds and not (margin <= lm.x <= 1.0 - margin and margin <= lm.y <= 1.0 - margin)
    )


def _landmark_confidence(lms: Iterable[Landmark]) -> float:
    vals = [
        float(lm.visibility)
        for lm in lms
        if lm.visibility is not None and math.isfinite(lm.visibility)
    ]
    return float(min(1.0, max(0.05, min(vals) if vals else 0.85)))


def hip_heading_3d_deg(
    left_hip_3d: Landmark | None,
    right_hip_3d: Landmark | None,
    left_hip_2d: Landmark | None = None,
    right_hip_2d: Landmark | None = None,
    min_span: float = 0.035,
) -> float | None:
    """Compute signed hip heading in degrees in the horizontal `(X, Z)` plane.

    Returns `None` when 3D hip coordinates are missing, non-finite, too foreshortened in the
    horizontal plane, or inconsistent with the 2D image projection.
    """
    if left_hip_3d is None or right_hip_3d is None:
        return None
    if left_hip_3d.z is None or right_hip_3d.z is None:
        return None
    if not (
        math.isfinite(left_hip_3d.x)
        and math.isfinite(left_hip_3d.z)
        and math.isfinite(right_hip_3d.x)
        and math.isfinite(right_hip_3d.z)
    ):
        return None
    dx = float(right_hip_3d.x - left_hip_3d.x)
    dz = float(right_hip_3d.z - left_hip_3d.z)
    span = math.hypot(dx, dz)
    if span < min_span:
        return None
    if left_hip_2d is not None and right_hip_2d is not None:
        dx_2d = float(right_hip_2d.x - left_hip_2d.x)
        # If both 2D and 3D X projections are substantial but have opposite signs, reject ambiguity
        if abs(dx_2d) > 0.03 and abs(dx) > 0.03 and (dx_2d * dx < 0):
            return None
    return math.degrees(math.atan2(dz, dx))


@dataclass
class _FootSample:
    frame: int
    time: float
    hx: float
    hy: float
    tx: float
    ty: float
    ax: float
    ay: float
    cx: float
    cy: float
    bottom_y: float
    hip_y: float | None
    scale: float
    confidence: float


class _SingleFootTracker:
    """State machine tracking one anatomical foot (`left` or `right`) for settled step placements."""

    def __init__(self, side: str, cfg: dict[str, Any]):
        self.side = side
        self.cfg = cfg
        self.state = "INITIAL"
        self.last_time: float | None = None
        self.smooth_cx: float | None = None
        self.smooth_cy: float | None = None
        self.prev_smooth_cx: float | None = None
        self.prev_smooth_cy: float | None = None
        self.anchor_cx: float | None = None
        self.anchor_cy: float | None = None
        self.anchor_hx: float | None = None
        self.anchor_hy: float | None = None
        self.anchor_tx: float | None = None
        self.anchor_ty: float | None = None
        self.initial_buffer: list[_FootSample] = []
        self.settle_buffer: list[_FootSample] = []
        self.last_step_time: float = -1e9
        self.max_leg_span: float = 0.0

    def reset(self) -> None:
        self.state = "INITIAL"
        self.last_time = None
        self.smooth_cx = None
        self.smooth_cy = None
        self.prev_smooth_cx = None
        self.prev_smooth_cy = None
        self.anchor_cx = None
        self.anchor_cy = None
        self.anchor_hx = None
        self.anchor_hy = None
        self.anchor_tx = None
        self.anchor_ty = None
        self.initial_buffer.clear()
        self.settle_buffer.clear()

    def update(
        self,
        sample: _FootSample | None,
        other_foot_bottom_y: float | None,
    ) -> dict[str, Any] | None:
        if sample is None:
            if self.last_time is not None:
                # We allow brief single-frame misses; longer gaps trigger reset on next sample
                pass
            return None

        if self.last_time is not None and (sample.time - self.last_time) > self.cfg["max_tracking_gap_s"]:
            self.reset()

        dt = 0.0 if self.last_time is None else max(1e-4, sample.time - self.last_time)
        self.last_time = sample.time

        if sample.hip_y is not None:
            leg_span = sample.bottom_y - sample.hip_y
            if leg_span > 0.5 * sample.scale:
                if self.max_leg_span <= 0.0:
                    self.max_leg_span = leg_span
                else:
                    # Slowly track full standing leg extension while allowing camera distance changes
                    self.max_leg_span = max(leg_span, 0.992 * self.max_leg_span + 0.008 * leg_span)

        tau = max(1e-4, float(self.cfg["step_smooth_tau_s"]))
        if self.smooth_cx is None or self.smooth_cy is None or dt <= 0.0:
            self.smooth_cx = sample.cx
            self.smooth_cy = sample.cy
            speed = 0.0
        else:
            self.prev_smooth_cx = self.smooth_cx
            self.prev_smooth_cy = self.smooth_cy
            alpha = 1.0 - math.exp(-dt / tau)
            self.smooth_cx += alpha * (sample.cx - self.smooth_cx)
            self.smooth_cy += alpha * (sample.cy - self.smooth_cy)
            step_dist = math.hypot(self.smooth_cx - self.prev_smooth_cx, self.smooth_cy - self.prev_smooth_cy)
            speed = step_dist / dt

        speed_norm = speed / max(1.0, sample.scale)

        if self.state == "INITIAL":
            self.initial_buffer.append(sample)
            if len(self.initial_buffer) >= 2:
                xs = [s.cx for s in self.initial_buffer[-3:]]
                ys = [s.cy for s in self.initial_buffer[-3:]]
                spread = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / max(1.0, sample.scale)
                if spread <= self.cfg["step_settle_radius_scale"] * 1.5:
                    self._set_anchor_from_samples(self.initial_buffer[-3:])
                    self.state = "ANCHORED"
                    self.initial_buffer.clear()
                elif len(self.initial_buffer) >= 3:
                    # Foot was already moving when first observed; set initial origin to first sample
                    self._set_anchor_from_samples(self.initial_buffer[:1])
                    self.state = "MOVING"
                    self.initial_buffer.clear()
            return None

        assert self.anchor_cx is not None and self.anchor_cy is not None
        assert self.anchor_hx is not None and self.anchor_hy is not None
        assert self.anchor_tx is not None and self.anchor_ty is not None

        dist_from_anchor = math.hypot(sample.cx - self.anchor_cx, sample.cy - self.anchor_cy) / max(
            1.0, sample.scale
        )

        if self.state == "ANCHORED":
            if (
                dist_from_anchor >= 0.075
                and speed_norm >= self.cfg["step_move_speed_scale_s"] * 0.75
            ) or dist_from_anchor >= self.cfg["step_min_center_travel_scale"]:
                self.state = "MOVING"
                self.settle_buffer.clear()
            return None

        if self.state == "MOVING":
            if speed_norm <= self.cfg["step_settle_speed_scale_s"]:
                self.state = "SETTLING"
                self.settle_buffer = [sample]
            return None

        if self.state == "SETTLING":
            self.settle_buffer.append(sample)
            xs = [s.cx for s in self.settle_buffer]
            ys = [s.cy for s in self.settle_buffer]
            spread = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / max(1.0, sample.scale)
            if spread > self.cfg["step_settle_radius_scale"] or speed_norm > self.cfg["step_settle_speed_scale_s"] * 1.35:
                # Keep only the latest settled sample if still decelerating, or return to MOVING
                if speed_norm <= self.cfg["step_settle_speed_scale_s"]:
                    self.settle_buffer = [sample]
                else:
                    self.state = "MOVING"
                    self.settle_buffer.clear()
                return None

            settled_duration = self.settle_buffer[-1].time - self.settle_buffer[0].time
            if len(self.settle_buffer) >= 2 and settled_duration + 1e-6 >= self.cfg["step_settle_min_duration_s"]:
                event = self._try_confirm_step(other_foot_bottom_y)
                self._set_anchor_from_samples(self.settle_buffer)
                self.state = "ANCHORED"
                self.settle_buffer.clear()
                return event

        return None

    def _set_anchor_from_samples(self, samples: list[_FootSample]) -> None:
        self.anchor_cx = float(np.median([s.cx for s in samples]))
        self.anchor_cy = float(np.median([s.cy for s in samples]))
        self.anchor_hx = float(np.median([s.hx for s in samples]))
        self.anchor_hy = float(np.median([s.hy for s in samples]))
        self.anchor_tx = float(np.median([s.tx for s in samples]))
        self.anchor_ty = float(np.median([s.ty for s in samples]))

    def _try_confirm_step(self, other_foot_bottom_y: float | None) -> dict[str, Any] | None:
        assert self.anchor_cx is not None and self.anchor_cy is not None
        assert self.anchor_hx is not None and self.anchor_hy is not None
        assert self.anchor_tx is not None and self.anchor_ty is not None

        first = self.settle_buffer[0]
        if (first.time - self.last_step_time) < self.cfg["step_debounce_s"]:
            return None

        scale = float(np.median([s.scale for s in self.settle_buffer]))
        hx = float(np.median([s.hx for s in self.settle_buffer]))
        hy = float(np.median([s.hy for s in self.settle_buffer]))
        tx = float(np.median([s.tx for s in self.settle_buffer]))
        ty = float(np.median([s.ty for s in self.settle_buffer]))
        ax = float(np.median([s.ax for s in self.settle_buffer]))
        ay = float(np.median([s.ay for s in self.settle_buffer]))
        cx = float(np.median([s.cx for s in self.settle_buffer]))
        cy = float(np.median([s.cy for s in self.settle_buffer]))
        bottom_y = float(np.median([s.bottom_y for s in self.settle_buffer]))

        center_travel = math.hypot(cx - self.anchor_cx, cy - self.anchor_cy) / max(1.0, scale)
        heel_travel = math.hypot(hx - self.anchor_hx, hy - self.anchor_hy) / max(1.0, scale)
        toe_travel = math.hypot(tx - self.anchor_tx, ty - self.anchor_ty) / max(1.0, scale)

        # Reject small jitter or in-place foot swivel around heel or forefoot
        if center_travel < self.cfg["step_min_center_travel_scale"]:
            return None
        if (
            min(heel_travel, toe_travel) < self.cfg["step_min_both_ends_travel_scale"]
            and center_travel < self.cfg["step_swivel_override_travel_scale"]
        ):
            return None

        # Reject suspended/airborne foot
        hip_ys = [s.hip_y for s in self.settle_buffer if s.hip_y is not None]
        if hip_ys and self.max_leg_span > 0.0:
            med_hip_y = float(np.median(hip_ys))
            leg_span = bottom_y - med_hip_y
            if leg_span < self.cfg["step_min_leg_extension_ratio"] * self.max_leg_span:
                return None
        if (
            other_foot_bottom_y is not None
            and (other_foot_bottom_y - bottom_y) > self.cfg["step_max_airborne_lift_scale"] * scale
        ):
            # In image space (+y downward), smaller y means higher in the air
            return None

        length_px = math.hypot(tx - hx, ty - hy)
        angle_deg = math.degrees(math.atan2(ty - hy, tx - hx))
        confidence = float(np.median([s.confidence for s in self.settle_buffer]))
        self.last_step_time = first.time
        return {
            "frame": int(first.frame),
            "time": round(float(first.time), 6),
            "side": self.side,
            "x": round(cx, 2),
            "y": round(cy, 2),
            "hx": round(hx, 2),
            "hy": round(hy, 2),
            "tx": round(tx, 2),
            "ty": round(ty, 2),
            "ax": round(ax, 2),
            "ay": round(ay, 2),
            "angle_deg": round(angle_deg, 2),
            "length_px": round(length_px, 2),
            "confidence": round(confidence, 4),
        }


def _estimate_body_scale(
    pose: dict[str, Landmark],
    width: int,
    height: int,
    confidence: float,
    margin: float,
) -> float | None:
    """Estimate a robust body scale in source pixels (roughly torso length)."""
    candidates: list[float] = []
    l_sh, r_sh = pose.get("left_shoulder"), pose.get("right_shoulder")
    l_hip, r_hip = pose.get("left_hip"), pose.get("right_hip")
    if (
        _usable_landmark(l_sh, confidence, margin)
        and _usable_landmark(r_sh, confidence, margin)
        and _usable_landmark(l_hip, confidence, margin)
        and _usable_landmark(r_hip, confidence, margin)
        and l_sh is not None
        and r_sh is not None
        and l_hip is not None
        and r_hip is not None
    ):
        sh_x = 0.5 * (l_sh.x + r_sh.x) * width
        sh_y = 0.5 * (l_sh.y + r_sh.y) * height
        hp_x = 0.5 * (l_hip.x + r_hip.x) * width
        hp_y = 0.5 * (l_hip.y + r_hip.y) * height
        candidates.append(math.hypot(sh_x - hp_x, sh_y - hp_y))

    for side in ("left", "right"):
        sh = pose.get(f"{side}_shoulder")
        hp = pose.get(f"{side}_hip")
        if (
            _usable_landmark(sh, confidence, margin)
            and _usable_landmark(hp, confidence, margin)
            and sh is not None
            and hp is not None
        ):
            candidates.append(
                math.hypot((sh.x - hp.x) * width, (sh.y - hp.y) * height)
            )

    for side in ("left", "right"):
        hp = pose.get(f"{side}_hip")
        ank = pose.get(f"{side}_ankle")
        if (
            _usable_landmark(hp, confidence, margin)
            and _usable_landmark(ank, confidence, margin)
            and hp is not None
            and ank is not None
        ):
            leg = math.hypot((hp.x - ank.x) * width, (hp.y - ank.y) * height)
            candidates.append(0.56 * leg)

    if not candidates:
        return None
    scale = float(np.median(candidates))
    if scale < max(20.0, 0.08 * height):
        return None
    return scale


def analyse_movement_observations(
    observations: Iterable[tuple[int, FrameObservation]],
    *,
    width: int,
    height: int,
    fps: float,
    frame_count: int,
    sample_stride: int = 1,
    model_hash: str = "",
    source_sha256: str = "",
    segment_index: int = 0,
    source_member: str = "video_00.mp4",
    timing_note: str = "",
    detector_config: dict[str, Any] | None = None,
    cancel: threading.Event | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Pure offline movement analysis over timestamped pose observations."""
    if width <= 0 or height <= 0 or not math.isfinite(fps) or fps <= 0.0 or frame_count < 1:
        raise ValueError("Invalid video dimensions, frame rate, or frame count")
    if frame_count > MAX_FRAME_COUNT:
        raise ValueError(f"Video exceeds maximum supported frame count ({MAX_FRAME_COUNT})")

    cfg = {**DETECTOR_CONFIG, **(detector_config or {})}
    conf = float(cfg["min_landmark_confidence"])
    margin = float(cfg["frame_margin"])

    sampled_frames = 0
    valid_pose_frames = 0
    pivot_valid_frames = 0
    left_foot_valid_frames = 0
    right_foot_valid_frames = 0

    scale_history: list[float] = []
    last_hip_center: tuple[float, float] | None = None
    last_pose_time: float | None = None
    cut_frames: list[int] = []

    left_tracker = _SingleFootTracker("left", cfg)
    right_tracker = _SingleFootTracker("right", cfg)
    steps: list[dict[str, Any]] = []

    # Per-sample records for offline pivot detection and hip geometry interpolation
    pivot_samples: list[dict[str, Any]] = []
    hip_keypoints: list[dict[str, Any]] = []
    last_progress = -1

    for frame_idx, obs in observations:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Movement analysis cancelled")
        if not (0 <= frame_idx < frame_count):
            continue
        sampled_frames += 1
        replay_time = frame_idx / fps

        if on_progress is not None:
            pct = min(99, int((frame_idx + 1) * 100 / max(1, frame_count)))
            if pct != last_progress:
                last_progress = pct
                on_progress(pct)

        if len(obs.poses) != 1:
            pivot_samples.append({"frame": frame_idx, "time": replay_time, "valid": False})
            continue

        pose = obs.poses[0]
        world_pose = obs.world_poses[0] if obs.world_poses and len(obs.world_poses) >= 1 else {}

        raw_scale = _estimate_body_scale(pose, width, height, conf, margin)
        if raw_scale is None:
            pivot_samples.append({"frame": frame_idx, "time": replay_time, "valid": False})
            continue

        if scale_history:
            med_scale = float(np.median(scale_history[-15:]))
            if abs(raw_scale - med_scale) / max(1.0, med_scale) > 0.65:
                # Implausible sudden scale jump / camera cut
                cut_frames.append(frame_idx)
                scale_history.clear()
                left_tracker.reset()
                right_tracker.reset()
                last_hip_center = None
                last_pose_time = None
                pivot_samples.append({"frame": frame_idx, "time": replay_time, "valid": False, "cut": True})
                continue

        scale_history.append(raw_scale)
        scale = float(np.median(scale_history[-9:]))

        l_hip = pose.get("left_hip")
        r_hip = pose.get("right_hip")
        hips_2d_ok = (
            _usable_landmark(l_hip, conf, margin)
            and _usable_landmark(r_hip, conf, margin)
            and l_hip is not None
            and r_hip is not None
        )
        hip_mid_y: float | None = None
        if hips_2d_ok and l_hip is not None and r_hip is not None:
            lx, ly = l_hip.x * width, l_hip.y * height
            rx, ry = r_hip.x * width, r_hip.y * height
            cx, cy = 0.5 * (lx + rx), 0.5 * (ly + ry)
            hip_mid_y = cy
            if last_hip_center is not None and last_pose_time is not None:
                dt_pose = replay_time - last_pose_time
                jump_px = math.hypot(cx - last_hip_center[0], cy - last_hip_center[1])
                if (
                    0 < dt_pose <= cfg["max_tracking_gap_s"]
                    and jump_px > max(0.55 * width, 3.2 * scale)
                ):
                    cut_frames.append(frame_idx)
                    left_tracker.reset()
                    right_tracker.reset()
                    last_hip_center = (cx, cy)
                    last_pose_time = replay_time
                    pivot_samples.append({"frame": frame_idx, "time": replay_time, "valid": False, "cut": True})
                    continue
            last_hip_center = (cx, cy)
            last_pose_time = replay_time
        else:
            for side_name in ("left", "right"):
                hp = pose.get(f"{side_name}_hip")
                if _usable_landmark(hp, conf, margin) and hp is not None:
                    hip_mid_y = hp.y * height
                    break

        valid_pose_frames += 1

        # Extract per-foot samples (requiring ankle, heel, and foot_index)
        foot_samples: dict[str, _FootSample | None] = {"left": None, "right": None}
        foot_points_2d: dict[str, tuple[float, float] | None] = {"left": None, "right": None}
        for side_name in ("left", "right"):
            ank = pose.get(f"{side_name}_ankle")
            heel = pose.get(f"{side_name}_heel")
            fidx = pose.get(f"{side_name}_foot_index")
            if (
                _usable_landmark(ank, conf, margin)
                and _usable_landmark(heel, conf, margin)
                and _usable_landmark(fidx, conf, margin)
                and ank is not None
                and heel is not None
                and fidx is not None
            ):
                ax, ay = ank.x * width, ank.y * height
                hx, hy = heel.x * width, heel.y * height
                tx, ty = fidx.x * width, fidx.y * height
                flen = math.hypot(tx - hx, ty - hy)
                if 0.03 * scale <= flen <= 0.70 * scale:
                    fcx = 0.45 * hx + 0.45 * tx + 0.10 * ax
                    fcy = 0.45 * hy + 0.45 * ty + 0.10 * ay
                    bottom_y = max(hy, ty, ay)
                    fconf = _landmark_confidence((ank, heel, fidx))
                    foot_samples[side_name] = _FootSample(
                        frame=frame_idx,
                        time=replay_time,
                        hx=hx,
                        hy=hy,
                        tx=tx,
                        ty=ty,
                        ax=ax,
                        ay=ay,
                        cx=fcx,
                        cy=fcy,
                        bottom_y=bottom_y,
                        hip_y=hip_mid_y,
                        scale=scale,
                        confidence=fconf,
                    )
                    # Forefoot-weighted anchor point for pivot support verification
                    foot_points_2d[side_name] = (0.65 * tx + 0.35 * ax, 0.65 * ty + 0.35 * ay)
                    if side_name == "left":
                        left_foot_valid_frames += 1
                    else:
                        right_foot_valid_frames += 1
            elif _usable_landmark(ank, conf, margin) and ank is not None:
                # Ankle alone is not enough to place a heel-to-toe footprint, but can help pivot anchor
                ax, ay = ank.x * width, ank.y * height
                if _usable_landmark(fidx, conf, margin) and fidx is not None:
                    tx, ty = fidx.x * width, fidx.y * height
                    foot_points_2d[side_name] = (0.65 * tx + 0.35 * ax, 0.65 * ty + 0.35 * ay)
                else:
                    foot_points_2d[side_name] = (ax, ay)

        left_bottom = foot_samples["left"].bottom_y if foot_samples["left"] is not None else None
        right_bottom = foot_samples["right"].bottom_y if foot_samples["right"] is not None else None
        left_event = left_tracker.update(foot_samples["left"], right_bottom)
        if left_event is not None and len(steps) < MAX_STEP_EVENTS:
            steps.append(left_event)
        right_event = right_tracker.update(foot_samples["right"], left_bottom)
        if right_event is not None and len(steps) < MAX_STEP_EVENTS:
            steps.append(right_event)

        # Pivot sample extraction
        if hips_2d_ok and l_hip is not None and r_hip is not None:
            lx, ly = l_hip.x * width, l_hip.y * height
            rx, ry = r_hip.x * width, r_hip.y * height
            cx, cy = 0.5 * (lx + rx), 0.5 * (ly + ry)
            hip_span = math.hypot(rx - lx, ry - ly)
            if hip_span >= cfg["min_2d_hip_span_ratio"] * scale:
                hip_keypoints.append(
                    {
                        "frame": frame_idx,
                        "time": replay_time,
                        "cx": cx,
                        "cy": cy,
                        "lx": lx,
                        "ly": ly,
                        "rx": rx,
                        "ry": ry,
                        "scale": scale,
                        "hip_span": hip_span,
                    }
                )
            w_l_hip = world_pose.get("left_hip") if world_pose else l_hip
            w_r_hip = world_pose.get("right_hip") if world_pose else r_hip
            heading_deg = None
            if (
                _usable_landmark(w_l_hip, conf, check_bounds=False)
                and _usable_landmark(w_r_hip, conf, check_bounds=False)
            ):
                heading_deg = hip_heading_3d_deg(
                    w_l_hip,
                    w_r_hip,
                    l_hip,
                    r_hip,
                    min_span=float(cfg["min_3d_hip_span"]),
                )
            if heading_deg is not None and (
                foot_points_2d["left"] is not None or foot_points_2d["right"] is not None
            ):
                pivot_valid_frames += 1
                pivot_samples.append(
                    {
                        "frame": frame_idx,
                        "time": replay_time,
                        "valid": True,
                        "heading_deg": heading_deg,
                        "scale": scale,
                        "left_foot": foot_points_2d["left"],
                        "right_foot": foot_points_2d["right"],
                        "quality": _landmark_confidence((l_hip, r_hip)),
                    }
                )
            else:
                pivot_samples.append({"frame": frame_idx, "time": replay_time, "valid": False})
        else:
            pivot_samples.append({"frame": frame_idx, "time": replay_time, "valid": False})

    steps.sort(key=lambda item: (item["frame"], 0 if item["side"] == "left" else 1))
    pivots = _detect_pivot_intervals(pivot_samples, fps=fps, cfg=cfg)
    hip_frames = _build_interpolated_hip_frames(hip_keypoints, pivots, fps=fps, cfg=cfg)

    if on_progress is not None:
        on_progress(100)

    return {
        "schema_version": MOVEMENT_SCHEMA_VERSION,
        "detector_version": DETECTOR_VERSION,
        "detector_fingerprint": detector_fingerprint(cfg),
        "model_hash": model_hash,
        "segment_index": int(segment_index),
        "source_member": source_member,
        "source_sha256": source_sha256,
        "width": int(width),
        "height": int(height),
        "fps": float(fps),
        "frame_count": int(frame_count),
        "duration": round(float(frame_count / fps), 6),
        "timing_basis": "frame_index_over_fps",
        "timing_note": timing_note,
        "sample_stride": int(sample_stride),
        "coverage": {
            "sampled_frames": int(sampled_frames),
            "valid_pose_frames": int(valid_pose_frames),
            "pivot_valid_frames": int(pivot_valid_frames),
            "left_foot_valid_frames": int(left_foot_valid_frames),
            "right_foot_valid_frames": int(right_foot_valid_frames),
        },
        "cut_frames": cut_frames,
        "pivots": pivots[:MAX_PIVOT_EVENTS],
        "hip_frames": hip_frames[:MAX_HIP_FRAMES],
        "steps": steps[:MAX_STEP_EVENTS],
    }


def _foot_anchored_in_slice(
    samples: list[dict[str, Any]],
    side_key: str,
    radius_scale: float,
) -> bool:
    pts = [s[side_key] for s in samples if s.get(side_key) is not None]
    if len(pts) < max(2, math.ceil(0.70 * len(samples))):
        return False
    med_x = float(np.median([p[0] for p in pts]))
    med_y = float(np.median([p[1] for p in pts]))
    med_scale = float(np.median([s["scale"] for s in samples]))
    max_disp = max(math.hypot(p[0] - med_x, p[1] - med_y) for p in pts)
    return max_disp <= radius_scale * max(1.0, med_scale)


def _support_side_for_slice(
    samples: list[dict[str, Any]],
    radius_scale: float,
) -> str | None:
    left_ok = _foot_anchored_in_slice(samples, "left_foot", radius_scale)
    right_ok = _foot_anchored_in_slice(samples, "right_foot", radius_scale)
    if left_ok and right_ok:
        return "both"
    if left_ok:
        return "left"
    if right_ok:
        return "right"
    return None


def _detect_pivot_intervals(
    pivot_samples: list[dict[str, Any]],
    *,
    fps: float,
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    # Split into contiguous valid runs separated by <= max_tracking_gap_s and no camera cuts
    runs: list[list[dict[str, Any]]] = []
    current_run: list[dict[str, Any]] = []
    for item in pivot_samples:
        if not item.get("valid", False):
            if item.get("cut", False) and current_run:
                runs.append(current_run)
                current_run = []
            continue
        if (
            current_run
            and (item["time"] - current_run[-1]["time"]) > cfg["max_tracking_gap_s"]
        ):
            runs.append(current_run)
            current_run = []
        current_run.append(item)
    if current_run:
        runs.append(current_run)

    pivots: list[dict[str, Any]] = []
    tau = max(1e-4, float(cfg["heading_smooth_tau_s"]))
    win_min = float(cfg["pivot_window_min_s"])
    win_max = float(cfg["pivot_window_max_s"])
    min_net = float(cfg["pivot_min_net_deg"])
    entry_rate = float(cfg["pivot_entry_rate_deg_s"])
    exit_rate = float(cfg["pivot_exit_rate_deg_s"])
    trim_rate = float(cfg["pivot_trim_rate_deg_s"])
    anchor_radius = float(cfg["pivot_foot_anchor_radius_scale"])

    for run in runs:
        if len(run) < 3 or (run[-1]["time"] - run[0]["time"]) < win_min * 0.85:
            continue
        raw_headings = [float(s["heading_deg"]) for s in run]
        unwrapped = unwrap_angles(raw_headings)
        smoothed: list[float] = [unwrapped[0]]
        for idx in range(1, len(run)):
            dt = max(1e-4, run[idx]["time"] - run[idx - 1]["time"])
            alpha = 1.0 - math.exp(-dt / tau)
            smoothed.append(smoothed[-1] + alpha * (unwrapped[idx] - smoothed[-1]))
        for idx, s in enumerate(run):
            s["unwrapped_deg"] = unwrapped[idx]
            s["smoothed_deg"] = smoothed[idx]
            if idx == 0:
                s["rate_deg_s"] = 0.0
                s["raw_rate_deg_s"] = 0.0
            else:
                dt = max(1e-4, s["time"] - run[idx - 1]["time"])
                s["rate_deg_s"] = (smoothed[idx] - smoothed[idx - 1]) / dt
                s["raw_rate_deg_s"] = (unwrapped[idx] - unwrapped[idx - 1]) / dt

        # Scan for candidate segments using sliding window entry + exit hysteresis + reversal split
        n = len(run)
        active_start: int | None = None
        active_dir: int = 0
        min_lookback_idx: int = 0
        idx = 0
        raw_segments: list[tuple[int, int, int]] = []

        while idx < n:
            if active_start is None:
                # Look back over [j..idx] with duration in [win_min, win_max]
                matched_j: int | None = None
                matched_dir: int = 0
                for j in range(idx - 1, min_lookback_idx - 1, -1):
                    dt_win = run[idx]["time"] - run[j]["time"]
                    if dt_win < win_min - 1e-6:
                        continue
                    if dt_win > win_max + 1e-6:
                        break
                    net_deg = run[idx]["smoothed_deg"] - run[j]["smoothed_deg"]
                    if abs(net_deg) < min_net:
                        continue
                    mean_rate = abs(net_deg) / dt_win
                    if mean_rate < entry_rate:
                        continue
                    cand_dir = 1 if net_deg > 0 else -1
                    steps_deg = [
                        run[m]["smoothed_deg"] - run[m - 1]["smoothed_deg"]
                        for m in range(j + 1, idx + 1)
                    ]
                    opp_deg = sum(-cand_dir * d for d in steps_deg if cand_dir * d < 0)
                    if opp_deg > cfg["pivot_max_opposite_fraction"] * abs(net_deg):
                        continue
                    if _support_side_for_slice(run[j : idx + 1], anchor_radius) is None:
                        continue
                    matched_j = j
                    matched_dir = cand_dir
                if matched_j is not None:
                    active_start = matched_j
                    active_dir = matched_dir
                idx += 1
            else:
                # Check if rotation in active_dir continues at idx
                step_rate = active_dir * float(run[idx]["rate_deg_s"])
                # Also check short recent window (last ~0.2s) rate
                back = max(active_start, idx - 2)
                dt_recent = max(1e-4, run[idx]["time"] - run[back]["time"])
                recent_rate = active_dir * (run[idx]["smoothed_deg"] - run[back]["smoothed_deg"]) / dt_recent
                support_ok = _support_side_for_slice(
                    run[max(active_start, idx - 6) : idx + 1], anchor_radius * 1.15
                ) is not None
                reversal = step_rate < -entry_rate * 0.55 or recent_rate < -exit_rate
                if reversal or (step_rate < exit_rate and recent_rate < exit_rate) or not support_ok:
                    raw_segments.append((active_start, idx - 1, active_dir))
                    min_lookback_idx = idx - 1 if reversal else idx
                    active_start = None
                    active_dir = 0
                    # Re-evaluate idx as potential start of a reversed turn
                    continue
                idx += 1

        if active_start is not None:
            raw_segments.append((active_start, n - 1, active_dir))

        prev_end_frame = 0
        for seg_start, seg_end, seg_dir in raw_segments:
            # Trim leading/trailing frames where rotation in seg_dir had not yet started / already stopped
            s_i, e_i = seg_start, seg_end
            while s_i < e_i and (
                seg_dir * float(run[s_i + 1]["rate_deg_s"]) < trim_rate
                or seg_dir * float(run[s_i + 1]["raw_rate_deg_s"]) < trim_rate * 0.5
            ):
                s_i += 1
            while e_i > s_i and (
                seg_dir * float(run[e_i]["rate_deg_s"]) < trim_rate
                or seg_dir * float(run[e_i]["raw_rate_deg_s"]) < trim_rate * 0.5
            ):
                e_i -= 1
            if e_i <= s_i:
                continue
            dt_seg = run[e_i]["time"] - run[s_i]["time"]
            # Use unsmoothed unwrapped heading at the trimmed endpoints so short smoothing lag doesn't shrink net rotation
            net_unwrapped = run[e_i]["unwrapped_deg"] - run[s_i]["unwrapped_deg"]
            net_smoothed = run[e_i]["smoothed_deg"] - run[s_i]["smoothed_deg"]
            net_deg = net_unwrapped if seg_dir * net_unwrapped >= seg_dir * net_smoothed else net_smoothed
            if dt_seg + 1e-6 < win_min * 0.80 or seg_dir * net_deg < min_net:
                continue
            support_side = _support_side_for_slice(run[s_i : e_i + 1], anchor_radius * 1.15)
            if support_side is None:
                continue
            start_frame = max(prev_end_frame, int(run[s_i]["frame"]))
            # Half-open [start_frame, end_frame) covering up to the last rotating sample frame (+1)
            end_frame = int(run[e_i]["frame"]) + 1
            if end_frame <= start_frame:
                continue
            prev_end_frame = end_frame
            direction = "clockwise" if seg_dir > 0 else "anticlockwise"
            quality = float(np.median([s["quality"] for s in run[s_i : e_i + 1]]))
            pivots.append(
                {
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "start_time": round(start_frame / fps, 6),
                    "end_time": round(end_frame / fps, 6),
                    "direction": direction,
                    "net_rotation_deg": round(float(net_deg), 2),
                    "mean_rate_deg_s": round(float(net_deg / max(1e-4, dt_seg)), 2),
                    "support_side": support_side,
                    "quality": round(quality, 4),
                }
            )

    return pivots


def _build_interpolated_hip_frames(
    hip_keypoints: list[dict[str, Any]],
    pivots: list[dict[str, Any]],
    *,
    fps: float,
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    if not hip_keypoints or not pivots:
        return []

    def in_any_pivot(frame_idx: int) -> bool:
        return any(p["start_frame"] <= frame_idx < p["end_frame"] for p in pivots)

    max_interp_gap_s = float(cfg["max_interp_gap_s"])
    by_frame: dict[int, dict[str, Any]] = {}
    for kp in hip_keypoints:
        f = int(kp["frame"])
        if in_any_pivot(f):
            by_frame[f] = {
                "frame": f,
                "cx": round(float(kp["cx"]), 2),
                "cy": round(float(kp["cy"]), 2),
                "lx": round(float(kp["lx"]), 2),
                "ly": round(float(kp["ly"]), 2),
                "rx": round(float(kp["rx"]), 2),
                "ry": round(float(kp["ry"]), 2),
                "scale": round(float(kp["scale"]), 2),
                "hip_span": round(float(kp["hip_span"]), 2),
            }

    for a, b in itertools.pairwise(hip_keypoints):
        fa, fb = int(a["frame"]), int(b["frame"])
        if fb - fa <= 1:
            continue
        if (fb - fa) / fps > max_interp_gap_s:
            # Never interpolate across a tracking gap
            continue
        for f in range(fa + 1, fb):
            if not in_any_pivot(f):
                continue
            ratio = (f - fa) / (fb - fa)
            lx = a["lx"] + ratio * (b["lx"] - a["lx"])
            ly = a["ly"] + ratio * (b["ly"] - a["ly"])
            rx = a["rx"] + ratio * (b["rx"] - a["rx"])
            ry = a["ry"] + ratio * (b["ry"] - a["ry"])
            cx = 0.5 * (lx + rx)
            cy = 0.5 * (ly + ry)
            scale = a["scale"] + ratio * (b["scale"] - a["scale"])
            hip_span = math.hypot(rx - lx, ry - ly)
            by_frame[f] = {
                "frame": f,
                "cx": round(float(cx), 2),
                "cy": round(float(cy), 2),
                "lx": round(float(lx), 2),
                "ly": round(float(ly), 2),
                "rx": round(float(rx), 2),
                "ry": round(float(ry), 2),
                "scale": round(float(scale), 2),
                "hip_span": round(float(hip_span), 2),
            }

    return [by_frame[k] for k in sorted(by_frame)]


def scan_video_movement(
    video_path: Path,
    estimator: PoseEstimator,
    *,
    model_hash: str = "",
    source_sha256: str | None = None,
    segment_index: int = 0,
    source_member: str | None = None,
    detector_config: dict[str, Any] | None = None,
    cancel: threading.Event | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Stream a video file sequentially and run offline movement analysis."""
    resolved = video_path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"Video not found: {resolved}")
    byte_size = resolved.stat().st_size
    if byte_size <= 0:
        raise ValueError(f"Video file is empty: {resolved.name}")
    if byte_size > MAX_VIDEO_BYTES:
        raise ValueError(f"Video exceeds size limit ({MAX_VIDEO_BYTES // (1024 * 1024)} MB)")

    computed_sha = source_sha256 or file_sha256(resolved)
    cap = cv2.VideoCapture(str(resolved))
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        count_raw = float(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if (
            not cap.isOpened()
            or not math.isfinite(fps)
            or fps <= 0.0
            or not math.isfinite(count_raw)
            or count_raw < 1
            or width <= 0
            or height <= 0
        ):
            raise ValueError(f"Cannot decode video or read its timing: {resolved.name}")
        declared_count = int(count_raw)
        if declared_count / fps > MAX_VIDEO_DURATION_SECONDS:
            raise ValueError("Video exceeds maximum duration limit for offline analysis")
        if declared_count > MAX_FRAME_COUNT:
            raise ValueError(f"Video exceeds maximum frame count ({MAX_FRAME_COUNT})")

        cfg = {**DETECTOR_CONFIG, **(detector_config or {})}
        target_fps = max(1.0, float(cfg["target_sample_fps"]))
        stride = max(1, round(fps / target_fps)) if fps > target_fps * 1.35 else 1

        timing_notes: list[str] = []
        pts_deltas: list[float] = []
        last_pts_ms: float | None = None
        decoded_count = 0

        def frame_stream() -> Iterable[tuple[int, FrameObservation]]:
            nonlocal decoded_count, last_pts_ms
            frame_idx = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("Movement analysis cancelled")
                ok, image = cap.read()
                if not ok or image is None:
                    break
                decoded_count = frame_idx + 1
                pts_ms = float(cap.get(cv2.CAP_PROP_POS_MSEC))
                if math.isfinite(pts_ms) and pts_ms > 0.0:
                    if last_pts_ms is not None and pts_ms > last_pts_ms:
                        pts_deltas.append((pts_ms - last_pts_ms) / 1000.0)
                    last_pts_ms = pts_ms
                if frame_idx % stride == 0:
                    timestamp = frame_idx / fps
                    obs = estimator.estimate(image, timestamp)
                    yield frame_idx, obs
                frame_idx += 1
                if frame_idx > MAX_FRAME_COUNT:
                    raise ValueError(f"Video exceeds maximum frame count ({MAX_FRAME_COUNT})")

        result = analyse_movement_observations(
            frame_stream(),
            width=width,
            height=height,
            fps=fps,
            frame_count=max(1, declared_count),
            sample_stride=stride,
            model_hash=model_hash,
            source_sha256=computed_sha,
            segment_index=segment_index,
            source_member=source_member or resolved.name,
            detector_config=cfg,
            cancel=cancel,
            on_progress=on_progress,
        )
        if decoded_count < 1:
            raise ValueError(f"Video contains no decodable frames: {resolved.name}")
        if decoded_count != result["frame_count"]:
            result["frame_count"] = decoded_count
            result["duration"] = round(float(decoded_count / fps), 6)
            timing_notes.append(
                f"Decoded frame count ({decoded_count}) differed from container header ({declared_count})."
            )
        if len(pts_deltas) >= 8:
            med_dt = float(np.median(pts_deltas))
            p95_spread = float(np.percentile( np.abs(np.asarray(pts_deltas) - med_dt), 95 ))
            if med_dt > 0 and (p95_spread / med_dt) > 0.35:
                timing_notes.append(
                    "Variable frame rate detected; replay timing uses frame_index / fps and is approximate."
                )
        if timing_notes:
            result["timing_note"] = " ".join(timing_notes)
        return result
    finally:
        cap.release()


def validate_movement_analysis(
    data: Any,
    *,
    expected_segment_index: int | None = None,
    expected_source_sha256: str | None = None,
    expected_model_hash: str | None = None,
    expected_width: int | None = None,
    expected_height: int | None = None,
    expected_frame_count: int | None = None,
) -> dict[str, Any]:
    """Validate a movement analysis payload before replay rendering."""
    if not isinstance(data, dict):
        raise ValueError("Invalid movement analysis structure")  # noqa: TRY004
    if data.get("schema_version") != MOVEMENT_SCHEMA_VERSION:
        raise ValueError("Unsupported movement analysis schema version")
    if data.get("detector_version") != DETECTOR_VERSION:
        raise ValueError("Movement analysis detector version changed; re-run analysis")
    if data.get("detector_fingerprint") != detector_fingerprint():
        raise ValueError("Movement analysis detector configuration changed; re-run analysis")
    if data.get("timing_basis") != "frame_index_over_fps":
        raise ValueError("Unsupported movement analysis timing basis")

    if expected_model_hash and data.get("model_hash") and data.get("model_hash") != expected_model_hash:
        raise ValueError("Pose model changed since movement analysis was created; re-run analysis")
    if expected_segment_index is not None and data.get("segment_index") != expected_segment_index:
        raise ValueError("Movement analysis segment index mismatch")
    if expected_source_sha256 is not None and data.get("source_sha256") != expected_source_sha256:
        raise ValueError("Movement analysis does not match the current video stream")

    width = data.get("width")
    height = data.get("height")
    fps = data.get("fps")
    frame_count = data.get("frame_count")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or width <= 0
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height <= 0
        or not isinstance(frame_count, int)
        or isinstance(frame_count, bool)
        or not (1 <= frame_count <= MAX_FRAME_COUNT)
        or not isinstance(fps, (int, float))
        or isinstance(fps, bool)
        or not math.isfinite(fps)
        or fps <= 0.0
    ):
        raise ValueError("Invalid video dimensions or timing in movement analysis")

    if expected_width is not None and width != expected_width:
        raise ValueError("Selected video width does not match analysed Raw video")
    if expected_height is not None and height != expected_height:
        raise ValueError("Selected video height does not match analysed Raw video")
    if expected_frame_count is not None and abs(frame_count - expected_frame_count) > 2:
        raise ValueError("Selected video frame count does not match analysed Raw video")

    pivots = data.get("pivots")
    steps = data.get("steps")
    hip_frames = data.get("hip_frames")
    cut_frames = data.get("cut_frames", [])
    if (
        not isinstance(pivots, list)
        or len(pivots) > MAX_PIVOT_EVENTS
        or not isinstance(steps, list)
        or len(steps) > MAX_STEP_EVENTS
        or not isinstance(hip_frames, list)
        or len(hip_frames) > MAX_HIP_FRAMES
        or not isinstance(cut_frames, list)
    ):
        raise ValueError("Invalid or oversized event lists in movement analysis")

    prev_end = 0
    for pivot in pivots:
        if not isinstance(pivot, dict):
            raise ValueError("Invalid pivot entry")  # noqa: TRY004
        sf = pivot.get("start_frame")
        ef = pivot.get("end_frame")
        direction = pivot.get("direction")
        net_deg = pivot.get("net_rotation_deg")
        qual = pivot.get("quality")
        if (
            not isinstance(sf, int)
            or isinstance(sf, bool)
            or not isinstance(ef, int)
            or isinstance(ef, bool)
            or not (0 <= sf < ef <= frame_count)
            or sf < prev_end
            or direction not in ("clockwise", "anticlockwise")
            or not isinstance(net_deg, (int, float))
            or not math.isfinite(net_deg)
            or not isinstance(qual, (int, float))
            or not math.isfinite(qual)
            or not (0.0 < qual <= 1.0)
        ):
            raise ValueError("Invalid pivot interval in movement analysis")
        prev_end = ef

    prev_step_frame = -1
    prev_step_time = -1.0
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Invalid step entry")  # noqa: TRY004
        f = step.get("frame")
        t = step.get("time")
        side = step.get("side")
        conf = step.get("confidence")
        if (
            not isinstance(f, int)
            or isinstance(f, bool)
            or not (0 <= f < frame_count)
            or f < prev_step_frame
            or not isinstance(t, (int, float))
            or isinstance(t, bool)
            or not math.isfinite(t)
            or t < 0.0
            or t + 1e-9 < prev_step_time
            or side not in ("left", "right")
            or not isinstance(conf, (int, float))
            or not math.isfinite(conf)
            or not (0.0 < conf <= 1.0)
        ):
            raise ValueError("Invalid step event in movement analysis")
        for coord_key, limit in (
            ("hx", width),
            ("tx", width),
            ("ax", width),
            ("hy", height),
            ("ty", height),
            ("ay", height),
        ):
            val = step.get(coord_key)
            if (
                not isinstance(val, (int, float))
                or isinstance(val, bool)
                or not math.isfinite(val)
                or not (-0.15 * limit <= float(val) <= 1.15 * limit)
            ):
                raise ValueError(f"Invalid step coordinate {coord_key}")
        prev_step_frame = f
        prev_step_time = float(t)

    prev_hip_frame = -1
    for hf in hip_frames:
        if not isinstance(hf, dict):
            raise ValueError("Invalid hip geometry entry")  # noqa: TRY004
        f = hf.get("frame")
        if (
            not isinstance(f, int)
            or isinstance(f, bool)
            or not (0 <= f < frame_count)
            or f <= prev_hip_frame
        ):
            raise ValueError("Invalid hip frame index in movement analysis")
        for key in ("cx", "cy", "lx", "ly", "rx", "ry", "scale", "hip_span"):
            val = hf.get(key)
            if not isinstance(val, (int, float)) or isinstance(val, bool) or not math.isfinite(val):
                raise ValueError(f"Non-finite hip geometry field {key}")
        if float(hf["scale"]) <= 0.0 or float(hf["hip_span"]) < 0.0:
            raise ValueError("Invalid hip scale or span")
        prev_hip_frame = f

    return data


def _draw_footprint(
    canvas: np.ndarray,
    hx: float,
    hy: float,
    tx: float,
    ty: float,
    side: str,
    opacity: float,
) -> None:
    if opacity <= 0.01:
        return
    dx = tx - hx
    dy = ty - hy
    length = max(14.0, math.hypot(dx, dy))
    angle_deg = math.degrees(math.atan2(dy, dx))
    ux = dx / max(1e-6, math.hypot(dx, dy))
    uy = dy / max(1e-6, math.hypot(dx, dy))
    px, py = -uy, ux

    heel_w = max(5.0, 0.22 * length)
    ball_w = max(7.0, 0.30 * length)
    ball_x = hx + 0.68 * dx
    ball_y = hy + 0.68 * dy

    # Bounding ROI for fast alpha blending
    pad = math.ceil(length * 0.65 + 14)
    cx_mid = round(0.5 * (hx + tx))
    cy_mid = round(0.5 * (hy + ty))
    h_img, w_img = canvas.shape[:2]
    x0 = max(0, cx_mid - pad)
    y0 = max(0, cy_mid - pad)
    x1 = min(w_img, cx_mid + pad)
    y1 = min(h_img, cy_mid + pad)
    if x1 <= x0 or y1 <= y0:
        return

    roi = canvas[y0:y1, x0:x1]
    layer = roi.copy()

    # Left foot: warm amber/gold; Right foot: bright cyan/teal; both with dark halo & light core
    fill_color = (20, 185, 255) if side == "left" else (235, 205, 0)
    outline_color = (18, 24, 30)
    inner_color = (225, 245, 255) if side == "left" else (255, 248, 220)

    lhx, lhy = hx - x0, hy - y0
    ltx, lty = tx - x0, ty - y0
    lbx, lby = ball_x - x0, ball_y - y0

    poly = np.array(
        [
            [round(lhx + px * heel_w), round(lhy + py * heel_w)],
            [round(lbx + px * ball_w), round(lby + py * ball_w)],
            [round(ltx + px * (0.16 * length)), round(lty + py * (0.16 * length))],
            [round(ltx - px * (0.16 * length)), round(lty - py * (0.16 * length))],
            [round(lbx - px * ball_w), round(lby - py * ball_w)],
            [round(lhx - px * heel_w), round(lhy - py * heel_w)],
        ],
        dtype=np.int32,
    )

    heel_center = (round(lhx), round(lhy))
    toe_center = (round(ltx), round(lty))
    heel_axes = (max(4, round(0.24 * length)), max(4, round(heel_w)))
    toe_axes = (max(5, round(0.28 * length)), max(4, round(ball_w)))

    cv2.fillConvexPoly(layer, poly, fill_color, cv2.LINE_AA)
    cv2.ellipse(layer, heel_center, heel_axes, angle_deg, 0, 360, fill_color, -1, cv2.LINE_AA)
    cv2.ellipse(layer, toe_center, toe_axes, angle_deg, 0, 360, fill_color, -1, cv2.LINE_AA)

    cv2.polylines(layer, [poly], True, outline_color, 2, cv2.LINE_AA)
    cv2.ellipse(layer, heel_center, heel_axes, angle_deg, 0, 360, outline_color, 2, cv2.LINE_AA)
    cv2.ellipse(layer, toe_center, toe_axes, angle_deg, 0, 360, outline_color, 2, cv2.LINE_AA)

    # Inner heel-to-toe axis line so direction & foot placement read clearly on any floor
    cv2.arrowedLine(
        layer,
        heel_center,
        toe_center,
        inner_color,
        2,
        cv2.LINE_AA,
        tipLength=0.28,
    )

    alpha = float(max(0.0, min(1.0, opacity)))
    cv2.addWeighted(layer, alpha, roi, 1.0 - alpha, 0.0, dst=roi)


def _draw_pivot_ring(
    canvas: np.ndarray,
    hip: dict[str, Any],
    direction: str,
    sx: float,
    sy: float,
    mirror: bool,
) -> None:
    width = canvas.shape[1]
    scale = float(hip["scale"]) * 0.5 * (sx + sy)
    lx = float(hip["lx"]) * sx
    ly = float(hip["ly"]) * sy
    rx = float(hip["rx"]) * sx
    ry = float(hip["ry"]) * sy
    if mirror:
        lx = (width - 1) - lx
        rx = (width - 1) - rx

    hip_span = math.hypot(rx - lx, ry - ly)
    if hip_span < max(12.0, 0.08 * scale):
        return

    cx = 0.5 * (lx + rx)
    cy = 0.5 * (ly + ry)
    # Normalize angle to [-90, 90] so the ellipse's bottom/front arc is consistently oriented
    dx = rx - lx
    dy = ry - ly
    if dx < 0:
        dx, dy = -dx, -dy
    angle_deg = math.degrees(math.atan2(dy, dx))

    major_a = max(24, round(max(hip_span * 0.72, scale * 0.38)))
    minor_b = max(10, round(major_a * 0.38))
    center = (round(cx), round(cy))
    axes = (major_a, minor_b)

    # Effective screen-space direction reflects once when mirrored
    effective_dir = direction
    if mirror:
        effective_dir = "anticlockwise" if direction == "clockwise" else "clockwise"

    ring_color = (0, 225, 255) if effective_dir == "clockwise" else (255, 205, 0)
    halo_color = (18, 24, 30)

    # Draw full perspective ring with dark halo for contrast
    cv2.ellipse(canvas, center, axes, angle_deg, 15, 345, halo_color, 5, cv2.LINE_AA)
    cv2.ellipse(canvas, center, axes, angle_deg, 15, 345, ring_color, 2, cv2.LINE_AA)

    # Compute tangent arrowhead on the front (lower-screen, phi ~ 65 deg or 115 deg) arc of the ellipse
    rot_rad = math.radians(angle_deg)
    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

    def ellipse_pt(phi_deg: float) -> tuple[float, float]:
        p_rad = math.radians(phi_deg)
        ex = major_a * math.cos(p_rad)
        ey = minor_b * math.sin(p_rad)
        return (
            cx + ex * cos_r - ey * sin_r,
            cy + ex * sin_r + ey * cos_r,
        )

    if effective_dir == "clockwise":
        tail_pt = ellipse_pt(115.0)
        tip_pt = ellipse_pt(65.0)
    else:
        tail_pt = ellipse_pt(65.0)
        tip_pt = ellipse_pt(115.0)

    tail = (round(tail_pt[0]), round(tail_pt[1]))
    tip = (round(tip_pt[0]), round(tip_pt[1]))
    cv2.arrowedLine(canvas, tail, tip, halo_color, 6, cv2.LINE_AA, tipLength=0.45)
    cv2.arrowedLine(canvas, tail, tip, ring_color, 3, cv2.LINE_AA, tipLength=0.45)


def render_movement_overlay(
    image: np.ndarray,
    analysis: dict[str, Any] | None,
    frame_index: int,
    fps: float | None = None,
    *,
    show_pivots: bool = False,
    show_steps: bool = False,
    step_history_seconds: float = 5.0,
    mirror: bool = False,
) -> np.ndarray:
    """Composite the movement analysis layer on a fresh copy of `image`."""
    canvas = cv2.flip(image, 1) if mirror else image.copy()
    if analysis is None or (not show_pivots and not show_steps):
        return canvas

    src_w = int(analysis.get("width", canvas.shape[1]))
    src_h = int(analysis.get("height", canvas.shape[0]))
    if src_w <= 0 or src_h <= 0:
        return canvas
    sx = canvas.shape[1] / src_w
    sy = canvas.shape[0] / src_h
    effective_fps = float(fps if fps and math.isfinite(fps) and fps > 0 else analysis.get("fps", 15.0))
    media_time = frame_index / effective_fps

    if show_steps:
        cut_frames = analysis.get("cut_frames", [])
        latest_cut = max((c for c in cut_frames if isinstance(c, int) and c <= frame_index), default=-1)
        for step in analysis.get("steps", ()):
            step_frame = int(step["frame"])
            if step_frame < latest_cut or step_frame > frame_index:
                continue
            step_time = float(step.get("time", step_frame / effective_fps))
            age = media_time - step_time
            # Ensure a step on its exact landing frame has a visible initial ramp opacity
            if frame_index == step_frame and age == 0.0:
                age = min(0.04, 0.25 * min(0.15, 0.15 * max(0.5, step_history_seconds)))
            opacity = step_opacity(age, step_history_seconds)
            if opacity <= 0.0:
                continue
            hx = float(step["hx"]) * sx
            hy = float(step["hy"]) * sy
            tx = float(step["tx"]) * sx
            ty = float(step["ty"]) * sy
            if mirror:
                hx = (canvas.shape[1] - 1) - hx
                tx = (canvas.shape[1] - 1) - tx
            _draw_footprint(canvas, hx, hy, tx, ty, str(step["side"]), opacity)

    if show_pivots:
        active_pivot = next(
            (
                p
                for p in analysis.get("pivots", ())
                if int(p["start_frame"]) <= frame_index < int(p["end_frame"])
            ),
            None,
        )
        if active_pivot is not None:
            hip_entry = next(
                (hf for hf in analysis.get("hip_frames", ()) if int(hf["frame"]) == frame_index),
                None,
            )
            if hip_entry is not None:
                _draw_pivot_ring(
                    canvas,
                    hip_entry,
                    str(active_pivot["direction"]),
                    sx,
                    sy,
                    mirror,
                )

    return canvas


@dataclass(frozen=True)
class RecordingTarget:
    kind: str  # "archive" or "standalone"
    path: Path
    segment_index: int = 0

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.kind, str(self.path.resolve()), int(self.segment_index))


@dataclass
class _MovementJob:
    job_id: str
    target: RecordingTarget
    model_path: Path
    cancel: threading.Event


class MovementAnalysisCoordinator:
    """Single background worker with bounded queue, deduplication and clean shutdown."""

    def __init__(
        self,
        event_sink: Callable[[tuple[Any, ...]], None],
        *,
        estimator_factory: Callable[[Path], PoseEstimator] | None = None,
        max_pending: int = 32,
    ):
        self.event_sink = event_sink
        self.estimator_factory = estimator_factory or MediaPipeEstimator
        self.max_pending = max_pending
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending: OrderedDict[tuple[str, str, int], _MovementJob] = OrderedDict()
        self._active: _MovementJob | None = None
        self._closed = False
        self._worker: threading.Thread | None = None

    def is_busy(self, key: tuple[str, str, int]) -> bool:
        with self._lock:
            return key in self._pending or (self._active is not None and self._active.target.key == key)

    def is_queued(self, key: tuple[str, str, int]) -> bool:
        with self._lock:
            return key in self._pending

    def is_active(self, key: tuple[str, str, int]) -> bool:
        with self._lock:
            return self._active is not None and self._active.target.key == key

    def enqueue(self, target: RecordingTarget, model_path: Path) -> str | None:
        key = target.key
        with self._cond:
            if self._closed:
                return None
            if key in self._pending:
                return self._pending[key].job_id
            if self._active is not None and self._active.target.key == key and not self._active.cancel.is_set():
                return self._active.job_id
            if len(self._pending) >= self.max_pending:
                raise RuntimeError("Movement analysis queue is full; wait for pending jobs to finish")
            job = _MovementJob(
                job_id=uuid4().hex,
                target=target,
                model_path=model_path,
                cancel=threading.Event(),
            )
            self._pending[key] = job
            self._ensure_worker_locked()
            self._cond.notify_all()
        self.event_sink(("movement_queued", job.job_id, key))
        return job.job_id

    def cancel(self, key: tuple[str, str, int]) -> bool:
        cancelled_job: _MovementJob | None = None
        was_pending = False
        with self._cond:
            if key in self._pending:
                cancelled_job = self._pending.pop(key)
                cancelled_job.cancel.set()
                was_pending = True
            elif self._active is not None and self._active.target.key == key:
                cancelled_job = self._active
                cancelled_job.cancel.set()
            self._cond.notify_all()
        if was_pending and cancelled_job is not None:
            self.event_sink(("movement_cancelled", cancelled_job.job_id, key))
        return cancelled_job is not None

    def close(self) -> None:
        with self._cond:
            self._closed = True
            for job in self._pending.values():
                job.cancel.set()
            self._pending.clear()
            if self._active is not None:
                self._active.cancel.set()
            self._cond.notify_all()
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=1.5)

    def _ensure_worker_locked(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run_loop, name="movement-analysis-worker", daemon=True
            )
            self._worker.start()

    def _run_loop(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._closed:
                    self._cond.wait()
                if self._closed:
                    self._active = None
                    return
                _, job = self._pending.popitem(last=False)
                if job.cancel.is_set():
                    self.event_sink(("movement_cancelled", job.job_id, job.target.key))
                    continue
                self._active = job

            try:
                self._execute_job(job)
            finally:
                with self._cond:
                    if self._active is job:
                        self._active = None

    def _execute_job(self, job: _MovementJob) -> None:
        key = job.target.key
        if job.cancel.is_set():
            self.event_sink(("movement_cancelled", job.job_id, key))
            return

        self.event_sink(("movement_progress", job.job_id, key, 0))
        estimator: PoseEstimator | None = None
        try:
            with TemporaryDirectory(prefix="tango-movement-") as tmp_dir:
                tmp_root = Path(tmp_dir)
                if job.target.kind == "archive":
                    member_name = f"video_{job.target.segment_index:02d}.mp4"
                    extracted_video = tmp_root / member_name
                    source_sha256 = extract_segment_video(
                        job.target.path, job.target.segment_index, extracted_video
                    )
                    video_to_scan = extracted_video
                    seg_idx = job.target.segment_index
                else:
                    video_to_scan = job.target.path.resolve()
                    if not video_to_scan.is_file():
                        raise ValueError(f"Video not found: {video_to_scan.name}")
                    source_sha256 = file_sha256(video_to_scan)
                    member_name = video_to_scan.name
                    seg_idx = -1

                if job.cancel.is_set():
                    self.event_sink(("movement_cancelled", job.job_id, key))
                    return

                estimator = self.estimator_factory(job.model_path)
                if self.estimator_factory is MediaPipeEstimator:
                    model_hash = getattr(estimator, "model_hash", "") or verify(job.model_path)
                else:
                    model_hash = getattr(estimator, "model_hash", "") or (
                        file_sha256(job.model_path) if job.model_path.is_file() else "custom-estimator"
                    )

                def report_progress(pct: int) -> None:
                    if not job.cancel.is_set():
                        self.event_sink(("movement_progress", job.job_id, key, pct))

                result = scan_video_movement(
                    video_to_scan,
                    estimator,
                    model_hash=model_hash,
                    source_sha256=source_sha256,
                    segment_index=seg_idx,
                    source_member=member_name,
                    cancel=job.cancel,
                    on_progress=report_progress,
                )

                if job.cancel.is_set():
                    self.event_sink(("movement_cancelled", job.job_id, key))
                    return

                validate_movement_analysis(
                    result,
                    expected_segment_index=seg_idx,
                    expected_source_sha256=source_sha256,
                )

                readonly_warning = False
                if job.target.kind == "archive":
                    try:
                        store_movement_analysis(
                            job.target.path,
                            job.target.segment_index,
                            source_sha256,
                            result,
                        )
                    except PermissionError:
                        readonly_warning = True
                    except OSError as error:
                        # Distinguish read-only filesystem / permission errors from corrupt archive
                        if getattr(error, "errno", None) in (1, 13, 30):
                            readonly_warning = True
                        else:
                            raise

                self.event_sink(("movement_done", job.job_id, key, result, readonly_warning))
        except Exception as error:  # noqa: BLE001 - worker boundary: report error to UI without crashing worker
            if job.cancel.is_set() or "cancelled" in str(error).lower():
                self.event_sink(("movement_cancelled", job.job_id, key))
            else:
                self.event_sink(("movement_error", job.job_id, key, str(error)))
        finally:
            if estimator is not None:
                with suppress(Exception):
                    estimator.close()


__all__ = [
    "DETECTOR_CONFIG",
    "DETECTOR_VERSION",
    "MOVEMENT_SCHEMA_VERSION",
    "MovementAnalysisCoordinator",
    "RecordingTarget",
    "analyse_movement_observations",
    "detector_fingerprint",
    "hip_heading_3d_deg",
    "movement_member_name",
    "read_movement_analysis",
    "render_movement_overlay",
    "scan_video_movement",
    "step_opacity",
    "unwrap_angles",
    "validate_movement_analysis",
    "wrapped_angle_diff",
]
