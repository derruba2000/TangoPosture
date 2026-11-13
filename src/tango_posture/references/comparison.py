"""Content-verified relinking and manually anchored clip comparison."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..archive import file_sha256
from ..review import ReportRecord, update_report_metadata
from .companions import load_report_movement_data
from .video import probe_import_video


@dataclass(frozen=True)
class RelinkOutcome:
    """Result of relinking a moved local video file to an existing report."""

    success: bool
    exact_match: bool
    video_path: Path
    source_sha256: str
    message: str


def relink_report_video(
    report: ReportRecord,
    new_video_path: Path | str,
) -> RelinkOutcome:
    """Relink a report to a local video file, verifying SHA-256 content identity.

    - If the new file's SHA-256 matches the stored `source_sha256`, existing movement
      overlays, frame timestamps, `[clip_in, clip_out)` bounds, and YouTube clock
      alignments remain valid and attached.
    - If the file content differs (e.g. re-trimmed or re-encoded), updates the video path
      and new SHA-256 but invalidates stale overlays and YouTube clock alignments rather
      than silently misaligning them.
    """
    probe = probe_import_video(Path(new_video_path))
    new_sha = file_sha256(probe.path)
    previous_sha = str(report.source_sha256 or "")
    exact_match = bool(not previous_sha or previous_sha == new_sha)

    updates: dict[str, Any] = {
        "raw_video": str(probe.path.resolve()),
        "source_video": str(probe.path.resolve()),
        "source_sha256": new_sha,
    }
    if not exact_match:
        updates["youtube_alignment"] = None
        updates["movement_analysis_file"] = None
        updates["coach_overlay_file"] = None
        updates["frame_timestamps"] = []
        updates["relinked_content_changed"] = True
        msg = (
            f"Relinked '{probe.path.name}', but file content changed (SHA-256 mismatch); "
            "prior frame overlays and YouTube timestamp alignments were cleared. Re-analyse clip to regenerate."
        )
    else:
        updates["relinked_content_changed"] = False
        msg = f"Relinked '{probe.path.name}' with verified SHA-256 content identity."

    update_report_metadata(report, updates)
    return RelinkOutcome(
        success=True,
        exact_match=exact_match,
        video_path=probe.path.resolve(),
        source_sha256=new_sha,
        message=msg,
    )


@dataclass(frozen=True)
class SyncedComparisonPoint:
    """Clip-relative positions and boundary states for two anchored clips at a shared offset."""

    offset_from_anchor: float
    left_clip_time: float
    right_clip_time: float
    left_source_time: float
    right_source_time: float
    left_at_boundary: bool
    right_at_boundary: bool


@dataclass(frozen=True)
class ClipComparisonResult:
    """Observable comparison between a teacher/reference clip and a practice clip around manual anchors."""

    left_title: str
    right_title: str
    anchor_left_clip_time: float
    anchor_right_clip_time: float
    comparability_warnings: list[str]
    pivot_comparison: dict[str, Any]
    step_comparison: dict[str, Any]
    coverage_comparison: dict[str, dict[str, Any]]
    summary_lines: list[str]

    def sync_at_offset(
        self,
        offset_seconds: float,
        *,
        left_in: float,
        left_out: float,
        right_in: float,
        right_out: float,
    ) -> SyncedComparisonPoint:
        """Map a shared offset from the manual anchors to each clip's own timeline, holding at clip bounds."""
        left_dur = max(0.0, left_out - left_in)
        right_dur = max(0.0, right_out - right_in)
        raw_left = self.anchor_left_clip_time + offset_seconds
        raw_right = self.anchor_right_clip_time + offset_seconds

        left_clamped = min(left_dur, max(0.0, raw_left))
        right_clamped = min(right_dur, max(0.0, raw_right))
        left_bound = abs(left_clamped - raw_left) > 1e-6
        right_bound = abs(right_clamped - raw_right) > 1e-6

        return SyncedComparisonPoint(
            offset_from_anchor=float(offset_seconds),
            left_clip_time=float(left_clamped),
            right_clip_time=float(right_clamped),
            left_source_time=float(left_in + left_clamped),
            right_source_time=float(right_in + right_clamped),
            left_at_boundary=left_bound,
            right_at_boundary=right_bound,
        )


def compare_clips(
    left: ReportRecord,
    right: ReportRecord,
    *,
    anchor_left_clip_time: float = 0.0,
    anchor_right_clip_time: float = 0.0,
    left_movement: dict[str, Any] | None = None,
    right_movement: dict[str, Any] | None = None,
    window_seconds: float = 2.5,
) -> ClipComparisonResult:
    """Compare two independent clip records around user-selected manual anchors.

    Keeps each clip's reference, movement data, and valid coverage independent,
    surfaces comparability warnings when camera view/setup or coverage differ, and
    computes observable pivot and step differences around the aligned anchors.
    """
    left_in = float(left.clip_in or 0.0)
    left_out = float(
        left.clip_out
        if left.clip_out is not None and left.clip_out > left_in
        else left_in + max(left.duration_seconds, 0.0)
    )
    right_in = float(right.clip_in or 0.0)
    right_out = float(
        right.clip_out
        if right.clip_out is not None and right.clip_out > right_in
        else right_in + max(right.duration_seconds, 0.0)
    )

    left_dur = max(0.0, left_out - left_in)
    right_dur = max(0.0, right_out - right_in)
    a_left = (
        min(left_dur, max(0.0, float(anchor_left_clip_time)))
        if left_dur > 0
        else max(0.0, float(anchor_left_clip_time))
    )
    a_right = (
        min(right_dur, max(0.0, float(anchor_right_clip_time)))
        if right_dur > 0
        else max(0.0, float(anchor_right_clip_time))
    )

    left_source_anchor = left_in + a_left
    right_source_anchor = right_in + a_right

    warnings: list[str] = []
    l_prof = left.capture_profile or {}
    r_prof = right.capture_profile or {}
    if l_prof and r_prof:
        if (
            l_prof.get("side") != r_prof.get("side")
            or l_prof.get("facing") != r_prof.get("facing")
        ):
            warnings.append(
                f"Camera view differs: '{left.title}' uses side={l_prof.get('side')}/facing={l_prof.get('facing')} "
                f"vs '{right.title}' side={r_prof.get('side')}/facing={r_prof.get('facing')}."
            )
        l_ar = float(l_prof.get("aspect_ratio") or 0.0)
        r_ar = float(r_prof.get("aspect_ratio") or 0.0)
        if l_ar > 0 and r_ar > 0 and abs(l_ar - r_ar) > 0.15:
            warnings.append(
                f"Framing aspect ratio differs ({l_prof.get('width')}x{l_prof.get('height')} vs "
                f"{r_prof.get('width')}x{r_prof.get('height')})."
            )
    if left.is_imported_reference != right.is_imported_reference:
        warnings.append(
            "Comparing an external reference video with a local practice recording: camera placement, lens perspective, and floor framing may differ."
        )
    if (left.reference_summary or {}).get("id") != (right.reference_summary or {}).get("id"):
        warnings.append(
            "Clips use different posture references (or uncalibrated movement mode); compare relative timing and movement cues rather than absolute angles."
        )

    l_mov = left_movement or load_report_movement_data(left)
    r_mov = right_movement or load_report_movement_data(right)

    for label, rec in ((left.title, left), (right.title, right)):
        cov = rec.coverage or {}
        for fam in ("pivots", "steps"):
            fam_info = cov.get(fam)
            if isinstance(fam_info, dict) and fam_info.get("status") == "not_enough_evidence":
                warnings.append(
                    f"'{label}' has insufficient {fam} evidence ({fam_info.get('reason') or 'low visibility'})."
                )

    summary_lines: list[str] = []

    # Pivot comparison near anchors
    pivot_cmp: dict[str, Any] = {"supported": False}
    if l_mov is not None and r_mov is not None:
        l_fps = float(l_mov.get("fps") or 30.0)
        r_fps = float(r_mov.get("fps") or 30.0)
        l_pivots = [
            p
            for p in l_mov.get("pivots", [])
            if abs(float(p.get("start_time", p["start_frame"] / l_fps)) - left_source_anchor)
            <= window_seconds
            or (
                float(p.get("start_time", p["start_frame"] / l_fps))
                <= left_source_anchor
                <= float(p.get("end_time", p["end_frame"] / l_fps))
            )
        ]
        r_pivots = [
            p
            for p in r_mov.get("pivots", [])
            if abs(float(p.get("start_time", p["start_frame"] / r_fps)) - right_source_anchor)
            <= window_seconds
            or (
                float(p.get("start_time", p["start_frame"] / r_fps))
                <= right_source_anchor
                <= float(p.get("end_time", p["end_frame"] / r_fps))
            )
        ]
        if l_pivots and r_pivots:
            lp = min(
                l_pivots,
                key=lambda p: abs(
                    float(p.get("start_time", p["start_frame"] / l_fps)) - left_source_anchor
                ),
            )
            rp = min(
                r_pivots,
                key=lambda p: abs(
                    float(p.get("start_time", p["start_frame"] / r_fps)) - right_source_anchor
                ),
            )
            l_start = float(lp.get("start_time", lp["start_frame"] / l_fps))
            r_start = float(rp.get("start_time", rp["start_frame"] / r_fps))
            l_rel_onset = l_start - left_source_anchor
            r_rel_onset = r_start - right_source_anchor
            delta_onset_ms = (r_rel_onset - l_rel_onset) * 1000.0
            same_dir = lp["direction"] == rp["direction"]
            l_turn = abs(float(lp["net_rotation_deg"]))
            r_turn = abs(float(rp["net_rotation_deg"]))
            pivot_cmp = {
                "supported": True,
                "left_direction": lp["direction"],
                "right_direction": rp["direction"],
                "direction_match": same_dir,
                "left_onset_rel_s": round(l_rel_onset, 3),
                "right_onset_rel_s": round(r_rel_onset, 3),
                "delta_onset_ms": round(delta_onset_ms, 1),
                "left_turn_deg": round(l_turn, 1),
                "right_turn_deg": round(r_turn, 1),
                "delta_turn_deg": round(r_turn - l_turn, 1),
            }
            dir_text = (
                f"both {lp['direction']}"
                if same_dir
                else f"direction differs ({lp['direction']} vs {rp['direction']})"
            )
            summary_lines.append(
                f"Pivot near anchor: {dir_text}; practice onset {delta_onset_ms:+.0f} ms relative to anchor; "
                f"turn {l_turn:.0f}° vs {r_turn:.0f}°."
            )
        else:
            pivot_cmp = {
                "supported": False,
                "reason": "No matching pivot detected within ±2.5s of both anchors",
            }
            summary_lines.append(
                "Pivot near anchor: no matching pivot detected near both anchors."
            )

    # Step comparison near anchors
    step_cmp: dict[str, Any] = {"supported": False}
    if l_mov is not None and r_mov is not None:
        l_steps = [
            s
            for s in l_mov.get("steps", [])
            if abs(float(s["time"]) - left_source_anchor) <= window_seconds
        ]
        r_steps = [
            s
            for s in r_mov.get("steps", [])
            if abs(float(s["time"]) - right_source_anchor) <= window_seconds
        ]
        if l_steps and r_steps:
            ls = min(l_steps, key=lambda s: abs(float(s["time"]) - left_source_anchor))
            rs = min(r_steps, key=lambda s: abs(float(s["time"]) - right_source_anchor))
            l_rel_step = float(ls["time"]) - left_source_anchor
            r_rel_step = float(rs["time"]) - right_source_anchor
            delta_step_ms = (r_rel_step - l_rel_step) * 1000.0
            l_travel = float(ls.get("travel_px", 0.0))
            r_travel = float(rs.get("travel_px", 0.0))
            step_cmp = {
                "supported": True,
                "left_foot": ls["side"],
                "right_foot": rs["side"],
                "foot_match": ls["side"] == rs["side"],
                "delta_placement_ms": round(delta_step_ms, 1),
                "left_travel_px": round(l_travel, 1),
                "right_travel_px": round(r_travel, 1),
            }
            summary_lines.append(
                f"Step near anchor: {ls['side']} foot vs {rs['side']} foot; "
                f"placement {delta_step_ms:+.0f} ms relative to anchor."
            )
        else:
            step_cmp = {
                "supported": False,
                "reason": "No matching step placement detected within ±2.5s of both anchors",
            }
            summary_lines.append(
                "Step near anchor: no matching step placement detected near both anchors."
            )

    coverage_cmp = {
        "left": left.coverage or {},
        "right": right.coverage or {},
    }

    return ClipComparisonResult(
        left_title=left.title,
        right_title=right.title,
        anchor_left_clip_time=a_left,
        anchor_right_clip_time=a_right,
        comparability_warnings=warnings,
        pivot_comparison=pivot_cmp,
        step_comparison=step_cmp,
        coverage_comparison=coverage_cmp,
        summary_lines=summary_lines,
    )
