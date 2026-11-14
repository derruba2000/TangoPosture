"""Per-frame coach evidence, posture annotations and cached rendering."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import cv2
import numpy as np

from ..calibration.measurements import measure_targets
from ..calibration.models import (
    METRIC_DETAILS,
    ApprovedRange,
    RangeProfile,
    profile_from_calibration,
)
from ..config import Config, RangeDefault
from ..domain import (
    Calibration,
    FrameObservation,
    Landmark,
)
from ..geometry import (
    torso_axis_alignment,
)
from ..overlay import MESSAGES, draw, screen_point
from ..pose.constants import CONNECTIONS
from ..pose.interpolation import interpolate_poses
from ..quality import reliable
from ..temporal import Evaluator
from .constants import SOUNDTRACK_SOURCE_LABEL


def range_feedback(
    rows: list[dict[str, Any]], profile: RangeProfile,
) -> tuple[str, list[str]]:
    return target_feedback(rows, profile.metrics)


def target_feedback(
    rows: list[dict[str, Any]], targets: Mapping[str, RangeDefault | ApprovedRange],
) -> tuple[str, list[str]]:
    messages = []
    lines = []
    for row in rows:
        key = row["metric"]
        target = targets[key]
        label = METRIC_DETAILS[key][1]
        value = row.get("smoothed")
        measured = "Unknown" if value is None else f"{value:.3g}"
        limits = f"{target.lower:.3g} .. {target.upper:.3g} {target.unit}"
        reason = f" | {row['reason']}" if row.get("reason") else ""
        lines.append(f"{label}: {measured} | approved {limits} | {row['state']}{reason}")
        if row["state"] == "DEVIATION":
            messages.append(f"{label}: {measured} outside approved range {limits}")
    return "; ".join(messages), lines


def _coach_observation(
    entry: dict[str, Any], width: int, height: int, *, require_measurements: bool = False,
    use_display_poses: bool = False,
) -> FrameObservation:
    if require_measurements and "measurement_poses" not in entry:
        raise ValueError("This cache has display landmarks only. Analyze posture again before comparing.")
    raw_poses = entry.get("measurement_poses", entry.get("poses"))
    if use_display_poses and not require_measurements and "display_poses" in entry:
        raw_poses = entry["display_poses"]
    if require_measurements and not isinstance(raw_poses, list):
        raise TypeError("Invalid cached measurement poses. Analyze posture again.")
    if not isinstance(raw_poses, list):
        raw_poses = [entry.get("pose")]
    poses = []
    for raw_pose in raw_poses:
        if require_measurements and not isinstance(raw_pose, dict):
            raise TypeError("Invalid cached measurement pose. Analyze posture again.")
        if not isinstance(raw_pose, dict) or not raw_pose:
            continue
        pose = {}
        for name, coords in raw_pose.items():
            if require_measurements and (
                not isinstance(coords, list | tuple) or len(coords) < 3
            ):
                raise ValueError("Invalid cached measurement landmark. Analyze posture again.")
            if isinstance(coords, list | tuple) and len(coords) >= 3:
                pose[str(name)] = Landmark(
                    x=float(coords[0]), y=float(coords[1]), z=float(coords[2]),
                    visibility=float(coords[3]) if len(coords) >= 4 and coords[3] is not None else None,
                    presence=float(coords[4]) if len(coords) >= 5 and coords[4] is not None else None,
                )
        if pose:
            poses.append(pose)
    return FrameObservation(float(entry.get("timestamp", 0.0)), width, height, poses)


def compare_coach_data(
    data: dict[str, Any], cfg: Config, reference: Calibration,
) -> dict[str, Any]:
    """Compare raw cached landmarks with approved ranges, without rerunning inference."""
    profile = profile_from_calibration(reference)
    if profile is None:
        raise ValueError("This legacy calibration has no approved ranges. Select a range profile.")
    if data.get("model_hash") != profile.model_hash:
        raise ValueError("Calibration and recording use different measurement models. Reanalyze the recording.")
    capture = data.get("capture_profile")
    if not isinstance(capture, dict) or any(
        capture.get(key) != profile.capture_profile[key] for key in ("side", "facing")
    ):
        raise ValueError("Recording side/facing does not match the selected calibration.")
    comparison_cfg = cfg.model_copy(update={
        "side": profile.capture_profile["side"], "facing": profile.capture_profile["facing"],
    })
    return _compare_targets(data, comparison_cfg, profile.metrics, profile.name, {
        "calibration_id": profile.id, "calibration_name": profile.name,
    })


def compare_default_coach_data(data: dict[str, Any], cfg: Config) -> dict[str, Any]:
    capture = data.get("capture_profile")
    if not isinstance(capture, dict) or any(
        capture.get(key) not in ("left", "right") for key in ("side", "facing")
    ):
        raise ValueError("Cached recording side/facing is missing. Analyze posture again.")
    cfg = cfg.model_copy(update={key: capture[key] for key in ("side", "facing")})
    return _compare_targets(data, cfg, cfg.calibration_defaults, "Settings default ranges", {
        "calibration_name": "Settings default ranges",
    })


def _compare_targets(
    data: dict[str, Any], cfg: Config, targets: Mapping[str, RangeDefault | ApprovedRange],
    name: str, metadata: dict[str, Any],
) -> dict[str, Any]:
    frames = data.get("frames")
    if not isinstance(frames, dict):
        raise TypeError("No cached posture frames. Analyze posture before comparing.")
    if not frames:
        raise ValueError("No cached posture frames. Analyze posture before comparing.")
    width, height = data.get("width"), data.get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("Cached frame dimensions are missing or invalid. Analyze posture again.")
    evaluators = {
        key: Evaluator(key, 0, cfg, bounds=(target.lower, target.upper))
        for key, target in targets.items()
    }
    compared = {}
    for index in sorted(frames, key=int):
        entry = frames[index]
        if not isinstance(entry, dict):
            raise TypeError("Invalid cached posture frame")
        observation = _coach_observation(entry, width, height, require_measurements=True)
        measured = measure_targets(observation, cfg, targets)
        rows = [
            {"metric": key, "unit": targets[key].unit,
             "reason": measured.quality[key].reason, **evaluator.update(
                observation.timestamp,
                measured.values[key] if measured.quality[key].valid else None,
            )}
            for key, evaluator in evaluators.items()
        ]
        message, lines = target_feedback(rows, targets)
        compared[index] = {
            **entry, "message": message, "lines": lines,
            "notes": [name, *lines],
            "calibration_rows": rows,
        }
    return {
        **data, "frames": compared, "settings_coaching": False,
        **metadata,
    }


def interpolate_coach_data(data: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """Prepare a separate display layer, leaving raw evidence and coaching untouched."""
    frames = data.get("frames")
    if cfg.replay_joint_max_gap <= 0 or not isinstance(frames, dict) or not frames:
        return data
    width, height = int(data.get("width", 0)), int(data.get("height", 0))
    indices = sorted(frames, key=int)
    observations = [_coach_observation(frames[index], width, height) for index in indices]
    interpolated = interpolate_poses(
        observations, method=cfg.replay_joint_interpolation,
        max_gap=cfg.replay_joint_max_gap, confidence=cfg.confidence,
    )
    return {
        **data, "frames": {
            index: {
                **frames[index],
                "display_poses": [_serialise_pose(pose) for pose in observation.poses],
            }
            for index, observation in zip(indices, interpolated, strict=True)
        },
    }


def render_coach_frame_overlay(
    image: np.ndarray,
    coach_data: dict[str, Any],
    frame_index: int,
    cfg: Config,
    *,
    verbose_skeleton: bool = True,
    show_vertical_axis: bool = True,
    show_footer: bool = False,
    lines_only: bool = False,
) -> np.ndarray:
    """Render the Open Practice Coach HUD (axis lines, skeleton, alerts, role, beats) on a frame.

    Uses the per-frame state saved by `analyse_imported_clip` and the live-coach `overlay.draw()`.
    Returns `image` unchanged when the frame has no saved coach state or sizes disagree.
    `lines_only` draws reliable skeleton connections without labels, dots or alerts.
    """
    frames_map = coach_data.get("frames")
    if not isinstance(frames_map, dict):
        return image
    entry = frames_map.get(str(frame_index))
    if not isinstance(entry, dict):
        return image

    width = int(coach_data.get("width") or image.shape[1])
    height = int(coach_data.get("height") or image.shape[0])
    if image.shape[1] != width or image.shape[0] != height:
        return image

    observation = _coach_observation(entry, width, height)
    poses = _coach_observation(entry, width, height, use_display_poses=True).poses

    if lines_only:
        canvas = image.copy()
        for pose in poses:
            for a, b in CONNECTIONS:
                if reliable(pose.get(a), cfg.confidence) and reliable(
                    pose.get(b), cfg.confidence
                ):
                    pa, pb = pose[a], pose[b]
                    cv2.line(
                        canvas,
                        screen_point(pa.x, pa.y, width, height, False),
                        screen_point(pb.x, pb.y, width, height, False),
                        (80, 220, 80),
                        2,
                        cv2.LINE_AA,
                    )
        return canvas

    def _opt(key: str) -> float | None:
        value = entry.get(key)
        return float(value) if isinstance(value, int | float) else None

    beat_state = entry.get("beat_state")
    return cast(
        np.ndarray,
        draw(
            image,
            observation,
            False,
            list(entry.get("lines") or []),
            str(entry.get("message") or ""),
            cfg.confidence,
            head_down_angle=cfg.head_down_angle,
            head_down_duration=cfg.head_down_duration,
            head_down_elapsed=float(entry.get("head_down_elapsed", 0.0)),
            head_angle_smoothed=_opt("head_angle"),
            role=str(entry.get("role") or "NONE"),
            beat_state=beat_state if isinstance(beat_state, dict) else None,
            head_neutral_offset=float(coach_data.get("neutral_offset", cfg.head_neutral_offset)),
            shoulder_raise_threshold=cfg.shoulder_raise_threshold,
            shoulder_tension_duration=cfg.shoulder_tension_duration,
            shoulder_tension_elapsed=float(entry.get("shoulder_tension_elapsed", 0.0)),
            shoulder_neutral_clearance=float(
                entry.get("shoulder_neutral_clearance", cfg.shoulder_neutral_clearance)
            ),
            shoulder_elev_smoothed=_opt("shoulder_elev_pct"),
            embrace_type=str(entry.get("embrace_type") or "OPEN"),
            embrace_closed_threshold=cfg.embrace_closed_threshold,
            closed_embrace_min_knee_bend=cfg.closed_embrace_min_knee_bend,
            knee_straight_duration=cfg.knee_straight_duration,
            knee_straight_elapsed=float(entry.get("knee_straight_elapsed", 0.0)),
            knee_bend_smoothed=_opt("knee_bend_deg"),
            facing=cfg.facing,
            side=cfg.side,
            axis_neutral_offset=float(
                coach_data.get("axis_neutral_offset", cfg.axis_neutral_offset)
            ),
            axis_curve_entry=cfg.axis_curve_entry,
            axis_curve_clear=cfg.axis_curve_clear,
            axis_curve_duration=cfg.axis_curve_duration,
            axis_curve_elapsed=float(entry.get("axis_curve_elapsed", 0.0)),
            axis_curve_smoothed=_opt("axis_curve_smoothed"),
            axis_curve_state=str(entry.get("axis_curve_state") or "WITHIN_LIMIT"),
            ankle_high_lift_ratio=cfg.ankle_high_lift_ratio,
            ankle_lift_smoothed=_opt("ankle_lift_ratio"),
            consecutive_high_ankle_steps=int(entry.get("consecutive_high_ankle_steps", 0)),
            max_consecutive_high_ankle_steps=cfg.max_consecutive_high_ankle_steps,
            slow_tango_max_bpm=cfg.slow_tango_max_bpm,
            ankle_high_alert=bool(entry.get("ankle_alert", False)),
            verbose_skeleton=verbose_skeleton,
            show_vertical_axis=show_vertical_axis,
            show_footer=show_footer,
            settings_coaching=bool(coach_data.get("settings_coaching", True)),
            skeleton_poses=poses,
        ),
    )


def coach_notes_for_frame(coach_data: dict[str, Any], frame_index: int) -> list[str] | None:
    """Return the saved Live-coaching-notes lines for a source frame, if present."""
    frames_map = coach_data.get("frames")
    if not isinstance(frames_map, dict):
        return None
    entry = frames_map.get(str(frame_index))
    if not isinstance(entry, dict):
        return None
    notes = entry.get("notes")
    if isinstance(notes, list) and notes:
        return [str(n) for n in notes]
    return None


@dataclass
class _AlertTimers:
    """Sustained-alert timers mirroring the live Open Practice Coach (`Application.run`)."""

    head_down_since: float | None = None
    shoulder_tension_since: float | None = None
    shoulder_last_valid: float | None = None
    knee_straight_since: float | None = None
    knee_last_valid: float | None = None


def _round_or_none(value: float | None, digits: int = 4) -> float | None:
    return None if value is None or not math.isfinite(value) else round(float(value), digits)


def _serialise_pose(pose: dict[str, Landmark]) -> dict[str, list[float | None]]:
    out: dict[str, list[float | None]] = {}
    for name, lm in pose.items():
        if not (math.isfinite(lm.x) and math.isfinite(lm.y)):
            continue
        out[name] = [
            round(float(lm.x), 4),
            round(float(lm.y), 4),
            round(float(lm.z), 4) if lm.z is not None and math.isfinite(lm.z) else 0.0,
            _round_or_none(lm.visibility, 3),
            _round_or_none(lm.presence, 3),
        ]
    return out


def _build_coach_frame_entry(
    *,
    cfg: Config,
    frame_obs: FrameObservation,
    display_poses: list[dict[str, Landmark]],
    source_time: float,
    clip_time: float,
    head_angle: float | None,
    shoulder_elev_pct: float | None,
    shoulder_neutral_clearance: float,
    knee_bend_deg: float | None,
    ankle_lift_ratio: float | None,
    consecutive_high_ankle_steps: int,
    bad_ankles: bool,
    ankle_high_now: bool,
    axis_curve_smoothed: float | None,
    axis_curve_state: str,
    axis_curve_elapsed: float,
    bad_axis: bool,
    role: str,
    embrace_type: str,
    upper_body: dict[str, float] | None,
    st_state: dict[str, Any] | None,
    quality_reason: str,
    timers: _AlertTimers,
    session_rows: list[dict[str, Any]],
    single_pose_ok: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute alert timers and per-frame coaching state/notes for one analysed frame."""
    # Head pitch: continuous downward tilt.
    if head_angle is not None and head_angle < cfg.head_down_angle:
        if timers.head_down_since is None:
            timers.head_down_since = source_time
        head_down_elapsed = source_time - timers.head_down_since
    else:
        timers.head_down_since = None
        head_down_elapsed = 0.0
    bad_head = head_down_elapsed >= cfg.head_down_duration

    # Shoulder tension with hysteresis.
    sh_clear = max(4.0, cfg.shoulder_raise_threshold * 0.65)
    if shoulder_elev_pct is not None:
        timers.shoulder_last_valid = source_time
        if shoulder_elev_pct >= cfg.shoulder_raise_threshold:
            if timers.shoulder_tension_since is None:
                timers.shoulder_tension_since = source_time
            shoulder_tension_elapsed = source_time - timers.shoulder_tension_since
        elif timers.shoulder_tension_since is not None and shoulder_elev_pct >= sh_clear:
            shoulder_tension_elapsed = source_time - timers.shoulder_tension_since
        else:
            timers.shoulder_tension_since = None
            shoulder_tension_elapsed = 0.0
    elif (
        timers.shoulder_last_valid is None
        or (source_time - timers.shoulder_last_valid) > cfg.max_gap
    ):
        timers.shoulder_tension_since = None
        shoulder_tension_elapsed = 0.0
    elif timers.shoulder_tension_since is not None:
        shoulder_tension_elapsed = source_time - timers.shoulder_tension_since
    else:
        shoulder_tension_elapsed = 0.0
    bad_shoulders = shoulder_tension_elapsed >= cfg.shoulder_tension_duration

    # Knee bend with hysteresis.
    knee_clear = cfg.closed_embrace_min_knee_bend + 2.5
    if knee_bend_deg is not None:
        timers.knee_last_valid = source_time
        if knee_bend_deg < cfg.closed_embrace_min_knee_bend:
            if timers.knee_straight_since is None:
                timers.knee_straight_since = source_time
            knee_straight_elapsed = source_time - timers.knee_straight_since
        elif timers.knee_straight_since is not None and knee_bend_deg < knee_clear:
            knee_straight_elapsed = source_time - timers.knee_straight_since
        else:
            timers.knee_straight_since = None
            knee_straight_elapsed = 0.0
    elif timers.knee_last_valid is None or (source_time - timers.knee_last_valid) > cfg.max_gap:
        timers.knee_straight_since = None
        knee_straight_elapsed = 0.0
    elif timers.knee_straight_since is not None:
        knee_straight_elapsed = source_time - timers.knee_straight_since
    else:
        knee_straight_elapsed = 0.0
    bad_knees = knee_straight_elapsed >= cfg.knee_straight_duration

    sh_disp = shoulder_elev_pct if shoulder_elev_pct is not None else 0.0
    head_disp = head_angle if head_angle is not None else 0.0
    knee_disp = knee_bend_deg if knee_bend_deg is not None else 0.0
    alert = ""
    if bad_ankles:
        lift_pct = (ankle_lift_ratio or cfg.ankle_high_lift_ratio) * 100.0
        alert = (
            f"KEEP FEET CLOSE TO FLOOR (SLOW TANGO <={cfg.slow_tango_max_bpm:.0f} BPM): "
            f"ankle raised too high ({lift_pct:.1f}%) for {consecutive_high_ankle_steps} "
            f"consecutive steps (>={cfg.max_consecutive_high_ankle_steps})"
        )
    elif bad_axis and axis_curve_smoothed is not None:
        alert = (
            f"HIP OFFSET: hip/pelvis +{axis_curve_smoothed * 100.0:.1f}% "
            f"past ankle-to-head line ({axis_curve_elapsed:.1f}s)"
        )
    elif bad_knees:
        alert = (
            f"BEND YOUR KNEES: knee bend {knee_disp:.1f} deg "
            f"< {cfg.closed_embrace_min_knee_bend:.1f} deg ({knee_straight_elapsed:.1f}s)"
        )
    elif bad_head and bad_shoulders:
        alert = (
            f"BAD HEAD ALIGNMENT ({head_disp:+.1f} deg) & "
            f"LOWER YOUR SHOULDERS ({sh_disp:+.0f}% for {shoulder_tension_elapsed:.1f}s)"
        )
    elif bad_shoulders:
        alert = (
            f"LOWER YOUR SHOULDERS: shoulder tension detected "
            f"({sh_disp:+.0f}% raised for {shoulder_tension_elapsed:.1f}s)"
        )
    elif bad_head:
        alert = (
            f"BAD HEAD ALIGNMENT: head pointing down ({head_disp:+.1f} deg for "
            f"{head_down_elapsed:.1f}s)"
        )

    if alert:
        primary = alert
    elif not frame_obs.poses:
        primary = quality_reason or "Coaching status: No dancer detected in this frame"
    elif quality_reason:
        primary = quality_reason
    elif any(r.get("state") == "DEVIATION" for r in session_rows):
        primary = next(
            MESSAGES.get(r["metric"], f"{r['metric']}: outside approved range")
            for r in session_rows if r.get("state") == "DEVIATION"
        )
    else:
        primary = "Coaching status: Posture & axis within target thresholds"

    # Beat/phrase state for the HUD and notes.
    beat_state: dict[str, Any] | None = None
    if st_state is not None:
        beat_state = {
            "beat": int(st_state.get("beat") or 0),
            "phrase": int(st_state.get("phrase") or 0),
            "bpm": float(st_state.get("bpm") or cfg.tango_bpm),
            "playing": bool(st_state.get("playing", False)),
            "analyzing": bool(st_state.get("analyzing", False)),
            "analyzed_seconds": 0.0,
            "analysis_target_seconds": 30.0,
            "phrase_locked": st_state.get("phrase") is not None,
            "source": str(st_state.get("source") or SOUNDTRACK_SOURCE_LABEL),
        }
    if st_state is None:
        beat_txt = "disabled"
    elif not st_state.get("playing") or not st_state.get("beat"):
        beat_txt = "No rhythmic beat detected at this moment"
    elif st_state.get("phrase") is None:
        beat_txt = f"Beat pulse {st_state.get('bpm') or 0:.0f} BPM ({SOUNDTRACK_SOURCE_LABEL})"
    else:
        beat_txt = (
            f"Phrase {st_state['phrase']} Beat {st_state['beat']}/8 "
            f"({float(st_state.get('bpm') or 0.0):.0f} BPM, {SOUNDTRACK_SOURCE_LABEL})"
        )
    role_txt = (
        f"{role} ({embrace_type})"
        if role in ("LEADER", "FOLLOWER") and embrace_type in ("CLOSED", "OPEN")
        else role
    )

    torso_txt = "N/A"
    torso_tilt = None
    if single_pose_ok:
        torso = torso_axis_alignment(
            frame_obs.poses[0], frame_obs.width, frame_obs.height, cfg.confidence
        )
        if torso is not None:
            torso_tilt = abs(torso[0])
            torso_txt = f"{torso_tilt:.1f} deg from vertical"
    if axis_curve_smoothed is None:
        hip_txt = "N/A"
    elif bad_axis:
        hip_txt = f"HIGH {axis_curve_smoothed * 100.0:+.1f}% [DEVIATION]"
    elif axis_curve_state == "PENDING":
        hip_txt = f"PENDING {axis_curve_smoothed * 100.0:+.1f}%"
    else:
        hip_txt = f"OK ({axis_curve_smoothed * 100.0:+.1f}%)"
    head_txt = "N/A" if head_angle is None else f"{head_angle:+.1f} deg"
    sh_txt = (
        "N/A"
        if shoulder_elev_pct is None
        else (
            f"HIGH {shoulder_elev_pct:+.0f}%"
            if shoulder_elev_pct >= cfg.shoulder_raise_threshold
            else f"OK ({shoulder_elev_pct:+.0f}%)"
        )
    )
    knee_txt = (
        "N/A"
        if knee_bend_deg is None
        else (
            f"STRAIGHT {knee_bend_deg:.1f} deg"
            if bad_knees or knee_bend_deg < cfg.closed_embrace_min_knee_bend
            else f"OK ({knee_bend_deg:.1f} deg)"
        )
    )
    feet_txt = (
        "N/A"
        if ankle_lift_ratio is None
        else (
            f"HIGH {ankle_lift_ratio * 100.0:.1f}% "
            f"({consecutive_high_ankle_steps}/{cfg.max_consecutive_high_ankle_steps} steps)"
            if bad_ankles or ankle_high_now
            else f"OK ({ankle_lift_ratio * 100.0:.1f}%, "
            f"{consecutive_high_ankle_steps}/{cfg.max_consecutive_high_ankle_steps} steps)"
        )
    )
    lines = [
        f"Role: {role_txt} | Beat: {beat_txt}",
        f"Torso axis: {torso_txt} | Hip offset: {hip_txt}",
        f"Head: {head_txt} | Shoulders: {sh_txt} | Knees: {knee_txt} | Feet: {feet_txt}",
    ]
    if upper_body:
        lines.append(
            "Upper body: "
            + " | ".join(f"{name} {float(val):+.3f}" for name, val in upper_body.items())
        )
    for row in session_rows:
        val = "N/A" if row.get("smoothed") is None else f"{float(row['smoothed']):.3f}"
        delta = "N/A" if row.get("delta") is None else f"{float(row['delta']):+.3f}"
        lines.append(f"{row['metric']}: {val} | delta {delta} | {row['state']}")

    entry: dict[str, Any] = {
        "timestamp": round(float(source_time), 4),
        "clip_timestamp": round(float(clip_time), 4),
        "poses": [_serialise_pose(p) for p in display_poses if p],
        "message": alert,
        "lines": [],
        "head_angle": _round_or_none(head_angle, 3),
        "head_down_elapsed": round(head_down_elapsed, 3),
        "shoulder_elev_pct": _round_or_none(shoulder_elev_pct, 2),
        "shoulder_tension_elapsed": round(shoulder_tension_elapsed, 3),
        "shoulder_neutral_clearance": round(float(shoulder_neutral_clearance), 4),
        "knee_bend_deg": _round_or_none(knee_bend_deg, 2),
        "knee_straight_elapsed": round(knee_straight_elapsed, 3),
        "embrace_type": embrace_type,
        "ankle_alert": bool(bad_ankles),
        "ankle_lift_ratio": _round_or_none(ankle_lift_ratio, 4),
        "consecutive_high_ankle_steps": int(consecutive_high_ankle_steps),
        "axis_curve_smoothed": _round_or_none(axis_curve_smoothed, 4),
        "torso_axis_tilt": _round_or_none(torso_tilt, 3),
        "axis_curve_state": axis_curve_state if axis_curve_state != "UNKNOWN" else "WITHIN_LIMIT",
        "axis_curve_elapsed": round(axis_curve_elapsed, 3),
        "role": role,
        "beat_state": beat_state,
        "alerts": {
            "head": bad_head,
            "shoulders": bad_shoulders,
            "knees": bad_knees,
            "axis": bad_axis,
            "ankles": bool(bad_ankles),
        },
    }
    return entry, {"primary": primary, "lines": lines}


def _movement_note_lines(
    movement_data: dict[str, Any],
    frame_index: int,
    fps: float,
    step_history_seconds: float,
) -> list[str]:
    """Pivot/step insights for the current frame plus clip totals."""
    out: list[str] = []
    pivots = list(movement_data.get("pivots") or [])
    steps = list(movement_data.get("steps") or [])
    for p in pivots:
        if int(p["start_frame"]) <= frame_index < int(p["end_frame"]):
            rate = p.get("peak_rate_deg_s", p.get("mean_rate_deg_s"))
            rate_txt = f", peak {abs(float(rate)):.0f} deg/s" if rate is not None else ""
            out.append(
                f"Active Pivot: {str(p['direction']).upper()} "
                f"({float(p['net_rotation_deg']):+.1f} deg net turn{rate_txt})"
            )
            break
    window = max(1, round(step_history_seconds * max(fps, 1.0)))
    for st in steps:
        sf = int(st["frame"])
        if sf <= frame_index < sf + window:
            t = float(st.get("time", sf / max(fps, 1.0)))
            mins = int(t // 60)
            conf = float(st.get("confidence", 0.0)) * 100.0
            out.append(
                f"Recent Step: {str(st['side']).upper()} foot landed at "
                f"{mins:02d}:{t - mins * 60:04.1f} "
                f"(travel {float(st.get('travel_px', 0.0)):.0f} px, conf {conf:.0f}%)"
            )
    out.append(f"Clip Movement Totals: {len(pivots)} pivots, {len(steps)} steps detected")
    return out
