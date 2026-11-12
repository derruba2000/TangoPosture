"""Recording / scoring state machine for a practice session.

Recording (media capture) and scoring (reference-relative analysis) are orthogonal state
variables. Every command is validated; an illegal command raises ``InvalidTransition`` and
leaves the state untouched. The state is immutable so callers can diff old/new snapshots.

See spec section 4.2. Phases::

    Idle -> Preparing -> Preview (-> Idle on close)
    Preparing/Preview -> Error -> Preparing (retry)

Inside ``Preview`` recording is on/off and scoring is off/active/paused independently.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from .errors import InvalidTransition
from .types import SourceKind


class Phase(StrEnum):
    IDLE = "idle"
    PREPARING = "preparing"
    PREVIEW = "preview"
    ERROR = "error"


class Scoring(StrEnum):
    OFF = "off"
    ACTIVE = "active"
    PAUSED = "paused"


@dataclass(frozen=True)
class SessionState:
    phase: Phase = Phase.IDLE
    source: SourceKind | None = None
    recording: bool = False
    scoring: Scoring = Scoring.OFF
    reference_id: str | None = None
    saved_segments: int = 0
    auto_recordings: int = 0
    error: str | None = None

    # ----- derived -----------------------------------------------------------------
    @property
    def has_unsaved_content(self) -> bool:
        return self.recording

    @property
    def label(self) -> str:
        """Flat name matching the spec diagram, for status text and tests."""
        if self.phase is not Phase.PREVIEW:
            return self.phase.value.capitalize()
        if self.recording and self.scoring is Scoring.ACTIVE:
            return "Both"
        if self.recording and self.scoring is Scoring.PAUSED:
            return "BothRecordingPausedScoring"
        if self.recording:
            return "Recording"
        if self.scoring is Scoring.ACTIVE:
            return "Scoring"
        if self.scoring is Scoring.PAUSED:
            return "PausedScoring"
        return "Preview"

    def allowed(self) -> frozenset[str]:
        """Commands that are currently legal; drives enabled/disabled UI state."""
        commands = set()
        for name in COMMANDS:
            try:
                apply(self, name, *_PROBE_ARGS.get(name, ()))
            except InvalidTransition:
                continue
            commands.add(name)
        return frozenset(commands)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidTransition(message)


def choose_source(state: SessionState, source: SourceKind | str) -> SessionState:
    _require(state.phase in (Phase.IDLE, Phase.ERROR), "Close the current session first")
    return SessionState(phase=Phase.PREPARING, source=SourceKind(source))


def ready(state: SessionState) -> SessionState:
    _require(state.phase is Phase.PREPARING, "Only a preparing session can become ready")
    return replace(state, phase=Phase.PREVIEW, error=None)


def fail(state: SessionState, reason: str) -> SessionState:
    _require(
        state.phase in (Phase.PREPARING, Phase.PREVIEW),
        "Only an opening or open session can fail",
    )
    # A device failure ends capture; already committed segments are kept (saved_segments).
    return replace(
        state, phase=Phase.ERROR, recording=False, scoring=Scoring.OFF, error=str(reason)
    )


def retry(state: SessionState) -> SessionState:
    _require(state.phase is Phase.ERROR, "Retry is only available after an error")
    return replace(state, phase=Phase.PREPARING, error=None)


def accept_reference(state: SessionState, reference_id: str) -> SessionState:
    _require(state.phase is Phase.PREVIEW, "Open the preview before accepting a reference")
    _require(bool(str(reference_id).strip()), "A reference identifier is required")
    # The saved reference must not silently change while scoring against it.
    _require(state.scoring is Scoring.OFF, "Stop scoring before changing the reference")
    return replace(state, reference_id=str(reference_id))


def start_recording(state: SessionState, max_auto: int | None = None) -> SessionState:
    _require(state.phase is Phase.PREVIEW, "Open the preview before recording")
    _require(not state.recording, "Already recording")
    if max_auto is not None:
        _require(state.auto_recordings < max_auto, "Maximum automatic recordings reached")
        return replace(state, recording=True, auto_recordings=state.auto_recordings + 1)
    return replace(state, recording=True)


def stop_and_save(state: SessionState) -> SessionState:
    _require(state.phase is Phase.PREVIEW and state.recording, "Nothing is being recorded")
    return replace(state, recording=False, saved_segments=state.saved_segments + 1)


def abort_unsaved(state: SessionState) -> SessionState:
    _require(state.phase is Phase.PREVIEW and state.recording, "There is no unsaved recording")
    # Only the in-progress segment is discarded; saved_segments is untouched.
    return replace(state, recording=False)


def start_scoring(state: SessionState) -> SessionState:
    _require(state.phase is Phase.PREVIEW, "Open the preview before scoring")
    _require(state.scoring is Scoring.OFF, "Scoring is already on")
    _require(state.reference_id is not None, "Accept a reference before scoring")
    return replace(state, scoring=Scoring.ACTIVE)


def stop_scoring(state: SessionState) -> SessionState:
    _require(state.phase is Phase.PREVIEW, "No open session")
    _require(state.scoring is not Scoring.OFF, "Scoring is not on")
    return replace(state, scoring=Scoring.OFF)


def pause_scoring(state: SessionState) -> SessionState:
    _require(state.scoring is Scoring.ACTIVE, "Scoring is not active")
    # Pausing scoring never pauses video recording.
    return replace(state, scoring=Scoring.PAUSED)


def resume_scoring(state: SessionState) -> SessionState:
    _require(state.scoring is Scoring.PAUSED, "Scoring is not paused")
    return replace(state, scoring=Scoring.ACTIVE)


def close(state: SessionState) -> SessionState:
    _require(state.phase in (Phase.PREVIEW, Phase.ERROR), "No open session to close")
    _require(not state.recording, "Stop & save or abort the recording before closing")
    return SessionState(saved_segments=state.saved_segments)


COMMANDS: dict[str, Callable[..., SessionState]] = {
    "choose_source": choose_source,
    "ready": ready,
    "fail": fail,
    "retry": retry,
    "accept_reference": accept_reference,
    "start_recording": start_recording,
    "stop_and_save": stop_and_save,
    "abort_unsaved": abort_unsaved,
    "start_scoring": start_scoring,
    "stop_scoring": stop_scoring,
    "pause_scoring": pause_scoring,
    "resume_scoring": resume_scoring,
    "close": close,
}


def apply(state: SessionState, command: str, *args: object) -> SessionState:
    try:
        handler = COMMANDS[command]
    except KeyError as error:
        raise InvalidTransition(f"Unknown command: {command}") from error
    return handler(state, *args)


_PROBE_ARGS: dict[str, tuple] = {
    "choose_source": (SourceKind.CAMERA,),
    "fail": ("probe",),
    "accept_reference": ("probe",),
}
