import pytest

from tango_posture.core.errors import InvalidTransition
from tango_posture.core.session_state import (
    Phase,
    Scoring,
    SessionState,
    abort_unsaved,
    accept_reference,
    apply,
    choose_source,
    close,
    fail,
    pause_scoring,
    ready,
    resume_scoring,
    retry,
    start_recording,
    start_scoring,
    stop_and_save,
)
from tango_posture.core.types import SourceKind


def test_initial_state():
    s = SessionState()
    assert s.phase is Phase.IDLE
    assert not s.recording
    assert s.scoring is Scoring.OFF
    assert s.label == "Idle"
    assert "choose_source" in s.allowed()
    assert "start_recording" not in s.allowed()


def test_prepare_and_ready():
    s0 = SessionState()
    s1 = choose_source(s0, SourceKind.CAMERA)
    assert s1.phase is Phase.PREPARING
    assert s1.source is SourceKind.CAMERA
    assert s1.label == "Preparing"

    s2 = ready(s1)
    assert s2.phase is Phase.PREVIEW
    assert s2.label == "Preview"
    assert "start_recording" in s2.allowed()


def test_recording_lifecycle():
    s = ready(choose_source(SessionState(), "camera"))
    assert not s.recording
    assert not s.has_unsaved_content

    # Start recording
    s_rec = start_recording(s)
    assert s_rec.recording
    assert s_rec.has_unsaved_content
    assert s_rec.label == "Recording"

    # Stop and save commits segment
    s_saved = stop_and_save(s_rec)
    assert not s_saved.recording
    assert s_saved.saved_segments == 1
    assert not s_saved.has_unsaved_content
    assert s_saved.label == "Preview"

    # Abort unsaved recording
    s_rec2 = start_recording(s_saved)
    s_abort = abort_unsaved(s_rec2)
    assert not s_abort.recording
    assert s_abort.saved_segments == 1  # Prior segment preserved!


def test_scoring_requires_reference():
    s = ready(choose_source(SessionState(), "camera"))
    # Cannot start scoring without reference
    with pytest.raises(InvalidTransition, match="Accept a reference"):
        start_scoring(s)

    s_ref = accept_reference(s, "ref-uuid-123")
    assert s_ref.reference_id == "ref-uuid-123"

    s_score = start_scoring(s_ref)
    assert s_score.scoring is Scoring.ACTIVE
    assert s_score.label == "Scoring"

    # Cannot silently change reference while scoring is active
    with pytest.raises(InvalidTransition, match="Stop scoring before"):
        accept_reference(s_score, "new-ref")


def test_orthogonal_recording_and_scoring():
    s = ready(choose_source(SessionState(), "camera"))
    s = accept_reference(s, "ref-uuid")

    # Start scoring
    s = start_scoring(s)
    assert s.label == "Scoring"

    # Start recording while scoring
    s = start_recording(s)
    assert s.label == "Both"
    assert s.recording
    assert s.scoring is Scoring.ACTIVE

    # Pause scoring does NOT pause recording
    s = pause_scoring(s)
    assert s.label == "BothRecordingPausedScoring"
    assert s.recording
    assert s.scoring is Scoring.PAUSED

    # Resume scoring
    s = resume_scoring(s)
    assert s.label == "Both"

    # Stop recording commits segment, keeps scoring active
    s = stop_and_save(s)
    assert s.label == "Scoring"
    assert s.saved_segments == 1
    assert s.scoring is Scoring.ACTIVE


def test_error_and_retry():
    s = ready(choose_source(SessionState(), "camera"))
    s = start_recording(s)

    s_err = fail(s, "Device unplugged")
    assert s_err.phase is Phase.ERROR
    assert not s_err.recording
    assert s_err.error == "Device unplugged"

    s_retry = retry(s_err)
    assert s_retry.phase is Phase.PREPARING
    assert s_retry.error is None


def test_close_guard():
    s = ready(choose_source(SessionState(), "camera"))
    s_rec = start_recording(s)

    # Cannot close while actively recording
    with pytest.raises(InvalidTransition, match="Stop & save or abort"):
        close(s_rec)

    s_stopped = stop_and_save(s_rec)
    s_closed = close(s_stopped)
    assert s_closed.phase is Phase.IDLE
    assert s_closed.saved_segments == 1


def test_apply_generic_dispatcher():
    s = SessionState()
    s = apply(s, "choose_source", SourceKind.CAMERA)
    assert s.phase is Phase.PREPARING

    with pytest.raises(InvalidTransition, match="Unknown command"):
        apply(s, "non_existent_command")
