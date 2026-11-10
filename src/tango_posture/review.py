"""Local report/video associations and durable, report-specific LLM analyses."""

import hashlib
import json
import math
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import uuid4


def report_directory(data_dir: str) -> Path:
    return Path("reports") if data_dir == "data" else Path(data_dir) / "reports"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def write_metadata(
    report: Path,
    source: str,
    raw: Path | None,
    overlay: Path | None,
    timestamps: list[float],
    *,
    extra: dict[str, Any] | None = None,
) -> Path:
    source_video: str | None = None
    if source.startswith("video:"):
        source_video = str(Path(source[6:]).resolve())
    elif source.startswith("import:") and raw is not None:
        source_video = str(raw.resolve())
    data: dict[str, Any] = {
        "version": 1,
        "report_file": report.name,
        "source": source,
        "raw_video": str(raw.resolve()) if raw else None,
        "overlay_video": str(overlay.resolve()) if overlay else None,
        "source_video": source_video,
        "frame_timestamps": [round(float(t), 6) for t in timestamps],
    }
    if extra:
        for k, v in extra.items():
            if k not in ("version", "report_file"):
                data[k] = v
    meta_path = report.with_suffix(".metadata.json")
    _write_json(meta_path, data)
    _write_json(report.parent / "latest_report.metadata.json", data)
    return meta_path


def _local_path(value: str | None, folder: Path) -> Path | None:
    if not value:
        return None
    if value.startswith("file:"):
        url = urlsplit(value)
        if url.netloc not in ("", "localhost"):
            return None
        value = url2pathname(url.path)
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (folder / path).resolve()


@dataclass
class ReportRecord:
    path: Path
    markdown: str
    videos: dict[str, Path]
    timestamps: list[float] = field(default_factory=list)
    warning: str = ""
    archive_path: Path | None = None
    title: str = ""
    source: str = ""
    is_imported_reference: bool = False
    source_sha256: str = ""
    clip_in: float | None = None
    clip_out: float | None = None
    analysis_mode: str = "posture"
    readiness: dict[str, Any] = field(default_factory=dict)
    reference_summary: dict[str, Any] | None = None
    youtube_link: dict[str, Any] | None = None
    facebook_reel: dict[str, Any] | None = None
    youtube_alignment: dict[str, Any] | None = None
    movement_analysis_file: str | None = None
    coach_overlay_file: str | None = None
    coverage: dict[str, Any] = field(default_factory=dict)
    soundtrack: dict[str, Any] = field(default_factory=dict)
    capture_profile: dict[str, Any] = field(default_factory=dict)
    relinked_content_changed: bool = False

    def __post_init__(self) -> None:
        if not self.title:
            self.title = self.path.stem

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.markdown.encode("utf-8")).hexdigest()

    @property
    def recorded_video(self) -> Path | None:
        return (
            self.videos.get("Raw video")
            or self.videos.get("Source video")
            or next(iter(self.videos.values()), None)
        )

    @property
    def clip_range(self) -> tuple[float, float] | None:
        if (
            self.clip_in is not None
            and self.clip_out is not None
            and math.isfinite(self.clip_in)
            and math.isfinite(self.clip_out)
            and self.clip_out > self.clip_in
        ):
            return (float(self.clip_in), float(self.clip_out))
        return None

    @property
    def duration_seconds(self) -> float:
        bounds = self.clip_range
        if bounds is not None:
            return bounds[1] - bounds[0]
        if len(self.timestamps) >= 2:
            return max(0.0, float(self.timestamps[-1] - self.timestamps[0]))
        return 0.0

    def recording_seconds(self, seconds: float, fps: float, stream: str | None = None) -> float:
        """Map a report timestamp to a position in the selected video stream.

        Imported clips log absolute source-video times for `[clip_in, clip_out)`. The raw/source
        video is the whole file, so the time is used directly (clamped into the clip); any
        clip-length overlay video starts at `clip_in`, so that offset is removed.
        """
        bounds = self.clip_range if self.is_imported_reference else None
        if bounds is not None:
            clamped = min(max(seconds, bounds[0]), bounds[1])
            if stream in ("Overlay", "Full lines + messages"):
                return max(0.0, clamped - bounds[0])
            return clamped
        if not self.timestamps:
            return seconds
        index = min(bisect_left(self.timestamps, seconds), len(self.timestamps) - 1)
        return index / fps


def read_report(path: Path) -> ReportRecord:
    path = path.expanduser().resolve()
    text = path.read_text(encoding="utf-8")
    videos: dict[str, Path] = {}
    timestamps: list[float] = []
    warning = ""
    title = ""
    source = ""
    is_imported_reference = False
    source_sha256 = ""
    clip_in: float | None = None
    clip_out: float | None = None
    analysis_mode = "posture"
    readiness: dict[str, Any] = {}
    reference_summary: dict[str, Any] | None = None
    youtube_link: dict[str, Any] | None = None
    facebook_reel: dict[str, Any] | None = None
    youtube_alignment: dict[str, Any] | None = None
    movement_analysis_file: str | None = None
    coach_overlay_file: str | None = None
    coverage: dict[str, Any] = {}
    soundtrack: dict[str, Any] = {}
    capture_profile: dict[str, Any] = {}
    relinked_content_changed = False

    metadata = path.with_suffix(".metadata.json")
    if metadata.exists():
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("unsupported metadata version")
            # Resolve the latest alias to the immutable report it represents.
            name = Path(data["report_file"]).name
            if not name.startswith("report_") or not name.endswith(".md"):
                raise ValueError("invalid report filename")
            original = path.parent / name
            if path.name == "latest_report.md" and original.is_file():
                path, text = original, original.read_text(encoding="utf-8")
            for label, key in (
                ("Raw video", "raw_video"),
                ("Overlay", "overlay_video"),
                ("Source video", "source_video"),
            ):
                value = data.get(key)
                if value is not None and not isinstance(value, str):
                    raise ValueError("invalid video path")
                video = _local_path(value, path.parent)
                if video:
                    videos[label] = video
            times = data.get("frame_timestamps", [])
            if not isinstance(times, list) or any(
                not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0 for t in times
            ):
                raise ValueError("invalid frame timeline")
            if any(a > b for a, b in pairwise(times)):
                raise ValueError("unordered frame timeline")
            timestamps = [float(t) for t in times]

            if isinstance(data.get("title"), str):
                title = data["title"]
            if isinstance(data.get("source"), str):
                source = data["source"]
            is_imported_reference = bool(data.get("is_imported_reference", False))
            if isinstance(data.get("source_sha256"), str):
                source_sha256 = data["source_sha256"]
            raw_in = data.get("clip_in")
            raw_out = data.get("clip_out")
            if isinstance(raw_in, (int, float)) and math.isfinite(raw_in) and raw_in >= 0:
                clip_in = float(raw_in)
            if isinstance(raw_out, (int, float)) and math.isfinite(raw_out) and raw_out > 0:
                clip_out = float(raw_out)
            if isinstance(data.get("analysis_mode"), str):
                analysis_mode = data["analysis_mode"]
            if isinstance(data.get("readiness"), dict):
                readiness = data["readiness"]
            if isinstance(data.get("reference_summary"), dict):
                reference_summary = data["reference_summary"]
            if isinstance(data.get("youtube_link"), dict):
                youtube_link = data["youtube_link"]
            if isinstance(data.get("facebook_reel"), dict):
                facebook_reel = data["facebook_reel"]
            if isinstance(data.get("youtube_alignment"), dict):
                youtube_alignment = data["youtube_alignment"]
            if isinstance(data.get("movement_analysis_file"), str):
                movement_analysis_file = data["movement_analysis_file"]
            if isinstance(data.get("coach_overlay_file"), str):
                coach_overlay_file = data["coach_overlay_file"]
            if isinstance(data.get("coverage"), dict):
                coverage = data["coverage"]
            if isinstance(data.get("soundtrack"), dict):
                soundtrack = data["soundtrack"]
            if isinstance(data.get("capture_profile"), dict):
                capture_profile = data["capture_profile"]
            relinked_content_changed = bool(data.get("relinked_content_changed", False))
            if relinked_content_changed and not warning:
                warning = (
                    "Relinked video content differs from the analysed file; "
                    "prior overlays and YouTube alignments were cleared."
                )
        except (ValueError, OSError, KeyError, TypeError) as error:
            warning = f"Could not read video metadata: {error}. Timestamp seeking is approximate."
            videos = {}
    else:
        warning = "Older report: timestamp seeking uses video time and may be approximate."
    # Existing reports remain readable; only known local video fields are interpreted.
    for label, field_name in (
        ("Raw video", "Saved Raw Video"),
        ("Overlay", "Saved Annotated Overlay Video"),
    ):
        if label not in videos:
            match = re.search(r"\| " + field_name + r" \| .*?\]\((file://.*?)\)", text)
            if match:
                video = _local_path(match[1], path.parent)
                if video:
                    videos[label] = video
    if "Source video" not in videos:
        match = re.search(r"\| Source \| `video:(.*?)` \|", text)
        if match:
            # Legacy source paths were relative to the application's working directory.
            video = _local_path(match[1], Path.cwd())
            if video:
                videos["Source video"] = video
    if not source:
        src_match = re.search(r"\| Source \| `(.*?)` \|", text)
        if src_match:
            source = src_match[1]
    return ReportRecord(
        path=path,
        markdown=text,
        videos=videos,
        timestamps=timestamps,
        warning=warning,
        title=title or path.stem,
        source=source,
        is_imported_reference=is_imported_reference,
        source_sha256=source_sha256,
        clip_in=clip_in,
        clip_out=clip_out,
        analysis_mode=analysis_mode,
        readiness=readiness,
        reference_summary=reference_summary,
        youtube_link=youtube_link,
        facebook_reel=facebook_reel,
        youtube_alignment=youtube_alignment,
        movement_analysis_file=movement_analysis_file,
        coach_overlay_file=coach_overlay_file,
        coverage=coverage,
        soundtrack=soundtrack,
        capture_profile=capture_profile,
        relinked_content_changed=relinked_content_changed,
    )


def update_report_metadata(report: ReportRecord, updates: dict[str, Any]) -> Path:
    """Update specific fields in a report's `.metadata.json` file atomically."""
    meta_path = report.path.with_suffix(".metadata.json")
    data: dict[str, Any] = {
        "version": 1,
        "report_file": report.path.name,
        "raw_video": str(report.videos["Raw video"]) if "Raw video" in report.videos else None,
        "overlay_video": str(report.videos["Overlay"]) if "Overlay" in report.videos else None,
        "source_video": (
            str(report.videos["Source video"]) if "Source video" in report.videos else None
        ),
        "frame_timestamps": report.timestamps,
    }
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("version") == 1:
                data.update(loaded)
        except (OSError, ValueError):
            pass
    for k, v in updates.items():
        if k not in ("version", "report_file"):
            data[k] = v
    _write_json(meta_path, data)
    latest_meta = report.path.parent / "latest_report.metadata.json"
    if latest_meta.is_file():
        try:
            latest_data = json.loads(latest_meta.read_text(encoding="utf-8"))
            if isinstance(latest_data, dict) and latest_data.get("report_file") == report.path.name:
                _write_json(latest_meta, data)
        except (OSError, ValueError):
            pass
    return meta_path


def save_analysis(
    report: ReportRecord,
    model: str,
    endpoint: str,
    instruction: str,
    analysis: str,
    complete: bool = True,
) -> Path:
    if not analysis.strip():
        raise ValueError("Ollama returned an empty analysis")
    now = datetime.now(UTC)
    payload = {
        "version": 1,
        "report_sha256": report.digest,
        "report_file": report.path.name,
        "created_at_utc": now.isoformat(),
        "model": model,
        "endpoint": endpoint,
        "instruction": instruction,
        "analysis": analysis,
        "complete": complete,
    }
    path = report.path.with_name(
        f"{report.path.stem}.analysis-{now:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}.json"
    )
    _write_json(path, payload)
    if report.archive_path is not None:
        from .archive import store_analysis

        store_analysis(report.archive_path, path)
    return path


def saved_analyses(report: ReportRecord) -> list[tuple[Path, dict]]:
    records = []
    for path in sorted(
        report.path.parent.glob(f"{report.path.stem}.analysis-*.json"), reverse=True
    ):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError(f"Unsupported analysis file: {path.name}")
        if data.get("report_sha256") == report.digest:
            if not all(
                isinstance(data.get(key), str)
                for key in ("analysis", "model", "endpoint", "created_at_utc")
            ):
                raise ValueError(f"Invalid analysis file: {path.name}")
            records.append((path, data))
    return records
