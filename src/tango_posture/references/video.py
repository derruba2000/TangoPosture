"""Local video inspection and timestamp-aware virtual clip decoding."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2

from .constants import MAX_IMPORT_RANGE_SECONDS, MAX_IMPORT_VIDEO_BYTES
from .errors import ImportCancelledError


@dataclass(frozen=True)
class VideoProbeInfo:
    """Metadata inspected from a local video file without modifying it."""

    path: Path
    size_bytes: int
    duration_seconds: float
    fps: float
    frame_count: int
    width: int
    height: int
    first_pts_seconds: float = 0.0


@dataclass(frozen=True)
class DecodedClipFrame:
    """A decoded video frame with explicit frame index, source media time, and clip time."""

    frame_index: int
    source_time: float
    clip_time: float
    image: Any


def probe_import_video(
    path: Path | str,
    *,
    max_bytes: int = MAX_IMPORT_VIDEO_BYTES,
) -> VideoProbeInfo:
    """Inspect a local video file, enforcing file existence, size limits, and decodability."""
    resolved = Path(path)
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"Video file does not exist: '{resolved}'")
    size_bytes = resolved.stat().st_size
    if size_bytes <= 0:
        raise ValueError(f"Video file is empty: '{resolved.name}'")
    if size_bytes > max_bytes:
        limit_gb = max_bytes / (1024**3)
        raise ValueError(
            f"Video file exceeds maximum size limit ({limit_gb:.1f} GB): '{resolved.name}'"
        )

    cap = cv2.VideoCapture(str(resolved))
    try:
        if not cap.isOpened():
            raise ValueError(f"Cannot decode video file: '{resolved.name}'")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        raw_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        ok, first_frame = cap.read()
        if not ok or first_frame is None:
            raise ValueError(f"Video file contains no decodable frames: '{resolved.name}'")
        height, width = int(first_frame.shape[0]), int(first_frame.shape[1])
        if width <= 0 or height <= 0:
            raise ValueError(f"Invalid frame dimensions in '{resolved.name}'")
        first_msec = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
        first_pts = first_msec / 1000.0 if math.isfinite(first_msec) and first_msec >= 0.0 else 0.0
        if not math.isfinite(fps) or fps <= 0.0:
            fps = 30.0
        frame_count = int(raw_count) if math.isfinite(raw_count) and raw_count > 0 else 0
        duration = frame_count / fps if frame_count > 0 else 0.0
        if duration <= 0.0:
            count = 1
            last_t = 0.0
            while True:
                ok_next, _ = cap.read()
                if not ok_next:
                    break
                msec = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
                t_val = (
                    (msec / 1000.0 - first_pts)
                    if math.isfinite(msec) and msec > 0
                    else count / fps
                )
                last_t = max(last_t, t_val)
                count += 1
            frame_count = count
            duration = max(last_t, frame_count / fps)
        return VideoProbeInfo(
            path=resolved,
            size_bytes=size_bytes,
            duration_seconds=float(duration),
            fps=float(fps),
            frame_count=int(frame_count),
            width=width,
            height=height,
            first_pts_seconds=float(first_pts),
        )
    finally:
        cap.release()


def validate_clip_range(
    probe: VideoProbeInfo,
    selected_in: float,
    selected_out: float,
    *,
    max_range_seconds: float = MAX_IMPORT_RANGE_SECONDS,
) -> tuple[float, float]:
    """Validate `[selected_in, selected_out)` as a half-open range within clip limits."""
    if not (math.isfinite(selected_in) and math.isfinite(selected_out)):
        raise ValueError("Clip in and out points must be finite numbers")
    if selected_in < 0.0:
        raise ValueError("Clip in-point must be >= 0.0s")
    if selected_out <= selected_in:
        raise ValueError("Clip out-point must be greater than in-point")
    if probe.duration_seconds > 0.0 and selected_in >= probe.duration_seconds:
        raise ValueError(
            f"Clip in-point ({selected_in:.2f}s) is at or past video duration ({probe.duration_seconds:.2f}s)"
        )
    clamped_out = (
        min(float(selected_out), float(probe.duration_seconds) + 1.0 / max(probe.fps, 1.0))
        if probe.duration_seconds > 0.0
        else float(selected_out)
    )
    if clamped_out <= selected_in:
        raise ValueError("Selected clip range contains no playable duration")
    clip_duration = clamped_out - selected_in
    if clip_duration > max_range_seconds + 1e-6:
        raise ValueError(
            f"Selected clip range ({clip_duration:.1f}s) exceeds maximum allowed duration ({max_range_seconds:.0f}s)"
        )
    return (float(selected_in), float(clamped_out))


def iter_virtual_clip_frames(
    path: Path | str,
    selected_in: float,
    selected_out: float,
    *,
    probe: VideoProbeInfo | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Iterator[DecodedClipFrame]:
    """Seek and decode forward through `[selected_in, selected_out)` without copying the file.

    Keeps three timeline coordinates explicit:
    1. `frame_index`: 0-based decoded frame index in the source video
    2. `source_time`: media time in seconds measured from the first playable video timestamp
    3. `clip_time`: `source_time - selected_in` in `[0, selected_out - selected_in)`
    """
    info = probe or probe_import_video(path)
    clip_in, clip_out = validate_clip_range(info, selected_in, selected_out)

    cap = cv2.VideoCapture(str(info.path))
    try:
        if not cap.isOpened():
            raise ValueError(f"Cannot open video file: '{info.path.name}'")
        fps = info.fps if math.isfinite(info.fps) and info.fps > 0 else 30.0
        frame_index = 0
        first_pts: float | None = None
        last_source_time = -1.0
        fallback = False
        fallback_offset: float | None = None

        preroll_target = max(0.0, clip_in - 1.5)
        if preroll_target >= 1.0:
            cap.set(cv2.CAP_PROP_POS_MSEC, (preroll_target + info.first_pts_seconds) * 1000.0)
            pos_frames = cap.get(cv2.CAP_PROP_POS_FRAMES)
            if math.isfinite(pos_frames) and pos_frames >= 0:
                frame_index = round(pos_frames)
            first_pts = info.first_pts_seconds

        while True:
            if cancel_check is not None and cancel_check():
                raise ImportCancelledError("Reference video analysis was cancelled")
            ok, image = cap.read()
            if not ok or image is None:
                break

            raw_msec = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
            raw_sec = raw_msec / 1000.0 if math.isfinite(raw_msec) else float("nan")
            if first_pts is None:
                first_pts = raw_sec if math.isfinite(raw_sec) and raw_sec >= 0.0 else 0.0

            source_time = raw_sec - first_pts if math.isfinite(raw_sec) else float("nan")
            if (
                fallback
                or not math.isfinite(source_time)
                or source_time < -1e-6
                or source_time <= last_source_time
            ):
                if fallback_offset is None:
                    fallback_offset = max(
                        0.0,
                        (last_source_time + 1.0 / fps - frame_index / fps)
                        if last_source_time >= 0.0
                        else 0.0,
                    )
                fallback = True
                source_time = frame_index / fps + fallback_offset

            last_source_time = source_time
            current_index = frame_index
            frame_index += 1

            if source_time < clip_in - 1e-6:
                continue
            if source_time >= clip_out - 1e-6:
                break

            clip_time = max(0.0, source_time - clip_in)
            yield DecodedClipFrame(
                frame_index=current_index,
                source_time=float(source_time),
                clip_time=float(clip_time),
                image=image,
            )
    finally:
        cap.release()
