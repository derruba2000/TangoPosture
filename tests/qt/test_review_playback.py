"""Offscreen replay tests using generated local media, never a camera."""

import json
import threading
import time
from types import SimpleNamespace
from zipfile import ZipFile

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget

from tango_posture.archive import append_segment, file_sha256, read_analytics, read_manifest
from tango_posture.config import Config
from tango_posture.desktop_qt.library.archives import archive_report, migrate_legacy_archives
from tango_posture.desktop_qt.review.analysis import ReviewAnalysis, analyse_review_clip
from tango_posture.desktop_qt.review.playback import ReviewPlayback
from tango_posture.desktop_qt.router import Router
from tango_posture.desktop_qt.screens.review import ReviewScreen
from tango_posture.desktop_qt.service_bridge import ServiceBridge
from tango_posture.movement import render_floor_guide
from tango_posture.references.coach import render_coach_frame_overlay
from tango_posture.review import write_metadata


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication(["-platform", "offscreen"])


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "sequence.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"MJPG"), 10, (64, 48))
    assert writer.isOpened()
    try:
        for index in range(12):
            writer.write(np.full((48, 64, 3), index * 15, np.uint8))
    finally:
        writer.release()
    return path


@pytest.fixture
def screen(qapp):
    widget = ReviewScreen(ServiceBridge(Config()), Router())
    yield widget
    widget.close()


def make_archive(tmp_path, video):
    path = tmp_path / "practice.tp"
    report = tmp_path / "report.md"
    report.write_text("# Stored report\nActual recorded findings.\n", encoding="utf-8")
    append_segment(path, report, video, video, [0.0, 0.1])
    return path, report


def test_empty_review_has_no_fake_playback_or_insights(screen):
    assert screen.duration_sec == 0
    assert screen.time_label.text() == "00:00.0 / 00:00.0"
    assert not screen.play_btn.isEnabled()
    assert not screen.scrubber.isEnabled()
    assert not screen.compare_cb.isEnabled()
    assert not screen.analytics_btn.isEnabled()
    assert not screen.stream_combo.isEnabled()
    assert screen.report_view.toPlainText() == ""
    assert all(value.text() == "Not observed" for value in screen.metric_values.values())
    assert "when a frame is analyzed" in screen.alerts_text.text()
    screen._toggle_playback()
    assert not screen.is_playing
    assert not screen.timer.isActive()


@pytest.mark.parametrize("archived", [False, True])
def test_review_posture_analysis_runs_off_ui_thread_and_syncs_overlay(
    screen, video, monkeypatch, qapp, tmp_path, archived
):
    observed = {}
    coach_data = {
        "width": 64,
        "height": 48,
        "frames": {
            "0": {
                "head_angle": -8.0,
                "shoulder_elev_pct": 24.0,
                "axis_curve_smoothed": 0.12,
                "knee_bend_deg": 14.5,
                "message": "BEND YOUR KNEES",
                "poses": [{"nose": [0.5, 0.1, 0.0]}],
                "notes": ["BEND YOUR KNEES", "Knees: STRAIGHT (14.5 deg)"],
            }
        },
    }

    def analyze(path, cfg, *, selected_out, progress_callback, cancel_check):
        observed["thread"] = threading.get_ident()
        observed["path"] = path
        observed["selected_out"] = selected_out
        observed["cancel_check"] = cancel_check
        progress_callback(50, 100, "Analyzing posture")
        return ReviewAnalysis(coach_data, {"pivots": [], "steps": []})

    def render(image, data, frame_index, cfg, **kwargs):
        observed["overlay_frame"] = frame_index
        image[0, 0] = (0, 0, 255)
        return image

    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.review.analyse_review_clip", analyze
    )
    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.review.render_coach_frame_overlay", render
    )
    path = video
    if archived:
        report = tmp_path / "report.md"
        report.write_text("# Archived practice\n")
        path = tmp_path / "practice.tp"
        append_segment(path, report, video, None, [index / 10 for index in range(12)])
    screen.load_archive(path)
    screen._start_posture_analysis()

    deadline = time.monotonic() + 3
    while screen._analysis_thread is not None and time.monotonic() < deadline:
        qapp.processEvents()
        screen._poll_posture_analysis()
        time.sleep(0.01)

    assert screen._analysis_thread is None
    assert observed["thread"] != threading.get_ident()
    assert observed["path"].read_bytes() == video.read_bytes()
    assert observed["selected_out"] == pytest.approx(1.2)
    assert not observed["cancel_check"]()
    assert observed["overlay_frame"] == 0
    assert screen.metric_values["head_angle"].text() == "-8.0°"
    assert screen.metric_values["shoulder_elev_pct"].text() == "+24.0%"
    assert screen.metric_values["axis_curve_smoothed"].text() == "+12.0%"
    assert screen.metric_values["knee_bend_deg"].text() == "14.5°"
    assert screen.alerts_text.text() == "BEND YOUR KNEES"
    assert "Knees: STRAIGHT" in screen.insights_text.text()
    assert screen.primary_stage._image.pixelColor(0, 0).red() == 255
    assert screen.splitter.count() == 2
    assert screen.inspector.maximumWidth() == 420
    if archived:
        assert read_analytics(path, 0)["coach_data"] == coach_data
        screen.load_archive(path)
        assert screen.metric_values["head_angle"].text() == "-8.0°"
        assert screen._movement_data == {"pivots": [], "steps": []}


def test_review_analysis_uses_temporary_storage_and_shared_analyzer(
    video, tmp_path, monkeypatch
):
    observed = {}
    coach_data = {"frames": {}}

    def analyze_imported_clip(**kwargs):
        observed.update(kwargs)
        return SimpleNamespace(coach_data=coach_data, movement_data={"steps": [{"frame": 1}]})

    monkeypatch.setattr(
        'tango_posture.desktop_qt.review.analysis.analyse_imported_clip',
        analyze_imported_clip,
    )
    result = analyse_review_clip(
        video,
        Config(data_dir=str(tmp_path)),
        selected_out=1.0,
        progress_callback=lambda *_: None,
        cancel_check=lambda: False,
    )

    assert result.coach_data is coach_data
    assert result.movement_data == {"steps": [{"frame": 1}]}
    assert observed["video_path"] == video
    assert observed["selected_in"] == 0.0
    assert observed["selected_out"] == 1.0
    assert observed["analysis_mode"] == "movement"
    assert observed["analyse_soundtrack"] is False
    assert not observed["report_dir"].exists()


def test_review_draws_steps_and_pivots_and_can_toggle_them(screen, video):
    screen.load_archive(video)
    screen.show_coach_cb.setChecked(False)
    screen._movement_data = {
        "width": 64, "height": 48,
        "steps": [{"frame": 0, "time": 0.0, "hx": 18, "hy": 42,
                   "tx": 22, "ty": 35, "side": "left"}],
        "pivots": [{"start_frame": 0, "end_frame": 5, "direction": "clockwise"}],
        "hip_frames": [{"frame": 0, "lx": 20, "ly": 20, "rx": 44, "ry": 20, "scale": 30}],
    }
    screen._show_frame(0)
    with_movement = screen.primary_stage._image.copy()
    screen.show_steps_cb.setChecked(False)
    screen.show_pivots_cb.setChecked(False)
    without_movement = screen.primary_stage._image.copy()
    assert with_movement != without_movement
    screen.show_steps_cb.setChecked(True)
    assert screen.primary_stage._image != without_movement
    screen.show_steps_cb.setChecked(False)
    screen.show_pivots_cb.setChecked(True)
    assert screen.primary_stage._image != without_movement


def test_lines_only_uses_floor_and_skeleton_without_text(screen, video, monkeypatch):
    screen.load_archive(video)
    screen.scrubber.setValue(8)
    frame = np.full((48, 64, 3), 120, np.uint8)
    floor = render_floor_guide(frame)
    screen._coach_data = {
        "width": 64, "height": 48,
        "frames": {"8": {"poses": [{
            "left_shoulder": [0.4, 0.25, 0, 1, 1],
            "left_hip": [0.4, 0.6, 0, 1, 1],
        }], "message": "LOWER YOUR SHOULDERS"}},
    }

    def no_text(*args, **kwargs):
        pytest.fail("Lines-only replay must not draw text")

    monkeypatch.setattr(cv2, "putText", no_text)
    screen.lines_only_cb.setChecked(True)
    expected = render_coach_frame_overlay(
        floor, screen._coach_data, 8, Config(), lines_only=True
    )
    image = screen.primary_stage._image
    assert image.width() == 64
    assert image.pixelColor(5, 5).red() == int(expected[5, 5, 2])
    assert image.pixelColor(26, 20).green() > image.pixelColor(26, 20).red()
    assert not np.array_equal(expected, floor)


def test_archive_comparison_uses_raw_video_without_stream_selector(screen, video, tmp_path):
    path, _ = make_archive(tmp_path, video)
    screen.load_archive(path)
    screen._load_comparison(path)
    assert not hasattr(screen, "comparison_stream_combo")
    assert screen._comparison.reader.path == screen._comparison.variants["Raw video"].resolve()
    screen.lines_only_cb.setChecked(True)
    assert screen.stream_combo.currentText() == "Raw video"


@pytest.mark.parametrize("as_reference", [False, True])
def test_library_import_packages_matching_clips_and_rebased_analytics(
    video, tmp_path, monkeypatch, screen, as_reference,
):
    cfg = Config(data_dir=str(tmp_path))
    report = tmp_path / "reports" / "report_import.md"
    report.parent.mkdir()
    report.write_text("# Imported steps\n")
    write_metadata(report, f"import:{video.name}", video, None, [], extra={
        "clip_in": 0.2, "clip_out": 0.8, "is_imported_reference": True,
        "source_sha256": file_sha256(video),
    })
    coach = {"width": 64, "height": 48, "frames": {
        "2": {"head_angle": 5, "poses": []}, "7": {"head_angle": 7, "poses": []},
    }}
    movement = {
        "steps": [{"frame": 4, "time": 0.4, "hx": 18, "hy": 42,
                   "tx": 22, "ty": 35, "side": "left"}],
        "pivots": [{"start_frame": 1, "end_frame": 6, "direction": "clockwise"}],
        "hip_frames": [], "cut_frames": [3], "width": 64, "height": 48,
    }
    monkeypatch.setattr(
        'tango_posture.desktop_qt.library.archives.load_report_coach_data', lambda _: coach,
    )
    monkeypatch.setattr(
        'tango_posture.desktop_qt.library.archives.load_report_movement_data', lambda _: movement,
    )
    source_bytes = video.read_bytes()
    path = archive_report(report, cfg, lambda: False, as_reference=as_reference)
    assert path.suffix == (".tpr" if as_reference else ".tp")
    analytics = read_analytics(path, 0)
    assert set(analytics["coach_data"]["frames"]) == {"0", "5"}
    assert analytics["movement_data"]["steps"][0]["frame"] == 2
    assert analytics["movement_data"]["steps"][0]["time"] == pytest.approx(0.2)
    assert analytics["movement_data"]["pivots"][0]["start_frame"] == 0
    assert analytics["movement_data"]["cut_frames"] == [1]
    assert analytics["frame_timestamps"] == pytest.approx([0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
    assert video.read_bytes() == source_bytes
    assert report.is_file()
    screen.load_archive(path)
    assert screen.metric_values["head_angle"].text() == "+5.0°"
    for label in screen._playback.variants:
        assert screen._playback.select_variant(label).count == 6
        assert len(screen._playback.timestamps) == 6
    assert read_manifest(path)["segments"][0]["lines"] == "lines_00.mp4"
    before = path.read_bytes()
    count, errors = migrate_legacy_archives(cfg, lambda: False)
    assert count == 0 and errors == []
    assert path.read_bytes() == before


def test_review_posture_analysis_can_be_cancelled(screen, video, monkeypatch, qapp):
    started = threading.Event()

    def analyze(path, cfg, *, selected_out, progress_callback, cancel_check):
        started.set()
        while not cancel_check():
            time.sleep(0.005)
        raise RuntimeError("analysis canceled")

    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.review.analyse_review_clip", analyze
    )
    screen.load_archive(video)
    screen._start_posture_analysis()

    deadline = time.monotonic() + 3
    while not started.is_set() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert started.is_set()
    screen._cancel_posture_analysis()

    deadline = time.monotonic() + 3
    while screen._analysis_thread is not None and time.monotonic() < deadline:
        qapp.processEvents()
        screen._poll_posture_analysis()
        time.sleep(0.01)

    assert screen._analysis_thread is None
    assert screen.analysis_status.text() == "Posture analysis canceled."
    assert all(value.text() == "Not observed" for value in screen.metric_values.values())


def test_local_video_decodes_seeks_and_plays_to_real_end(screen, video):
    now = [100.0]
    screen._clock = lambda: now[0]
    screen.load_archive(video)
    reader = screen._playback.reader
    assert reader is not None
    assert screen.duration_sec == pytest.approx(1.2)
    assert screen.scrubber.maximum() == 11
    assert screen.primary_stage._image is not None
    assert screen.primary_stage._image.pixelColor(20, 20).red() == pytest.approx(0, abs=3)
    first_image = screen.primary_stage._image
    screen.scrubber.setValue(8)
    assert reader.index == 8
    assert screen.current_time_sec == pytest.approx(0.8)
    assert screen.primary_stage._image.pixelColor(20, 20).red() == pytest.approx(120, abs=3)
    assert first_image.pixelColor(20, 20).red() == pytest.approx(0, abs=3)
    screen.scrubber.setValue(2)
    assert reader.index == 2
    assert screen.time_label.text() == "00:00.2 / 00:01.2"

    screen._toggle_playback()
    assert screen.timer.isActive()
    now[0] += 0.35
    screen._on_tick()
    assert reader.index == 5
    screen._toggle_playback()
    assert not screen.is_playing
    assert not screen.timer.isActive()
    now[0] += 20
    screen._on_tick()
    assert reader.index == 5

    screen._toggle_playback()
    screen.scrubber.setValue(1)  # Seeking while playing rebases the clock.
    now[0] += 0.35
    screen._on_tick()
    assert reader.index == 4
    now[0] += 2
    screen._on_tick()
    assert not screen.is_playing
    assert not screen.timer.isActive()
    assert reader.index == 11
    assert screen.current_time_sec == pytest.approx(1.2)
    assert screen.time_label.text() == "00:01.2 / 00:01.2"
    screen._toggle_playback()
    assert reader.index == 0
    assert screen.is_playing


def test_replay_speed_and_frame_step_controls(screen, video):
    now = [50.0]
    screen._clock = lambda: now[0]
    screen.load_archive(video)
    screen.speed_combo.setCurrentIndex(screen.speed_combo.findData(2.0))
    screen._toggle_playback()
    now[0] += 0.35
    screen._on_tick()
    reader = screen._playback.reader
    assert reader is not None
    assert reader.index == 7

    screen._step_frame(-1)
    assert not screen.is_playing
    assert reader.index == 6
    assert screen.current_time_sec == pytest.approx(0.6)
    screen._step_frame(1)
    assert reader.index == 7


def test_synchronized_side_by_side_comparison(screen, video, monkeypatch):
    screen.load_archive(video)
    screen.scrubber.setValue(8)
    monkeypatch.setattr(
        screen.bridge, "choose_library_archive",
        lambda *args: video,
    )
    screen.compare_btn.click()

    comparison = screen._comparison.reader
    assert comparison is not None
    assert screen.compare_cb.isChecked()
    assert not screen.comparison_stage.isHidden()
    assert comparison.index == 8

    screen.scrubber.setValue(3)
    assert comparison.index == 3
    screen.compare_cb.setChecked(False)
    assert screen.comparison_stage.isHidden()
    screen.compare_cb.setChecked(True)
    assert not screen.comparison_stage.isHidden()
    assert comparison.index == 3


def test_export_copies_selected_video_without_modifying_source(screen, video, tmp_path, monkeypatch):
    destination = tmp_path / "exported.avi"
    screen.load_archive(video)
    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.review.QFileDialog.getSaveFileName",
        lambda *args: (str(destination), "Video files"),
    )
    screen._export_video()
    assert destination.read_bytes() == video.read_bytes()
    assert video.is_file()


def test_standalone_import_report_relinks_by_hash_and_seeks(screen, video, tmp_path, monkeypatch):
    source = tmp_path / "source.avi"
    source.write_bytes(video.read_bytes())
    report = tmp_path / "report_import.md"
    report.write_text(
        "# Imported clip\n\nObservation at `00:00.8 (0.8s)`.\n", encoding="utf-8"
    )
    timestamps = [index / 10 for index in range(12)]
    write_metadata(
        report,
        "import:source.avi",
        source,
        None,
        timestamps,
        extra={
            "is_imported_reference": True,
            "source_sha256": file_sha256(source),
            "clip_in": 0.0,
            "clip_out": 1.2,
        },
    )
    moved = tmp_path / "moved.avi"
    source.rename(moved)
    messages = []
    screen.bridge.status_message.connect(messages.append)

    screen.load_archive(report)
    assert screen._playback.reader is None
    assert screen.relink_btn.isEnabled()
    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.review.QFileDialog.getOpenFileName",
        lambda *args: (str(moved), "Video files"),
    )
    screen._relink_report_video()

    reader = screen._playback.reader
    assert reader is not None
    assert screen._playback._report is not None
    assert screen._playback._report.source_sha256 == file_sha256(moved)
    screen.report_view.anchorClicked.emit(QUrl("seek:0.800000"))
    assert reader.index == 8
    assert any("verified SHA-256" in message for message in messages)


def test_calibration_archive_can_be_used_as_replay_comparison(video, tmp_path):
    path = tmp_path / "reference.tpc"
    report = "# Reference\n\nCalibration capture.\n"
    metadata = {
        "version": 1,
        "report_file": "report_00.md",
        "raw_video": "video_00.mp4",
        "overlay_video": None,
        "source_video": None,
        "frame_timestamps": [index / 10 for index in range(12)],
    }
    manifest = {
        "version": 1,
        "kind": "calibration",
        "name": "Reference",
        "reference": "reference.json",
        "segments": [
            {
                "index": 0,
                "video": "video_00.mp4",
                "overlay": None,
                "report": "report_00.md",
                "metadata": "report_00.metadata.json",
            }
        ],
    }
    with ZipFile(path, "w") as archive, video.open("rb") as stream:
        archive.writestr("video_00.mp4", stream.read())
        archive.writestr("reference.json", "{}")
        archive.writestr("report_00.md", report)
        archive.writestr("report_00.metadata.json", json.dumps(metadata))
        archive.writestr("manifest.json", json.dumps(manifest))

    playback = ReviewPlayback()
    try:
        playback.open(path)
        playback.select_segment(0)
        reader = playback.select_variant("Raw video")
        assert reader.count == 12
    finally:
        playback.close()


def test_archive_frame_timestamps_drive_nonuniform_playback_timeline(screen, video, tmp_path):
    path = tmp_path / "timed-practice.tp"
    report = tmp_path / "report.md"
    report.write_text(
        "# Timestamped recording\n\nEvent at `00:00.8 (0.8s)`.\n", encoding="utf-8"
    )
    timestamps = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]
    append_segment(path, report, video, None, timestamps)

    now = [100.0]
    screen._clock = lambda: now[0]
    screen.load_archive(path)
    assert screen.duration_sec == pytest.approx(1.4)
    assert screen._playback.timestamps == timestamps

    screen._toggle_playback()
    now[0] += 0.65
    screen._on_tick()
    reader = screen._playback.reader
    assert reader is not None
    assert reader.index == 5
    assert screen.current_time_sec == pytest.approx(0.5)

    now[0] += 0.15
    screen._on_tick()
    assert reader.index == 6
    assert screen.current_time_sec == pytest.approx(0.8)
    assert "00:00.8" in screen.report_view.toPlainText()
    screen.report_view.anchorClicked.emit(QUrl("seek:0.800000"))
    assert reader.index == 6
    screen.scrubber.setValue(8)
    assert screen.current_time_sec == pytest.approx(1.0)

    screen._toggle_playback()
    now[0] = 101.5
    screen._on_tick()
    assert reader.index == 11
    assert screen.current_time_sec == pytest.approx(1.4)
    assert not screen.is_playing


def test_archive_recordings_variants_and_reports_are_real_and_owned(screen, video, tmp_path):
    path, report = make_archive(tmp_path, video)
    report.write_text("# Second recording\n", encoding="utf-8")
    append_segment(path, report, video, None, [0.0, 0.1])
    screen.load_archive(path)
    archive = screen._playback.archive
    overlay_reader = screen._playback.reader
    assert archive is not None
    assert overlay_reader is not None
    folder = archive.folder
    assert folder.is_dir()
    assert screen.segment_combo.count() == 2
    assert [screen.stream_combo.itemText(i) for i in range(screen.stream_combo.count())] == [
        "Overlay video",
        "Raw video",
    ]
    assert "Stored report" in screen.report_view.toPlainText()
    assert "Actual recorded findings." in screen.report_view.toPlainText()
    screen.stream_combo.setCurrentText("Raw video")
    assert not overlay_reader.cap.isOpened()
    raw_reader = screen._playback.reader
    assert raw_reader is not None
    assert raw_reader.path.name == "video_00.mp4"
    screen.segment_combo.setCurrentIndex(1)
    assert not raw_reader.cap.isOpened()
    assert screen.stream_combo.count() == 1
    assert screen.stream_combo.currentText() == "Raw video"
    assert "Unavailable in this recording: Overlay video." in screen.status_label.text()
    assert "Second recording" in screen.report_view.toPlainText()
    assert "frame timestamps do not match" in screen.status_label.text()
    second_reader = screen._playback.reader
    screen.load_archive(video)
    assert second_reader is not None and not second_reader.cap.isOpened()
    assert not folder.exists()
    assert screen._playback.archive is None
    assert screen.report_view.toPlainText() == ""


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "unsupported",
        "corrupt_video",
        "corrupt_archive",
        "empty_archive",
        "missing_member",
    ],
)
def test_open_failure_clears_previous_media_and_reports(screen, video, tmp_path, kind):
    archive_path, _ = make_archive(tmp_path, video)
    screen.load_archive(archive_path)
    archive = screen._playback.archive
    reader = screen._playback.reader
    assert archive is not None and reader is not None
    path = tmp_path / "bad.mp4"
    if kind == "unsupported":
        path = tmp_path / "notes.txt"
        path.write_text("not a supported replay file", encoding="utf-8")
    elif kind == "corrupt_video":
        path.write_bytes(b"not video")
    elif kind == "corrupt_archive":
        path = tmp_path / "bad.tp"
        path.write_bytes(b"not a zip")
    elif kind in ("empty_archive", "missing_member"):
        path = tmp_path / "bad.tp"
        segments = []
        if kind == "missing_member":
            segments = [
                {
                    "index": 0,
                    "video": "video_00.mp4",
                    "overlay": None,
                    "report": "report_00.md",
                    "metadata": "report_00.metadata.json",
                }
            ]
        with ZipFile(path, "w") as stored:
            stored.writestr("manifest.json", json.dumps({"version": 1, "segments": segments}))
    screen.load_archive(path)
    assert "Replay unavailable:" in screen.status_label.text()
    assert not reader.cap.isOpened()
    assert not archive.folder.exists()
    assert screen._playback.reader is None
    assert screen._playback.archive is None
    assert screen.primary_stage._image is None
    assert screen.duration_sec == 0
    assert not screen.play_btn.isEnabled()
    assert not screen.scrubber.isEnabled()
    assert screen.report_view.toPlainText() == ""


def test_report_only_recording_is_explicit_and_other_recordings_can_still_play(
    screen,
    video,
    tmp_path,
):
    path = tmp_path / "report-only.tp"
    report = tmp_path / "report.md"
    report.write_text("# Real report without media\n", encoding="utf-8")
    append_segment(path, report, None, None, [])
    append_segment(path, report, video, None, [])
    screen.load_archive(path)
    assert "no raw or overlay video available" in screen.status_label.text()
    assert not screen.play_btn.isEnabled()
    assert not screen.stream_combo.isEnabled()
    assert screen.primary_stage._image is None
    assert "Real report without media" in screen.report_view.toPlainText()
    screen.segment_combo.setCurrentIndex(1)
    assert screen.play_btn.isEnabled()
    assert screen._playback.reader is not None


def test_bad_overlay_is_not_replaced_with_fake_content_and_raw_can_be_selected(
    screen,
    video,
    tmp_path,
):
    path = tmp_path / "bad-overlay.tp"
    report = tmp_path / "report.md"
    report.write_text("# Real report", encoding="utf-8")
    overlay = tmp_path / "overlay.mp4"
    overlay.write_bytes(b"broken video")
    append_segment(path, report, video, overlay, [])
    screen.load_archive(path)
    assert "Replay unavailable:" in screen.status_label.text()
    assert screen.primary_stage._image is None
    assert not screen.play_btn.isEnabled()
    assert screen.duration_sec == 0
    screen.stream_combo.setCurrentText("Raw video")
    assert screen.play_btn.isEnabled()
    assert screen.duration_sec == pytest.approx(1.2)
    assert screen._playback.reader.path.name == "video_00.mp4"


def test_archive_metadata_cannot_supply_external_unarchived_variants(screen, video, tmp_path):
    path = tmp_path / "external.tp"
    with ZipFile(path, "w") as stored:
        stored.writestr(
            "manifest.json",
            json.dumps(
                {
                    "version": 1,
                    "segments": [
                        {
                            "index": 0,
                            "video": None,
                            "overlay": None,
                            "report": "report_00.md",
                            "metadata": "report_00.metadata.json",
                        }
                    ],
                }
            ),
        )
        stored.writestr("report_00.md", "# Real report")
        stored.writestr(
            "report_00.metadata.json",
            json.dumps(
                {
                    "version": 1,
                    "report_file": "report_00.md",
                    "raw_video": str(video),
                    "overlay_video": str(video),
                    "frame_timestamps": [],
                }
            ),
        )
    screen.load_archive(path)
    assert "no raw or overlay video available" in screen.status_label.text()
    assert not screen.play_btn.isEnabled()
    assert screen.stream_combo.count() == 0


@pytest.mark.parametrize("operation", ["playback", "seek"])
def test_decode_failure_stops_and_releases(screen, video, monkeypatch, operation):
    now = [0.0]
    screen._clock = lambda: now[0]
    screen.load_archive(video)
    reader = screen._playback.reader
    assert reader is not None

    def broken_frame(index):
        raise ValueError(f"Cannot decode video frame {index}")

    monkeypatch.setattr(reader, "frame", broken_frame)
    if operation == "playback":
        screen._toggle_playback()
        now[0] = 0.5
        screen._on_tick()
    else:
        screen.scrubber.setValue(5)
    assert "Cannot decode video frame 5" in screen.status_label.text()
    assert not screen.timer.isActive()
    assert not screen.is_playing
    assert not reader.cap.isOpened()
    assert screen.primary_stage._image is None
    assert not screen.play_btn.isEnabled()


def test_navigation_parent_close_and_destruction_release_resources(qapp, video, tmp_path):
    stack = QStackedWidget()
    screen = ReviewScreen(ServiceBridge(Config()), Router())
    stack.addWidget(screen)
    stack.addWidget(QWidget())
    stack.show()
    qapp.processEvents()
    path, _ = make_archive(tmp_path, video)
    screen.load_archive(path)
    archive = screen._playback.archive
    reader = screen._playback.reader
    screen._toggle_playback()
    stack.setCurrentIndex(1)
    assert archive is not None and reader is not None
    assert not reader.cap.isOpened()
    assert not archive.folder.exists()
    assert not screen.timer.isActive()

    stack.setCurrentIndex(0)
    qapp.processEvents()
    assert screen.current_archive_path == path
    assert screen._playback.reader is not None
    assert screen._playback.archive is not None
    screen.load_archive(path)
    archive = screen._playback.archive
    reader = screen._playback.reader
    stack.close()
    assert archive is not None and reader is not None
    assert not reader.cap.isOpened()
    assert not archive.folder.exists()

    # An unshown widget can be destroyed without receiving a hide/close event.
    playback = ReviewPlayback()
    unshown = ReviewScreen(ServiceBridge(Config()), Router(), playback=playback)
    unshown.load_archive(path)
    archive = playback.archive
    reader = playback.reader
    unshown.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert archive is not None and reader is not None
    assert not reader.cap.isOpened()
    assert not archive.folder.exists()
    playback.close()  # Cleanup is idempotent.
