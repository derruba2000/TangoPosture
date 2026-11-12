"""Typed in-process commands sent from Qt UI/ViewModels to application services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ..core.types import ReferenceMode, SourceKind


@dataclass(frozen=True)
class PreparePractice:
    source: SourceKind = SourceKind.CAMERA
    video_path: Path | None = None
    reference_mode: ReferenceMode = ReferenceMode.SAVED
    calibration_id: str = "latest"
    confirm_camera_unchanged: bool = False
    interval_start: float = 0.0
    interval_end: float = 12.0
    reference_name: str = "My reference"
    practice_name: str = "My practice"
    teacher_note: str = ""
    headless: bool = False


@dataclass(frozen=True)
class StartPreview:
    session_id: UUID


@dataclass(frozen=True)
class SetRecording:
    session_id: UUID
    enabled: bool


@dataclass(frozen=True)
class SetScoring:
    session_id: UUID
    enabled: bool
    reference_id: str | None = None


@dataclass(frozen=True)
class PauseScoring:
    session_id: UUID
    paused: bool


@dataclass(frozen=True)
class StopAndSave:
    session_id: UUID


@dataclass(frozen=True)
class AbortUnsaved:
    session_id: UUID


@dataclass(frozen=True)
class CutVideoSegment:
    source_path: Path
    output_path: Path
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class ExportOverlayVideo:
    source_path: Path
    output_path: Path
    start_frame: int
    end_frame: int
    show_coach: bool = True
    show_pivots: bool = True
    show_steps: bool = True
    floor_only: bool = False
