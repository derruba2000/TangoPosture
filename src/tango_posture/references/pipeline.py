"""Coordinate cached pose, movement, soundtrack and report analysis."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from ..archive import file_sha256
from ..calibration.baseline import calibrate, profile
from ..calibration.readiness import check_calibration_readiness
from ..config import Config
from ..domain import (
    METRIC_VERSION,
    Calibration,
    FrameObservation,
    MetricObservation,
    PoseEstimator,
)
from ..geometry import (
    AnkleStepTracker,
    AxisCurveTracker,
    EmbraceTracker,
    PoseSmoother,
    RoleTracker,
    axis_curve_alignment,
    calculate,
    detect_role,
    head_horizontal_alignment,
    knee_bend_alignment,
    pixel,
    shoulder_tension_alignment,
)
from ..metrics import measure
from ..models import verify
from ..movement import analyse_movement_observations
from ..pose.mediapipe_backend import MediaPipeEstimator
from ..reports import write_markdown_report
from ..review import write_metadata
from ..session import Session
from ..storage import Storage
from .coach import _AlertTimers, _build_coach_frame_entry, _movement_note_lines, _serialise_pose
from .companions import save_coach_companion, save_movement_companion
from .constants import MAX_IMPORT_RANGE_SECONDS
from .coverage import _compute_family_coverage
from .downloads import download_facebook_reel, download_youtube_video, imports_directory
from .errors import ImportCancelledError
from .links import YouTubeClockAlignment, parse_facebook_reel_url, parse_youtube_url
from .results import ImportedClipResult
from .soundtrack import analyse_video_soundtrack
from .video import iter_virtual_clip_frames, probe_import_video, validate_clip_range


def analyse_imported_clip(
    *,
    video_path: Path | str,
    cfg: Config,
    report_dir: Path | None = None,
    storage: Storage | None = None,
    selected_in: float = 0.0,
    selected_out: float | None = None,
    title: str = "",
    youtube_url: str | None = None,
    youtube_alignment: YouTubeClockAlignment | None = None,
    facebook_url: str | None = None,
    analysis_mode: str = "movement",
    reference: Calibration | None = None,
    calibration_interval: tuple[float, float] | None = None,
    analyse_soundtrack: bool = False,
    estimator_factory: Callable[[], PoseEstimator] | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    youtube_downloader: Callable[[str, Path], Path] | None = None,
    facebook_downloader: Callable[[str, Path], Path] | None = None,
) -> ImportedClipResult:
    """Import and analyse a selected range `[selected_in, selected_out)` of a local video file.

    When `video_path` is blank and `youtube_url` is valid, the video is first downloaded into
    `imports_directory(cfg.data_dir)` (reusing a cached copy) and analysed; a downloaded video is
    the exact same edit as YouTube, so a confirmed 1:1 clock alignment is stored automatically.

    - Leaves the user's video file untouched on disk.
    - Supports `analysis_mode="movement"` (no calibration required) and
      `analysis_mode="posture"` (using `reference` or `calibration_interval`).
    - Optionally decodes and analyses the video soundtrack (`analyse_soundtrack=True`).
    - Writes a Markdown report, companion `.metadata.json`, and `.movement.v1.json`.
    """
    yt_link = parse_youtube_url(youtube_url) if youtube_url and youtube_url.strip() else None
    facebook_link = (
        parse_facebook_reel_url(facebook_url)
        if facebook_url and facebook_url.strip()
        else None
    )
    if yt_link is not None and facebook_link is not None:
        raise ValueError("Import from only one video URL at a time")
    downloaded_from_youtube = False
    downloaded_from_facebook = False
    if not video_path or not str(video_path).strip():
        if yt_link is None and facebook_link is None:
            raise ValueError("Choose a local video file or enter a YouTube or Facebook Reel URL")
        if yt_link is not None:
            downloaded_path, _ = download_youtube_video(
                yt_link.canonical_url,
                imports_directory(cfg.data_dir),
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                downloader=youtube_downloader,
            )
            downloaded_from_youtube = True
        else:
            assert facebook_link is not None
            downloaded_path, _ = download_facebook_reel(
                facebook_link.canonical_url,
                imports_directory(cfg.data_dir),
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                downloader=facebook_downloader,
            )
            downloaded_from_facebook = True
        video_path = downloaded_path

    probe = probe_import_video(Path(video_path))
    if (
        yt_link is not None
        and not downloaded_from_youtube
        and probe.path.name == f"youtube_{yt_link.video_id}.mp4"
        and probe.path.parent.resolve() == imports_directory(cfg.data_dir).resolve()
    ):
        downloaded_from_youtube = True
    if (
        facebook_link is not None
        and not downloaded_from_facebook
        and probe.path.name == f"facebook_reel_{facebook_link.reel_id}.mp4"
        and probe.path.parent.resolve() == imports_directory(cfg.data_dir).resolve()
    ):
        downloaded_from_facebook = True
    effective_in = float(selected_in)
    if downloaded_from_youtube and yt_link is not None and yt_link.anchor_seconds is not None:
        if effective_in == 0.0 and 0.0 < yt_link.anchor_seconds < probe.duration_seconds:
            effective_in = float(yt_link.anchor_seconds)
        elif (
            effective_in >= probe.duration_seconds
            and abs(effective_in - float(yt_link.anchor_seconds)) < 1e-6
        ):
            effective_in = 0.0
    if selected_out is not None:
        raw_out = float(selected_out)
    elif downloaded_from_youtube or downloaded_from_facebook:
        raw_out = min(probe.duration_seconds, effective_in + MAX_IMPORT_RANGE_SECONDS)
    else:
        raw_out = probe.duration_seconds
    clip_in, clip_out = validate_clip_range(probe, effective_in, raw_out)
    clip_duration = clip_out - clip_in

    model_path = Path(cfg.model_path)
    if estimator_factory is None and model_path.is_file():
        model_hash = verify(model_path)
    elif reference is not None:
        model_hash = reference.model_hash
    elif model_path.is_file():
        model_hash = file_sha256(model_path)
    else:
        model_hash = hashlib.sha256(b"tango-posture-pose-landmarker").hexdigest()

    readiness = check_calibration_readiness(
        analysis_mode=analysis_mode,
        cfg=cfg,
        probe=probe,
        selected_in=clip_in,
        selected_out=clip_out,
        reference=reference,
        calibration_interval=calibration_interval,
        model_hash=model_hash,
    )
    if not readiness.ready:
        raise ValueError(readiness.reason or readiness.summary)

    if progress_callback is not None:
        progress_callback(0, 100, "Computing file identity…")
    source_sha = file_sha256(probe.path)

    validated_alignment: YouTubeClockAlignment | None = None
    if yt_link is not None and downloaded_from_youtube:
        # TangoPosture fetched this exact file from YouTube, so the clocks are 1:1.
        validated_alignment = YouTubeClockAlignment(
            local_source_seconds=clip_in,
            youtube_seconds=clip_in,
            confirmed_same_edit=True,
            source_sha256=source_sha,
            youtube_video_id=yt_link.video_id,
        )
    elif yt_link is not None and youtube_alignment is not None and youtube_alignment.is_valid_for(
        source_sha256=source_sha,
        youtube_video_id=yt_link.video_id,
        clip_in=clip_in,
        clip_out=clip_out,
    ):
        validated_alignment = youtube_alignment

    est = (
        estimator_factory()
        if estimator_factory is not None
        else MediaPipeEstimator(model_path)
    )
    decoded_frames: list[tuple[int, float, float, FrameObservation]] = []
    frame_times: list[tuple[int, float, float]] = []
    expected_frames = max(1, round(clip_duration * max(probe.fps, 1.0)))
    infer_times: list[float] = []

    try:
        for idx, item in enumerate(
            iter_virtual_clip_frames(
                probe.path,
                clip_in,
                clip_out,
                probe=probe,
                cancel_check=cancel_check,
            )
        ):
            t0 = time.perf_counter()
            obs = est.estimate(item.image, item.source_time)
            infer_times.append(time.perf_counter() - t0)
            decoded_frames.append((item.frame_index, item.source_time, item.clip_time, obs))
            frame_times.append((item.frame_index, item.source_time, item.clip_time))
            if progress_callback is not None and (idx % 5 == 0 or idx + 1 >= expected_frames):
                pct = min(75, round(((idx + 1) / max(expected_frames, idx + 1)) * 75))
                progress_callback(pct, 100, f"Analysing video frames ({idx + 1})…")
    finally:
        with suppress(Exception):
            est.close()

    if not decoded_frames:
        raise ValueError("Selected clip range contains no decodable frames")

    if cancel_check is not None and cancel_check():
        raise ImportCancelledError("Reference video analysis was cancelled")

    if progress_callback is not None:
        progress_callback(80, 100, "Analysing soundtrack & movement…")

    soundtrack_summary = analyse_video_soundtrack(
        probe.path,
        clip_in,
        clip_out,
        frame_times,
        cfg,
        enabled=analyse_soundtrack,
        video_first_pts_seconds=probe.first_pts_seconds,
    )

    cap_profile = profile(cfg, probe.width, probe.height, "video")
    active_reference: Calibration | None = reference if analysis_mode == "posture" else None
    cal_window: tuple[float, float] | None = (
        (float(calibration_interval[0]), float(calibration_interval[1]))
        if analysis_mode == "posture" and calibration_interval is not None
        else None
    )

    if analysis_mode == "posture" and active_reference is None and cal_window is not None:
        cal_in, cal_out = cal_window
        cal_samples: list[MetricObservation] = []
        cal_head_angles: list[float] = []
        cal_shoulder_clearances: list[float] = []
        cal_axis_offsets: list[float] = []
        for _, source_time, _, frame_obs in decoded_frames:
            if cal_in - 1e-6 <= source_time <= cal_out + 1e-6:
                cal_samples.append(measure(frame_obs, cfg, None))
                if len(frame_obs.poses) == 1:
                    pose = frame_obs.poses[0]
                    raw_head = head_horizontal_alignment(
                        pose, frame_obs.width, frame_obs.height, neutral_offset=0.0
                    )
                    if raw_head is not None:
                        cal_head_angles.append(raw_head[0])
                    sh_info = shoulder_tension_alignment(
                        pose,
                        frame_obs.width,
                        frame_obs.height,
                        neutral_clearance=cfg.shoulder_neutral_clearance,
                    )
                    if sh_info is not None:
                        cal_shoulder_clearances.append(sh_info[1])
                    ax_info = axis_curve_alignment(
                        pose,
                        frame_obs.width,
                        frame_obs.height,
                        facing=cfg.facing,
                        side=cfg.side,
                        neutral_offset=cfg.axis_neutral_offset,
                    )
                    if ax_info is not None:
                        cal_axis_offsets.append(ax_info[1])
        cal_cfg = cfg.model_copy(
            update={
                "calibration_duration": max(
                    float(cal_out - cal_in), float(cfg.calibration_valid_duration)
                )
            }
        )
        active_reference = calibrate(
            cal_samples,
            cal_in,
            cal_cfg,
            cap_profile,
            model_hash,
            name=f"{title or probe.path.stem} clip reference",
            review={
                "label": "imported-clip-interval",
                "interval": [round(cal_in, 3), round(cal_out, 3)],
            },
        )
        if cal_head_angles:
            active_reference.quality["head_neutral_offset"] = float(np.median(cal_head_angles))
        if cal_shoulder_clearances:
            active_reference.quality["shoulder_neutral_clearance"] = float(
                np.median(cal_shoulder_clearances)
            )
        if cal_axis_offsets:
            active_reference.quality["axis_neutral_offset"] = float(np.median(cal_axis_offsets))

    role_tracker = RoleTracker(assign_ms=cfg.role_assign_ms, hold_ms=cfg.role_hold_ms)
    embrace_tracker = EmbraceTracker(
        closed_threshold=cfg.embrace_closed_threshold,
        switch_sec=cfg.embrace_hysteresis_seconds,
    )
    axis_tracker = AxisCurveTracker(
        entry=cfg.axis_curve_entry,
        clear=cfg.axis_curve_clear,
        entry_duration=cfg.axis_curve_duration,
        clear_duration=cfg.axis_curve_clear_duration,
        smoothing=cfg.smoothing,
        max_gap=cfg.max_gap,
    )
    ankle_tracker = AnkleStepTracker(
        high_lift_ratio=cfg.ankle_high_lift_ratio,
        max_consecutive_steps=cfg.max_consecutive_high_ankle_steps,
        slow_tango_max_bpm=cfg.slow_tango_max_bpm,
        max_gap=cfg.max_gap,
    )

    target_dir = (
        report_dir
        if report_dir is not None
        else Path("reports")
        if cfg.data_dir == "data"
        else Path(cfg.data_dir) / "reports"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = (
        Path(cfg.data_dir) / "tango_posture.db"
        if storage is not None or cfg.data_dir != "data"
        else target_dir / "import_sessions.sqlite3"
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = storage or Storage(db_path)
    session: Session | None = None
    if active_reference is not None:
        store.save_calibration(active_reference)
        scored_start = (
            cal_window[1]
            if cal_window is not None and cal_window[0] <= clip_in + 1e-3
            else clip_in
        )
        session = Session(
            active_reference,
            cfg,
            store,
            scored_start,
            f"import:{probe.path.name}",
            {"model_hash": model_hash, "metric_version": METRIC_VERSION},
        )

    head_smoothed: float | None = None
    head_last_ts: float | None = None
    shoulder_smoothed: float | None = None
    shoulder_last_ts: float | None = None
    knee_smoothed: float | None = None
    knee_last_ts: float | None = None
    relaxed_clearances: list[float] = []
    frame_log: list[dict[str, Any]] = []

    neutral_offset = (
        float(active_reference.quality["head_neutral_offset"])
        if active_reference and "head_neutral_offset" in active_reference.quality
        else cfg.head_neutral_offset
    )
    sh_neutral_base = (
        float(active_reference.quality["shoulder_neutral_clearance"])
        if active_reference and "shoulder_neutral_clearance" in active_reference.quality
        else cfg.shoulder_neutral_clearance
    )
    axis_neutral = (
        float(active_reference.quality["axis_neutral_offset"])
        if active_reference and "axis_neutral_offset" in active_reference.quality
        else cfg.axis_neutral_offset
    )
    view_range = (
        tuple(active_reference.quality["view_range"])
        if active_reference and "view_range" in active_reference.quality
        else None
    )

    pose_smoother = PoseSmoother(smoothing_sec=cfg.overlay_smoothing, max_gap=cfg.max_gap)
    timers = _AlertTimers()
    coach_pending: dict[int, dict[str, Any]] = {}
    coach_frame_meta: dict[int, dict[str, Any]] = {}

    for frame_index, source_time, clip_time, frame_obs in decoded_frames:
        in_cal = bool(
            cal_window is not None and cal_window[0] - 1e-6 <= source_time < cal_window[1] - 1e-6
        )
        from ..calibration.measurements import measure_profile
        from ..calibration.models import profile_from_calibration

        range_profile = profile_from_calibration(active_reference) if active_reference is not None else None
        observation = (
            measure_profile(frame_obs, cfg, range_profile) if range_profile is not None else
            measure(frame_obs, cfg, view_range if not in_cal else None)
        )
        session_rows: list[dict[str, Any]] = []
        if session is not None and not in_cal and source_time >= (session.start or clip_in):
            session_rows = session.update(observation)

        head_angle = None
        shoulder_elev_pct = None
        knee_bend_deg = None
        axis_dev_raw = None
        upper_body = None
        raw_role = "NONE"
        sh_neutral = (
            max(sh_neutral_base, float(np.percentile(relaxed_clearances, 75)))
            if len(relaxed_clearances) >= 15
            else sh_neutral_base
        )

        if len(frame_obs.poses) == 1:
            pose = frame_obs.poses[0]
            raw_role = detect_role(pose)
            metric_tau = max(1e-4, cfg.overlay_smoothing or cfg.smoothing)
            knee_info = knee_bend_alignment(
                pose, frame_obs.width, frame_obs.height, side=cfg.side
            )
            if knee_info is not None:
                raw_knee_deg = knee_info[0]
                if (
                    knee_smoothed is None
                    or knee_last_ts is None
                    or (source_time - knee_last_ts) > cfg.max_gap
                ):
                    knee_smoothed = raw_knee_deg
                else:
                    dt_kn = max(0.0, source_time - knee_last_ts)
                    alpha_kn = 1.0 - math.exp(-dt_kn / metric_tau)
                    knee_smoothed += float(alpha_kn) * (raw_knee_deg - knee_smoothed)
                knee_last_ts = source_time
                knee_bend_deg = knee_smoothed

            axis_info = axis_curve_alignment(
                pose,
                frame_obs.width,
                frame_obs.height,
                facing=cfg.facing,
                side=cfg.side,
                neutral_offset=axis_neutral,
            )
            if axis_info is not None:
                axis_dev_raw = axis_info[0]

            head_info = head_horizontal_alignment(
                pose, frame_obs.width, frame_obs.height, neutral_offset=neutral_offset
            )
            if head_info is not None:
                raw_head_deg = head_info[0]
                if (
                    cfg.overlay_smoothing <= 0
                    or head_smoothed is None
                    or head_last_ts is None
                    or (source_time - head_last_ts) > cfg.max_gap
                ):
                    head_smoothed = raw_head_deg
                else:
                    dt_hd = max(0.0, source_time - head_last_ts)
                    alpha_hd = 1.0 - math.exp(-dt_hd / metric_tau)
                    head_smoothed += float(alpha_hd) * (raw_head_deg - head_smoothed)
                head_last_ts = source_time
                head_angle = head_smoothed

            sh_info = shoulder_tension_alignment(
                pose, frame_obs.width, frame_obs.height, neutral_clearance=sh_neutral
            )
            if sh_info is not None:
                raw_elev_pct, raw_clear = sh_info[0], sh_info[1]
                if not in_cal and len(relaxed_clearances) < 600 and 0.65 <= raw_clear <= 1.50:
                    relaxed_clearances.append(raw_clear)
                if (
                    shoulder_smoothed is None
                    or shoulder_last_ts is None
                    or (source_time - shoulder_last_ts) > cfg.max_gap
                ):
                    shoulder_smoothed = raw_elev_pct
                else:
                    dt_sh = max(0.0, source_time - shoulder_last_ts)
                    alpha_sh = 1.0 - math.exp(-dt_sh / metric_tau)
                    shoulder_smoothed += float(alpha_sh) * (raw_elev_pct - shoulder_smoothed)
                shoulder_last_ts = source_time
                shoulder_elev_pct = shoulder_smoothed

            if all(observation.values.get(m) is not None for m in observation.values):
                upper_body = {
                    m: float(v) for m, v in observation.values.items() if v is not None
                }
            else:
                for side_cand in (cfg.side, "left", "right"):
                    ear_lm = pose.get(f"{side_cand}_ear")
                    sh_lm = pose.get(f"{side_cand}_shoulder")
                    hip_lm = pose.get(f"{side_cand}_hip")
                    if all(
                        lm is not None
                        and 0 <= lm.x <= 1
                        and 0 <= lm.y <= 1
                        and (lm.visibility is None or lm.visibility >= 0.5)
                        for lm in (ear_lm, sh_lm, hip_lm)
                    ):
                        assert ear_lm is not None and sh_lm is not None and hip_lm is not None
                        try:
                            upper_body = calculate(
                                pixel(ear_lm, frame_obs.width, frame_obs.height),
                                pixel(sh_lm, frame_obs.width, frame_obs.height),
                                pixel(hip_lm, frame_obs.width, frame_obs.height),
                                cfg.facing,
                            )
                            break
                        except ValueError:
                            upper_body = None

        role = role_tracker.update(raw_role, source_time)
        embrace_type = embrace_tracker.update(
            frame_obs.poses[0] if len(frame_obs.poses) == 1 else None,
            role,
            source_time,
        )
        axis_track = axis_tracker.update(axis_dev_raw, source_time)
        axis_curve_smoothed = (
            float(axis_track["smoothed"]) if axis_track["smoothed"] is not None else None
        )
        axis_curve_state = str(axis_track["state"])
        bad_axis = bool(axis_track["alert"])

        quality_reason = next(
            (q.reason for q in observation.quality.values() if not q.valid), ""
        )
        st_state = soundtrack_summary.frame_states.get(frame_index)
        active_bpm = (
            float(st_state["bpm"])
            if st_state and st_state.get("bpm") is not None
            else cfg.tango_bpm
        )
        ankle_track = ankle_tracker.update(
            frame_obs.poses[0] if len(frame_obs.poses) == 1 else None,
            source_time,
            bpm=active_bpm,
            width=frame_obs.width,
            height=frame_obs.height,
        )
        ankle_lift_ratio = (
            float(ankle_track["smoothed_ratio"])
            if ankle_track["smoothed_ratio"] is not None
            else None
        )
        consecutive_high_ankle_steps = int(ankle_track["consecutive_high_steps"] or 0)
        bad_ankles = bool(ankle_track["alert"])

        coach_entry, coach_meta = _build_coach_frame_entry(
            cfg=cfg,
            frame_obs=frame_obs,
            display_poses=pose_smoother.update(frame_obs.poses, source_time),
            source_time=source_time,
            clip_time=clip_time,
            head_angle=head_angle,
            shoulder_elev_pct=shoulder_elev_pct,
            shoulder_neutral_clearance=sh_neutral,
            knee_bend_deg=knee_bend_deg,
            ankle_lift_ratio=ankle_lift_ratio,
            consecutive_high_ankle_steps=consecutive_high_ankle_steps,
            bad_ankles=bad_ankles,
            ankle_high_now=bool(ankle_track.get("high_now")),
            axis_curve_smoothed=axis_curve_smoothed,
            axis_curve_state=axis_curve_state,
            axis_curve_elapsed=float(axis_track.get("elapsed") or 0.0),
            bad_axis=bad_axis,
            role=role,
            embrace_type=embrace_type,
            upper_body=upper_body,
            st_state=st_state,
            quality_reason=quality_reason,
            timers=timers,
            session_rows=session_rows,
            single_pose_ok=len(frame_obs.poses) == 1,
        )
        coach_pending[frame_index] = coach_entry
        coach_entry["measurement_poses"] = [_serialise_pose(pose) for pose in frame_obs.poses]
        coach_frame_meta[frame_index] = coach_meta

        frame_log.append(
            {
                "frame_index": frame_index,
                "timestamp": source_time,
                "clip_timestamp": clip_time,
                "in_calibration_window": in_cal,
                "head_angle": head_angle,
                "shoulder_elev_pct": shoulder_elev_pct,
                "knee_bend_deg": knee_bend_deg,
                "ankle_lift_ratio": ankle_lift_ratio,
                "consecutive_high_ankle_steps": consecutive_high_ankle_steps,
                "ankle_high_alert": bad_ankles,
                "axis_curve_ratio": axis_curve_smoothed,
                "axis_curve_state": axis_curve_state,
                "axis_curve_alert": bad_axis,
                "role": role,
                "embrace_type": embrace_type,
                "upper_body": upper_body,
                "quality_reason": quality_reason,
                "beat": st_state.get("beat") if st_state else None,
                "phrase": st_state.get("phrase") if st_state else None,
                "bpm": st_state.get("bpm") if st_state else None,
                "beat_source": st_state.get("source") if st_state else None,
            }
        )

    total_source_frames = max(probe.frame_count, decoded_frames[-1][0] + 1)
    movement_observations = [
        (frame_index, obs) for frame_index, _, _, obs in decoded_frames
    ]
    movement_data = analyse_movement_observations(
        movement_observations,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        frame_count=total_source_frames,
        sample_stride=1,
        model_hash=model_hash,
        source_sha256=source_sha,
        segment_index=-1,
        source_member=probe.path.name,
    )

    for frame_index, entry in coach_pending.items():
        meta = coach_frame_meta[frame_index]
        entry["lines"] = list(meta["lines"])
        entry["notes"] = [
            meta["primary"],
            *meta["lines"],
            *_movement_note_lines(
                movement_data, frame_index, probe.fps, cfg.replay_step_history_seconds
            ),
        ]
    coach_data: dict[str, Any] = {
        "version": 1,
        "model_hash": model_hash,
        "capture_profile": cap_profile,
        "source_sha256": source_sha,
        "width": probe.width,
        "height": probe.height,
        "fps": probe.fps,
        "clip_in": round(clip_in, 4),
        "clip_out": round(clip_out, 4),
        "neutral_offset": neutral_offset,
        "axis_neutral_offset": axis_neutral,
        "frames": {str(k): v for k, v in coach_pending.items()},
    }

    coverage = _compute_family_coverage(
        clip_duration=clip_duration,
        total_frames=len(decoded_frames),
        fps=probe.fps,
        frame_log=frame_log,
        movement_data=movement_data,
        soundtrack=soundtrack_summary,
        cfg=cfg,
    )

    mean_duration = float(np.mean(infer_times)) if infer_times else 0.0
    performance = {
        "model_hash": model_hash,
        "resolution": [probe.width, probe.height],
        "processed_fps": 1.0 / mean_duration if mean_duration > 0 else None,
        "dropped_frames": 0,
        "timestamp_fallback": False,
    }
    session_summary: dict[str, Any] | None = None
    session_events: list[dict[str, Any]] | None = None
    if session is not None:
        last_ts = decoded_frames[-1][1]
        session_summary = session.finish(last_ts, "completed", performance)
        session_events = [asdict(e) for v in session.evaluators.values() for e in v.events]

    clean_title = title.strip() or probe.path.stem
    scored_frame_log = [
        row for row in frame_log if not row.get("in_calibration_window", False)
    ] or frame_log

    if progress_callback is not None:
        progress_callback(92, 100, "Writing report & companion metadata…")

    import_context = {
        "is_imported_reference": True,
        "title": clean_title,
        "source_filename": probe.path.name,
        "source_sha256": source_sha,
        "clip_in": round(clip_in, 3),
        "clip_out": round(clip_out, 3),
        "clip_duration": round(clip_duration, 3),
        "analysis_mode": analysis_mode,
        "readiness": readiness.to_dict(),
        "youtube_link": yt_link.to_dict() if yt_link is not None else None,
        "facebook_reel": facebook_link.to_dict() if facebook_link is not None else None,
        "youtube_alignment": (
            validated_alignment.to_dict() if validated_alignment is not None else None
        ),
        "coverage": {k: v.to_dict() for k, v in coverage.items()},
        "soundtrack": soundtrack_summary.to_metadata_dict(),
        "pivot_count": len(movement_data.get("pivots", [])),
        "step_count": len(movement_data.get("steps", [])),
    }

    report_path = write_markdown_report(
        report_dir=target_dir,
        cfg=cfg,
        source=f"import:{probe.path.name}",
        status="completed",
        frame_log=scored_frame_log,
        performance=performance,
        reference=active_reference,
        session_id=session.id if session is not None else None,
        session_summary=session_summary,
        session_events=session_events,
        recorded_video=probe.path,
        recorded_overlay=None,
        import_context=import_context,
        movement_data=movement_data,
    )

    companion_movement_path = target_dir / f"{report_path.stem}.movement.v1.json"
    save_movement_companion(companion_movement_path, movement_data)
    companion_coach_path = target_dir / f"{report_path.stem}.coach.v1.json"
    save_coach_companion(companion_coach_path, coach_data)

    meta_path = write_metadata(
        report_path,
        f"import:{probe.path.name}",
        probe.path,
        None,
        [float(row["timestamp"]) for row in frame_log],
        extra={
            "title": clean_title,
            "is_imported_reference": True,
            "source_sha256": source_sha,
            "clip_in": round(clip_in, 4),
            "clip_out": round(clip_out, 4),
            "analysis_mode": analysis_mode,
            "readiness": readiness.to_dict(),
            "reference_summary": (
                {
                    "id": active_reference.id,
                    "name": active_reference.name,
                    "created_at_utc": active_reference.created_at_utc,
                    "profile": active_reference.profile,
                    "baseline": active_reference.baseline,
                    "quality": active_reference.quality,
                    "model_hash": active_reference.model_hash,
                }
                if active_reference is not None
                else None
            ),
            "youtube_link": yt_link.to_dict() if yt_link is not None else None,
            "facebook_reel": facebook_link.to_dict() if facebook_link is not None else None,
            "youtube_alignment": (
                validated_alignment.to_dict() if validated_alignment is not None else None
            ),
            "movement_analysis_file": companion_movement_path.name,
            "coach_overlay_file": companion_coach_path.name,
            "coverage": {k: v.to_dict() for k, v in coverage.items()},
            "soundtrack": soundtrack_summary.to_metadata_dict(),
            "capture_profile": cap_profile,
        },
    )

    if progress_callback is not None:
        progress_callback(100, 100, "Reference video analysis complete.")

    return ImportedClipResult(
        report_path=report_path,
        metadata_path=meta_path,
        movement_path=companion_movement_path,
        title=clean_title,
        video_path=probe.path,
        source_sha256=source_sha,
        selected_in=clip_in,
        selected_out=clip_out,
        analysis_mode=analysis_mode,
        readiness=readiness,
        reference=active_reference,
        movement_data=movement_data,
        soundtrack=soundtrack_summary,
        coverage=coverage,
        youtube_link=yt_link,
        youtube_alignment=validated_alignment,
        facebook_reel=facebook_link,
        coach_path=companion_coach_path,
        coach_data=coach_data,
    )
