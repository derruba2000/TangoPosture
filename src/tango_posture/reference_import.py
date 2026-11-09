"""Offline reference video import, virtual clip range decoding, YouTube provenance,
optional soundtrack beat analysis, and teacher-vs-practice comparison.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import cv2
import numpy as np

from .archive import file_sha256
from .beat import BeatTracker
from .calibration import calibrate, compatible, profile
from .config import Config
from .domain import (
    METRIC_VERSION,
    Calibration,
    FrameObservation,
    Landmark,
    MetricObservation,
    PoseEstimator,
)
from .geometry import (
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
    torso_axis_alignment,
)
from .metrics import measure
from .models import verify
from .movement import (
    analyse_movement_observations,
    validate_movement_analysis,
)
from .overlay import MESSAGES, draw
from .pose.mediapipe_backend import MediaPipeEstimator
from .reports import write_markdown_report
from .review import ReportRecord, update_report_metadata, write_metadata
from .session import Session
from .storage import Storage

MAX_IMPORT_VIDEO_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GB per source file
MAX_IMPORT_RANGE_SECONDS: float = 120.0  # 120 seconds per selected clip range
MIN_EVIDENCE_COVERAGE_RATIO: float = 0.25
MIN_EVIDENCE_DURATION_SECONDS: float = 0.75
SOUNDTRACK_SAMPLE_RATE: int = 16000
SOUNDTRACK_SOURCE_LABEL: str = "Video soundtrack"
SUPPORTED_VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TIME_TOKEN_RE = re.compile(
    r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+(?:\.\d+)?)s?)?$",
    re.IGNORECASE,
)


class ImportCancelledError(RuntimeError):
    """Raised when an offline reference import job is cancelled by the user."""


@dataclass(frozen=True)
class YouTubeSourceLink:
    """Validated, canonicalised YouTube provenance link with optional nonnegative time anchor."""

    canonical_url: str
    video_id: str
    anchor_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_url": self.canonical_url,
            "video_id": self.video_id,
            "anchor_seconds": (
                round(float(self.anchor_seconds), 3)
                if self.anchor_seconds is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> YouTubeSourceLink | None:
        if not isinstance(payload, dict):
            return None
        video_id = str(payload.get("video_id") or "").strip()
        canonical_url = str(payload.get("canonical_url") or "").strip()
        if not _YOUTUBE_ID_RE.match(video_id) or not canonical_url:
            return None
        raw_anchor = payload.get("anchor_seconds")
        anchor: float | None = None
        if isinstance(raw_anchor, int | float) and math.isfinite(raw_anchor) and raw_anchor >= 0:
            anchor = float(raw_anchor)
        return cls(
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
            video_id=video_id,
            anchor_seconds=anchor,
        )


@dataclass(frozen=True)
class YouTubeClockAlignment:
    """Explicit alignment between a local video source timestamp and a YouTube timestamp."""

    local_source_seconds: float
    youtube_seconds: float
    confirmed_same_edit: bool
    source_sha256: str
    youtube_video_id: str

    def is_valid_for(
        self,
        *,
        source_sha256: str,
        youtube_video_id: str,
        clip_in: float,
        clip_out: float,
        local_source_seconds: float | None = None,
    ) -> bool:
        if not self.confirmed_same_edit:
            return False
        if not source_sha256 or not youtube_video_id:
            return False
        if self.source_sha256 != source_sha256 or self.youtube_video_id != youtube_video_id:
            return False
        if not (
            math.isfinite(self.local_source_seconds)
            and math.isfinite(self.youtube_seconds)
            and self.local_source_seconds >= 0.0
            and self.youtube_seconds >= 0.0
        ):
            return False
        if not (clip_in - 1e-6 <= self.local_source_seconds <= clip_out + 1e-6):
            return False
        if local_source_seconds is not None:
            if not math.isfinite(local_source_seconds):
                return False
            if not (clip_in - 1e-6 <= local_source_seconds <= clip_out + 1e-6):
                return False
            mapped = self.youtube_seconds + (local_source_seconds - self.local_source_seconds)
            if mapped < -1e-6:
                return False
        return True

    def youtube_timestamp_at(
        self,
        local_source_seconds: float,
        *,
        source_sha256: str,
        youtube_video_id: str,
        clip_in: float,
        clip_out: float,
    ) -> float | None:
        if not self.is_valid_for(
            source_sha256=source_sha256,
            youtube_video_id=youtube_video_id,
            clip_in=clip_in,
            clip_out=clip_out,
            local_source_seconds=local_source_seconds,
        ):
            return None
        mapped = self.youtube_seconds + (local_source_seconds - self.local_source_seconds)
        return max(0.0, float(mapped))

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_source_seconds": round(float(self.local_source_seconds), 4),
            "youtube_seconds": round(float(self.youtube_seconds), 4),
            "confirmed_same_edit": bool(self.confirmed_same_edit),
            "source_sha256": self.source_sha256,
            "youtube_video_id": self.youtube_video_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> YouTubeClockAlignment | None:
        if not isinstance(payload, dict):
            return None
        try:
            local_sec = float(payload["local_source_seconds"])
            yt_sec = float(payload["youtube_seconds"])
            confirmed = bool(payload.get("confirmed_same_edit", False))
            sha = str(payload.get("source_sha256") or "")
            vid = str(payload.get("youtube_video_id") or "")
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(local_sec) and math.isfinite(yt_sec)):
            return None
        return cls(
            local_source_seconds=local_sec,
            youtube_seconds=yt_sec,
            confirmed_same_edit=confirmed,
            source_sha256=sha,
            youtube_video_id=vid,
        )


def _parse_time_anchor(raw_value: str) -> float:
    text = raw_value.strip()
    if not text:
        raise ValueError("Empty YouTube time anchor")
    if text.startswith("-"):
        raise ValueError("YouTube time anchor must be nonnegative")
    with_numeric: float | None = None
    try:
        with_numeric = float(text)
    except ValueError:
        with_numeric = None
    if with_numeric is not None:
        if not math.isfinite(with_numeric) or with_numeric < 0.0:
            raise ValueError("YouTube time anchor must be finite and nonnegative")
        return float(with_numeric)

    match = _TIME_TOKEN_RE.match(text)
    if not match or not any(match.group(k) for k in ("hours", "minutes", "seconds")):
        raise ValueError(f"Unrecognised YouTube time anchor: {raw_value!r}")
    hours = float(match.group("hours") or 0.0)
    minutes = float(match.group("minutes") or 0.0)
    seconds = float(match.group("seconds") or 0.0)
    total = hours * 3600.0 + minutes * 60.0 + seconds
    if not math.isfinite(total) or total < 0.0:
        raise ValueError("YouTube time anchor must be finite and nonnegative")
    return total


def parse_youtube_url(raw_url: str | None) -> YouTubeSourceLink | None:
    """Validate and canonicalise a user-entered YouTube URL without any network access.

    Accepts recognised HTTPS YouTube URLs (`youtube.com/watch?v=...`, `youtu.be/...`,
    `youtube.com/shorts/...`, including `www.` and `m.` hosts). Rejects lookalike domains,
    embedded credentials, and non-HTTPS schemes. Strips tracking and playlist parameters.
    """
    if raw_url is None:
        return None
    text = raw_url.strip()
    if not text:
        return None

    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError("YouTube link must use https://")
    if parts.username is not None or parts.password is not None:
        raise ValueError("YouTube link must not contain embedded credentials")
    host = (parts.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise ValueError(
            "Unrecognised YouTube domain; use https://www.youtube.com/watch?v=... or https://youtu.be/..."
        )
    if parts.port not in (None, 443):
        raise ValueError("YouTube link must not specify a custom port")

    query = parse_qs(parts.query, keep_blank_values=True)
    path_segments = [seg for seg in parts.path.split("/") if seg]
    video_id = ""
    if host == "youtu.be":
        if len(path_segments) != 1:
            raise ValueError("Short YouTube link must have the form https://youtu.be/<video_id>")
        video_id = path_segments[0]
    else:
        if path_segments == ["watch"]:
            v_vals = query.get("v", [])
            if len(v_vals) != 1:
                raise ValueError("YouTube watch link must include a single v=<video_id> parameter")
            video_id = v_vals[0]
        elif len(path_segments) == 2 and path_segments[0] == "shorts":
            video_id = path_segments[1]
        else:
            raise ValueError(
                "Unsupported YouTube URL path; use /watch?v=<id>, /shorts/<id>, or youtu.be/<id>"
            )

    if not _YOUTUBE_ID_RE.match(video_id):
        raise ValueError("YouTube video ID must be an 11-character identifier")

    anchor_seconds: float | None = None
    for key in ("t", "start"):
        if query.get(key):
            raw_anchor = query[key][0].strip()
            if raw_anchor:
                anchor_seconds = _parse_time_anchor(raw_anchor)
                break

    return YouTubeSourceLink(
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
        anchor_seconds=anchor_seconds,
    )


def build_youtube_open_url(
    link: YouTubeSourceLink,
    *,
    timestamp_seconds: float | None = None,
) -> str:
    """Return the canonical YouTube watch URL with an optional integer timestamp parameter."""
    target = timestamp_seconds if timestamp_seconds is not None else link.anchor_seconds
    if target is not None and math.isfinite(target) and target >= 0.0:
        sec_int = round(max(0.0, float(target)))
        if sec_int > 0:
            return f"{link.canonical_url}&t={sec_int}s"
    return link.canonical_url


def imports_directory(data_dir: str) -> Path:
    """Directory where YouTube-imported videos are stored locally."""
    return Path("videos") / "imports" if data_dir == "data" else Path(data_dir) / "imports"


_YTDLP_FORMAT = "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best"
_YTDLP_MISSING_MESSAGE = (
    "Cannot download from YouTube: 'yt-dlp' is not installed. Install it with "
    "'brew install yt-dlp' or 'uv tool install yt-dlp', or download the MP4 locally "
    "and choose it with 'Choose file…'."
)
_YTDLP_TIMEOUT_SECONDS: float = 1800.0


def _find_ytdlp_command() -> list[str] | None:
    """Locate a `yt-dlp` binary, then `uv tool run yt-dlp`, without any shell execution."""
    candidates = [
        shutil.which("yt-dlp"),
        str(Path(sys.executable).parent / "yt-dlp"),
        "/opt/homebrew/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
        str(Path.home() / ".local" / "bin" / "yt-dlp"),
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return [cand]
    uv_candidates = [
        shutil.which("uv"),
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
        str(Path.home() / ".local" / "bin" / "uv"),
    ]
    for uv_cand in uv_candidates:
        if uv_cand and Path(uv_cand).is_file():
            return [uv_cand, "tool", "run", "yt-dlp"]
    return None


def _partial_download_bytes(folder: Path, video_id: str) -> int:
    total = 0
    for p in folder.glob(f"youtube_{video_id}.*"):
        with suppress(OSError):
            total += p.stat().st_size
    return total


def _discard_partial_downloads(folder: Path, video_id: str, keep: Path | None = None) -> None:
    for p in folder.glob(f"youtube_{video_id}.*"):
        if keep is not None and p == keep:
            continue
        with suppress(OSError):
            p.unlink()


def _download_with_python_module(
    yt_dlp_module: Any,
    link: YouTubeSourceLink,
    target_mp4: Path,
    cancel_check: Callable[[], bool] | None,
) -> None:
    cancelled = False

    def hook(status: dict[str, Any]) -> None:
        nonlocal cancelled
        if cancel_check is not None and cancel_check():
            cancelled = True
            raise ImportCancelledError("YouTube video download was cancelled")
        downloaded = status.get("downloaded_bytes")
        if isinstance(downloaded, int | float) and downloaded > MAX_IMPORT_VIDEO_BYTES:
            raise ValueError("YouTube video exceeds the 2 GB import limit")

    opts: dict[str, Any] = {
        "format": _YTDLP_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(target_mp4),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp_module.YoutubeDL(opts) as ydl:
            ydl.download([link.canonical_url])
    except ImportCancelledError:
        raise
    except Exception as exc:
        if cancelled:
            raise ImportCancelledError("YouTube video download was cancelled") from exc
        raise ValueError(f"YouTube download failed: {exc}") from exc


def _download_with_subprocess(
    cmd_prefix: list[str],
    link: YouTubeSourceLink,
    target_mp4: Path,
    cancel_check: Callable[[], bool] | None,
) -> None:
    cmd = [
        *cmd_prefix,
        "--no-playlist",
        "--no-warnings",
        "-f",
        _YTDLP_FORMAT,
        "--merge-output-format",
        "mp4",
        "-o",
        str(target_mp4),
        link.canonical_url,
    ]
    log_path = target_mp4.parent / f".youtube_{link.video_id}.log"
    try:
        with log_path.open("w+b") as log:
            try:
                proc = subprocess.Popen(
                    cmd,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"Failed to launch YouTube downloader: {exc}") from exc
            started = time.monotonic()
            try:
                while True:
                    try:
                        proc.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if cancel_check is not None and cancel_check():
                        raise ImportCancelledError("YouTube video download was cancelled")
                    if _partial_download_bytes(target_mp4.parent, link.video_id) > (
                        MAX_IMPORT_VIDEO_BYTES
                    ):
                        raise ValueError("YouTube video exceeds the 2 GB import limit")
                    if time.monotonic() - started > _YTDLP_TIMEOUT_SECONDS:
                        raise ValueError("YouTube video download timed out")
            except BaseException:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                _discard_partial_downloads(target_mp4.parent, link.video_id)
                raise
            if proc.returncode != 0:
                log.seek(0)
                text = log.read().decode("utf-8", errors="replace")
                err_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                detail = err_lines[-1] if err_lines else f"exit code {proc.returncode}"
                raise ValueError(f"YouTube download failed: {detail}")
    finally:
        with suppress(OSError):
            log_path.unlink()


def download_youtube_video(
    youtube_url: str,
    output_dir: Path | str,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    downloader: Callable[[str, Path], Path] | None = None,
) -> tuple[Path, YouTubeSourceLink]:
    """Validate `youtube_url` and download the video locally into `output_dir`.

    - Always validates `youtube_url` with `parse_youtube_url` first.
    - Reuses `<output_dir>/youtube_<video_id>.mp4` if an already-downloaded valid video exists.
    - Resolution order: injectable `downloader(canonical_url, target_path) -> Path`, Python
      `yt_dlp`, a local `yt-dlp` binary, then `uv tool run yt-dlp`.
    """
    link = parse_youtube_url(youtube_url)
    if link is None:
        raise ValueError("Enter a valid HTTPS YouTube URL to import")
    out_folder = Path(output_dir)
    out_folder.mkdir(parents=True, exist_ok=True)
    target_mp4 = out_folder / f"youtube_{link.video_id}.mp4"

    if target_mp4.is_file() and target_mp4.stat().st_size > 0:
        with suppress(ValueError):
            probe_import_video(target_mp4)
            return (target_mp4.resolve(), link)

    if cancel_check is not None and cancel_check():
        raise ImportCancelledError("YouTube video download was cancelled")

    if progress_callback is not None:
        progress_callback(2, 100, f"Downloading YouTube video ({link.video_id})…")

    if downloader is not None:
        downloaded = Path(downloader(link.canonical_url, target_mp4))
        if downloaded.is_file() and downloaded.stat().st_size > MAX_IMPORT_VIDEO_BYTES:
            raise ValueError("YouTube video exceeds the 2 GB import limit")
        probe_import_video(downloaded)
        return (downloaded.resolve(), link)

    yt_dlp_module: Any = None
    try:
        import yt_dlp as yt_dlp_module  # type: ignore[import-not-found,no-redef]
    except ImportError:
        yt_dlp_module = None

    if yt_dlp_module is not None:
        _download_with_python_module(yt_dlp_module, link, target_mp4, cancel_check)
    else:
        cmd_prefix = _find_ytdlp_command()
        if cmd_prefix is None:
            raise ValueError(_YTDLP_MISSING_MESSAGE)
        _download_with_subprocess(cmd_prefix, link, target_mp4, cancel_check)

    if cancel_check is not None and cancel_check():
        raise ImportCancelledError("YouTube video download was cancelled")

    result: Path | None = target_mp4 if target_mp4.is_file() else None
    if result is None:
        matches = sorted(
            (
                p
                for p in out_folder.glob(f"youtube_{link.video_id}.*")
                if p.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
            ),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        result = matches[0] if matches else None
    if result is None:
        raise ValueError("YouTube download completed, but no playable local video file was produced")
    if result.stat().st_size > MAX_IMPORT_VIDEO_BYTES:
        result.unlink(missing_ok=True)
        raise ValueError("YouTube video exceeds the 2 GB import limit")
    probe_import_video(result)
    return (result.resolve(), link)


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


@dataclass(frozen=True)
class CalibrationReadiness:
    """Pre-check or outcome for reference-relative posture review."""

    ready: bool
    mode: str  # "movement" | "existing_reference" | "clip_interval"
    summary: str
    reason: str = ""
    reference_id: str | None = None
    reference_name: str | None = None
    interval_source: tuple[float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": bool(self.ready),
            "mode": self.mode,
            "summary": self.summary,
            "reason": self.reason,
            "reference_id": self.reference_id,
            "reference_name": self.reference_name,
            "interval_source": list(self.interval_source) if self.interval_source else None,
        }


def check_calibration_readiness(
    *,
    analysis_mode: str,
    cfg: Config,
    probe: VideoProbeInfo,
    selected_in: float,
    selected_out: float,
    reference: Calibration | None = None,
    calibration_interval: tuple[float, float] | None = None,
    model_hash: str | None = None,
) -> CalibrationReadiness:
    """Check whether reference-relative posture review can run with the chosen inputs."""
    clip_in, clip_out = validate_clip_range(probe, selected_in, selected_out)
    if analysis_mode == "movement":
        return CalibrationReadiness(
            ready=True,
            mode="movement",
            summary="Movement review (no calibration required)",
        )
    if analysis_mode != "posture":
        raise ValueError(f"Unsupported analysis_mode: {analysis_mode!r}")

    if reference is not None:
        resolved_hash = model_hash or (
            verify(Path(cfg.model_path)) if Path(cfg.model_path).is_file() else reference.model_hash
        )
        cap_profile = profile(cfg, probe.width, probe.height, "video")
        try:
            compatible(reference, cap_profile, resolved_hash)
        except ValueError as exc:
            return CalibrationReadiness(
                ready=False,
                mode="existing_reference",
                summary="Existing reference is incompatible with this clip",
                reason=str(exc),
                reference_id=reference.id,
                reference_name=reference.name,
            )
        return CalibrationReadiness(
            ready=True,
            mode="existing_reference",
            summary=f"Ready with reference '{reference.name}' ({reference.id[:8]})",
            reference_id=reference.id,
            reference_name=reference.name,
        )

    if calibration_interval is not None:
        cal_in, cal_out = float(calibration_interval[0]), float(calibration_interval[1])
        if cal_in < clip_in - 1e-6 or cal_out > clip_out + 1e-6 or cal_out <= cal_in:
            return CalibrationReadiness(
                ready=False,
                mode="clip_interval",
                summary="Calibration interval must lie inside the selected clip range",
                reason=f"Calibration interval [{cal_in:.1f}s, {cal_out:.1f}s) is outside [{clip_in:.1f}s, {clip_out:.1f}s)",
                interval_source=(cal_in, cal_out),
            )
        cal_len = cal_out - cal_in
        if cal_len + 1e-6 < cfg.calibration_valid_duration:
            return CalibrationReadiness(
                ready=False,
                mode="clip_interval",
                summary="Calibration interval is too short",
                reason=(
                    f"Selected calibration interval is {cal_len:.1f}s; "
                    f"need at least {cfg.calibration_valid_duration:.1f}s of stable posture"
                ),
                interval_source=(cal_in, cal_out),
            )
        if (clip_out - clip_in) - cal_len < 0.5:
            return CalibrationReadiness(
                ready=False,
                mode="clip_interval",
                summary="Calibration interval leaves no practice frames to score",
                reason="Select a clip range longer than the calibration interval so practice frames remain",
                interval_source=(cal_in, cal_out),
            )
        return CalibrationReadiness(
            ready=True,
            mode="clip_interval",
            summary=f"Ready to calibrate from clip interval [{cal_in:.1f}s, {cal_out:.1f}s)",
            interval_source=(cal_in, cal_out),
        )

    return CalibrationReadiness(
        ready=False,
        mode="existing_reference",
        summary="Reference-relative posture review requires a reference or calibration interval",
        reason="Select a compatible saved reference, mark a stable calibration interval in the clip, or switch to Movement review",
    )


@dataclass(frozen=True)
class SoundtrackAnalysisSummary:
    """Result of optional offline video soundtrack beat and phrase analysis."""

    enabled: bool
    available: bool
    status: str  # "disabled" | "unavailable" | "not_enough_evidence" | "no_events_detected" | "detected"
    source: str = SOUNDTRACK_SOURCE_LABEL
    reason: str = ""
    audio_offset_seconds: float = 0.0
    analyzed_duration_seconds: float = 0.0
    beat_active_duration_seconds: float = 0.0
    estimated_bpm: float | None = None
    counted_beats: int = 0
    phrase_supported: bool = False
    phrase_count: int = 0
    frame_states: dict[int, dict[str, Any]] = field(default_factory=dict)

    def to_metadata_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "available": bool(self.available),
            "status": self.status,
            "source": self.source,
            "reason": self.reason,
            "audio_offset_seconds": round(float(self.audio_offset_seconds), 4),
            "analyzed_duration_seconds": round(float(self.analyzed_duration_seconds), 3),
            "beat_active_duration_seconds": round(float(self.beat_active_duration_seconds), 3),
            "estimated_bpm": (
                round(float(self.estimated_bpm), 2)
                if self.estimated_bpm is not None
                else None
            ),
            "counted_beats": int(self.counted_beats),
            "phrase_supported": bool(self.phrase_supported),
            "phrase_count": int(self.phrase_count),
        }


def _probe_audio_stream_offset(path: Path, ffprobe_bin: str | None) -> tuple[bool, float, str]:
    """Inspect whether the local container has an audio stream and return its start offset."""
    if ffprobe_bin is None:
        return (True, 0.0, "")
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type,start_time",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (False, 0.0, f"Audio stream inspection failed: {exc}")
    if proc.returncode != 0:
        return (False, 0.0, "Video file could not be inspected for audio")
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return (False, 0.0, "Invalid audio stream metadata")
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        return (False, 0.0, "Video file contains no soundtrack audio stream")
    first = streams[0] if isinstance(streams[0], dict) else {}
    raw_start = first.get("start_time")
    offset = 0.0
    if raw_start is not None:
        try:
            parsed = float(raw_start)
            if math.isfinite(parsed):
                offset = parsed
        except (TypeError, ValueError):
            offset = 0.0
    return (True, offset, "")


def _decode_wav_segment(
    wav_path: Path,
    selected_in: float,
    selected_out: float,
) -> tuple[np.ndarray, float]:
    """Decode `[selected_in, selected_out)` from a local PCM WAV file and resample to 16 kHz mono."""
    with wave.open(str(wav_path), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes = wf.getnframes()
        if framerate <= 0 or channels <= 0 or sampwidth not in (1, 2, 4):
            raise ValueError("Unsupported WAV audio format")
        start_frame = min(nframes, max(0, round(selected_in * framerate)))
        end_frame = min(nframes, max(start_frame, round(selected_out * framerate)))
        wf.setpos(start_frame)
        raw_bytes = wf.readframes(end_frame - start_frame)
    if not raw_bytes:
        return (np.zeros(0, dtype=np.float32), 0.0)
    if sampwidth == 2:
        pcm = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        pcm = np.frombuffer(raw_bytes, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        pcm = (np.frombuffer(raw_bytes, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    if framerate != SOUNDTRACK_SAMPLE_RATE and pcm.size > 1:
        duration = pcm.size / float(framerate)
        target_len = max(1, round(duration * SOUNDTRACK_SAMPLE_RATE))
        src_x = np.linspace(0.0, 1.0, num=pcm.size, endpoint=False)
        dst_x = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
        pcm = np.interp(dst_x, src_x, pcm).astype(np.float32)
    return (pcm.astype(np.float32, copy=False), 0.0)


def decode_video_audio_segment(
    video_path: Path,
    selected_in: float,
    selected_out: float,
    *,
    video_first_pts_seconds: float = 0.0,
) -> tuple[np.ndarray | None, float, str]:
    """Decode audio for `[selected_in, selected_out)` into 16 kHz mono float32 samples.

    Uses a local companion `.wav` if present, or invokes a local `ffmpeg` subprocess
    without a shell (`shell=False`, `-nostdin`, bounded runtime and output). Never
    accesses the network or live microphone.
    """
    resolved = Path(video_path)
    if not resolved.is_file():
        return (None, 0.0, "Local video file not found")
    if resolved.stat().st_size > MAX_IMPORT_VIDEO_BYTES:
        return (None, 0.0, "Video file exceeds size limit")

    companion_wav = resolved if resolved.suffix.lower() == ".wav" else resolved.with_suffix(".wav")
    if companion_wav.is_file():
        try:
            samples, offset = _decode_wav_segment(companion_wav, selected_in, selected_out)
            return (samples, offset - video_first_pts_seconds, "")
        except (OSError, ValueError, wave.Error) as exc:
            return (None, 0.0, f"Cannot decode WAV soundtrack: {exc}")

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return (
            None,
            0.0,
            "Local audio decoder (ffmpeg) is not installed; visual analysis remains available",
        )
    ffprobe_bin = shutil.which("ffprobe")
    has_audio, audio_start_sec, probe_reason = _probe_audio_stream_offset(resolved, ffprobe_bin)
    if not has_audio:
        return (None, 0.0, probe_reason or "Video file contains no audio stream")

    duration = max(0.0, float(selected_out - selected_in))
    if duration <= 0.0 or duration > MAX_IMPORT_RANGE_SECONDS + 1.0:
        return (None, 0.0, "Selected clip range is invalid for soundtrack decoding")

    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{selected_in:.4f}",
        "-t",
        f"{duration:.4f}",
        "-i",
        str(resolved),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SOUNDTRACK_SAMPLE_RATE),
        "-f",
        "f32le",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (None, 0.0, "Soundtrack decoding timed out")
    except (OSError, subprocess.SubprocessError) as exc:
        return (None, 0.0, f"Soundtrack decoder failed: {exc}")

    if proc.returncode != 0 or not proc.stdout:
        stderr_txt = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        if (
            "Output file does not contain any stream" in stderr_txt
            or "matches no streams" in stderr_txt
        ):
            return (None, 0.0, "Video file contains no audio stream")
        return (
            None,
            0.0,
            stderr_txt.splitlines()[-1]
            if stderr_txt
            else "Video file contains no decodable audio stream",
        )

    max_bytes = int((duration + 2.0) * SOUNDTRACK_SAMPLE_RATE * 4)
    raw = proc.stdout[: (max_bytes // 4) * 4]
    samples = np.frombuffer(raw, dtype=np.float32).copy()
    stream_offset = float(audio_start_sec - video_first_pts_seconds)
    return (samples, stream_offset, "")


def analyse_video_soundtrack(
    video_path: Path,
    selected_in: float,
    selected_out: float,
    frame_times: list[tuple[int, float, float]],
    cfg: Config,
    *,
    enabled: bool = False,
    video_first_pts_seconds: float = 0.0,
) -> SoundtrackAnalysisSummary:
    """Offline soundtrack beat/phrase analysis for `[selected_in, selected_out)`.

    Never opens the live microphone and never invents nominal clock/tap beats when
    audio is silent, missing, or disabled.
    """
    if not enabled:
        return SoundtrackAnalysisSummary(
            enabled=False,
            available=False,
            status="disabled",
            reason="Soundtrack analysis disabled",
        )

    samples, stream_offset, reason = decode_video_audio_segment(
        video_path,
        selected_in,
        selected_out,
        video_first_pts_seconds=video_first_pts_seconds,
    )
    if samples is None:
        return SoundtrackAnalysisSummary(
            enabled=True,
            available=False,
            status="unavailable",
            reason=reason or "Soundtrack unavailable",
        )
    if samples.size < SOUNDTRACK_SAMPLE_RATE // 2:
        return SoundtrackAnalysisSummary(
            enabled=True,
            available=True,
            status="not_enough_evidence",
            reason="Audio track is shorter than 0.5s in the selected range",
            audio_offset_seconds=stream_offset,
            analyzed_duration_seconds=samples.size / float(SOUNDTRACK_SAMPLE_RATE),
        )

    tracker = BeatTracker(
        bpm=cfg.tango_bpm,
        enable_audio=False,
        require_audio=True,
        analysis_seconds=cfg.beat_analysis_seconds,
    )
    chunk_size = 1024
    frame_states: dict[int, dict[str, Any]] = {}
    sample_cursor = 0
    total_samples = int(samples.size)

    def _advance_audio_to(source_time: float) -> None:
        nonlocal sample_cursor
        rel_audio_sec = (source_time - selected_in) - stream_offset
        target_sample = min(
            total_samples,
            max(0, round(rel_audio_sec * SOUNDTRACK_SAMPLE_RATE)),
        )
        while sample_cursor < target_sample:
            end_idx = min(target_sample, sample_cursor + chunk_size)
            chunk = samples[sample_cursor:end_idx]
            chunk_end_source_time = (
                selected_in + stream_offset + (end_idx / float(SOUNDTRACK_SAMPLE_RATE))
            )
            tracker.feed_samples(chunk, chunk_end_source_time)
            sample_cursor = end_idx

    beat_active_frames = 0
    phrase_locked_frames = 0
    counted_beat_transitions = 0
    last_beat_num = 0
    max_phrase_seen = 0
    bpms: list[float] = []

    for frame_index, source_time, clip_time in frame_times:
        _advance_audio_to(source_time)
        state = tracker.state_at(source_time)
        playing = bool(state.get("playing", False))
        analyzing = bool(state.get("analyzing", False))
        beat_num = int(state.get("beat", 0) or 0) if playing else 0
        phrase_supported_now = bool(playing and not analyzing and beat_num > 0)
        phrase_num = int(state.get("phrase", 0) or 0) if phrase_supported_now else None
        bpm_val = float(state["bpm"]) if playing and state.get("bpm") is not None else None
        if playing and beat_num > 0:
            beat_active_frames += 1
            if bpm_val is not None:
                bpms.append(bpm_val)
            if beat_num != last_beat_num:
                counted_beat_transitions += 1
                last_beat_num = beat_num
        else:
            last_beat_num = 0
        if phrase_supported_now and phrase_num is not None and phrase_num > 0:
            phrase_locked_frames += 1
            max_phrase_seen = max(max_phrase_seen, phrase_num)

        frame_states[frame_index] = {
            "source_time": round(float(source_time), 4),
            "clip_time": round(float(clip_time), 4),
            "playing": playing,
            "analyzing": analyzing,
            "beat": beat_num if beat_num > 0 else None,
            "phrase": phrase_num,
            "bpm": round(bpm_val, 2) if bpm_val is not None else None,
            "source": SOUNDTRACK_SOURCE_LABEL if playing else "silent",
        }

    clip_duration = max(0.0, float(selected_out - selected_in))
    fps_est = len(frame_times) / clip_duration if clip_duration > 0 and frame_times else 30.0
    beat_active_duration = beat_active_frames / max(fps_est, 1.0)
    phrase_supported = phrase_locked_frames > 0 and counted_beat_transitions >= 4

    if beat_active_frames == 0:
        status = "no_events_detected" if clip_duration >= 1.5 else "not_enough_evidence"
        status_reason = (
            "Soundtrack is silent or contains no clear rhythmic onsets in the selected range"
            if status == "no_events_detected"
            else "Selected range is too short to establish rhythmic beat evidence"
        )
    elif counted_beat_transitions < 2:
        status = "not_enough_evidence"
        status_reason = "Fewer than 2 rhythmic beats detected in the selected range"
    else:
        status = "detected"
        status_reason = (
            ""
            if phrase_supported
            else "Beat pulse detected; phrase labels withheld (insufficient phrase evidence)"
        )

    return SoundtrackAnalysisSummary(
        enabled=True,
        available=True,
        status=status,
        source=SOUNDTRACK_SOURCE_LABEL,
        reason=status_reason,
        audio_offset_seconds=stream_offset,
        analyzed_duration_seconds=samples.size / float(SOUNDTRACK_SAMPLE_RATE),
        beat_active_duration_seconds=beat_active_duration,
        estimated_bpm=float(np.median(bpms)) if bpms else None,
        counted_beats=counted_beat_transitions,
        phrase_supported=phrase_supported,
        phrase_count=max_phrase_seen if phrase_supported else 0,
        frame_states=frame_states,
    )


def save_movement_companion(path: Path, data: dict[str, Any]) -> Path:
    """Save a validated movement analysis JSON companion file."""
    validate_movement_analysis(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return path


def load_movement_companion(path: Path) -> dict[str, Any] | None:
    """Load and validate a movement analysis JSON companion file if it exists."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return validate_movement_analysis(raw)
    except (OSError, ValueError, TypeError):
        return None


def save_coach_companion(path: Path, data: dict[str, Any]) -> Path:
    """Save a per-frame Open Practice Coach overlay and insights companion file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8"
    )
    return path


def load_coach_companion(path: Path) -> dict[str, Any] | None:
    """Load a per-frame Open Practice Coach companion file if it exists and is valid."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("frames"), dict):
            return None
        return raw
    except (OSError, ValueError, TypeError):
        return None


def load_report_coach_data(report: ReportRecord) -> dict[str, Any] | None:
    """Load the per-frame coach overlay & insights JSON associated with a report if valid."""
    if report.relinked_content_changed:
        return None
    candidates: list[Path] = []
    coach_file = getattr(report, "coach_overlay_file", None)
    if coach_file:
        candidates.append(report.path.parent / coach_file)
    candidates.append(report.path.parent / f"{report.path.stem}.coach.v1.json")
    for cand in candidates:
        data = load_coach_companion(cand)
        if data is not None:
            if report.source_sha256 and data.get("source_sha256") != report.source_sha256:
                continue
            return data
    return None


def render_coach_frame_overlay(
    image: np.ndarray,
    coach_data: dict[str, Any],
    frame_index: int,
    cfg: Config,
    *,
    verbose_skeleton: bool = True,
    show_vertical_axis: bool = True,
    show_footer: bool = False,
) -> np.ndarray:
    """Render the Open Practice Coach HUD (axis lines, skeleton, alerts, role, beats) on a frame.

    Uses the per-frame state saved by `analyse_imported_clip` and the live-coach `overlay.draw()`.
    Returns `image` unchanged when the frame has no saved coach state or sizes disagree.
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

    poses: list[dict[str, Landmark]] = []
    raw_poses = entry.get("poses")
    if not isinstance(raw_poses, list):
        raw_poses = [entry.get("pose")]
    for raw_pose in raw_poses:
        if not isinstance(raw_pose, dict) or not raw_pose:
            continue
        pose_dict: dict[str, Landmark] = {}
        for name, coords in raw_pose.items():
            if isinstance(coords, list | tuple) and len(coords) >= 3:
                pose_dict[str(name)] = Landmark(
                    x=float(coords[0]),
                    y=float(coords[1]),
                    z=float(coords[2]),
                    visibility=(
                        float(coords[3]) if len(coords) >= 4 and coords[3] is not None else None
                    ),
                    presence=(
                        float(coords[4]) if len(coords) >= 5 and coords[4] is not None else None
                    ),
                )
        if pose_dict:
            poses.append(pose_dict)

    def _opt(key: str) -> float | None:
        value = entry.get(key)
        return float(value) if isinstance(value, int | float) else None

    observation = FrameObservation(
        timestamp=float(entry.get("timestamp", 0.0)), width=width, height=height, poses=poses
    )
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


@dataclass(frozen=True)
class ImportedClipResult:
    """Complete result of importing and analysing a reference video clip."""

    report_path: Path
    metadata_path: Path
    movement_path: Path
    title: str
    video_path: Path
    source_sha256: str
    selected_in: float
    selected_out: float
    analysis_mode: str
    readiness: CalibrationReadiness
    reference: Calibration | None
    movement_data: dict[str, Any]
    soundtrack: SoundtrackAnalysisSummary
    coverage: dict[str, FamilyCoverage]
    youtube_link: YouTubeSourceLink | None = None
    youtube_alignment: YouTubeClockAlignment | None = None
    coach_path: Path | None = None
    coach_data: dict[str, Any] | None = None


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
        primary = next(MESSAGES[r["metric"]] for r in session_rows if r.get("state") == "DEVIATION")
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
    if single_pose_ok:
        torso = torso_axis_alignment(
            frame_obs.poses[0], frame_obs.width, frame_obs.height, cfg.confidence
        )
        if torso is not None:
            torso_txt = f"{abs(torso[0]):.1f} deg from vertical"
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
    analysis_mode: str = "movement",
    reference: Calibration | None = None,
    calibration_interval: tuple[float, float] | None = None,
    analyse_soundtrack: bool = False,
    estimator_factory: Callable[[], PoseEstimator] | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    youtube_downloader: Callable[[str, Path], Path] | None = None,
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
    downloaded_from_youtube = False
    if not video_path or not str(video_path).strip():
        if yt_link is None:
            raise ValueError("Choose a local video file or enter a YouTube link to analyse")
        downloaded_path, _ = download_youtube_video(
            yt_link.canonical_url,
            imports_directory(cfg.data_dir),
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            downloader=youtube_downloader,
        )
        video_path = downloaded_path
        downloaded_from_youtube = True

    probe = probe_import_video(Path(video_path))
    if (
        yt_link is not None
        and not downloaded_from_youtube
        and probe.path.name == f"youtube_{yt_link.video_id}.mp4"
        and probe.path.parent.resolve() == imports_directory(cfg.data_dir).resolve()
    ):
        downloaded_from_youtube = True
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
    elif downloaded_from_youtube:
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
            t0 = time.monotonic()
            obs = est.estimate(item.image, item.source_time)
            infer_times.append(time.monotonic() - t0)
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
        observation = measure(frame_obs, cfg, view_range if not in_cal else None)
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

    performance = {
        "model_hash": model_hash,
        "resolution": [probe.width, probe.height],
        "processed_fps": (1.0 / float(np.mean(infer_times))) if infer_times else probe.fps,
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
        coach_path=companion_coach_path,
        coach_data=coach_data,
    )


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


def load_report_movement_data(report: ReportRecord) -> dict[str, Any] | None:
    """Load the movement analysis JSON associated with a report if valid."""
    if report.relinked_content_changed:
        return None
    candidates: list[Path] = []
    if report.movement_analysis_file:
        candidates.append(report.path.parent / report.movement_analysis_file)
    candidates.append(report.path.parent / f"{report.path.stem}.movement.v1.json")
    for cand in candidates:
        data = load_movement_companion(cand)
        if data is not None:
            if report.source_sha256 and data.get("source_sha256") != report.source_sha256:
                continue
            return data
    return None


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


class ReferenceImportWorker:
    """Cancellable background worker for importing and analysing reference video clips."""

    def __init__(self, event_sink: Callable[[tuple[Any, ...]], None]) -> None:
        self.event_sink = event_sink
        self._lock = threading.Lock()
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._active_job_id: str | None = None

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, **kwargs: Any) -> str:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("A reference video import is already running")
            job_id = uuid4().hex
            cancel_ev = threading.Event()
            self._cancel = cancel_ev
            self._active_job_id = job_id

            def _run() -> None:
                try:
                    result = analyse_imported_clip(
                        **kwargs,
                        progress_callback=lambda cur, tot, msg: self.event_sink(
                            ("import_progress", job_id, cur, tot, msg)
                        ),
                        cancel_check=cancel_ev.is_set,
                    )
                    if cancel_ev.is_set():
                        self.event_sink(("import_cancelled", job_id))
                    else:
                        self.event_sink(("import_done", job_id, result))
                except ImportCancelledError:
                    self.event_sink(("import_cancelled", job_id))
                except Exception as exc:  # noqa: BLE001 - worker boundary reports error to UI
                    if cancel_ev.is_set():
                        self.event_sink(("import_cancelled", job_id))
                    else:
                        self.event_sink(("import_error", job_id, str(exc)))

            thread = threading.Thread(target=_run, name="reference-import-worker", daemon=True)
            self._thread = thread
            thread.start()
            return job_id

    def start_download(
        self,
        youtube_url: str,
        output_dir: Path,
        *,
        downloader: Callable[[str, Path], Path] | None = None,
    ) -> str:
        """Download a YouTube video in the background without analysing it."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("A reference video import is already running")
            job_id = uuid4().hex
            cancel_ev = threading.Event()
            self._cancel = cancel_ev
            self._active_job_id = job_id

            def _run() -> None:
                try:
                    path, link = download_youtube_video(
                        youtube_url,
                        output_dir,
                        progress_callback=lambda cur, tot, msg: self.event_sink(
                            ("import_progress", job_id, cur, tot, msg)
                        ),
                        cancel_check=cancel_ev.is_set,
                        downloader=downloader,
                    )
                    if cancel_ev.is_set():
                        self.event_sink(("import_cancelled", job_id))
                    else:
                        self.event_sink(("youtube_downloaded", job_id, path, link))
                except ImportCancelledError:
                    self.event_sink(("import_cancelled", job_id))
                except Exception as exc:  # noqa: BLE001 - worker boundary reports error to UI
                    if cancel_ev.is_set():
                        self.event_sink(("import_cancelled", job_id))
                    else:
                        self.event_sink(("import_error", job_id, str(exc)))

            thread = threading.Thread(target=_run, name="youtube-download-worker", daemon=True)
            self._thread = thread
            thread.start()
            return job_id

    def cancel(self) -> bool:
        with self._lock:
            if self._cancel is not None:
                self._cancel.set()
                return True
            return False

    def close(self) -> None:
        self.cancel()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
