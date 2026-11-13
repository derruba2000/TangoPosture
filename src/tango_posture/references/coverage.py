"""Duration-based evidence coverage for imported recordings."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

from ..config import Config
from .constants import MIN_EVIDENCE_COVERAGE_RATIO, MIN_EVIDENCE_DURATION_SECONDS
from .soundtrack import SoundtrackAnalysisSummary


@dataclass(frozen=True)
class FamilyCoverage:
    """Valid coverage and evidence outcome for one analysis family."""

    family: str
    valid_duration: float
    total_duration: float
    valid_frames: int
    total_frames: int
    event_count: int
    status: str  # "detected" | "no_events_detected" | "not_enough_evidence" | "disabled" | "unavailable"
    reason: str = ""

    @property
    def coverage_ratio(self) -> float:
        if self.total_duration <= 0.0:
            return 0.0
        return min(1.0, max(0.0, self.valid_duration / self.total_duration))

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "valid_duration": round(float(self.valid_duration), 3),
            "total_duration": round(float(self.total_duration), 3),
            "coverage_ratio": round(float(self.coverage_ratio), 4),
            "valid_frames": int(self.valid_frames),
            "total_frames": int(self.total_frames),
            "event_count": int(self.event_count),
            "status": self.status,
            "reason": self.reason,
        }


def _compute_family_coverage(
    *,
    clip_duration: float,
    total_frames: int,
    fps: float,
    frame_log: list[dict[str, Any]],
    movement_data: dict[str, Any],
    soundtrack: SoundtrackAnalysisSummary,
    cfg: Config,
) -> dict[str, FamilyCoverage]:
    """Compute separate valid coverage and evidence status for posture, pivots, steps, and soundtrack."""
    rate = fps if math.isfinite(fps) and fps > 0 else 30.0

    # 1. Posture coverage
    valid_posture_times = [
        float(row["timestamp"])
        for row in frame_log
        if not row.get("in_calibration_window", False) and not row.get("quality_reason")
    ]
    valid_posture_dur = sum(
        b - a
        for a, b in itertools.pairwise(valid_posture_times)
        if 0.0 < b - a <= cfg.max_gap
    )
    if len(valid_posture_times) == 1 and clip_duration > 0:
        valid_posture_dur = min(clip_duration, 1.0 / rate)
    posture_alerts = sum(
        1
        for row in frame_log
        if not row.get("in_calibration_window", False)
        and (
            row.get("ankle_high_alert")
            or row.get("axis_curve_alert")
            or (
                row.get("head_angle") is not None
                and float(row["head_angle"]) < cfg.head_down_angle
            )
            or (
                row.get("shoulder_elev_pct") is not None
                and float(row["shoulder_elev_pct"]) >= cfg.shoulder_raise_threshold
            )
            or (
                row.get("embrace_type") == "CLOSED"
                and row.get("knee_bend_deg") is not None
                and float(row["knee_bend_deg"]) < cfg.closed_embrace_min_knee_bend
            )
        )
    )
    scored_dur = max(0.0, float(clip_duration))
    if (
        valid_posture_dur < MIN_EVIDENCE_DURATION_SECONDS
        or (scored_dur > 0 and valid_posture_dur / scored_dur < MIN_EVIDENCE_COVERAGE_RATIO)
    ):
        reasons = [
            str(row.get("quality_reason") or "")
            for row in frame_log
            if row.get("quality_reason")
        ]
        top_reason = (
            max(set(reasons), key=reasons.count)
            if reasons
            else "Insufficient valid upper-body frames"
        )
        posture_status = "not_enough_evidence"
    elif posture_alerts > 0:
        top_reason = ""
        posture_status = "detected"
    else:
        top_reason = "Sufficient valid coverage; no posture alerts triggered"
        posture_status = "no_events_detected"

    posture_cov = FamilyCoverage(
        family="posture",
        valid_duration=valid_posture_dur,
        total_duration=scored_dur,
        valid_frames=len(valid_posture_times),
        total_frames=total_frames,
        event_count=posture_alerts,
        status=posture_status,
        reason=top_reason,
    )

    # 2. Pivot coverage (from movement_data["coverage"]["pivot_valid_frames"])
    mov_cov = movement_data.get("coverage") or {}
    pivot_valid_frames = int(mov_cov.get("pivot_valid_frames", 0))
    valid_pivot_dur = min(clip_duration, pivot_valid_frames / rate)
    pivots_list = movement_data.get("pivots") or []
    pivot_count = len(pivots_list)
    if (
        valid_pivot_dur < MIN_EVIDENCE_DURATION_SECONDS
        or (clip_duration > 0 and valid_pivot_dur / clip_duration < MIN_EVIDENCE_COVERAGE_RATIO)
    ):
        pivot_status = "not_enough_evidence"
        pivot_reason = "Pelvis and support-foot landmarks were not reliably visible long enough to evaluate pivots"
    elif pivot_count > 0:
        pivot_status = "detected"
        pivot_reason = ""
    else:
        pivot_status = "no_events_detected"
        pivot_reason = "Sufficient hip/foot coverage; no pivot rotations exceeded the detection threshold"

    pivot_cov = FamilyCoverage(
        family="pivots",
        valid_duration=valid_pivot_dur,
        total_duration=clip_duration,
        valid_frames=pivot_valid_frames,
        total_frames=total_frames,
        event_count=pivot_count,
        status=pivot_status,
        reason=pivot_reason,
    )

    # 3. Step coverage (lower-body / ankle, heel & foot_index visibility)
    left_foot_valid = int(mov_cov.get("left_foot_valid_frames", 0))
    right_foot_valid = int(mov_cov.get("right_foot_valid_frames", 0))
    step_valid_frames = max(left_foot_valid, right_foot_valid)
    valid_step_dur = min(clip_duration, step_valid_frames / rate)
    steps_list = movement_data.get("steps") or []
    step_count = len(steps_list)
    if (
        valid_step_dur < MIN_EVIDENCE_DURATION_SECONDS
        or (clip_duration > 0 and valid_step_dur / clip_duration < MIN_EVIDENCE_COVERAGE_RATIO)
    ):
        step_status = "not_enough_evidence"
        step_reason = "Feet, heels, and ankles were occluded, cropped, or below confidence threshold"
    elif step_count > 0:
        step_status = "detected"
        step_reason = ""
    else:
        step_status = "no_events_detected"
        step_reason = "Sufficient lower-body coverage; no step placements detected"

    step_cov = FamilyCoverage(
        family="steps",
        valid_duration=valid_step_dur,
        total_duration=clip_duration,
        valid_frames=step_valid_frames,
        total_frames=total_frames,
        event_count=step_count,
        status=step_status,
        reason=step_reason,
    )

    # 4. Soundtrack coverage
    soundtrack_cov = FamilyCoverage(
        family="soundtrack",
        valid_duration=soundtrack.beat_active_duration_seconds if soundtrack.available else 0.0,
        total_duration=clip_duration,
        valid_frames=sum(1 for st in soundtrack.frame_states.values() if st.get("playing")),
        total_frames=total_frames,
        event_count=soundtrack.counted_beats,
        status=soundtrack.status,
        reason=soundtrack.reason,
    )

    return {
        "posture": posture_cov,
        "pivots": pivot_cov,
        "steps": step_cov,
        "soundtrack": soundtrack_cov,
    }
