"""Typed domain events emitted by application services to Qt ViewModels."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from ..core.entities import MetricSample
from ..core.session_state import SessionState


@dataclass(frozen=True)
class DomainEvent:
    id: UUID = field(default_factory=uuid4)
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    sequence_id: int = 0


@dataclass(frozen=True)
class SessionStateChanged(DomainEvent):
    session_id: UUID = field(default_factory=uuid4)
    state: SessionState = field(default_factory=SessionState)


@dataclass(frozen=True)
class MetricSnapshotEmitted(DomainEvent):
    session_id: UUID = field(default_factory=uuid4)
    sample: MetricSample = field(default_factory=lambda: MetricSample(timestamp_ms=0))
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BeatTracked(DomainEvent):
    session_id: UUID = field(default_factory=uuid4)
    bpm: float = 120.0
    beat_number: int = 1
    confidence: float = 0.0
    is_active: bool = False


@dataclass(frozen=True)
class SegmentSaved(DomainEvent):
    session_id: UUID = field(default_factory=uuid4)
    archive_path: Path = field(default_factory=Path)
    segment_index: int = 0
    report_path: Path = field(default_factory=Path)


@dataclass(frozen=True)
class PracticeCompleted(DomainEvent):
    session_id: UUID = field(default_factory=uuid4)
    exit_code: int = 0
    message: str = ""
    saved_archive: Path | None = None


@dataclass(frozen=True)
class ServiceErrorOccurred(DomainEvent):
    session_id: UUID = field(default_factory=uuid4)
    message: str = ""
    detail: str = ""


@dataclass(frozen=True)
class AIAnalysisProgress(DomainEvent):
    job_id: str = ""
    chunk: str = ""
    done: bool = False
    error: str | None = None
