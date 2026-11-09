"""Unit tests for reference video import, YouTube provenance, virtual clip decoding,
soundtrack analysis, relinking, and teacher-vs-practice comparison.
"""

from __future__ import annotations

import math
import shutil
import wave
from pathlib import Path

import cv2
import numpy as np
import pytest

from tango_posture.config import Config
from tango_posture.domain import FrameObservation, Landmark
from tango_posture.reference_import import (
    MAX_IMPORT_RANGE_SECONDS,
    SOUNDTRACK_SAMPLE_RATE,
    SOUNDTRACK_SOURCE_LABEL,
    ImportCancelledError,
    YouTubeClockAlignment,
    analyse_imported_clip,
    analyse_video_soundtrack,
    build_youtube_open_url,
    coach_notes_for_frame,
    compare_clips,
    download_youtube_video,
    imports_directory,
    iter_virtual_clip_frames,
    load_report_coach_data,
    load_report_movement_data,
    parse_youtube_url,
    probe_import_video,
    relink_report_video,
    render_coach_frame_overlay,
    validate_clip_range,
)
from tango_posture.review import read_report


def _write_test_video(
    path: Path,
    *,
    fps: float = 10.0,
    duration_seconds: float = 6.0,
    width: int = 160,
    height: int = 120,
) -> Path:
    total_frames = round(fps * duration_seconds)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter.fourcc(*"MJPG"), fps, (width, height)
    )
    assert writer.isOpened()
    for idx in range(total_frames):
        val = int((idx * 7) % 250)
        frame = np.full((height, width, 3), val, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return path


def _make_pose_for_time(
    t: float,
    *,
    pivot_window: tuple[float, float] | None = (2.0, 2.6),
    step_at: float | None = 3.2,
    crop_feet: bool = False,
    unstable: bool = False,
) -> tuple[list[dict[str, Landmark]], list[dict[str, Landmark]]]:
    jitter = 0.08 * math.sin(t * 17.0) if unstable else 0.0
    yaw_rad = 0.0
    if pivot_window is not None and pivot_window[0] <= t <= pivot_window[1]:
        frac = (t - pivot_window[0]) / max(1e-6, pivot_window[1] - pivot_window[0])
        yaw_rad = math.radians(95.0 * frac)
    elif pivot_window is not None and t > pivot_window[1]:
        yaw_rad = math.radians(95.0)

    step_shift = 0.0
    if step_at is not None:
        if t < step_at - 0.2:
            step_shift = 0.0
        elif t <= step_at:
            step_shift = 0.12 * ((t - (step_at - 0.2)) / 0.2)
        else:
            step_shift = 0.12

    foot_conf = 0.10 if crop_feet else 0.95
    foot_y = 1.15 if crop_feet else 0.88
    ankle_conf = 0.92
    ankle_y = 0.88

    span_2d = max(0.06, 0.09 * abs(math.cos(yaw_rad)) + 0.06)
    pose_2d: dict[str, Landmark] = {
        "nose": Landmark(0.52 + jitter, 0.18, 0.96, 0.96, 0.0),
        "left_ear": Landmark(0.49 + jitter, 0.19, 0.95, 0.95, 0.0),
        "right_ear": Landmark(0.51 + jitter, 0.19, 0.95, 0.95, 0.0),
        "left_shoulder": Landmark(0.50 - span_2d + jitter, 0.30, 0.96, 0.96, 0.0),
        "right_shoulder": Landmark(0.50 + span_2d, 0.30, 0.96, 0.96, 0.0),
        "left_elbow": Landmark(0.42, 0.38, 0.92, 0.92, 0.0),
        "right_elbow": Landmark(0.58, 0.38, 0.92, 0.92, 0.0),
        "left_wrist": Landmark(0.38, 0.30, 0.92, 0.92, 0.0),
        "right_wrist": Landmark(0.60, 0.38, 0.92, 0.92, 0.0),
        "left_hip": Landmark(0.50 - span_2d, 0.54, 0.96, 0.96, 0.0),
        "right_hip": Landmark(0.50 + span_2d, 0.54, 0.96, 0.96, 0.0),
        "left_knee": Landmark(0.47, 0.71, 0.94, 0.94, 0.0),
        "right_knee": Landmark(0.53 + step_shift * 0.5, 0.71, 0.94, 0.94, 0.0),
        "left_ankle": Landmark(0.46, ankle_y, ankle_conf, ankle_conf, 0.0),
        "right_ankle": Landmark(0.54 + step_shift, ankle_y, ankle_conf, ankle_conf, 0.0),
        "left_heel": Landmark(0.44, foot_y + 0.02, foot_conf, foot_conf, 0.0),
        "right_heel": Landmark(0.52 + step_shift, foot_y + 0.02, foot_conf, foot_conf, 0.0),
        "left_foot_index": Landmark(0.50, foot_y + 0.02, foot_conf, foot_conf, 0.0),
        "right_foot_index": Landmark(
            0.58 + step_shift, foot_y + 0.02, foot_conf, foot_conf, 0.0
        ),
    }
    dx_3d = 0.14 * math.cos(yaw_rad)
    dz_3d = 0.14 * math.sin(yaw_rad)
    world_pose: dict[str, Landmark] = {
        **pose_2d,
        "left_hip": Landmark(-dx_3d, 0.0, 0.96, 0.96, -dz_3d),
        "right_hip": Landmark(dx_3d, 0.0, 0.96, 0.96, dz_3d),
    }
    return ([pose_2d], [world_pose])


class _SyntheticEstimator:
    def __init__(
        self,
        *,
        pivot_window: tuple[float, float] | None = (2.0, 2.6),
        step_at: float | None = 3.2,
        crop_feet: bool = False,
        multi_person: bool = False,
        unstable: bool = False,
    ) -> None:
        self.pivot_window = pivot_window
        self.step_at = step_at
        self.crop_feet = crop_feet
        self.multi_person = multi_person
        self.unstable = unstable

    def estimate(self, image: np.ndarray, timestamp: float) -> FrameObservation:
        h, w = int(image.shape[0]), int(image.shape[1])
        poses, world_poses = _make_pose_for_time(
            timestamp,
            pivot_window=self.pivot_window,
            step_at=self.step_at,
            crop_feet=self.crop_feet,
            unstable=self.unstable,
        )
        if self.multi_person:
            poses = [poses[0], poses[0]]
            world_poses = [world_poses[0], world_poses[0]]
        return FrameObservation(
            timestamp=timestamp,
            width=w,
            height=h,
            poses=poses,
            world_poses=world_poses,
        )

    def close(self) -> None:
        pass


def test_youtube_url_validation_and_canonicalisation():
    # Standard watch URL with tracking & playlist params
    link = parse_youtube_url(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&si=track99&t=1m30s"
    )
    assert link is not None
    assert link.video_id == "dQw4w9WgXcQ"
    assert link.canonical_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert link.anchor_seconds == pytest.approx(90.0)
    assert build_youtube_open_url(link) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=90s"

    # Short youtu.be link and mobile Shorts link
    short = parse_youtube_url("https://youtu.be/abcDEF123_-?start=42")
    assert short is not None
    assert short.video_id == "abcDEF123_-"
    assert short.canonical_url == "https://www.youtube.com/watch?v=abcDEF123_-"
    assert short.anchor_seconds == pytest.approx(42.0)

    shorts = parse_youtube_url("https://m.youtube.com/shorts/xyz98765432?feature=share")
    assert shorts is not None
    assert shorts.video_id == "xyz98765432"
    assert shorts.canonical_url == "https://www.youtube.com/watch?v=xyz98765432"
    assert shorts.anchor_seconds is None

    # Reject non-HTTPS, lookalike domains, embedded credentials, and bad IDs
    for invalid in (
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
        "https://user:pass@www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=-5",
    ):
        with pytest.raises(ValueError):
            parse_youtube_url(invalid)


def test_youtube_independent_clock_alignment():
    alignment = YouTubeClockAlignment(
        local_source_seconds=12.0,
        youtube_seconds=45.0,
        confirmed_same_edit=True,
        source_sha256="sha-original",
        youtube_video_id="dQw4w9WgXcQ",
    )
    assert alignment.youtube_timestamp_at(
        15.5,
        source_sha256="sha-original",
        youtube_video_id="dQw4w9WgXcQ",
        clip_in=10.0,
        clip_out=20.0,
    ) == pytest.approx(48.5)

    # Outside clip bounds -> None
    assert (
        alignment.youtube_timestamp_at(
            25.0,
            source_sha256="sha-original",
            youtube_video_id="dQw4w9WgXcQ",
            clip_in=10.0,
            clip_out=20.0,
        )
        is None
    )
    # Changed file SHA or YouTube video ID -> invalidated
    assert (
        alignment.youtube_timestamp_at(
            15.0,
            source_sha256="sha-different",
            youtube_video_id="dQw4w9WgXcQ",
            clip_in=10.0,
            clip_out=20.0,
        )
        is None
    )
    assert (
        alignment.youtube_timestamp_at(
            15.0,
            source_sha256="sha-original",
            youtube_video_id="otherVideo1",
            clip_in=10.0,
            clip_out=20.0,
        )
        is None
    )


def test_virtual_clip_range_decoding_and_limits(tmp_path):
    video_path = _write_test_video(tmp_path / "teacher.avi", fps=10.0, duration_seconds=6.0)
    orig_bytes = video_path.read_bytes()
    probe = probe_import_video(video_path)
    assert probe.frame_count == 60
    assert probe.duration_seconds == pytest.approx(6.0, abs=0.2)

    # Reject range > MAX_IMPORT_RANGE_SECONDS or out <= in
    with pytest.raises(ValueError, match="greater than in-point"):
        validate_clip_range(probe, 3.0, 2.0)
    fake_long_probe = probe.__class__(
        path=probe.path,
        size_bytes=probe.size_bytes,
        duration_seconds=300.0,
        fps=probe.fps,
        frame_count=3000,
        width=probe.width,
        height=probe.height,
    )
    with pytest.raises(ValueError, match="exceeds maximum allowed duration"):
        validate_clip_range(fake_long_probe, 10.0, 10.0 + MAX_IMPORT_RANGE_SECONDS + 5.0)

    # Decode sub-range [1.5, 3.5) and verify half-open interval & coordinate mapping
    frames = list(iter_virtual_clip_frames(video_path, 1.5, 3.5, probe=probe))
    assert len(frames) == 20
    assert frames[0].source_time == pytest.approx(1.5, abs=0.05)
    assert frames[0].clip_time == pytest.approx(0.0, abs=0.05)
    assert frames[-1].source_time < 3.5
    for f in frames:
        assert f.clip_time == pytest.approx(f.source_time - 1.5, abs=1e-6)
    # Original file is completely untouched
    assert video_path.read_bytes() == orig_bytes


def test_offline_import_movement_and_posture_modes_and_calibration_exclusion(tmp_path):
    video_path = _write_test_video(tmp_path / "clip.avi", fps=15.0, duration_seconds=6.0)
    cfg = Config(
        data_dir=str(tmp_path / "data"),
        allow_all_angles=True,
        calibration_duration=2.0,
        calibration_valid_duration=1.5,
        calibration_samples=15,
    )

    # 1. Movement review mode (uncalibrated) with YouTube link
    res_mov = analyse_imported_clip(
        video_path=video_path,
        cfg=cfg,
        report_dir=tmp_path / "reports",
        selected_in=1.0,
        selected_out=5.0,
        title="Milonga Turn Study",
        youtube_url="https://youtu.be/dQw4w9WgXcQ?t=30",
        analysis_mode="movement",
        estimator_factory=lambda: _SyntheticEstimator(pivot_window=(2.0, 2.6), step_at=3.2),
    )
    assert res_mov.report_path.is_file()
    assert res_mov.metadata_path.is_file()
    assert res_mov.movement_path.is_file()
    assert res_mov.coverage["pivots"].status == "detected"
    assert res_mov.coverage["steps"].status == "detected"
    # Exported markdown omits full absolute local path
    md_text = res_mov.report_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in md_text
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in md_text

    # 2. Reference-relative posture review with calibration interval [0.5, 2.5) inside [0.5, 5.5)
    res_posture = analyse_imported_clip(
        video_path=video_path,
        cfg=cfg,
        report_dir=tmp_path / "reports",
        selected_in=0.5,
        selected_out=5.5,
        title="Calibrated Study",
        analysis_mode="posture",
        calibration_interval=(0.5, 2.5),
        estimator_factory=lambda: _SyntheticEstimator(pivot_window=(3.2, 3.8), step_at=4.2),
    )
    assert res_posture.reference is not None
    record = read_report(res_posture.report_path)
    assert record.is_imported_reference
    assert record.clip_range == pytest.approx((0.5, 5.5))

    # 3. Unstable calibration interval is rejected with a clear reason
    with pytest.raises(ValueError, match="Calibration rejected"):
        analyse_imported_clip(
            video_path=video_path,
            cfg=cfg,
            report_dir=tmp_path / "reports",
            selected_in=0.5,
            selected_out=5.5,
            analysis_mode="posture",
            calibration_interval=(0.5, 2.5),
            estimator_factory=lambda: _SyntheticEstimator(unstable=True),
        )


def test_poor_footage_reports_not_enough_evidence_without_fabricating_events(tmp_path):
    video_path = _write_test_video(tmp_path / "cropped.avi", fps=15.0, duration_seconds=4.0)
    cfg = Config(data_dir=str(tmp_path / "data"), allow_all_angles=True)

    # Cropped feet -> steps family reports not_enough_evidence with 0 events
    res_cropped = analyse_imported_clip(
        video_path=video_path,
        cfg=cfg,
        report_dir=tmp_path / "reports",
        selected_in=0.0,
        selected_out=4.0,
        analysis_mode="movement",
        estimator_factory=lambda: _SyntheticEstimator(crop_feet=True, pivot_window=None, step_at=None),
    )
    assert res_cropped.coverage["steps"].status == "not_enough_evidence"
    assert res_cropped.coverage["steps"].event_count == 0
    assert res_cropped.coverage["pivots"].status == "no_events_detected"

    # Multi-person footage -> posture, pivots, and steps all report not_enough_evidence
    res_multi = analyse_imported_clip(
        video_path=video_path,
        cfg=cfg,
        report_dir=tmp_path / "reports",
        selected_in=0.0,
        selected_out=4.0,
        analysis_mode="movement",
        estimator_factory=lambda: _SyntheticEstimator(multi_person=True),
    )
    assert res_multi.coverage["posture"].status == "not_enough_evidence"
    assert res_multi.coverage["pivots"].status == "not_enough_evidence"
    assert res_multi.coverage["steps"].status == "not_enough_evidence"


def test_reopen_and_relink_verifies_sha256_identity(tmp_path):
    video_path = _write_test_video(tmp_path / "original.avi", fps=15.0, duration_seconds=4.0)
    cfg = Config(data_dir=str(tmp_path / "data"), allow_all_angles=True)
    res = analyse_imported_clip(
        video_path=video_path,
        cfg=cfg,
        report_dir=tmp_path / "reports",
        selected_in=0.5,
        selected_out=3.5,
        youtube_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        youtube_alignment=YouTubeClockAlignment(
            local_source_seconds=1.0,
            youtube_seconds=20.0,
            confirmed_same_edit=True,
            source_sha256="",  # will be set below via relink test
            youtube_video_id="dQw4w9WgXcQ",
        ),
        analysis_mode="movement",
        estimator_factory=_SyntheticEstimator,
    )

    # Move the video file to a new directory
    moved_dir = tmp_path / "moved"
    moved_dir.mkdir()
    moved_video = moved_dir / "relocated_teacher.avi"
    shutil.move(str(video_path), str(moved_video))

    record = read_report(res.report_path)
    outcome_same = relink_report_video(record, moved_video)
    assert outcome_same.exact_match is True
    reloaded = read_report(res.report_path)
    assert reloaded.recorded_video == moved_video.resolve()
    assert load_report_movement_data(reloaded) is not None

    # Relinking to a different/edited video clears stale overlays and alignments
    edited_video = _write_test_video(tmp_path / "edited.avi", fps=15.0, duration_seconds=3.0)
    outcome_diff = relink_report_video(reloaded, edited_video)
    assert outcome_diff.exact_match is False
    reloaded_diff = read_report(res.report_path)
    assert reloaded_diff.relinked_content_changed is True
    assert load_report_movement_data(reloaded_diff) is None
    assert reloaded_diff.youtube_alignment is None


def test_video_soundtrack_analysis_silent_vs_rhythmic(tmp_path):
    video_path = _write_test_video(tmp_path / "musical.avi", fps=15.0, duration_seconds=6.0)
    wav_path = video_path.with_suffix(".wav")
    frame_times = [(i, i / 15.0, i / 15.0) for i in range(90)]
    cfg = Config(tango_bpm=120.0, beat_analysis_seconds=1.5)

    # 1. Silent WAV -> no fake clock beats
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SOUNDTRACK_SAMPLE_RATE)
        wf.writeframes(np.zeros(SOUNDTRACK_SAMPLE_RATE * 6, dtype=np.int16).tobytes())

    silent_res = analyse_video_soundtrack(
        video_path, 0.0, 6.0, frame_times, cfg, enabled=True
    )
    assert silent_res.available is True
    assert silent_res.counted_beats == 0
    assert silent_res.status == "no_events_detected"

    # 2. Rhythmic 120 BPM pulse train (every 0.5s) -> detected beats with "Video soundtrack" source
    sr = SOUNDTRACK_SAMPLE_RATE
    t = np.arange(sr * 6, dtype=np.float64) / sr
    signal = np.zeros_like(t)
    for beat_sec in np.arange(0.25, 5.8, 0.5):
        mask = (t >= beat_sec) & (t < beat_sec + 0.05)
        signal[mask] = 0.85 * np.sin(2.0 * np.pi * 110.0 * (t[mask] - beat_sec))
    pcm16 = np.clip(signal * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())

    beat_res = analyse_video_soundtrack(
        video_path, 0.0, 6.0, frame_times, cfg, enabled=True
    )
    assert beat_res.available is True
    assert beat_res.status == "detected"
    assert beat_res.source == SOUNDTRACK_SOURCE_LABEL
    assert beat_res.counted_beats >= 4
    assert beat_res.estimated_bpm is not None


def test_teacher_vs_practice_comparison_anchors_and_bounds(tmp_path):
    teacher_vid = _write_test_video(tmp_path / "teacher.avi", fps=15.0, duration_seconds=6.0)
    practice_vid = _write_test_video(tmp_path / "practice.avi", fps=15.0, duration_seconds=5.0)
    cfg_teacher = Config(data_dir=str(tmp_path / "data"), side="left", facing="right", allow_all_angles=True)
    cfg_practice = Config(data_dir=str(tmp_path / "data"), side="right", facing="left", allow_all_angles=True)

    teacher_res = analyse_imported_clip(
        video_path=teacher_vid,
        cfg=cfg_teacher,
        report_dir=tmp_path / "reports",
        selected_in=1.0,
        selected_out=5.0,
        title="Teacher Example",
        analysis_mode="movement",
        estimator_factory=lambda: _SyntheticEstimator(pivot_window=(2.5, 3.1), step_at=3.4),
    )
    practice_res = analyse_imported_clip(
        video_path=practice_vid,
        cfg=cfg_practice,
        report_dir=tmp_path / "reports",
        selected_in=0.0,
        selected_out=4.0,
        title="My Practice",
        analysis_mode="movement",
        estimator_factory=lambda: _SyntheticEstimator(pivot_window=(1.7, 2.3), step_at=2.6),
    )

    teacher_rec = read_report(teacher_res.report_path)
    practice_rec = read_report(practice_res.report_path)

    # Teacher pivot is at source_time=2.5s (clip_time=1.5s); practice pivot is at source_time=1.7s (clip_time=1.7s)
    # Set both anchors at clip_time=1.5s so practice pivot is +200ms relative to teacher anchor
    cmp = compare_clips(
        teacher_rec,
        practice_rec,
        anchor_left_clip_time=1.5,
        anchor_right_clip_time=1.5,
    )
    assert cmp.pivot_comparison["supported"] is True
    assert cmp.pivot_comparison["direction_match"] is True
    assert cmp.pivot_comparison["delta_onset_ms"] == pytest.approx(200.0, abs=80.0)
    assert cmp.step_comparison["supported"] is True
    assert any("Camera view differs" in w for w in cmp.comparability_warnings)

    # Verify boundary clamping when seeking past clip bounds
    sync_point = cmp.sync_at_offset(
        10.0, left_in=1.0, left_out=5.0, right_in=0.0, right_out=4.0
    )
    assert sync_point.left_at_boundary is True
    assert sync_point.right_at_boundary is True
    assert sync_point.left_clip_time == pytest.approx(4.0)
    assert sync_point.right_clip_time == pytest.approx(4.0)


def test_direct_youtube_download_caching_and_full_open_coach_analysis(tmp_path, monkeypatch):
    cfg = Config(data_dir=str(tmp_path / "data"), side="left", facing="right", allow_all_angles=True)
    calls: list[tuple[str, Path]] = []

    def _fake_downloader(canonical_url: str, target_path: Path) -> Path:
        calls.append((canonical_url, target_path))
        return _write_test_video(target_path, fps=15.0, duration_seconds=5.0)

    # 1. Direct download validates URL, writes youtube_<id>.mp4, and reuses cached file on repeat
    imp_dir = imports_directory(cfg.data_dir)
    downloaded_path, link = download_youtube_video(
        "https://youtu.be/dQw4w9WgXcQ?t=1",
        imp_dir,
        downloader=_fake_downloader,
    )
    assert len(calls) == 1
    assert calls[0][0] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert downloaded_path.name == "youtube_dQw4w9WgXcQ.mp4"
    assert link.video_id == "dQw4w9WgXcQ"
    assert link.anchor_seconds == pytest.approx(1.0)

    cached_path, _ = download_youtube_video(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        imp_dir,
        downloader=_fake_downloader,
    )
    assert cached_path == downloaded_path
    assert len(calls) == 1  # Reused cached file without invoking downloader again

    # Cancellation check before download
    with pytest.raises(ImportCancelledError):
        download_youtube_video(
            "https://youtu.be/oHg5SJYRHA0",
            imp_dir,
            cancel_check=lambda: True,
            downloader=_fake_downloader,
        )

    # Missing yt-dlp surfaces clear actionable error when no downloader is available
    monkeypatch.setattr("tango_posture.reference_import._find_ytdlp_command", lambda: None)
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", None)
    with pytest.raises(ValueError, match="Cannot download from YouTube: 'yt-dlp' is not installed"):
        download_youtube_video("https://youtu.be/oHg5SJYRHA0", imp_dir)

    # 2. One-step analyse_imported_clip from YouTube URL (blank video_path)
    res = analyse_imported_clip(
        video_path="",
        youtube_url="https://youtu.be/9bZkp7q19f0?t=1",
        cfg=cfg,
        report_dir=tmp_path / "reports",
        title="YouTube Direct Study",
        analysis_mode="movement",
        analyse_soundtrack=True,
        estimator_factory=lambda: _SyntheticEstimator(pivot_window=(2.0, 2.7), step_at=3.2),
        youtube_downloader=_fake_downloader,
    )
    assert res.video_path.name == "youtube_9bZkp7q19f0.mp4"
    assert res.selected_in == pytest.approx(1.0)
    assert res.selected_out == pytest.approx(5.0)
    assert res.youtube_alignment is not None
    assert res.youtube_alignment.confirmed_same_edit is True
    assert res.youtube_alignment.local_source_seconds == pytest.approx(1.0)
    assert res.youtube_alignment.youtube_seconds == pytest.approx(1.0)
    assert res.coach_path is not None and res.coach_path.is_file()

    # 3. Verify report Section 3.4, coach companion data, overlay rendering, and notes
    rec = read_report(res.report_path)
    assert "### 3.4 Movement Analysis (Pivots & Step Placements Timeline)" in rec.markdown
    assert "#### Pivots" in rec.markdown
    assert "#### Step Placements" in rec.markdown
    assert rec.recording_seconds(2.5, 15.0) == pytest.approx(2.5)
    assert rec.recording_seconds(2.5, 15.0, "Overlay") == pytest.approx(1.5)

    coach_data = load_report_coach_data(rec)
    assert coach_data is not None
    first_frame_idx = int(next(iter(coach_data["frames"].keys())))
    raw_img = np.full((120, 160, 3), 40, dtype=np.uint8)
    rendered = render_coach_frame_overlay(raw_img, coach_data, first_frame_idx, cfg)
    assert rendered.shape[0] >= 120 and rendered.shape[1] >= 960
    assert not np.array_equal(cv2.resize(raw_img, (rendered.shape[1], rendered.shape[0])), rendered)

    notes = coach_notes_for_frame(coach_data, first_frame_idx)
    assert notes is not None and len(notes) >= 4
    joined = "\n".join(notes)
    assert "Role:" in joined and "Torso axis:" in joined and "Clip Movement Totals:" in joined

