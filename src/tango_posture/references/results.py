"""Typed results of reference clip analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..calibration.readiness import CalibrationReadiness
from ..domain import (
    Calibration,
)
from .coverage import FamilyCoverage
from .links import FacebookReelSourceLink, YouTubeClockAlignment, YouTubeSourceLink
from .soundtrack import SoundtrackAnalysisSummary


@dataclass(frozen=True)
class ImportedClipResult:
    """Complete result of importing and analysing a reference video clip."""

    report_path: Path
    metadata_path: Path
    movement_path: Path
    title: str
    video_path: Path
    source_sha256: str
    selected_in: float
    selected_out: float
    analysis_mode: str
    readiness: CalibrationReadiness
    reference: Calibration | None
    movement_data: dict[str, Any]
    soundtrack: SoundtrackAnalysisSummary
    coverage: dict[str, FamilyCoverage]
    youtube_link: YouTubeSourceLink | None = None
    youtube_alignment: YouTubeClockAlignment | None = None
    facebook_reel: FacebookReelSourceLink | None = None
    coach_path: Path | None = None
    coach_data: dict[str, Any] | None = None
