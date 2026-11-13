"""Create portable, indexed Library archives without changing source media."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from zipfile import ZipFile

import cv2

from ..archive import (
    _archive_lock,
    append_segment,
    file_sha256,
    practice_directory,
    read_analytics,
    read_manifest,
    rename_archive,
    store_analytics,
)
from ..config import Config
from ..movement import render_floor_guide, render_movement_overlay
from ..reference_import import (
    ImportCancelledError,
    load_report_coach_data,
    load_report_movement_data,
    render_coach_frame_overlay,
)
from ..replay import VideoReader, save_overlay_video
from ..review import read_report, report_directory
from .analytics_data import session_payload


def archive_report(
    report_path: Path, cfg: Config, cancel_check: Callable[[], bool], *,
    as_reference: bool = False,
) -> Path:
    from ..reference_archives import mark_reference, reference_directory

    record = read_report(report_path)
    source = record.recorded_video
    if source is None or not source.is_file():
        raise ValueError(f"{report_path.name}: linked video is unavailable")
    destination = (
        reference_directory(cfg.data_dir) / f"{report_path.stem}.tpr" if as_reference
        else practice_directory(cfg.data_dir) / f"{report_path.stem}.tp"
    )
    if destination.exists():
        read_manifest(destination)
        stored = read_analytics(destination, 0)
        if stored is not None and stored.get("original_report_sha256") == record.digest:
            return destination
        raise FileExistsError(f"Archive already exists: {destination.name}")
    coach = load_report_coach_data(record)
    movement = load_report_movement_data(record)
    reader = VideoReader(source)
    try:
        start = max(0, round((record.clip_in or 0) * reader.fps))
        end = min(reader.count - 1, round((record.clip_out or reader.duration) * reader.fps) - 1)
        fps = reader.fps
    finally:
        reader.close()
    if end < start:
        raise ValueError("Selected clip contains no video frames")

    def render(image: Any, index: int, rate: float, *, floor: bool = False) -> Any:
        if cancel_check():
            raise ImportCancelledError("Archive import canceled")
        height, width = image.shape[:2]
        target_width = max(960, width) if coach is not None and not floor else width
        target_size = (target_width, round(height * target_width / width))
        image = render_floor_guide(image) if floor else image
        if coach is not None:
            image = render_coach_frame_overlay(image, coach, index, cfg, lines_only=floor)
        if (image.shape[1], image.shape[0]) != target_size:
            image = cv2.resize(image, target_size, interpolation=cv2.INTER_LINEAR)
        return render_movement_overlay(
            image, movement, index, rate, show_pivots=True, show_steps=True,
            step_history_seconds=cfg.replay_step_history_seconds,
        )

    if as_reference:
        destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix="tango-library-archive-", dir=destination.parent if as_reference else None,
    ) as temporary:
        folder = Path(temporary)
        raw = folder / "raw.mp4"
        overlay = folder / "overlay.mp4"
        lines = folder / "lines.mp4"

        def raw_frame(image: Any, _index: int, _fps: float) -> Any:
            if cancel_check():
                raise ImportCancelledError("Archive import canceled")
            return image

        save_overlay_video(source, raw, start, end, frame_transform=raw_frame)
        save_overlay_video(source, overlay, start, end, frame_transform=render)
        save_overlay_video(
            source, lines, start, end,
            frame_transform=lambda image, index, rate: render(image, index, rate, floor=True),
        )
        indexed_coach = copy.deepcopy(coach)
        if indexed_coach is not None:
            indexed_coach["frames"] = {
                str(int(key) - start): entry
                for key, entry in indexed_coach.get("frames", {}).items()
                if start <= int(key) <= end
            }
            indexed_coach["source_sha256"] = file_sha256(raw)
        indexed_movement = copy.deepcopy(movement)
        if indexed_movement is not None:
            indexed_movement.update(
                source_sha256=file_sha256(raw), segment_index=0,
                frame_count=end - start + 1,
            )
            for key in ("steps", "hip_frames"):
                indexed_movement[key] = [
                    {**entry, "frame": entry["frame"] - start,
                     **({"time": (entry["frame"] - start) / fps} if key == "steps" else {})}
                    for entry in indexed_movement.get(key, []) if start <= entry["frame"] <= end
                ]
            indexed_movement["pivots"] = [
                {**entry, "start_frame": max(start, entry["start_frame"]) - start,
                 "end_frame": min(end + 1, entry["end_frame"]) - start}
                for entry in indexed_movement.get("pivots", [])
                if entry["end_frame"] > start and entry["start_frame"] <= end
            ]
            indexed_movement["cut_frames"] = [
                frame - start for frame in indexed_movement.get("cut_frames", [])
                if start <= frame <= end
            ]
        timestamps = (
            record.timestamps if len(record.timestamps) == end - start + 1
            else [frame / fps for frame in range(start, end + 1)]
        )
        analytics: dict[str, Any] = {
            "model_hash": coach.get("model_hash") if coach else None,
            "capture_profile": coach.get("capture_profile") if coach else None,
            "max_gap": cfg.max_gap,
            "frame_timestamps": timestamps,
            "coach_data": indexed_coach, "movement_data": indexed_movement,
            "original_report_sha256": record.digest,
            "source_provenance": {
                "sha256": record.source_sha256 or file_sha256(source),
                "clip_in": record.clip_in, "clip_out": record.clip_out,
                "youtube_link": record.youtube_link, "facebook_reel": record.facebook_reel,
            },
        }
        if cancel_check():
            raise ImportCancelledError("Archive import canceled")
        output = folder / "reference.tp" if as_reference else destination
        append_segment(
            output, report_path, raw, overlay, timestamps,
            analytics=analytics, lines=lines,
        )
        rename_archive(output, record.title)
        if as_reference:
            mark_reference(output, record.title, {
                "provenance": analytics["source_provenance"],
                "camera_view": analytics.get("capture_profile"),
                "clip_start": record.clip_in, "clip_end": record.clip_out,
            })
            destination.parent.mkdir(parents=True, exist_ok=True)
            with _archive_lock(destination):
                if destination.exists():
                    raise FileExistsError(f"Reference already exists: {destination.name}")
                output.replace(destination)
    return destination


def migrate_legacy_archives(
    cfg: Config, cancel_check: Callable[[], bool]
) -> tuple[int, list[str]]:
    """Copy loose reports and compatible DB evidence; never remove legacy data."""
    count = 0
    errors: list[str] = []
    from ..archive import calibration_directory
    from ..reference_archives import promote_reference, reference_directory

    for path in calibration_directory(cfg.data_dir).glob("*.tpc"):
        if cancel_check():
            return count, errors
        try:
            manifest = read_manifest(path)
            if manifest["version"] != 1:
                continue
            destination = reference_directory(cfg.data_dir) / f"{path.stem}.tpr"
            if not destination.exists():
                promote_reference(path, destination, manifest.get("name", path.stem),
                                  cancel_check=cancel_check)
                count += 1
            else:
                with ZipFile(destination) as archive:
                    descriptor = json.loads(archive.read("reference.json"))
                if not isinstance(descriptor, dict):
                    raise ValueError(f"{destination.name} contains invalid reference details")
                if descriptor.get("source_sha256") != file_sha256(path):
                    raise FileExistsError(f"{destination.name} belongs to a different source; choose a new name")
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            errors.append(f"{path.name}: {error}")
    for report in report_directory(cfg.data_dir).glob("report_*.md"):
        if cancel_check():
            return count, errors
        try:
            reference = reference_directory(cfg.data_dir) / f"{report.stem}.tpr"
            if reference.exists():
                stored = read_analytics(reference, 0)
                if stored is not None and stored.get("original_report_sha256") == read_report(report).digest:
                    continue
            existed = (practice_directory(cfg.data_dir) / f"{report.stem}.tp").exists()
            archive_report(report, cfg, cancel_check)
            count += int(not existed)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, cv2.error) as error:
            errors.append(f"{report.name}: {error}")
    for path in practice_directory(cfg.data_dir).glob("*.tp"):
        if cancel_check():
            break
        try:
            manifest = read_manifest(path)
            with ZipFile(path) as archive:
                reports = [
                    archive.read(segment["report"]).decode("utf-8")
                    for segment in manifest["segments"]
                ]
            for index, report_text in enumerate(reports):
                current = read_analytics(path, index)
                if current is not None and current.get("session") is not None:
                    continue
                match = re.search(r"^\| Session ID \| `([^`]+)` \|$", report_text, re.MULTILINE)
                if match is not None:
                    store_analytics(
                        path, index, {"session": session_payload(Path(cfg.data_dir), match[1])}
                    )
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
            errors.append(f"{path.name}: {error}")
    return count, errors
