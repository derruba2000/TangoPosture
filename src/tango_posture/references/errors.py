"""Explicit reference import cancellation."""

from __future__ import annotations


class ImportCancelledError(RuntimeError):
    """Raised when an offline reference import job is cancelled by the user."""
