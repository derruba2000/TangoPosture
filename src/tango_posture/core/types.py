"""Identifiers, intervals, timebase and status enums shared by services and UIs."""

from __future__ import annotations

from enum import StrEnum
from typing import NewType
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

SessionId = NewType("SessionId", UUID)
RecordingId = NewType("RecordingId", UUID)
ReferenceId = NewType("ReferenceId", UUID)
JobId = NewType("JobId", UUID)


def new_session_id() -> SessionId:
    return SessionId(uuid4())


def new_job_id() -> JobId:
    return JobId(uuid4())


class Evidence(StrEnum):
    """Confidence/provenance of a value. Unknown is never success."""

    OBSERVED = "observed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class Origin(StrEnum):
    MEASURED = "measured"
    AI_INTERPRETED = "ai_interpreted"


class SourceKind(StrEnum):
    CAMERA = "camera"
    VIDEO = "video"


class ReferenceMode(StrEnum):
    NEW = "new"
    SAVED = "saved"
    INTERVAL = "interval"


class ArchiveKind(StrEnum):
    PRACTICE = "practice"
    CALIBRATION = "calibration"
    LEGACY_REPORT = "legacy_report"


class Readiness(StrEnum):
    READY = "ready"
    WARNING = "warning"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class Interval(BaseModel):
    """Half-open media interval ``[start_ms, end_ms)``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> Interval:
        if self.end_ms < self.start_ms:
            raise ValueError("Interval end must not precede its start")
        return self

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def contains(self, time_ms: int) -> bool:
        return self.start_ms <= time_ms < self.end_ms

    def overlaps(self, other: Interval) -> bool:
        return self.start_ms < other.end_ms and other.start_ms < self.end_ms


class MediaTime(BaseModel):
    """Source media time, normalised session time, frame index and wall clock kept separate."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_ms: int = Field(ge=0)
    session_ms: int | None = Field(default=None, ge=0)
    frame_index: int | None = Field(default=None, ge=0)
    wall_clock_utc: str | None = None
    timebase_version: int = 1
