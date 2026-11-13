"""Reference import limits and media defaults."""

from __future__ import annotations

MAX_IMPORT_VIDEO_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GB per source file


MAX_IMPORT_RANGE_SECONDS: float = 120.0  # 120 seconds per selected clip range


MIN_EVIDENCE_COVERAGE_RATIO: float = 0.25


MIN_EVIDENCE_DURATION_SECONDS: float = 0.75


SOUNDTRACK_SAMPLE_RATE: int = 16000


SOUNDTRACK_SOURCE_LABEL: str = "Video soundtrack"


SUPPORTED_VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")
