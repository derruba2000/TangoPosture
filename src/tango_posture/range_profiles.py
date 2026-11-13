"""Versioned, evidence-backed calibration ranges independent of source media."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .calibration import utc_now
from .config import Config
from .domain import (
    METRIC_VERSION,
    Calibration,
    FrameObservation,
    Landmark,
    MetricObservation,
    QualityResult,
)
from .geometry import (
    axis_curve_alignment,
    head_horizontal_alignment,
    knee_bend_alignment,
    shoulder_tension_alignment,
    wrapped,
)
from .metrics import measure
from .quality import reliable

RANGE_VERSION: Literal["range-measurements/1.0"] = "range-measurements/1.0"
METRIC_DETAILS = {
    "head_pitch": ("Head", "Head pitch (ear-to-nose)", "degrees"),
    "head_forward": ("Head", "Head-forward offset", "torso_lengths"),
    "head_inclination": ("Head", "Head inclination", "degrees"),
    "shoulder_clearance": ("Shoulders", "Ear-to-shoulder clearance", "normalized_clearance"),
    "hip_offset": ("Hips", "Hip offset from ankle-to-head axis", "axis_lengths"),
    "trunk_inclination": ("Torso", "Torso alignment", "degrees"),
    "knee_flexion": ("Knees", "Least-bent reliably visible knee", "degrees"),
}
STEPS = ("Head", "Shoulders", "Hips", "Torso", "Knees")
CYCLIC_METRICS = {"head_inclination", "trunk_inclination"}


class Passage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Passage:
        if self.end <= self.start:
            raise ValueError("A passage must end after it starts")
        return self


class ApprovedRange(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    lower: float
    upper: float
    unit: str
    view: Literal["side", "front", "all"]
    passages: list[Passage] = Field(min_length=1)
    median: float
    p10: float
    p90: float
    valid_duration: float = Field(ge=0)
    selected_duration: float = Field(gt=0)
    samples: int = Field(ge=2)
    inside_fraction: float = Field(ge=0, le=1)
    approved: Literal[True] = True
    camera_check_passed: bool = False

    @model_validator(mode="after")
    def ordered(self) -> ApprovedRange:
        if self.lower > self.upper or self.p10 > self.p90:
            raise ValueError("Range limits must be ordered")
        if self.valid_duration > self.selected_duration + 1e-8:
            raise ValueError("Tracking coverage exceeds selected duration")
        return self


class RangeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[2] = 2
    measurement_version: Literal["range-measurements/1.0"] = RANGE_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1)
    created_at_utc: str = Field(default_factory=utc_now)
    context: str = Field(min_length=1)
    model_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    capture_profile: dict[str, Any]
    source: dict[str, Any]
    metrics: dict[str, ApprovedRange] = Field(min_length=1)
    camera_checks: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_metrics(self) -> RangeProfile:
        required = {"side", "facing", "width", "height", "aspect_ratio", "capture_profile", "source"}
        if not required <= self.capture_profile.keys():
            raise ValueError("Calibration requires a complete capture profile")
        capture = self.capture_profile
        if (
            capture["side"] not in ("left", "right") or capture["facing"] not in ("left", "right")
            or any(type(capture[key]) is not int or capture[key] <= 0 for key in ("width", "height"))
            or not isinstance(capture["source"], str)
            or not isinstance(capture["capture_profile"], str)
        ):
            raise ValueError("Invalid calibration capture settings")
        ratio = capture["aspect_ratio"]
        if not isinstance(ratio, (float, int)) or not math.isclose(
            ratio, capture["width"] / capture["height"], rel_tol=1e-8,
        ):
            raise ValueError("Calibration aspect ratio does not match the recorded dimensions")
        for key, target in self.metrics.items():
            if key not in METRIC_DETAILS or target.unit != METRIC_DETAILS[key][2]:
                raise ValueError(f"Unknown metric or incompatible unit: {key}")
        return self

    def calibration(self) -> Calibration:
        return Calibration(
            self.id, self.created_at_utc, self.name, self.capture_profile, {},
            {"range_profile": True}, self.model_hash, METRIC_VERSION,
            {"label": "approved ranges", "range_profile": self.model_dump(mode="json")},
        )


def profile_from_calibration(reference: Calibration) -> RangeProfile | None:
    payload = reference.review.get("range_profile")
    return RangeProfile.model_validate(payload) if payload is not None else None


def save_range_profile(path: Path, profile: RangeProfile) -> None:
    from .archive import _archive_lock, _replace_archive

    if path.suffix.lower() != ".tpc":
        raise ValueError("Calibration profiles must use the .tpc extension")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _archive_lock(path):
        if path.exists():
            raise FileExistsError("Choose a new profile name; existing profiles are not overwritten")
        with NamedTemporaryFile(dir=path.parent, suffix=".tpc.tmp", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("profile.json", profile.model_dump_json(indent=2))
                archive.writestr("manifest.json", json.dumps({
                    "version": 2, "kind": "calibration", "name": profile.name,
                    "profile": "profile.json", "segments": [],
                    "created_at_utc": profile.created_at_utc,
                }))
            _replace_archive(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_range_profile(path: Path) -> RangeProfile:
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if (
            manifest.get("version") != 2 or manifest.get("kind") != "calibration"
            or manifest.get("profile") != "profile.json"
        ):
            raise ValueError("Not a version-2 range calibration profile")
        result = RangeProfile.model_validate_json(archive.read("profile.json"))
        name = manifest.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Calibration profile name is missing")
        return result.model_copy(update={"name": name})


def measure_ranges(frame: FrameObservation, cfg: Config) -> MetricObservation:
    """Use only confidence-qualified landmarks and existing measurement definitions."""
    core = measure(frame, cfg)
    values = {key: core.values.get(key) for key in METRIC_DETAILS}
    quality = {
        key: core.quality.get(key, QualityResult(False, "Required landmarks unavailable"))
        for key in METRIC_DETAILS
    }
    if len(frame.poses) != 1:
        return MetricObservation(frame.timestamp, dict.fromkeys(METRIC_DETAILS), {
            key: QualityResult(False, "Dancer tracking unavailable or ambiguous")
            for key in METRIC_DETAILS
        })
    pose = {name: point for name, point in frame.poses[0].items() if reliable(point, cfg.confidence)}
    for key, result, component in (
        ("head_pitch", head_horizontal_alignment(pose, frame.width, frame.height), 0),
        ("shoulder_clearance", shoulder_tension_alignment(pose, frame.width, frame.height), 1),
        ("hip_offset", axis_curve_alignment(
            pose, frame.width, frame.height, cfg.facing, cfg.side,
        ), 1),
        ("knee_flexion", knee_bend_alignment(pose, frame.width, frame.height, cfg.side), 0),
    ):
        value = result[component] if result is not None else None
        if isinstance(value, (int, float)) and math.isfinite(value):
            values[key] = float(value)
            quality[key] = QualityResult(True)
        else:
            values[key] = None
    return MetricObservation(frame.timestamp, values, quality, core.view_width)


def measure_profile(frame: FrameObservation, cfg: Config, profile: RangeProfile) -> MetricObservation:
    """Use the same per-view measurement path in approval and subsequent scoring."""
    observations = {
        view: measure_ranges(frame, cfg.model_copy(update={"allow_all_angles": view != "side"}))
        for view in {target.view for target in profile.metrics.values()}
    }
    result = measure_ranges(frame, cfg.model_copy(update={"allow_all_angles": True}))
    for key, target in profile.metrics.items():
        observation = observations[target.view]
        result.values[key] = observation.values[key]
        result.quality[key] = observation.quality[key]
        if key in CYCLIC_METRICS and (value := result.values[key]) is not None:
            center = (target.lower + target.upper) / 2
            result.values[key] = center + wrapped(value - center)
        width = observation.view_width
        if target.view != "all" and (
            width is None or (target.view == "side" and width > cfg.max_view_width)
            or (target.view == "front" and width <= cfg.max_view_width)
        ):
            result.values[key] = None
            result.quality[key] = QualityResult(False, "Return to this metric's approved camera view")
    return result


@dataclass(frozen=True)
class Evidence:
    frames: list[FrameObservation]
    model_hash: str
    capture_profile: dict[str, Any]
    source: dict[str, Any]
    max_gap: float


def cached_evidence(path: Path, index: int = 0) -> Evidence:
    from .archive import _archive_lock

    with _archive_lock(path):
        return _cached_evidence(path, index)


def _cached_evidence(path: Path, index: int) -> Evidence:
    from .archive import file_sha256, read_analytics, read_manifest

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
    max_gap = float(data.get("max_gap", 0.5))
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


def metric_samples(
    frames: Sequence[FrameObservation], cfg: Config,
) -> dict[str, list[tuple[float, float | None]]]:
    result: dict[str, list[tuple[float, float | None]]] = {key: [] for key in METRIC_DETAILS}
    previous: dict[str, float] = {}
    for frame in frames:
        observation = measure_ranges(frame, cfg)
        for key, series in result.items():
            value = observation.values[key] if observation.quality[key].valid else None
            if value is not None and key in CYCLIC_METRICS:
                if key in previous:
                    value = previous[key] + wrapped(value - previous[key])
                previous[key] = value
            series.append((frame.timestamp, value))
    return result


def summarize_examples(
    samples: list[tuple[float, float | None]], passages: list[Passage],
    max_gap: float, lower: float | None = None, upper: float | None = None,
) -> dict[str, Any]:
    if not math.isfinite(max_gap) or max_gap <= 0:
        raise ValueError("Maximum tracking gap must be finite and positive")
    if any(not math.isfinite(t) for t, _ in samples) or any(
        b[0] <= a[0] for a, b in pairwise(samples)
    ):
        raise ValueError("Evidence timestamps must be finite and strictly increasing")
    ordered = sorted(passages, key=lambda passage: passage.start)
    if any(left.end > right.start for left, right in pairwise(ordered)):
        raise ValueError("Example passages must not overlap")
    selected = [(t, value) for t, value in samples if any(p.start <= t < p.end for p in ordered)]
    valid = [(t, value) for t, value in selected if value is not None and math.isfinite(value)]
    if len(valid) < 2:
        raise ValueError("Not enough reliable samples; choose another passage or skip this metric")
    values = [value for _, value in valid]
    p10, median, p90 = map(float, np.quantile(values, [0.1, 0.5, 0.9]))
    lo, hi = p10 if lower is None else lower, p90 if upper is None else upper
    if not math.isfinite(lo) or not math.isfinite(hi) or lo > hi:
        raise ValueError("Acceptable range must have finite, ordered limits")
    duration = sum(
        b[0] - a[0] for a, b in pairwise(selected)
        if a[1] is not None and b[1] is not None
        and math.isfinite(a[1]) and math.isfinite(b[1])
        and 0 < b[0] - a[0] <= max_gap
        and any(p.start <= a[0] <= b[0] < p.end for p in ordered)
    )
    if duration <= 0:
        raise ValueError("No continuous reliable footage; select a longer stable passage")
    return {
        "median": median, "p10": p10, "p90": p90, "lower": lo, "upper": hi,
        "valid_duration": duration, "selected_duration": sum(p.end - p.start for p in ordered),
        "samples": len(valid), "inside_fraction": sum(lo <= value <= hi for value in values) / len(values),
        "minimum_time": min(valid, key=lambda item: item[1])[0],
        "maximum_time": max(valid, key=lambda item: item[1])[0],
    }


def repeat_check(
    samples: list[tuple[float, float | None]], holds: list[Passage], max_gap: float,
) -> tuple[bool, str]:
    if len(holds) != 4:
        return False, "Capture neutral, change, return, and a fresh repetition"
    try:
        stats = [summarize_examples(samples, [hold], max_gap) for hold in holds]
    except ValueError as error:
        return False, str(error)
    medians = [item["median"] for item in stats]
    if any(item["valid_duration"] < 0.5 or
           item["valid_duration"] / item["selected_duration"] < 0.6 for item in stats):
        return False, "Too little reliable footage in a hold; adjust framing, then retry or skip"
    noise = max(item["p90"] - item["p10"] for item in stats)
    change, repeated = medians[1] - medians[0], medians[3] - medians[2]
    if min(abs(change), abs(repeated)) <= max(2 * noise, 1e-6) or change * repeated <= 0:
        return False, "Changes were not distinguishable from hold variation; adjust framing and retry"
    if abs(medians[2] - medians[0]) > max(noise * 2, abs(change) * 0.25, 1e-6):
        return False, "Return to neutral did not match the first hold; retry or skip this metric"
    return True, "Repeated change and return distinguished from within-hold variation"
