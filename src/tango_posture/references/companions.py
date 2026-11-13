"""Coach and movement companion persistence and report lookups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..movement import validate_movement_analysis
from ..review import ReportRecord


def save_movement_companion(path: Path, data: dict[str, Any]) -> Path:
    """Save a validated movement analysis JSON companion file."""
    validate_movement_analysis(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return path


def load_movement_companion(path: Path) -> dict[str, Any] | None:
    """Load and validate a movement analysis JSON companion file if it exists."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return validate_movement_analysis(raw)
    except (OSError, ValueError, TypeError):
        return None


def save_coach_companion(path: Path, data: dict[str, Any]) -> Path:
    """Save a per-frame Open Practice Coach overlay and insights companion file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8"
    )
    return path


def load_coach_companion(path: Path) -> dict[str, Any] | None:
    """Load a per-frame Open Practice Coach companion file if it exists and is valid."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("frames"), dict):
            return None
        return raw
    except (OSError, ValueError, TypeError):
        return None


def load_report_coach_data(report: ReportRecord) -> dict[str, Any] | None:
    """Load the per-frame coach overlay & insights JSON associated with a report if valid."""
    if report.relinked_content_changed:
        return None
    candidates: list[Path] = []
    coach_file = getattr(report, "coach_overlay_file", None)
    if coach_file:
        candidates.append(report.path.parent / coach_file)
    candidates.append(report.path.parent / f"{report.path.stem}.coach.v1.json")
    for cand in candidates:
        data = load_coach_companion(cand)
        if data is not None:
            if report.source_sha256 and data.get("source_sha256") != report.source_sha256:
                continue
            return data
    return None


def load_report_movement_data(report: ReportRecord) -> dict[str, Any] | None:
    """Load the movement analysis JSON associated with a report if valid."""
    if report.relinked_content_changed:
        return None
    candidates: list[Path] = []
    if report.movement_analysis_file:
        candidates.append(report.path.parent / report.movement_analysis_file)
    candidates.append(report.path.parent / f"{report.path.stem}.movement.v1.json")
    for cand in candidates:
        data = load_movement_companion(cand)
        if data is not None:
            if report.source_sha256 and data.get("source_sha256") != report.source_sha256:
                continue
            return data
    return None
