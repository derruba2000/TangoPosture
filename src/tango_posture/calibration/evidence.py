"""Cached landmark evidence and conservative explicit dancer association."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain import (
    FrameObservation,
    Landmark,
)
from ..quality import reliable


@dataclass(frozen=True)
class Evidence:
    frames: list[FrameObservation]
    model_hash: str
    capture_profile: dict[str, Any]
    source: dict[str, Any]
    max_gap: float


def cached_evidence(path: Path, index: int = 0) -> Evidence:
    from ..archive import _archive_lock

    with _archive_lock(path):
        return _cached_evidence(path, index)


def _cached_evidence(path: Path, index: int) -> Evidence:
    from ..archive import file_sha256, read_analytics, read_manifest

    manifest = read_manifest(path)
    if not 0 <= index < len(manifest["segments"]):
        raise ValueError("Reference recording not found")
    data = read_analytics(path, index)
    if data is None:
        raise ValueError("No landmark cache. Analyze posture & movement in Review first")
    coach = data.get("coach_data") or {}
    capture = data.get("capture_profile") or coach.get("capture_profile") or {}
    if not isinstance(capture, dict):
        raise TypeError("Invalid cached capture settings")
    entries = coach.get("frames")
    from_coach = bool(entries)
    if not entries:
        entries = {str(i): entry for i, entry in enumerate(data.get("frames") or [])}
    timestamps = data.get("frame_timestamps") or []
    width = coach.get("width") or capture.get("width")
    height = coach.get("height") or capture.get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("The cache has no verified frame dimensions; reanalyze this recording")
    model_hash = data.get("model_hash") or coach.get("model_hash")
    if not isinstance(model_hash, str):
        raise TypeError("The cache has no measured model identity; reanalyze this recording")
    frames = []
    for i, timestamp in enumerate(timestamps):
        entry = entries.get(str(i), {})
        if not isinstance(entry, dict):
            raise TypeError("Invalid cached frame")
        if from_coach and entry and "measurement_poses" not in entry:
            raise ValueError("Cache contains display landmarks only; reanalyze to cache raw measurements")
        raw_poses = entry.get("measurement_poses", []) if from_coach else entry.get("poses", [])
        if not isinstance(raw_poses, list):
            raise TypeError("Invalid cached frame / pose list")
        poses = []
        for raw in raw_poses:
            if not isinstance(raw, dict):
                raise TypeError("Invalid cached pose")
            pose = {}
            for name, coordinates in raw.items():
                if not isinstance(coordinates, list) or len(coordinates) != 5:
                    raise ValueError("Invalid cached landmark coordinates")
                if any(type(value) not in (int, float) and value is not None for value in coordinates):
                    raise TypeError("Cached landmark coordinates must be numbers or unknown")
                if coordinates[0] is None or coordinates[1] is None:
                    raise ValueError("Cached landmark image coordinates are missing")
                pose[name] = Landmark(
                    coordinates[0], coordinates[1], coordinates[3],
                    coordinates[4], coordinates[2],
                )
            poses.append(pose)
        frames.append(FrameObservation(timestamp, width, height, poses))
    if not frames or not any(frame.poses for frame in frames):
        raise ValueError("No usable landmark cache; reanalyze this recording")
    if "max_gap" not in data:
        raise ValueError("Cached tracking gap limit is missing; reanalyze this recording")
    max_gap = float(data["max_gap"])
    if not math.isfinite(max_gap) or max_gap <= 0:
        raise ValueError("Invalid cached tracking gap")
    return Evidence(frames, model_hash, capture, {
        "archive": path.name, "archive_sha256": file_sha256(path), "recording": index,
        "provenance": data.get("source_provenance") or {},
    }, max_gap)


def dancer_frames(evidence: Evidence, dancer: int, confidence: float) -> list[FrameObservation]:
    """Conservative spatial association; ambiguity/gaps end a track, never silently reseed it."""
    selected = []
    previous: tuple[float, float] | None = None
    last_timestamp: float | None = None
    lost = False
    for frame in evidence.frames:
        candidates = []
        for pose in frame.poses:
            hips = [pose.get(f"{side}_hip") for side in ("left", "right")]
            good = [point for point in hips if point is not None and reliable(point, confidence)]
            center = (
                (sum(point.x for point in good) / len(good),
                 sum(point.y for point in good) / len(good)) if good else None
            )
            candidates.append((center, pose))
        chosen = None
        if not lost:
            if previous is None:
                if 0 <= dancer < len(candidates) and candidates[dancer][0] is not None:
                    chosen = candidates[dancer]
                else:
                    lost = True
            elif last_timestamp is not None and frame.timestamp - last_timestamp <= evidence.max_gap:
                ranked = sorted(
                    ((math.dist(previous, center), center, pose)
                     for center, pose in candidates if center is not None), key=lambda item: item[0],
                )
                if (
                    ranked and ranked[0][0] < 0.15
                    and (len(ranked) == 1 or ranked[1][0] - ranked[0][0] > 0.03)
                ):
                    chosen = (ranked[0][1], ranked[0][2])
            if previous is not None and chosen is None:
                lost = True
        if chosen is not None:
            previous = chosen[0]
            last_timestamp = frame.timestamp
        selected.append(FrameObservation(
            frame.timestamp, frame.width, frame.height, [chosen[1]] if chosen is not None else [],
        ))
    return selected
