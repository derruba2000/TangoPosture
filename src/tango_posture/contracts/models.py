"""Pydantic request and response models for queries, library views, and diagnostics."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..core.types import ArchiveKind, Readiness


class PracticeFilterQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_term: str = ""
    kind: ArchiveKind | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class PracticeItemSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    path: Path
    kind: ArchiveKind
    created_at_iso: str
    segment_count: int = 0
    duration_seconds: float = 0.0


class LibraryPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[PracticeItemSummary] = []
    total_count: int = 0
    offset: int = 0
    limit: int = 50


class ReadinessCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Readiness
    detail: str = ""


class ReadinessReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overall: Readiness
    checks: list[ReadinessCheck] = []
