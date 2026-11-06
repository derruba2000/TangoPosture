from dataclasses import replace

import pytest

from tango_posture.config import Config
from tango_posture.practice import GuiDisplay, PracticeOptions, next_recordings


def test_saved_reference_requires_fresh_camera_confirmation():
    options = PracticeOptions(reference_mode="saved", calibration="my-reference")
    with pytest.raises(ValueError, match="camera placement"):
        options.arguments(Config(), "settings.toml")
    options.confirm_camera = True
    args = options.arguments(Config(), "settings.toml")
    assert args[args.index("--calibration") + 1] == "my-reference"
    assert "--confirm-camera-unchanged" in args


def test_video_interval_and_recording_arguments(tmp_path):
    video = tmp_path / "practice.avi"
    video.touch()
    options = PracticeOptions(
        source="video",
        video=str(video),
        reference_mode="interval",
        interval_start="3",
        interval_end="15",
        record_raw=True,
        raw_path=str(tmp_path / "raw.mp4"),
        record_overlay=True,
        overlay_path=str(tmp_path / "overlay.mp4"),
    )
    args = options.arguments(Config(), tmp_path / "settings.toml")
    assert args[
        args.index("--reference-interval") + 1 : args.index("--reference-interval") + 3
    ] == ["3.0", "15.0"]
    assert args[args.index("--practice-name") + 1] == options.practice_name
    assert "--record-overlay" not in args
    for end in ("nan", "14", "-2"):
        with pytest.raises(ValueError):
            replace(options, interval_end=end).arguments(Config(), tmp_path / "settings.toml")


def test_output_files_cannot_clobber_each_other_or_the_input(tmp_path):
    video = str(tmp_path / "same.mp4")
    options = PracticeOptions(source="video", video=video, record_raw=True, raw_path=video)
    with pytest.raises(ValueError, match="input video"):
        options.recording_paths()
    options = PracticeOptions(
        record_raw=True, record_overlay=True, raw_path=video, overlay_path=video
    )
    with pytest.raises(ValueError, match="different filenames"):
        options.recording_paths()


def test_suggested_names_and_display_control_queue(tmp_path):
    (tmp_path / "practice_01.mp4").touch()
    (tmp_path / "overlay_02.mp4").touch()
    raw, overlay = next_recordings(tmp_path)
    assert raw.endswith("practice_03.mp4") and overlay.endswith("overlay_03.mp4")
    display = GuiDisplay()
    display.request("c")
    assert display.poll_key(quit_only=True) == -1
    assert display.poll_key() == ord("c")
    display.show("old frame")
    display.show("latest frame")
    assert display.frames.qsize() == 1
    assert display.frames.get_nowait() == "latest frame"
    display.request("q")
    assert display.poll_key(quit_only=True) == ord("q")


def test_preset_flags_are_typed():
    with pytest.raises(TypeError, match="record_raw"):
        PracticeOptions(record_raw="false")


def test_analysis_without_preview_requires_a_video_reference(tmp_path):
    with pytest.raises(ValueError, match="requires a video"):
        PracticeOptions(headless=True).arguments(Config(), tmp_path / "cfg.toml")
    video = tmp_path / "practice.avi"
    video.touch()
    options = PracticeOptions(
        source="video", video=str(video), headless=True, reference_mode="interval"
    )
    assert "--headless" in options.arguments(Config(), tmp_path / "cfg.toml")
