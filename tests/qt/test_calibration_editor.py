import json

import cv2
import numpy as np
import pytest
from conftest import frame
from PySide6.QtWidgets import QApplication, QPushButton

from tango_posture.archive import append_segment, read_analytics, read_manifest
from tango_posture.calibration import profile
from tango_posture.config import Config
from tango_posture.desktop_qt.calibration_editor import CalibrationEditor
from tango_posture.desktop_qt.guided_calibration import GuidedCalibration
from tango_posture.desktop_qt.router import Router
from tango_posture.desktop_qt.screens.library import LibraryScreen
from tango_posture.desktop_qt.service_bridge import ServiceBridge
from tango_posture.models import MANIFEST
from tango_posture.range_profiles import read_range_profile
from tango_posture.reference_archives import promote_reference


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication(["-platform", "offscreen"])


@pytest.fixture
def cached_reference(tmp_path):
    video, report, practice = (tmp_path / name for name in ("video.mp4", "report.md", "practice.tp"))
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter.fourcc(*"mp4v"), 10, (640, 480))
    assert writer.isOpened()
    entries = []
    for index in range(40):
        writer.write(np.zeros((480, 640, 3), dtype=np.uint8))
        pose = frame(index / 10).poses[0]
        entries.append({"poses": [
            {name: [p.x, p.y, p.z, p.visibility, p.presence] for name, p in pose.items()}
        ]})
    writer.release()
    report.write_text("# Cached source\n")
    append_segment(practice, report, video, None, [i / 10 for i in range(40)], analytics={
        "model_hash": MANIFEST["sha256"],
        "capture_profile": profile(Config(), 640, 480, "video"),
        "frames": entries, "max_gap": 0.5,
    })
    reference = tmp_path / "references" / "Reference.tpr"
    promote_reference(practice, reference, "Teacher reference")
    return reference


def test_shared_editor_reuses_cache_and_saves_independent_profiles(qapp, tmp_path, cached_reference, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Range editing must not rerun inference")
    monkeypatch.setattr("tango_posture.app.MediaPipeEstimator", forbidden)
    cfg = Config(data_dir=str(tmp_path))
    saved = []
    for name in ("Solo walking", "Close embrace"):
        editor = CalibrationEditor(cached_reference, cfg)
        editor.show()
        qapp.processEvents()
        assert editor._stats is not None
        assert not editor.live_check.isEnabled()
        editor._save()
        assert editor.saved_path is None
        editor.passages_input.setText("0-1; 2-3")
        editor.lower_spin.setValue(-100)
        editor.upper_spin.setValue(100)
        editor._approve()
        editor.name_input.setText(name)
        editor.context_combo.setCurrentText(name)
        editor._save()
        assert editor.saved_path is not None
        saved.append(read_range_profile(editor.saved_path))
    assert saved[0].id != saved[1].id
    assert saved[0].metrics["head_pitch"].lower == -100
    assert len(saved[0].metrics["head_pitch"].passages) == 2
    assert not saved[0].metrics["head_pitch"].camera_check_passed


def test_library_distinguishes_profiles_from_replayable_references(qapp, tmp_path, cached_reference):
    cfg = Config(data_dir=str(tmp_path))
    editor = CalibrationEditor(cached_reference, cfg)
    editor._approve()
    editor._save()
    bridge = ServiceBridge(cfg)
    assert not bridge.list_references()[0].scoring_ready
    screen = LibraryScreen(bridge, Router())
    paths = {entry["path"].suffix for entry in screen.practices}
    assert {".tpc", ".tpr"} <= paths
    row = next(i for i in range(screen.table.rowCount()) if screen.table.item(i, 4).text() == ".tpc")
    buttons = screen.table.cellWidget(row, 3).findChildren(QPushButton)
    assert buttons[0].text() == "Profile details"
    assert all(button.text() != "Open in Review" for button in buttons)
    assert read_manifest(editor.saved_path)["segments"] == []
    screen.close()


def test_guided_dialog_never_opens_camera_until_requested(qapp, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Opening the dialog must not open camera/model")
    monkeypatch.setattr("tango_posture.desktop_qt.guided_calibration.QtPracticeWorker", forbidden)
    dialog = GuidedCalibration(ServiceBridge(Config(data_dir=str(tmp_path))))
    assert dialog._thread is None
    assert not dialog.start.isEnabled()
    assert dialog.preview.isEnabled()
    dialog.reject()


def test_explicit_dancer_restart_is_confirmed_and_persisted(qapp, tmp_path, cached_reference):
    from zipfile import ZipFile

    editor = CalibrationEditor(cached_reference, Config(data_dir=str(tmp_path)))
    editor._approve()
    editor.scrubber.setValue(20)
    editor._restart_track()
    assert not editor.approved
    before = cached_reference.read_bytes()
    editor._save_selection()
    assert cached_reference.read_bytes() == before
    editor.confirm_identity.setChecked(True)
    editor._save_selection()
    with ZipFile(cached_reference) as archive:
        descriptor = json.loads(archive.read("reference.json"))
    assert descriptor["dancer_anchors"] == [{"frame": 0, "dancer": 0}, {"frame": 20, "dancer": 0}]
    editor.reject()
    reopened = CalibrationEditor(cached_reference, Config(data_dir=str(tmp_path)))
    assert reopened._anchors == {0: 0, 20: 0}
    reopened.reject()


def test_promoted_excerpt_has_matching_video_and_cached_frame_indices(qapp, tmp_path, cached_reference):
    source = tmp_path / "practice.tp"
    before = source.read_bytes()
    destination = tmp_path / "references" / "Excerpt.tpr"
    promote_reference(source, destination, "Walking excerpt", start_frame=5, end_frame=15)
    analytics = read_analytics(destination, 0)
    assert len(analytics["frames"]) == 10
    assert analytics["frame_timestamps"] == pytest.approx([index / 10 for index in range(5, 15)])
    editor = CalibrationEditor(destination, Config(data_dir=str(tmp_path)))
    assert editor._reader.count == len(editor.evidence.frames) == 10
    editor.reject()
    assert source.read_bytes() == before


@pytest.mark.parametrize("responsive", [False, True])
def test_guided_holds_require_a_repeatable_change_before_continuing(qapp, tmp_path, responsive):
    from dataclasses import replace

    dialog = GuidedCalibration(ServiceBridge(Config(data_dir=str(tmp_path))))
    for index in range(240):
        observation = frame(index / 10)
        if responsive and (8 <= observation.timestamp < 13 or 18 <= observation.timestamp < 23):
            point = observation.poses[0]["nose"]
            observation.poses[0]["nose"] = replace(point, x=point.x + 0.06)
        dialog._frames.append(observation)
    dialog._latest = 0
    dialog._begin()
    for timestamp in (3, 8, 13, 18, 23):
        dialog._latest = timestamp
        dialog._poll()
    assert len(dialog._holds["Head"]) == 4
    assert dialog.next.isEnabled() is responsive
    assert dialog.retry.isEnabled() and dialog.skip.isEnabled()
    dialog.context.setCurrentText("Solo walking")
    dialog._step = 4
    dialog._next()
    assert dialog._walking
    dialog._latest = 33
    dialog._poll()
    assert dialog._review_requested
    assert dialog._holds["Walking"][0].start == 25
    dialog.reject()
