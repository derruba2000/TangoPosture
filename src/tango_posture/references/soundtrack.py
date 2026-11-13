"""Optional offline soundtrack decoding and beat evidence."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..beat import BeatTracker
from ..config import Config
from .constants import (
    MAX_IMPORT_RANGE_SECONDS,
    MAX_IMPORT_VIDEO_BYTES,
    SOUNDTRACK_SAMPLE_RATE,
    SOUNDTRACK_SOURCE_LABEL,
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
