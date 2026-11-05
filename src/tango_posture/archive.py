"""Portable practice archives and temporary, validated replay extraction."""

import json
import shutil
import stat
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile


def practice_directory(data_dir: str) -> Path:
    return Path("practice") if data_dir == "data" else Path(data_dir) / "practice"


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
                raise ValueError("Invalid practice segment list")
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
    except (BadZipFile, KeyError, UnicodeError) as error:
        raise ValueError(f"Invalid practice archive: {path.name}") from error


def append_segment(path: Path, report: Path, video: Path | None, overlay: Path | None,
                   timestamps: list[float]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(path) if path.exists() else {
        "version": 1, "name": path.stem, "segments": [],
    }
    index = len(manifest["segments"])
    segment = {
        "index": index, "video": f"video_{index:02d}.mp4" if video else None,
        "overlay": f"overlay_{index:02d}.mp4" if overlay else None,
        "report": f"report_{index:02d}.md",
        "metadata": f"report_{index:02d}.metadata.json",
    }
    metadata = {
        "version": 1, "report_file": segment["report"],
        "raw_video": segment["video"], "overlay_video": segment["overlay"],
        "source_video": None, "frame_timestamps": timestamps,
    }
    markdown = report.read_text(encoding="utf-8")
    for source, key in ((video, "video"), (overlay, "overlay")):
        if source is None:
            continue
        markdown = markdown.replace(source.resolve().as_uri(), segment[key])
        markdown = markdown.replace(str(source.resolve()), segment[key])
    manifest["segments"].append(segment)
    with NamedTemporaryFile(dir=path.parent, suffix=".tp.tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as output:
            if path.exists():
                with ZipFile(path) as previous:
                    for member in previous.infolist():
                        if member.filename == "manifest.json":
                            continue
                        with previous.open(member) as source, output.open(member, "w") as target:
                            shutil.copyfileobj(source, target)
            if video:
                output.write(video, segment["video"], compress_type=ZIP_STORED)
            if overlay:
                output.write(overlay, segment["overlay"], compress_type=ZIP_STORED)
            output.writestr(segment["report"], markdown)
            output.writestr(segment["metadata"], json.dumps(metadata, allow_nan=False))
            output.writestr("manifest.json", json.dumps(manifest, allow_nan=False))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return index


class PracticeArchive:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.manifest = read_manifest(self.path)
        self.temporary = TemporaryDirectory(prefix="tango-replay-")
        self.folder = Path(self.temporary.name)
        try:
            with ZipFile(self.path) as archive:
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