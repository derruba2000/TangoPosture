"""Records shared by the pure pipeline, adapters and persistence."""

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

METRICS = ("head_forward", "head_inclination", "trunk_inclination")
METRIC_VERSION = "1.0"
UNITS = dict(zip(METRICS, ("torso_lengths", "degrees", "degrees")))


@dataclass(frozen=True)
class Landmark:
    x: float
    y: float
    visibility: float | None = None
    presence: float | None = None
    z: float | None = None


@dataclass(frozen=True)
class FrameObservation:
    timestamp: float
    width: int
    height: int
    poses: list[dict[str, Landmark]]
    world_poses: list[dict[str, Landmark]] = field(default_factory=list)


@dataclass(frozen=True)
class QualityResult:
    valid: bool
    reason: str = ""
    view_width: float | None = None


@dataclass(frozen=True)
class MetricObservation:
    timestamp: float
    values: dict[str, float | None]
    quality: dict[str, QualityResult]
    view_width: float | None = None


@dataclass
class Calibration:
    id: str
    created_at_utc: str
    name: str
    profile: dict[str, Any]
    baseline: dict[str, float]
    quality: dict[str, Any]
    model_hash: str
    metric_version: str = METRIC_VERSION
    review: dict[str, Any] = field(default_factory=lambda: {"label": "self-selected"})


@dataclass
class DeviationEvent:
    metric: str
    onset: float
    detection: float
    peak_deviation: float
    end: float | None = None
    end_reason: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class SessionSummary:
    active_duration: float
    pause_duration: float
    metrics: dict[str, dict[str, Any]]
    schema_version: int = 1


class PoseEstimator(Protocol):
    def estimate(self, image: Any, timestamp: float) -> FrameObservation: ...
    def close(self) -> None: ...
