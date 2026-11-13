"""Conservative interval accounting and human-readable Markdown session reports."""

import itertools
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from .config import Config
from .domain import METRICS, UNITS, Calibration, SessionSummary
from .review import write_metadata


def _fmt_time(seconds: float) -> str:
    mins = int(seconds // 60)
    secs = seconds - mins * 60
    return f"{mins:02d}:{secs:04.1f} ({seconds:.1f}s)"


def _contiguous_spans(
    items: list[dict], predicate, max_gap: float = 0.5
) -> list[tuple[float, float, list[dict]]]:
    spans: list[tuple[float, float, list[dict]]] = []
    current: list[dict] = []
    for item in items:
        if predicate(item):
            if current and item["timestamp"] - current[-1]["timestamp"] > max_gap:
                spans.append((current[0]["timestamp"], current[-1]["timestamp"], current))
                current = []
            current.append(item)
        elif current:
            spans.append((current[0]["timestamp"], current[-1]["timestamp"], current))
            current = []
    if current:
        spans.append((current[0]["timestamp"], current[-1]["timestamp"], current))
    return spans


def summarize(
    rows: list[dict],
    events: list[dict],
    spans: list[tuple[float, float]],
    pause_duration: float,
    cfg: Config,
    ranges: dict[str, tuple[float, float]] | None = None,
) -> SessionSummary:
    active = sum(end - start for start, end in spans)
    result = {}
    for m in (ranges if ranges is not None else METRICS):
        observations = [r for r in rows if r["metric"] == m]
        valid = above = confirmed = 0.0
        intervals = []
        for start, end in spans:
            segment = [r for r in observations if start <= r["timestamp"] <= end]
            for a, b in itertools.pairwise(segment):
                dt = b["timestamp"] - a["timestamp"]
                if 0 < dt <= cfg.max_gap and a["delta"] is not None and b["delta"] is not None:
                    valid += dt
                    intervals.append((a["timestamp"], b["timestamp"]))
                    magnitude = a["delta"] if ranges is None and m == "head_forward" else abs(a["delta"])
                    if magnitude > (getattr(cfg, m).entry if ranges is None else 0):
                        above += dt
        own_events = [e for e in events if e["metric"] == m]
        lengths = []
        for event in own_events:
            end = event["end"] if event["end"] is not None else event["detection"]
            length = sum(max(0, min(b, end) - max(a, event["onset"])) for a, b in intervals)
            lengths.append(length)
        confirmed = sum(lengths)
        deltas = [r["delta"] for r in observations if r["delta"] is not None]
        result[m] = {
            "valid_duration": valid,
            "unknown_duration": max(0, active - valid),
            "valid_coverage": valid / active if active else None,
            "time_above_entry": above,
            "confirmed_event_duration": confirmed,
            "event_count": len(own_events),
            "longest_event": max(lengths, default=0),
            "median_delta": median(deltas) if deltas else None,
            "peak_absolute_delta": max(map(abs, deltas)) if deltas else None,
            "peak_positive_delta": max(0, max(deltas)) if deltas and m == "head_forward" else None,
            "above_entry_percent_of_valid": 100 * above / valid if valid else None,
            "confirmed_percent_of_valid": 100 * confirmed / valid if valid else None,
            "percentage_denominator_seconds": valid,
        }
    return SessionSummary(active, pause_duration, result)


def summary_dict(*args, **kwargs) -> dict:
    return asdict(summarize(*args, **kwargs))


def _format_video_ref(path: Path | None, *, omit_absolute_path: bool = False) -> str:
    if path is None:
        return "`None (not recorded)`"
    if omit_absolute_path:
        return f"`{path.name}`"
    resolved = path.resolve()
    return f"[`{path}`](file://{resolved})"


def write_markdown_report(
    report_dir: Path,
    cfg: Config,
    source: str,
    status: str,
    frame_log: list[dict[str, Any]],
    performance: dict[str, Any],
    reference: Calibration | None = None,
    session_id: str | None = None,
    session_summary: dict[str, Any] | None = None,
    session_events: list[dict[str, Any]] | None = None,
    recorded_video: Path | None = None,
    recorded_overlay: Path | None = None,
    import_context: dict[str, Any] | None = None,
    movement_data: dict[str, Any] | None = None,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    now_utc = datetime.now(UTC)
    stamp = now_utc.strftime("%Y%m%d_%H%M%S")
    suffix = f"_{session_id[:8]}" if session_id else ""
    report_path = report_dir / f"report_{stamp}{suffix}.md"
    counter = 1
    while report_path.exists():
        report_path = report_dir / f"report_{stamp}{suffix}_{counter:02d}.md"
        counter += 1
    latest_path = report_dir / "latest_report.md"
    is_imported = bool(import_context and import_context.get("is_imported_reference"))

    total_frames = len(frame_log)
    start_t = frame_log[0]["timestamp"] if frame_log else 0.0
    end_t = frame_log[-1]["timestamp"] if frame_log else 0.0
    total_duration = max(0.0, end_t - start_t)

    # Role analysis (Leader vs Follower)
    leader_spans = _contiguous_spans(frame_log, lambda f: f["role"] == "LEADER", cfg.max_gap)
    follower_spans = _contiguous_spans(frame_log, lambda f: f["role"] == "FOLLOWER", cfg.max_gap)
    leader_frames = sum(1 for f in frame_log if f["role"] == "LEADER")
    follower_frames = sum(1 for f in frame_log if f["role"] == "FOLLOWER")
    if leader_frames > follower_frames and leader_frames > 0:
        dominant_role = "LEADER (left arm raised, right arm horizontal)"
    elif follower_frames > leader_frames and follower_frames > 0:
        dominant_role = "FOLLOWER (right arm raised, left arm horizontal)"
    elif leader_frames > 0 and leader_frames == follower_frames:
        dominant_role = "BOTH LEADER & FOLLOWER (equal frames)"
    else:
        dominant_role = "NONE DETECTED (arms neither in Leader nor Follower embrace)"

    # Head horizontal alignment analysis
    head_angles = [f["head_angle"] for f in frame_log if f["head_angle"] is not None]
    down_spans = _contiguous_spans(
        frame_log,
        lambda f: f["head_angle"] is not None and f["head_angle"] < cfg.head_down_angle,
        cfg.max_gap,
    )
    confirmed_head_down = [
        (s, e, items) for s, e, items in down_spans if (e - s) + 1e-9 >= cfg.head_down_duration
    ]
    transient_head_down = [
        (s, e, items)
        for s, e, items in down_spans
        if 0.2 <= (e - s) < cfg.head_down_duration
    ]
    total_head_down_sec = sum(e - s for s, e, _ in down_spans)

    # Shoulder tension (raised / shrugged shoulders) analysis
    shoulder_elevs = [
        float(f["shoulder_elev_pct"])
        for f in frame_log
        if f.get("shoulder_elev_pct") is not None
    ]
    shoulder_spans = _contiguous_spans(
        frame_log,
        lambda f: (
            f.get("shoulder_elev_pct") is not None
            and float(f["shoulder_elev_pct"]) >= cfg.shoulder_raise_threshold
        ),
        cfg.max_gap,
    )
    confirmed_shoulder_tension = [
        (s, e, items)
        for s, e, items in shoulder_spans
        if (e - s) + 1e-9 >= cfg.shoulder_tension_duration
    ]
    total_shoulder_tension_sec = sum(e - s for s, e, _ in shoulder_spans)

    # Knee bend analysis
    knee_spans = _contiguous_spans(
        frame_log,
        lambda f: (
            f.get("knee_bend_deg") is not None
            and float(f["knee_bend_deg"]) < cfg.closed_embrace_min_knee_bend
        ),
        cfg.max_gap,
    )
    confirmed_knee_straight = [
        (s, e, items)
        for s, e, items in knee_spans
        if (e - s) + 1e-9 >= cfg.knee_straight_duration
    ]
    closed_frames = sum(1 for f in frame_log if f.get("embrace_type") == "CLOSED")
    open_frames = sum(1 for f in frame_log if f.get("embrace_type") == "OPEN")

    # Slow-tango high ankle lift analysis
    confirmed_ankle_high = _contiguous_spans(
        frame_log,
        lambda f: bool(f.get("ankle_high_alert")),
        cfg.max_gap,
    )
    max_consec_ankle_steps = max(
        (int(f.get("consecutive_high_ankle_steps") or 0) for f in frame_log),
        default=0,
    )

    # Ankle-hip-back-of-head axis curvature ('bump' past axis line) analysis
    axis_ratios = [
        float(f["axis_curve_ratio"])
        for f in frame_log
        if f.get("axis_curve_ratio") is not None
    ]
    axis_entry_spans = _contiguous_spans(
        frame_log,
        lambda f: (
            f.get("axis_curve_ratio") is not None
            and float(f["axis_curve_ratio"]) > cfg.axis_curve_entry
        ),
        cfg.max_gap,
    )
    total_axis_above_entry_sec = sum(e - s for s, e, _ in axis_entry_spans)
    confirmed_axis_curve = _contiguous_spans(
        frame_log,
        lambda f: bool(f.get("axis_curve_alert")) or f.get("axis_curve_state") == "DEVIATION",
        cfg.max_gap,
    )
    if not confirmed_axis_curve:
        confirmed_axis_curve = [
            (s, e, items)
            for s, e, items in axis_entry_spans
            if (e - s) + 1e-9 >= cfg.axis_curve_duration
        ]

    # Upper-body geometry (ear-shoulder-hip) across frames where those joints were usable
    upper_metrics: dict[str, list[float]] = {m: [] for m in METRICS}
    for f in frame_log:
        ub = f.get("upper_body")
        if ub:
            for m in METRICS:
                if ub.get(m) is not None:
                    upper_metrics[m].append(float(ub[m]))

    # Framing / quality abstention intervals
    quality_spans = _contiguous_spans(
        frame_log, lambda f: bool(f.get("quality_reason")), cfg.max_gap
    )

    fps = performance.get("processed_fps")
    fps_str = f"{fps:.1f} FPS" if isinstance(fps, (int, float)) else "N/A"
    med_ms = performance.get("capture_to_overlay_ms_median")
    lat_str = f"{med_ms:.1f} ms" if isinstance(med_ms, (int, float)) else "N/A"
    phrases_seen = [int(f["phrase"]) for f in frame_log if f.get("phrase") is not None]
    bpms_seen = [float(f["bpm"]) for f in frame_log if f.get("bpm") is not None]
    phrase_summary = (
        f"`{max(phrases_seen)} phrases` (median `{median(bpms_seen):.1f} BPM`, 8 beats/phrase)"
        if phrases_seen and bpms_seen
        else f"`{cfg.tango_bpm:.1f} BPM` (8 beats/phrase)"
    )

    lines = [
        "# Tango Posture & Body Alignment Report",
        "",
        "## 1. What This Report Is & Its Purpose",
        "",
        "- **What this report is**: An automated post-run body alignment, embrace-role, musical 8-beat phrase, and movement performance report generated locally by **Tango Posture Coach** from pose-landmark observations.",
        "- **Purpose of this report**:",
        "  1. **Body Alignment Analysis**: Quantify head horizontal pitch (ear-to-nose angle relative to horizontal), head-forward offset (ear relative to shoulder/torso), head segment inclination, and trunk vertical inclination.",
        "  2. **Leader / Follower Embrace Identification**: Detect whether the dancer adopts the **Leader** embrace (left arm raised, right arm horizontal around the follower) or **Follower** embrace (right arm raised, left arm horizontal around the leader's shoulder) and when each role occurs.",
        "  3. **Tango 8-Beat Phrase Tracking**: Track the 8-beat Argentine tango musical phrase (`Beat 1..8 -> 1..8`) and map alignment events to both time and musical phrase/beat.",
        "  4. **Body Performance & Issue Pinpointing (Where in Time)**: Identify every time interval where alignment deviated beyond configured thresholds (such as the head pointing down for longer than the `.env` threshold) or where camera framing obstructed full-body tracking, with exact timestamps so the dancer and teacher can inspect those moments.",
        "  5. **Scope & Disclaimer**: This is a geometric movement observation aid, not a medical assessment or an authority on artistic tango quality.",
        "",
        "---",
        "",
        "## 2. Run, Runtime Performance & Role Summary",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Generated (UTC) | `{now_utc.isoformat()}` |",
        f"| Source | `{source}` |",
        f"| Saved Raw Video | {_format_video_ref(recorded_video, omit_absolute_path=is_imported)} |",
        f"| Saved Annotated Overlay Video | {_format_video_ref(recorded_overlay, omit_absolute_path=is_imported)} |",
        f"| Run / Session Status | `{status}` |",
        f"| Session ID | `{session_id or 'uncalibrated-observation-run'}` |",
        f"| Observed Timeline | `{_fmt_time(start_t)}` to `{_fmt_time(end_t)}` (`{total_duration:.2f}s`, `{total_frames}` frames) |",
        f"| Selected Side / Facing | `side={cfg.side}`, `facing={cfg.facing}` |",
        f"| Head-Down Alert Config (`.env`) | Angle `< {cfg.head_down_angle:+.1f} deg` for `>= {cfg.head_down_duration:.1f}s` (neutral offset `{cfg.head_neutral_offset:+.1f} deg`) |",
        f"| Shoulder Tension Config (`.env`) | Elevation `>= +{cfg.shoulder_raise_threshold:.1f}%` for `>= {cfg.shoulder_tension_duration:.1f}s` |",
        f"| Axis Curvature Config (`axis_curve`) | Entry `>{cfg.axis_curve_entry * 100.0:+.1f}%` for `>= {cfg.axis_curve_duration:.1f}s`, Clear `<{cfg.axis_curve_clear * 100.0:+.1f}%` for `>= {cfg.axis_curve_clear_duration:.2f}s` |",
        f"| Closed-Embrace Knee Bend Config (`.env`) | Min knee bend `>= {cfg.closed_embrace_min_knee_bend:.1f} deg` (alert after `{cfg.knee_straight_duration:.1f}s`) |",
        f"| Embrace Type Distribution | `CLOSED`: `{closed_frames}` frames, `OPEN`: `{open_frames}` frames (hysteresis `{cfg.embrace_hysteresis_seconds:.2f}s`) |",
        f"| Slow-Tango Low-Feet Config (`.env`) | Tempo `<={cfg.slow_tango_max_bpm:.0f} BPM`, max ankle lift `<={cfg.ankle_high_lift_ratio * 100.0:.1f}%`, alert after `{cfg.max_consecutive_high_ankle_steps}` consecutive high steps (peak streak `{max_consec_ankle_steps}`) |",
        f"| Role Hysteresis Config (`.env`) | Assign after `{cfg.role_assign_ms:.0f} ms`, hold for `{cfg.role_hold_ms:.0f} ms` before returning to `NONE` |",
        f"| Musical 8-Beat Phrase Tracking | {phrase_summary} |",
        f"| Runtime Throughput & Latency | `{fps_str}` (median frame latency `{lat_str}`, dropped frames `{performance.get('dropped_frames', 0)}`) |",
        f"| **Dominant Embrace Role** | **{dominant_role}** |",
        "",
        "### Saved Video & Overlay Files (For Consultation & Analysis)",
        "",
        f"- **Raw Practice Video**: {_format_video_ref(recorded_video, omit_absolute_path=is_imported)}",
        f"- **Annotated Overlay Video**: {_format_video_ref(recorded_overlay, omit_absolute_path=is_imported)}",
        "",
    ]

    if is_imported and import_context:
        yt_info = import_context.get("youtube_link")
        yt_str = "None"
        if isinstance(yt_info, dict) and yt_info.get("canonical_url"):
            anchor_val = yt_info.get("anchor_seconds")
            anchor_suffix = f" (saved anchor `{anchor_val:.1f}s`)" if isinstance(anchor_val, (int, float)) else ""
            yt_str = f"[{yt_info['canonical_url']}]({yt_info['canonical_url']}){anchor_suffix}"
        facebook_info = import_context.get("facebook_reel")
        facebook_str = (
            f"[{facebook_info['canonical_url']}]({facebook_info['canonical_url']})"
            if isinstance(facebook_info, dict) and facebook_info.get("canonical_url")
            else "None"
        )
        readiness_info = import_context.get("readiness") or {}
        mode_lbl = (
            "Movement review (uncalibrated)"
            if import_context.get("analysis_mode") == "movement"
            else f"Reference-relative posture review ({readiness_info.get('summary') or 'calibrated'})"
        )
        lines.extend(
            [
                "### Imported Reference Video Provenance & Coverage",
                "",
                "| Field | Value |",
                "| --- | --- |",
                f"| Clip Title | **{import_context.get('title') or (recorded_video.stem if recorded_video else 'Reference clip')}** |",
                f"| Source Filename | `{import_context.get('source_filename') or (recorded_video.name if recorded_video else 'unknown')}` |",
                f"| Selected Source Range `[in, out)` | `{_fmt_time(float(import_context.get('clip_in', 0.0)))}` to `{_fmt_time(float(import_context.get('clip_out', total_duration)))}` (`{float(import_context.get('clip_duration', total_duration)):.2f}s`) |",
                f"| Analysis Mode | {mode_lbl} |",
                f"| YouTube Source Link | {yt_str} |",
                f"| Facebook Reel Source | {facebook_str} |",
                f"| Detected Pivots / Steps | `{import_context.get('pivot_count', 0)}` pivots, `{import_context.get('step_count', 0)}` steps |",
                "",
            ]
        )
        cov_map = import_context.get("coverage")
        if isinstance(cov_map, dict) and cov_map:
            lines.extend(
                [
                    "#### Analysis Family Valid Coverage & Evidence Status",
                    "",
                    "| Analysis Family | Valid Duration | Coverage | Events | Outcome Status | Detail |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            status_labels = {
                "detected": "Events detected",
                "no_events_detected": "No events detected",
                "not_enough_evidence": "Not enough evidence",
                "disabled": "Disabled",
                "unavailable": "Unavailable",
            }
            for fam_key in ("posture", "pivots", "steps", "soundtrack"):
                fc = cov_map.get(fam_key)
                if not isinstance(fc, dict):
                    continue
                vd = float(fc.get("valid_duration", 0.0))
                td = float(fc.get("total_duration", 0.0))
                ratio_pct = float(fc.get("coverage_ratio", 0.0)) * 100.0
                ev_cnt = int(fc.get("event_count", 0))
                st_raw = str(fc.get("status", ""))
                st_lbl = status_labels.get(st_raw, st_raw)
                detail = str(fc.get("reason") or "Sufficient valid observations")
                lines.append(
                    f"| `{fam_key}` | `{vd:.2f}s / {td:.2f}s` | `{ratio_pct:.1f}%` | `{ev_cnt}` | **{st_lbl}** | {detail} |"
                )
            lines.append("")

        st_meta = import_context.get("soundtrack")
        if isinstance(st_meta, dict) and st_meta.get("enabled"):
            bpm_val = st_meta.get("estimated_bpm")
            bpm_txt = f"`{float(bpm_val):.1f} BPM`" if isinstance(bpm_val, (int, float)) else "`N/A`"
            phrase_txt = (
                f"`{st_meta.get('phrase_count', 0)} phrases` (supported)"
                if st_meta.get("phrase_supported")
                else "Withheld (insufficient phrase evidence)"
            )
            lines.extend(
                [
                    "#### Video Soundtrack Beat & Phrase Summary",
                    "",
                    f"- **Timing Source**: `{st_meta.get('source', 'Video soundtrack')}`",
                    f"- **Status**: `{st_meta.get('status', 'unavailable')}` ({st_meta.get('reason') or 'active beat tracking'})",
                    f"- **Estimated Tempo & Counted Beats**: {bpm_txt}, `{st_meta.get('counted_beats', 0)}` beat transitions over `{float(st_meta.get('beat_active_duration_seconds', 0.0)):.2f}s`",
                    f"- **8-Beat Phrase Estimate**: {phrase_txt}",
                    "",
                ]
            )

    lines.extend(
        [
            "### Detected Leader / Follower Embrace Intervals",
            "",
        ]
    )

    if not leader_spans and not follower_spans:
        lines.append(
            "- No continuous `LEADER` or `FOLLOWER` arm embrace intervals were detected during this run."
        )
    else:
        for s, e, items in leader_spans:
            lines.append(
                f"- **LEADER** (`{_fmt_time(s)}` -> `{_fmt_time(e)}`, duration `{e - s:.1f}s`, `{len(items)}` frames): Left arm raised, right arm horizontal."
            )
        for s, e, items in follower_spans:
            lines.append(
                f"- **FOLLOWER** (`{_fmt_time(s)}` -> `{_fmt_time(e)}`, duration `{e - s:.1f}s`, `{len(items)}` frames): Right arm raised, left arm horizontal."
            )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 3. Body Alignment Metrics",
            "",
            "### 3.1 Head Horizontal Alignment (Ear-to-Nose Pitch)",
            "",
        ]
    )
    if head_angles:
        lines.extend(
            [
                "| Metric | Value | Interpretation |",
                "| --- | --- | --- |",
                f"| Frames with Visible Ear & Nose | `{len(head_angles)} / {total_frames}` | Head horizontal angle coverage |",
                f"| Median Head Horizontal Angle | `{median(head_angles):+.1f} deg` | `0 deg` = nose level with ear; negative = pointing down |",
                f"| Minimum (Most Downward) Angle | `{min(head_angles):+.1f} deg` | Lowest downward pitch observed |",
                f"| Maximum (Most Upward) Angle | `{max(head_angles):+.1f} deg` | Highest pitch observed |",
                f"| Total Time Pointing Down (`< {cfg.head_down_angle:+.1f} deg`) | `{total_head_down_sec:.2f}s` | Cumulative time below downward angle threshold |",
                f"| **Confirmed Bad Head Alignment Alerts (`>= {cfg.head_down_duration:.1f}s`)** | **`{len(confirmed_head_down)}`** | Triggered red on-screen `BAD HEAD ALIGNMENT` alert |",
            ]
        )
    else:
        lines.append("- Head horizontal angle could not be computed (ear/nose not visible).")

    lines.extend(
        [
            "",
            "### 3.2 Shoulder Tension (Raised / Shrugged Shoulders)",
            "",
        ]
    )
    if shoulder_elevs:
        lines.extend(
            [
                "| Metric | Value | Interpretation |",
                "| --- | --- | --- |",
                f"| Frames with Visible Ear & Shoulder | `{len(shoulder_elevs)} / {total_frames}` | Shoulder elevation coverage |",
                f"| Median Shoulder Elevation | `{median(shoulder_elevs):+.1f}%` | `0%` = relaxed neutral; positive = shoulders raised toward ears |",
                f"| Peak Shoulder Elevation | `{max(shoulder_elevs):+.1f}%` | Highest shoulder shrug / tension observed |",
                f"| Total Time Raised (`>= +{cfg.shoulder_raise_threshold:.1f}%`) | `{total_shoulder_tension_sec:.2f}s` | Cumulative time with elevated shoulders |",
                f"| **Confirmed Shoulder Tension Alerts (`>= {cfg.shoulder_tension_duration:.1f}s`)** | **`{len(confirmed_shoulder_tension)}`** | Triggered red on-screen `LOWER YOUR SHOULDERS` alert |",
            ]
        )
    else:
        lines.append("- Shoulder tension could not be computed (ear/shoulder not visible).")

    lines.extend(
        [
            "",
            "### 3.2b Full-Body Axis Curvature (Ankle–Hip–Back-of-Head Line)",
            "",
        ]
    )
    if axis_ratios:
        lines.extend(
            [
                "| Metric | Value | Interpretation |",
                "| --- | --- | --- |",
                f"| Frames with Visible Ankle, Hip & Head | `{len(axis_ratios)} / {total_frames}` | Full-body axis curvature coverage |",
                f"| Median Hip / Pelvis Offset | `{median(axis_ratios) * 100.0:+.1f}%` | `0.0%` = hip on reference ankle–back-of-head line; `+` = hip/bump protruding behind line |",
                f"| Peak Posterior Curvature | `{max(axis_ratios) * 100.0:+.1f}%` | Maximum hip protrusion behind ankle–back-of-head line |",
                f"| Total Time Above Entry (`>{cfg.axis_curve_entry * 100.0:+.1f}%`) | `{total_axis_above_entry_sec:.2f}s` | Cumulative time exceeding entry threshold |",
                f"| **Confirmed Axis Curvature Alerts (`>= {cfg.axis_curve_duration:.1f}s` w/ hysteresis `<{cfg.axis_curve_clear * 100.0:+.1f}%`)** | **`{len(confirmed_axis_curve)}`** | Triggered red on-screen `HIP OFFSET` alert; separate from the torso axis |",
            ]
        )
    else:
        lines.append("- Axis curvature could not be computed (ankle, hip, or ear not visible).")

    lines.extend(
        [
            "",
            "### 3.3 Upper-Body Geometry (Ear–Shoulder–Hip Alignment)",
            "",
            "| Metric | Unit | Observed Frames | Median | Min | Max | Interpretation |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    descriptions = {
        "head_forward": "Ear forward displacement relative to shoulder & torso length",
        "head_inclination": "Ear-shoulder segment angle relative to trunk",
        "trunk_inclination": "Projected trunk inclination relative to vertical",
    }
    for m in METRICS:
        vals = upper_metrics[m]
        if vals:
            lines.append(
                f"| `{m}` | `{UNITS[m]}` | `{len(vals)} / {total_frames}` | "
                f"`{median(vals):+.3f}` | `{min(vals):+.3f}` | `{max(vals):+.3f}` | {descriptions[m]} |"
            )
        else:
            lines.append(
                f"| `{m}` | `{UNITS[m]}` | `0 / {total_frames}` | `N/A` | `N/A` | `N/A` | "
                f"Requires visible {cfg.side} ear, shoulder, and hip in frame |"
            )

    if session_summary and "metrics" in session_summary:
        lines.extend(
            [
                "",
                f"### 3.3 Calibrated Reference Comparison (`{reference.name if reference else 'Reference'}`)",
                "",
                "| Metric | Valid Duration | Unknown Duration | Median Delta | Peak Abs Delta | Confirmed Events | % of Valid Time in Deviation |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for m, data in session_summary["metrics"].items():
            med_d = "N/A" if data["median_delta"] is None else f"{data['median_delta']:+.3f}"
            peak_d = (
                "N/A"
                if data["peak_absolute_delta"] is None
                else f"{data['peak_absolute_delta']:.3f}"
            )
            pct = (
                "N/A"
                if data["confirmed_percent_of_valid"] is None
                else f"{data['confirmed_percent_of_valid']:.1f}%"
            )
            lines.append(
                f"| `{m}` | `{data['valid_duration']:.2f}s` | `{data['unknown_duration']:.2f}s` | "
                f"`{med_d}` | `{peak_d}` | `{data['event_count']}` | `{pct}` |"
            )

    pivots_list = list((movement_data or {}).get("pivots") or [])
    steps_list = list((movement_data or {}).get("steps") or [])
    if movement_data is not None and (pivots_list or steps_list):
        lines.extend(
            [
                "",
                "### 3.4 Movement Analysis (Pivots & Step Placements Timeline)",
                "",
                f"Detected `{len(pivots_list)}` pivots and `{len(steps_list)}` step placements.",
                "",
            ]
        )
        if pivots_list:
            lines.extend(
                [
                    "#### Pivots",
                    "",
                    "| # | Start Time | End Time | Duration | Direction | Net Rotation | Peak Rate | Quality |",
                    "| --- | --- | --- | --- | --- | --- | --- | --- |",
                ]
            )
            for idx, p in enumerate(pivots_list, 1):
                st = float(p.get("start_time", 0.0))
                en = float(p.get("end_time", st))
                rate = p.get("peak_rate_deg_s", p.get("mean_rate_deg_s"))
                rate_txt = f"{abs(float(rate)):.0f} deg/s" if rate is not None else "N/A"
                qual = p.get("quality")
                qual_txt = f"{float(qual) * 100.0:.0f}%" if qual is not None else "N/A"
                lines.append(
                    f"| {idx} | {_fmt_time(st)} | {_fmt_time(en)} | `{max(0.0, en - st):.2f}s` | "
                    f"`{str(p.get('direction', '')).upper()}` | "
                    f"`{float(p.get('net_rotation_deg', 0.0)):+.1f} deg` | `{rate_txt}` | `{qual_txt}` |"
                )
            lines.append("")
        if steps_list:
            lines.extend(
                [
                    "#### Step Placements",
                    "",
                    "| # | Landing Time | Foot Side | Travel Distance | Confidence |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
            for idx, s in enumerate(steps_list, 1):
                conf = s.get("confidence")
                conf_txt = f"{float(conf) * 100.0:.0f}%" if conf is not None else "N/A"
                lines.append(
                    f"| {idx} | {_fmt_time(float(s.get('time', 0.0)))} | "
                    f"`{str(s.get('side', '')).upper()}` | "
                    f"`{float(s.get('travel_px', 0.0)):.0f} px` | `{conf_txt}` |"
                )
            lines.append("")

    lines.extend(
        [
            "",
            "---",
            "",
            "## 4. What Went Wrong & Where in Time (Issues Timeline)",
            "",
        ]
    )

    has_issues = False
    if confirmed_head_down:
        has_issues = True
        lines.append(
            f"### 4.1 Confirmed Bad Head Alignment (`< {cfg.head_down_angle:+.1f} deg` for `>= {cfg.head_down_duration:.1f}s` — RED ALERT)"
        )
        lines.append("")
        lines.append(
            "| Start Time (Phrase/Beat) | Red Alert Triggered At | End Time | Duration | Worst Angle | Median Angle | What Went Wrong |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for s, e, items in confirmed_head_down:
            angles = [float(x["head_angle"]) for x in items if x["head_angle"] is not None]
            trigger_t = s + cfg.head_down_duration
            p_num, b_num = items[0].get("phrase"), items[0].get("beat")
            beat_tag = f" (P{p_num} B{b_num}/8)" if p_num and b_num else ""
            lines.append(
                f"| `{_fmt_time(s)}{beat_tag}` | `{_fmt_time(trigger_t)}` | `{_fmt_time(e)}` | "
                f"`{e - s:.2f}s` | `{min(angles):+.1f} deg` | `{median(angles):+.1f} deg` | "
                f"**Bad head alignment**: head pointed downward continuously for `{e - s:.1f}s` |"
            )
        lines.append("")

    if confirmed_shoulder_tension:
        has_issues = True
        lines.append(
            f"### 4.2 Confirmed Shoulder Tension (`>= +{cfg.shoulder_raise_threshold:.1f}%` for `>= {cfg.shoulder_tension_duration:.1f}s` — LOWER YOUR SHOULDERS)"
        )
        lines.append("")
        lines.append(
            "| Start Time (Phrase/Beat) | Alert Triggered At | End Time | Duration | Peak Elevation | Median Elevation | What Went Wrong |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for s, e, items in confirmed_shoulder_tension:
            elevs = [
                float(x["shoulder_elev_pct"])
                for x in items
                if x.get("shoulder_elev_pct") is not None
            ]
            trigger_t = s + cfg.shoulder_tension_duration
            p_num, b_num = items[0].get("phrase"), items[0].get("beat")
            beat_tag = f" (P{p_num} B{b_num}/8)" if p_num and b_num else ""
            lines.append(
                f"| `{_fmt_time(s)}{beat_tag}` | `{_fmt_time(trigger_t)}` | `{_fmt_time(e)}` | "
                f"`{e - s:.2f}s` | `{max(elevs):+.1f}%` | `{median(elevs):+.1f}%` | "
                f"**Shoulder tension**: shoulders raised continuously for `{e - s:.1f}s` (lower your shoulders) |"
            )
        lines.append("")

    if confirmed_knee_straight:
        has_issues = True
        lines.append(
            f"### 4.2b Confirmed Unbent Knees in Closed Embrace (`< {cfg.closed_embrace_min_knee_bend:.1f} deg` for `>= {cfg.knee_straight_duration:.1f}s` — BEND YOUR KNEES)"
        )
        lines.append("")
        lines.append(
            "| Start Time (Phrase/Beat) | Alert Triggered At | End Time | Duration | Min Knee Bend | Median Knee Bend | What Went Wrong |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for s, e, items in confirmed_knee_straight:
            bends = [
                float(x["knee_bend_deg"])
                for x in items
                if x.get("knee_bend_deg") is not None
            ]
            trigger_t = s + cfg.knee_straight_duration
            p_num, b_num = items[0].get("phrase"), items[0].get("beat")
            beat_tag = f" (P{p_num} B{b_num}/8)" if p_num and b_num else ""
            lines.append(
                f"| `{_fmt_time(s)}{beat_tag}` | `{_fmt_time(trigger_t)}` | `{_fmt_time(e)}` | "
                f"`{e - s:.2f}s` | `{min(bends):.1f} deg` | `{median(bends):.1f} deg` | "
                f"**Straight knees in closed embrace**: knee bend `< {cfg.closed_embrace_min_knee_bend:.1f} deg` for `{e - s:.1f}s` (bend your knees) |"
            )
        lines.append("")

    if confirmed_axis_curve:
        has_issues = True
        lines.append(
            f"### 4.2c Confirmed Axis Curvature (`>{cfg.axis_curve_entry * 100.0:+.1f}%` for `>= {cfg.axis_curve_duration:.1f}s`, clear `<{cfg.axis_curve_clear * 100.0:+.1f}%` — HIP OFFSET)"
        )
        lines.append("")
        lines.append(
            "| Start Time (Phrase/Beat) | Alert Triggered At | End Time | Duration | Peak Curvature | Median Curvature | What Went Wrong |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for s, e, items in confirmed_axis_curve:
            curves = [
                float(x["axis_curve_ratio"]) * 100.0
                for x in items
                if x.get("axis_curve_ratio") is not None
            ]
            peak_c = max(curves) if curves else cfg.axis_curve_entry * 100.0
            med_c = median(curves) if curves else cfg.axis_curve_entry * 100.0
            p_num, b_num = items[0].get("phrase"), items[0].get("beat")
            beat_tag = f" (P{p_num} B{b_num}/8)" if p_num and b_num else ""
            lines.append(
                f"| `{_fmt_time(s)}{beat_tag}` | `{_fmt_time(s)}` | `{_fmt_time(e)}` | "
                f"`{e - s:.2f}s` | `{peak_c:+.1f}%` | `{med_c:+.1f}%` | "
                f"**Axis curving too much**: hip/pelvis protruded past ankle–back-of-head line for `{e - s:.1f}s` |"
            )
        lines.append("")

    if confirmed_ankle_high:
        has_issues = True
        lines.append(
            f"### 4.2d Confirmed High Ankle Lift in Slow Tango (`>={cfg.ankle_high_lift_ratio * 100.0:.1f}%` for `>={cfg.max_consecutive_high_ankle_steps}` consecutive steps at `<={cfg.slow_tango_max_bpm:.0f} BPM`)"
        )
        lines.append("")
        lines.append(
            "| Start Time (Phrase/Beat) | End Time | Duration | Peak Ankle Lift | Consecutive High Steps | What Went Wrong |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for s, e, items in confirmed_ankle_high:
            lifts = [
                float(x["ankle_lift_ratio"]) * 100.0
                for x in items
                if x.get("ankle_lift_ratio") is not None
            ]
            peak_lift = max(lifts) if lifts else cfg.ankle_high_lift_ratio * 100.0
            max_steps = max(
                (int(x.get("consecutive_high_ankle_steps") or 0) for x in items),
                default=cfg.max_consecutive_high_ankle_steps,
            )
            p_num, b_num = items[0].get("phrase"), items[0].get("beat")
            beat_tag = f" (P{p_num} B{b_num}/8)" if p_num and b_num else ""
            lines.append(
                f"| `{_fmt_time(s)}{beat_tag}` | `{_fmt_time(e)}` | `{e - s:.2f}s` | "
                f"`{peak_lift:.1f}%` | `{max_steps}` | "
                f"**Feet raised too high in slow tango**: `{max_steps}` consecutive steps above `{cfg.ankle_high_lift_ratio * 100.0:.1f}%` leg lift (keep feet close to the floor) |"
            )
        lines.append("")

    if transient_head_down:
        has_issues = True
        lines.append(
            f"### 4.2 Brief Head-Down Dips (`< {cfg.head_down_angle:+.1f} deg` for `< {cfg.head_down_duration:.1f}s`)"
        )
        lines.append("")
        lines.append("| Start Time | End Time | Duration | Worst Angle | Note |")
        lines.append("| --- | --- | --- | --- | --- |")
        for s, e, items in transient_head_down:
            angles = [float(x["head_angle"]) for x in items if x["head_angle"] is not None]
            lines.append(
                f"| `{_fmt_time(s)}` | `{_fmt_time(e)}` | `{e - s:.2f}s` | `{min(angles):+.1f} deg` | "
                f"Transient downward head tilt (recovered before `{cfg.head_down_duration:.1f}s` threshold) |"
            )
        lines.append("")

    if session_events:
        has_issues = True
        lines.append("### 4.3 Confirmed Reference Posture Deviations")
        lines.append("")
        lines.append("| Metric | Onset Time | Confirmed At | End Time | Peak Deviation | End Reason |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for ev in sorted(session_events, key=lambda x: x["onset"]):
            units = session_summary.get("units", UNITS) if session_summary else UNITS
            end_val = ev["end"] if ev["end"] is not None else ev["detection"]
            lines.append(
                f"| `{ev['metric']}` | `{_fmt_time(ev['onset'])}` | `{_fmt_time(ev['detection'])}` | "
                f"`{_fmt_time(end_val)}` | `{ev['peak_deviation']:.3f} {units[ev['metric']]}` | `{ev['end_reason']}` |"
            )
        lines.append("")

    if quality_spans:
        has_issues = True
        lines.append("### 4.4 Camera Framing & Tracking Abstentions")
        lines.append("")
        lines.append("| Start Time | End Time | Duration | Frames | Primary Reason |")
        lines.append("| --- | --- | --- | --- | --- |")
        for s, e, items in quality_spans:
            reasons: dict[str, int] = {}
            for item in items:
                r = str(item.get("quality_reason") or "Unknown")
                reasons[r] = reasons.get(r, 0) + 1
            top_reason = max(reasons, key=lambda k: reasons[k])
            lines.append(
                f"| `{_fmt_time(s)}` | `{_fmt_time(e)}` | `{e - s:.2f}s` | `{len(items)}` | {top_reason} |"
            )
        lines.append("")

    if not has_issues:
        lines.append(
            "- No alignment deviations, head-down alerts, or camera framing issues were detected during this run."
        )

    content = "\n".join(lines).rstrip() + "\n"
    report_path.write_text(content, encoding="utf-8")
    latest_path.write_text(content, encoding="utf-8")
    timestamps = [
        float(f["timestamp"])
        for f in frame_log
        if isinstance(f.get("timestamp"), (int, float))
    ]
    write_metadata(report_path, source, recorded_video, recorded_overlay, timestamps)
    return report_path
