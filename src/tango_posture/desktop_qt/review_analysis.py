"""Background posture analysis for Qt review playback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ..config import Config
from ..reference_import import analyse_imported_clip
from ..storage import Storage


@dataclass(frozen=True)
class ReviewAnalysis:
    coach_data: dict[str, Any]
    movement_data: dict[str, Any]


def analyse_review_clip(
    video_path: Path,
    cfg: Config,
    *,
    selected_out: float,
    progress_callback: Callable[[int, int, str], None],
    cancel_check: Callable[[], bool],
) -> ReviewAnalysis:
    """Analyze a local clip without persisting an extra report or changing the video."""
    with TemporaryDirectory(prefix="tango-review-analysis-") as temporary_dir:
        temporary_path = Path(temporary_dir)
        storage = Storage(temporary_path / "analytics.sqlite3")
        try:
            result = analyse_imported_clip(
                video_path=video_path,
                cfg=cfg,
                report_dir=temporary_path / "reports",
                storage=storage,
                selected_in=0.0,
                selected_out=selected_out,
                analysis_mode="movement",
                analyse_soundtrack=False,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        finally:
            storage.close()
    if result.coach_data is None:
        raise ValueError("Posture analysis completed without frame-level coaching data")
    return ReviewAnalysis(result.coach_data, result.movement_data)
