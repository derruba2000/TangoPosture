from concurrent.futures import ThreadPoolExecutor
from zipfile import ZipFile

import pytest

from tango_posture.archive import PracticeArchive, append_segment, read_manifest
from tango_posture.review import read_report, save_analysis, saved_analyses


def test_three_segments_are_portable_and_numbered(tmp_path):
    archive = tmp_path / "practice" / "Evening.tp"
    raw, overlay, report = (tmp_path / name for name in ("raw.mp4", "overlay.mp4", "report.md"))
    for index in range(3):
        raw.write_bytes(f"raw {index}".encode())
        overlay.write_bytes(f"overlay {index}".encode())
        report.write_text(f"# Segment {index}\n[Video]({raw.as_uri()})")
        assert append_segment(archive, report, raw, overlay, [index + 0.2]) == index
    assert len(read_manifest(archive)["segments"]) == 3
    with ZipFile(archive) as stored:
        assert len(stored.namelist()) == len(set(stored.namelist())) == 13
        for index in range(3):
            assert stored.read(f"video_{index:02d}.mp4") == f"raw {index}".encode()
            assert stored.read(f"overlay_{index:02d}.mp4") == f"overlay {index}".encode()
    replay = PracticeArchive(archive)
    try:
        for index in range(3):
            record = read_report(replay.report(index))
            assert record.videos["Raw video"].read_bytes() == f"raw {index}".encode()
            assert record.videos["Overlay"].read_bytes() == f"overlay {index}".encode()
            assert record.timestamps == [index + 0.2]
            assert str(tmp_path) not in record.markdown
    finally:
        replay.close()
    assert not replay.folder.exists()


def test_unsafe_extraction_is_rejected(tmp_path):
    archive = tmp_path / "unsafe.tp"
    with ZipFile(archive, "w") as stored:
        stored.writestr("manifest.json", '{"version": 1, "segments": []}')
        stored.writestr("../escaped.txt", "not allowed")
    with pytest.raises(ValueError, match="Unsafe"):
        PracticeArchive(archive)
    assert not (tmp_path / "escaped.txt").exists()


def test_failed_append_preserves_existing_practice(tmp_path):
    archive, report, raw, overlay = (tmp_path / name for name in (
        "Evening.tp", "report.md", "raw.mp4", "overlay.mp4",
    ))
    report.write_text("# First")
    raw.write_bytes(b"raw")
    overlay.write_bytes(b"overlay")
    append_segment(archive, report, raw, overlay, [0.0])
    before = archive.read_bytes()
    with pytest.raises(FileNotFoundError):
        append_segment(archive, report, tmp_path / "missing.mp4", overlay, [1.0])
    assert archive.read_bytes() == before


def test_saved_analysis_stays_in_the_practice_archive(tmp_path):
    archive, report, raw, overlay = (tmp_path / name for name in (
        "Evening.tp", "report.md", "raw.mp4", "overlay.mp4",
    ))
    report.write_text("# First")
    raw.write_bytes(b"raw")
    overlay.write_bytes(b"overlay")
    append_segment(archive, report, raw, overlay, [0.0])
    replay = PracticeArchive(archive)
    try:
        record = read_report(replay.report(0))
        record.archive_path = archive
        save_analysis(record, "model", "http://localhost", "focus", "Analysis")
    finally:
        replay.close()
    reopened = PracticeArchive(archive)
    try:
        record = read_report(reopened.report(0))
        assert saved_analyses(record)[0][1]["analysis"] == "Analysis"
    finally:
        reopened.close()


def test_concurrent_appends_do_not_replace_other_segments(tmp_path):
    archive, report, raw, overlay = (tmp_path / name for name in (
        "Evening.tp", "report.md", "raw.mp4", "overlay.mp4",
    ))
    report.write_text("# First")
    raw.write_bytes(b"raw")
    overlay.write_bytes(b"overlay")
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(append_segment, archive, report, raw, overlay, [0.0])
                   for _ in range(8)]
        assert sorted(future.result() for future in futures) == list(range(8))
    assert len(read_manifest(archive)["segments"]) == 8