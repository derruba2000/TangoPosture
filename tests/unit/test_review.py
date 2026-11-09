import json

import pytest

from tango_posture.config import Config
from tango_posture.replay import VideoReader, cut_video_segment, parse_time
from tango_posture.reports import write_markdown_report
from tango_posture.review import read_report, save_analysis, saved_analyses, write_metadata


def test_report_associations_latest_alias_and_frame_time_mapping(tmp_path):
    report = tmp_path / "report_first.md"
    report.write_text("# Practice\nAt 00:02.0 the frame was unknown.\n")
    (tmp_path / "latest_report.md").write_text(report.read_text())
    raw, overlay = tmp_path / "dance with spaces.avi", tmp_path / "overlay.avi"
    write_metadata(report, "camera:0", raw, overlay, [1.2, 1.8, 2.1, 2.8])
    record = read_report(tmp_path / "latest_report.md")
    assert record.path == report
    assert record.videos == {"Raw video": raw, "Overlay": overlay}
    # The camera clock and encoded-video clock need not start at zero or run at the same rate.
    assert record.recording_seconds(2.0, 10) == pytest.approx(0.2)
    assert record.recording_seconds(99, 10) == pytest.approx(0.3)


def test_existing_markdown_report_links_remain_replayable(tmp_path):
    raw = tmp_path / "raw video.mp4"
    overlay = tmp_path / "overlay.mp4"
    report = tmp_path / "report_old.md"
    report.write_text(
        f"| Saved Raw Video | [`raw`]({raw.as_uri()}) |\n| Saved Annotated Overlay Video | [`overlay`]({overlay.as_uri()}) |\n"
    )
    record = read_report(report)
    assert record.videos["Raw video"] == raw
    assert record.videos["Overlay"] == overlay
    assert "approximate" in record.warning


def test_saved_analyses_are_bound_to_report_content_and_keep_original(tmp_path):
    path = tmp_path / "report_first.md"
    path.write_text("# Original report\nUNKNOWN at 00:03.0\n")
    record = read_report(path)
    saved = save_analysis(
        record,
        "test-model",
        "http://localhost:11434",
        "Focus on coverage",
        "## Findings\nTracking was unknown.",
    )
    assert path.read_text() == record.markdown
    matches = saved_analyses(read_report(path))
    assert len(matches) == 1 and matches[0][0] == saved
    assert matches[0][1]["model"] == "test-model"
    path.write_text("# A different report\n")
    assert saved_analyses(read_report(path)) == []


def test_corrupt_metadata_does_not_prevent_reading_report(tmp_path):
    path = tmp_path / "report_bad.md"
    path.write_text("# Still readable\n")
    path.with_suffix(".metadata.json").write_text(
        json.dumps({"version": 1, "report_file": path.name, "frame_timestamps": [2, 1]})
    )
    record = read_report(path)
    assert record.markdown == "# Still readable\n"
    assert "unordered" in record.warning


def test_report_writer_saves_metadata_and_preserves_multiple_reports(tmp_path):
    paths = [
        write_markdown_report(tmp_path, Config(), "camera:0", "completed", [], {}) for _ in range(2)
    ]
    assert paths[0] != paths[1]
    assert all(path.with_suffix(".metadata.json").is_file() for path in paths)
    assert read_report(tmp_path / "latest_report.md").path == paths[1]


@pytest.mark.parametrize("value,expected", [("12.5", 12.5), ("01:02.5", 62.5), ("01:02:03", 3723)])
def test_replay_time_input(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["nan", "-1", "inf", "hello", "1:2:3:4"])
def test_invalid_replay_time(value):
    with pytest.raises(ValueError):
        parse_time(value)


def test_video_reader_seeks_and_releases_a_real_codec(tmp_path):
    import cv2
    import numpy as np

    path = tmp_path / "sequence.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 120))
    assert writer.isOpened()
    for i in range(12):
        writer.write(np.full((120, 160, 3), i * 15, np.uint8))
    writer.release()
    reader = VideoReader(path)
    assert reader.duration == pytest.approx(1.2)
    assert reader.frame(8).mean() == pytest.approx(120, abs=3)
    assert reader.index == 8
    assert reader.frame(2).mean() == pytest.approx(30, abs=3)
    reader.close()
    assert not reader.cap.isOpened()


def test_cut_video_segment_writes_selected_frames_and_preserves_source(tmp_path, monkeypatch):
    import cv2
    import numpy as np

    source = tmp_path / "source.avi"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter.fourcc(*"MJPG"), 10, (160, 120))
    assert writer.isOpened()
    for i in range(30):
        writer.write(np.full((120, 160, 3), i * 5, np.uint8))
    writer.release()
    original = source.read_bytes()
    monkeypatch.setattr("tango_posture.replay.shutil.which", lambda _: None)

    output = cut_video_segment(source, tmp_path / "cuts" / "selected.mp4", 1.0, 2.5)

    reader = VideoReader(output)
    assert reader.count == 15
    assert reader.duration == pytest.approx(1.5)
    reader.close()
    assert source.read_bytes() == original


def test_cut_video_segment_rejects_source_overwrite_and_invalid_range(tmp_path):
    import cv2
    import numpy as np

    source = tmp_path / "source.avi"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter.fourcc(*"MJPG"), 10, (160, 120))
    assert writer.isOpened()
    for i in range(10):
        writer.write(np.full((120, 160, 3), i * 10, np.uint8))
    writer.release()

    with pytest.raises(ValueError, match="source video stays unchanged"):
        cut_video_segment(source, source, 0.0, 0.5)
    with pytest.raises(ValueError, match="End time must be greater"):
        cut_video_segment(source, tmp_path / "bad.mp4", 1.0, 0.5)
