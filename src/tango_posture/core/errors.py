"""Stable domain errors. UI layers map ``code`` to user-facing copy."""

from __future__ import annotations


class TangoError(Exception):
    code = "tango_error"

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


class InvalidTransition(TangoError):
    code = "invalid_transition"


class ModelUnavailable(TangoError):
    code = "model_unavailable"


class ReferenceInvalid(TangoError):
    code = "reference_invalid"


class ArchiveDamaged(TangoError):
    code = "archive_damaged"


class SettingsInvalid(TangoError):
    code = "settings_invalid"


class Cancelled(TangoError):
    code = "cancelled"
