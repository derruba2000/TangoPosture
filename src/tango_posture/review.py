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
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import uuid4


def report_directory(data_dir: str) -> Path:
    return Path("reports") if data_dir == "data" else Path(data_dir) / "reports"


def _write_json(path: Path, payload: dict):
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
    report: Path, source: str, raw: Path | None, overlay: Path | None, timestamps: list[float]
) -> None:
    data = {
        "version": 1,
        "report_file": report.name,
        "raw_video": str(raw.resolve()) if raw else None,
        "overlay_video": str(overlay.resolve()) if overlay else None,
        "source_video": str(Path(source[6:]).resolve()) if source.startswith("video:") else None,
        "frame_timestamps": timestamps,
    }
    _write_json(report.with_suffix(".metadata.json"), data)
    _write_json(report.parent / "latest_report.metadata.json", data)


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

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.markdown.encode("utf-8")).hexdigest()

    def recording_seconds(self, seconds: float, fps: float) -> float:
        if not self.timestamps:
            return seconds
        index = min(bisect_left(self.timestamps, seconds), len(self.timestamps) - 1)
        return index / fps


def read_report(path: Path) -> ReportRecord:
    path = path.expanduser().resolve()
    text = path.read_text(encoding="utf-8")
    videos: dict[str, Path] = {}
    timestamps = []
    warning = ""
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
            timestamps = times
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
    return ReportRecord(path, text, videos, timestamps, warning)


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
