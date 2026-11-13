"""Portable reference recordings, distinct from practice and calibration profiles."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from ..archive import (
    PracticeArchive,
    _archive_lock,
    _rewrite_members,
    append_segment,
    file_sha256,
    practice_path,
    read_analytics,
    read_manifest,
)
from ..replay import VideoReader, save_overlay_video
from .errors import ImportCancelledError


def reference_directory(data_dir: str) -> Path:
    return Path("references") if data_dir == "data" else Path(data_dir) / "references"


def reference_path(folder: Path, name: str) -> Path:
    return practice_path(folder, name.removesuffix(".tpr")).with_suffix(".tpr")


def mark_reference(path: Path, name: str, details: dict) -> None:
    with _archive_lock(path):
        manifest = read_manifest(path)
        manifest.update(kind="reference", reference="reference.json", name=name)
        _rewrite_members(path, {
            "manifest.json": json.dumps(manifest, allow_nan=False).encode(),
            "reference.json": json.dumps({
                "schema_version": 1, "name": name, "selected_dancer": None, **details,
            }, allow_nan=False).encode(),
        })


def promote_reference(
    source: Path, destination: Path, name: str, index: int = 0, *,
    start_frame: int = 0, end_frame: int | None = None,
    cancel_check: Callable[[], bool] = lambda: False,
) -> Path:
    if destination.suffix.lower() != ".tpr":
        raise ValueError("Reference recordings must use .tpr")
    if destination.exists():
        raise FileExistsError("Choose a new reference name; existing references are not overwritten")
    with _archive_lock(source):
        source_hash = file_sha256(source)
        replay = PracticeArchive(source)
    try:
        if not 0 <= index < len(replay.manifest["segments"]):
            raise ValueError("Selected recording not found")
        segment = replay.manifest["segments"][index]
        if not segment.get("video"):
            raise ValueError("Promoting a reference requires the original video recording")
        metadata = json.loads((replay.folder / segment["metadata"]).read_text())
        timestamps = metadata.get("frame_timestamps", [])
        excerpt = start_frame != 0 or end_frame is not None
        analytics = read_analytics(replay.folder, index)
        with TemporaryDirectory(prefix="tango-reference-") as directory:
            temporary = Path(directory)
            media: dict[str, Path] = {}
            for key in ("video", "overlay", "lines"):
                member = segment.get(key)
                if key == "lines" and member is None and segment.get("overlay"):
                    legacy = f"{Path(segment['overlay']).stem}_lines.mp4"
                    if (replay.folder / legacy).is_file():
                        member = legacy
                if member is None:
                    continue
                original = replay.folder / member
                if excerpt:
                    reader = VideoReader(original)
                    count = reader.count
                    reader.close()
                    end = count if end_frame is None else end_frame
                    if not 0 <= start_frame < end <= count:
                        raise ValueError("Selected excerpt is outside the recording")
                    output = temporary / f"{key}.mp4"
                    def transform(image, _index, _fps):
                        if cancel_check():
                            raise ImportCancelledError("Reference creation cancelled")
                        return image
                    save_overlay_video(original, output, start_frame, end - 1, frame_transform=transform)
                    media[key] = output
                else:
                    media[key] = original
            selected_times = timestamps[start_frame:end_frame]
            if analytics is not None:
                analytics = copy.deepcopy(analytics)
                analytics["segment_index"] = 0
                analytics["frame_timestamps"] = selected_times
                if "frames" in analytics:
                    analytics["frames"] = analytics["frames"][start_frame:end_frame]
                coach = analytics.get("coach_data")
                if coach:
                    coach["frames"] = {
                        str(int(key) - start_frame): entry
                        for key, entry in coach.get("frames", {}).items()
                        if int(key) >= start_frame and (end_frame is None or int(key) < end_frame)
                    }
                    coach["source_sha256"] = file_sha256(media["video"])
                if excerpt:
                    analytics.pop("session", None)
                    analytics["movement_data"] = None
            descriptor = {
                "source_archive": source.name, "source_sha256": source_hash,
                "recording": index, "start_frame": start_frame, "end_frame": end_frame,
                "frame_timestamps": selected_times, "provenance": (
                    analytics.get("source_provenance") if analytics else None
                ),
                "camera_view": analytics.get("capture_profile") if analytics else None,
            }
            if replay.manifest.get("kind") == "calibration":
                descriptor["legacy_neutral_baseline"] = json.loads(
                    (replay.folder / "reference.json").read_text()
                )
            report = temporary / "report.md"
            markdown = replay.report(index).read_text(encoding="utf-8")
            for key, prefix in (("video", "video"), ("overlay", "overlay"), ("lines", "lines")):
                if segment.get(key):
                    markdown = markdown.replace(segment[key], f"{prefix}_00.mp4")
            if excerpt:
                markdown = (
                    "# Reference excerpt\n\nThe report below describes the **original full recording**; "
                    "it is provenance, not statistics for this excerpt. Cached landmarks and the "
                    "reference timeline cover only the selected excerpt.\n\n" + markdown
                )
            report.write_text(markdown, encoding="utf-8")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with _archive_lock(destination), TemporaryDirectory(
                dir=destination.parent, prefix=".reference-",
            ) as output_directory:
                if destination.exists():
                    raise FileExistsError("Reference already exists")
                staged = Path(output_directory) / "reference.tp"
                if cancel_check():
                    raise ImportCancelledError("Reference creation cancelled")
                append_segment(
                    staged, report, media["video"], media.get("overlay"),
                    selected_times, analytics=analytics, lines=media.get("lines"),
                )
                mark_reference(staged, name, descriptor)
                staged.replace(destination)
        return destination
    finally:
        replay.close()
