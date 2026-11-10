"""Portable practice archives and temporary, validated replay extraction."""

import hashlib
import json
import shutil
import stat
import sys
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

if sys.platform != "win32":
    import fcntl
else:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .domain import Calibration

_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()

def _replace_archive(temporary: Path, destination: Path) -> None:
    # Windows readers and file scanners may briefly deny an otherwise valid replacement.
    for delay in (0.025, 0.05, 0.1, 0.2, 0.4):
        try:
            temporary.replace(destination)
            return
        except PermissionError as error:
            if sys.platform != "win32" or getattr(error, "winerror", None) not in (5, 32, 33):
                raise
            time.sleep(delay)
    temporary.replace(destination)


@contextmanager
def _archive_lock(path: Path):
    resolved = path.resolve()
    with _locks_guard:
        thread_lock = _locks.setdefault(resolved, threading.RLock())
    with thread_lock:
        lock_file = None
        if fcntl is not None:
            with suppress(OSError):
                resolved.parent.mkdir(parents=True, exist_ok=True)
                lock_path = resolved.with_name(f".{resolved.name}.lock")
                lock_file = lock_path.open("a+", encoding="utf-8")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if lock_file is not None and fcntl is not None:
                with suppress(OSError):
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                with suppress(OSError):
                    lock_file.close()


def movement_member_name(index: int) -> str:
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("Invalid practice segment index")
    return f"movement_{index:02d}.v1.json"


def practice_directory(data_dir: str) -> Path:
    return Path("practice") if data_dir == "data" else Path(data_dir) / "practice"


def calibration_directory(data_dir: str) -> Path:
    return Path("calibrations") if data_dir == "data" else Path(data_dir) / "calibrations"


def calibration_path(folder: Path, name: str) -> Path:
    return practice_path(folder, name.removesuffix(".tpc")).with_suffix(".tpc")


def practice_path(folder: Path, name: str) -> Path:
    name = name.strip()
    if not name or name in (".", "..") or any(char in name for char in '/\\\x00'):
        raise ValueError("Give the practice a name without path separators")
    return folder / f"{name.removesuffix('.tp')}.tp"


def read_manifest(path: Path) -> dict:
    try:
        with ZipFile(path) as archive:
            data = json.loads(archive.read("manifest.json"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported practice archive version")
            segments = data.get("segments")
            if not isinstance(segments, list):
                raise TypeError("Invalid practice segment list")
            if path.suffix.lower() == ".tpc" and (
                data.get("kind") != "calibration" or data.get("reference") != "reference.json"
                or "reference.json" not in archive.namelist() or len(segments) != 1
            ):
                raise ValueError("Invalid calibration archive")
            for index, segment in enumerate(segments):
                if not isinstance(segment, dict) or segment.get("index") != index:
                    raise ValueError("Invalid practice segment index")
                for key, prefix, suffix in (
                    ("video", "video", ".mp4"), ("overlay", "overlay", ".mp4"),
                    ("report", "report", ".md"), ("metadata", "report", ".metadata.json"),
                ):
                    expected = f"{prefix}_{index:02d}{suffix}"
                    if key in ("video", "overlay") and segment.get(key) is None:
                        continue
                    if segment.get(key) != expected or expected not in archive.namelist():
                        raise ValueError(f"Missing practice segment file: {expected}")
            return data
    except (BadZipFile, KeyError, UnicodeError, TypeError) as error:
        raise ValueError(f"Invalid practice archive: {path.name}") from error


def write_review_companions(output, overlay, name):
    for suffix in ("_lines.mp4", ".notes.jsonl"):
        source = overlay.with_name(overlay.stem + suffix)
        if source.is_file():
            output.write(source, Path(name).stem + suffix, compress_type=ZIP_STORED)


def append_segment(path: Path, report: Path, video: Path | None, overlay: Path | None,
                   timestamps: list[float]) -> int:
    with _archive_lock(path):
        return _append_segment(path, report, video, overlay, timestamps)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_member_sha256(archive: ZipFile, member_name: str, destination: Path | None = None) -> str:
    digest = hashlib.sha256()
    with archive.open(member_name) as source:
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
        else:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def extract_segment_video(path: Path, index: int, destination: Path) -> str:
    with _archive_lock(path):
        manifest = read_manifest(path)
        if manifest.get("kind") == "calibration":
            raise ValueError("Calibration archives do not support movement analysis")
        segments = manifest["segments"]
        if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < len(segments)):
            raise ValueError(f"Recording {index:02d} not found in archive")
        video_member = segments[index].get("video")
        if not video_member:
            raise ValueError("Raw video required")
        with ZipFile(path) as archive:
            if video_member not in archive.namelist():
                raise ValueError("Raw video required")
            return _stream_member_sha256(archive, str(video_member), destination)


def store_movement_analysis(
    path: Path, index: int, expected_video_sha256: str, payload: dict[str, Any]
) -> str:
    member_name = movement_member_name(index)
    content = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    with _archive_lock(path):
        manifest = read_manifest(path)
        if manifest.get("kind") == "calibration":
            raise ValueError("Calibration archives do not support movement analysis")
        segments = manifest["segments"]
        if not (0 <= index < len(segments)):
            raise ValueError(f"Recording {index:02d} not found in archive")
        video_member = segments[index].get("video")
        if not video_member:
            raise ValueError("Raw video required")
        with ZipFile(path) as current_archive:
            if video_member not in current_archive.namelist():
                raise ValueError("Raw video required")
            actual_sha256 = _stream_member_sha256(current_archive, str(video_member))
        if actual_sha256 != expected_video_sha256:
            raise ValueError("Archived raw video changed before movement analysis could be committed")
        with NamedTemporaryFile(dir=path.parent, suffix=".tp.tmp", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with (
                ZipFile(path) as previous,
                ZipFile(temporary, "w", compression=ZIP_DEFLATED) as output,
            ):
                for member in previous.infolist():
                    if member.filename == member_name:
                        continue
                    with previous.open(member) as source, output.open(member, "w") as target:
                        shutil.copyfileobj(source, target)
                output.writestr(member_name, content)
            _replace_archive(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return member_name


def read_movement_analysis(path: Path, index: int) -> dict[str, Any] | None:
    member_name = movement_member_name(index)
    if path.is_dir():
        target = path / member_name
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise ValueError(f"Corrupt movement analysis {member_name}: {error}") from error
        if not isinstance(data, dict):
            raise ValueError(f"Invalid movement analysis {member_name}")
        return data
    with _archive_lock(path):
        read_manifest(path)
        try:
            with ZipFile(path) as archive:
                if member_name not in archive.namelist():
                    return None
                raw_bytes = archive.read(member_name)
        except (BadZipFile, KeyError, OSError) as error:
            raise ValueError(f"Cannot read movement analysis {member_name}: {error}") from error
    if len(raw_bytes) > 10 * 1024 * 1024:
        raise ValueError(f"Movement analysis {member_name} exceeds maximum size")
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as error:
        raise ValueError(f"Corrupt movement analysis {member_name}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"Invalid movement analysis {member_name}")  # noqa: TRY004
    return data


def _append_segment(path: Path, report: Path, video: Path | None, overlay: Path | None,
                    timestamps: list[float]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = read_manifest(path) if path.exists() else {
        "version": 1, "name": path.stem, "segments": [],
    }
    segments_list: list[dict[str, Any]] = manifest["segments"]
    index = len(segments_list)
    video_name = f"video_{index:02d}.mp4" if video else None
    overlay_name = f"overlay_{index:02d}.mp4" if overlay else None
    report_name = f"report_{index:02d}.md"
    metadata_name = f"report_{index:02d}.metadata.json"
    segment: dict[str, Any] = {
        "index": index, "video": video_name,
        "overlay": overlay_name,
        "report": report_name,
        "metadata": metadata_name,
    }
    metadata = {
        "version": 1, "report_file": report_name,
        "raw_video": video_name, "overlay_video": overlay_name,
        "source_video": None, "frame_timestamps": timestamps,
    }
    markdown = report.read_text(encoding="utf-8")
    for src_path, replacement_name in ((video, video_name), (overlay, overlay_name)):
        if src_path is None or replacement_name is None:
            continue
        markdown = markdown.replace(src_path.resolve().as_uri(), replacement_name)
        markdown = markdown.replace(str(src_path.resolve()), replacement_name)
        markdown = markdown.replace(str(src_path), replacement_name)
    segments_list.append(segment)
    with NamedTemporaryFile(dir=path.parent, suffix=".tp.tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as output:
            if path.exists():
                with ZipFile(path) as previous:
                    for member in previous.infolist():
                        if member.filename == "manifest.json":
                            continue
                        with previous.open(member) as member_src, output.open(member, "w") as target:
                            shutil.copyfileobj(member_src, target)
            if video and video_name:
                output.write(video, video_name, compress_type=ZIP_STORED)
            if overlay and overlay_name:
                output.write(overlay, overlay_name, compress_type=ZIP_STORED)
                write_review_companions(output, overlay, overlay_name)
            output.writestr(report_name, markdown)
            output.writestr(metadata_name, json.dumps(metadata, allow_nan=False))
            output.writestr("manifest.json", json.dumps(manifest, allow_nan=False))
        _replace_archive(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return index


def save_calibration_archive(path: Path, reference: Calibration, video: Path,
                             overlay: Path, timestamps: list[float]) -> None:
    video_name = "video_00.mp4"
    overlay_name = "overlay_00.mp4"
    report_name = "report_00.md"
    metadata_name = "report_00.metadata.json"
    segment: dict[str, Any] = {
        "index": 0, "video": video_name, "overlay": overlay_name,
        "report": report_name, "metadata": metadata_name,
    }
    manifest = {
        "version": 1, "kind": "calibration", "name": reference.name,
        "reference": "reference.json", "segments": [segment],
    }
    metadata = {
        "version": 1, "report_file": report_name, "raw_video": video_name,
        "overlay_video": overlay_name, "source_video": None,
        "frame_timestamps": timestamps,
    }
    report = (
        f"# Calibration: {reference.name}\n\n"
        f"Created: {reference.created_at_utc}\n\n"
        "## Reference baseline\n\n| Metric | Value |\n| --- | --- |\n"
        + "".join(f"| {metric} | {value} |\n" for metric, value in reference.baseline.items())
        + "\n## Calibration reference JSON\n\n```json\n"
        + json.dumps(asdict(reference), indent=2, allow_nan=False) + "\n```\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with _archive_lock(path):
        with NamedTemporaryFile(dir=path.parent, suffix=".tpc.tmp", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as output:
                output.write(video, video_name, compress_type=ZIP_STORED)
                output.write(overlay, overlay_name, compress_type=ZIP_STORED)
                write_review_companions(output, overlay, overlay_name)
                output.writestr("reference.json", json.dumps(asdict(reference), allow_nan=False))
                output.writestr(report_name, report)
                output.writestr(metadata_name, json.dumps(metadata, allow_nan=False))
                output.writestr("manifest.json", json.dumps(manifest, allow_nan=False))
            _replace_archive(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_calibration_archive(path: Path) -> Calibration:
    read_manifest(path)
    try:
        with ZipFile(path) as archive:
            return Calibration(**json.loads(archive.read("reference.json")))
    except (BadZipFile, KeyError, TypeError) as error:
        raise ValueError("Invalid calibration reference JSON") from error


def store_analysis(path: Path, analysis: Path) -> None:
    with _archive_lock(path):
        _store_analysis(path, analysis)


def _store_analysis(path: Path, analysis: Path) -> None:
    read_manifest(path)
    with NamedTemporaryFile(dir=path.parent, suffix=".tp.tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(path) as previous, ZipFile(temporary, "w", compression=ZIP_DEFLATED) as output:
            for member in previous.infolist():
                if member.filename == analysis.name:
                    continue
                with previous.open(member) as source, output.open(member, "w") as target:
                    shutil.copyfileobj(source, target)
            output.write(analysis, analysis.name)
        _replace_archive(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PracticeArchive:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.temporary = TemporaryDirectory(prefix="tango-replay-")
        self.folder = Path(self.temporary.name)
        try:
            with _archive_lock(self.path), ZipFile(self.path) as archive:
                self.manifest = read_manifest(self.path)
                names = set()
                for member in archive.infolist():
                    name = PurePosixPath(member.filename)
                    mode = member.external_attr >> 16
                    if (
                        name.is_absolute() or ".." in name.parts or "\\" in member.filename
                        or stat.S_ISLNK(mode) or member.filename in names
                    ):
                        raise ValueError("Unsafe path in practice archive")
                    names.add(member.filename)
                    target = self.folder / member.filename
                    if not target.resolve().is_relative_to(self.folder.resolve()):
                        raise ValueError("Unsafe path in practice archive")
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
        except Exception:
            self.close()
            raise

    def report(self, index: int) -> Path:
        return self.folder / self.manifest["segments"][index]["report"]

    def close(self) -> None:
        self.temporary.cleanup()