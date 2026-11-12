"""Core domain entities for TangoPosture (pure Python, framework-independent)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .types import ArchiveKind, Evidence, Interval, Origin, SourceKind


@dataclass(frozen=True)
class CaptureSetup:
    camera_index: int = 0
    width: int = 640
    height: int = 480
    side: str = "left"
    facing: str = "right"
    capture_profile: str = "default"
    mirror: bool = False
    source: SourceKind = SourceKind.CAMERA


@dataclass(frozen=True)
class MetricSample:
    timestamp_ms: int
    head_angle: float | None = None
    head_forward_ratio: float | None = None
    shoulder_elevation_pct: float | None = None
    knee_bend_deg: float | None = None
    hip_offset_ratio: float | None = None
    role: str = "NONE"
    embrace_type: str = "N/A"
    evidence: Evidence = Evidence.OBSERVED
    alerts: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class IssueEvent:
    id: UUID = field(default_factory=uuid4)
    interval: Interval = field(default_factory=lambda: Interval(start_ms=0, end_ms=0))
    category: str = "head"  # head, shoulder, knee, axis/hip
    metric_value: float = 0.0
    threshold_value: float = 0.0
    confirmed: bool = True
    evidence: Evidence = Evidence.OBSERVED
    origin: Origin = Origin.MEASURED


@dataclass(frozen=True)
class MovementRecord:
    pivots: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    width: int = 640
    height: int = 480
    fps: float = 30.0


@dataclass
class Recording:
    id: UUID = field(default_factory=uuid4)
    session_id: UUID = field(default_factory=uuid4)
    index: int = 0
    title: str = ""
    video_path: Path | None = None
    overlay_path: Path | None = None
    report_path: Path | None = None
    duration_seconds: float = 0.0
    created_at_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    is_calibration: bool = False
    movement: MovementRecord | None = None


@dataclass
class Practice:
    id: UUID = field(default_factory=uuid4)
    name: str = ""
    archive_path: Path | None = None
    kind: ArchiveKind = ArchiveKind.PRACTICE
    created_at_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    recordings: list[Recording] = field(default_factory=list)
    reference_id: str | None = None
    setup: CaptureSetup = field(default_factory=CaptureSetup)


@dataclass(frozen=True)
class ReferenceSummary:
    id: str
    name: str
    path: Path
    side: str
    facing: str
    camera: str
    created_at: str = ""
    is_archive: bool = True
