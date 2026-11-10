import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from zipfile import ZipFile

import pytest

from tango_posture.archive import (
    PracticeArchive,
    append_segment,
    extract_segment_video,
    file_sha256,
    read_manifest,
    read_movement_analysis,
    save_calibration_archive,
    store_movement_analysis,
)
from tango_posture.calibration import load_calibration_file, save_calibration_file
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


def test_replay_extraction_blocks_archive_replacement(tmp_path, monkeypatch):
    archive, report, raw = (tmp_path / name for name in ("practice.tp", "report.md", "raw.mp4"))
    report.write_text("# First")
    raw.write_bytes(b"raw")
    append_segment(archive, report, raw, None, [0.0])
    extracting = threading.Event()
    release = threading.Event()
    original_open = ZipFile.open
    replay_thread = None

    def extract_replay():
        nonlocal replay_thread
        replay_thread = threading.get_ident()
        return PracticeArchive(archive)

    def open_member(stored, name, mode="r", *args, **kwargs):
        if (
            str(stored.filename) == str(archive)
            and getattr(name, "filename", name) == "video_00.mp4"
            and threading.get_ident() == replay_thread
        ):
            extracting.set()
            assert release.wait(5), "Replay extraction was not released"
        return original_open(stored, name, mode, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "open", open_member)
    with ThreadPoolExecutor(max_workers=2) as workers:
        replay_job = workers.submit(extract_replay)
        try:
            assert extracting.wait(5), "Replay extraction did not start"
            append_job = workers.submit(append_segment, archive, report, raw, None, [1.0])
            with pytest.raises(TimeoutError):
                append_job.result(timeout=0.1)
        finally:
            release.set()
            replay = replay_job.result(timeout=5)
            replay.close()
        assert append_job.result(timeout=5) == 1
    assert len(read_manifest(archive)["segments"]) == 2


def test_calibration_archive_replaces_all_media_and_loads_its_reference(tmp_path, reference):
    archive = tmp_path / "My reference.tpc"
    raw, overlay = tmp_path / "raw.mp4", tmp_path / "overlay.mp4"
    for index in range(2):
        current = replace(reference, name=f"Reference {index}")
        raw.write_bytes(f"raw {index}".encode())
        overlay.write_bytes(f"overlay {index}".encode())
        save_calibration_archive(archive, current, raw, overlay, [0.1, 0.2])
        assert load_calibration_file(str(archive)) == current
    with ZipFile(archive) as stored:
        assert stored.read("video_00.mp4") == b"raw 1"
        assert stored.read("overlay_00.mp4") == b"overlay 1"
        assert "video_01.mp4" not in stored.namelist()
        assert len(stored.namelist()) == len(set(stored.namelist())) == 6
    replay = PracticeArchive(archive)
    try:
        assert "Reference 1" in read_report(replay.report(0)).markdown
    finally:
        replay.close()


def test_failed_calibration_save_keeps_the_previous_archive(tmp_path, reference):
    archive = tmp_path / "Reference.tpc"
    raw, overlay = tmp_path / "raw.mp4", tmp_path / "overlay.mp4"
    raw.write_bytes(b"raw")
    overlay.write_bytes(b"overlay")
    save_calibration_archive(archive, reference, raw, overlay, [0.1])
    before = archive.read_bytes()
    with pytest.raises(FileNotFoundError):
        save_calibration_archive(archive, reference, tmp_path / "missing.mp4", overlay, [0.1])
    assert archive.read_bytes() == before


def test_latest_and_uuid_reference_loading_prefer_new_archives(tmp_path, reference):
    save_calibration_file(reference, tmp_path)
    current = replace(reference, id="archive-reference", name="New reference")
    archive = tmp_path / "Reference.tpc"
    raw, overlay = tmp_path / "raw.mp4", tmp_path / "overlay.mp4"
    raw.write_bytes(b"raw")
    overlay.write_bytes(b"overlay")
    save_calibration_archive(archive, current, raw, overlay, [0.0])
    assert load_calibration_file("latest", tmp_path) == current
    assert load_calibration_file(current.id, tmp_path) == current


def test_movement_analysis_roundtrip_and_hash_verification(tmp_path):
    archive = tmp_path / "Practice.tp"
    report = tmp_path / "report.md"
    raw0, raw1 = tmp_path / "raw0.mp4", tmp_path / "raw1.mp4"
    overlay = tmp_path / "overlay.mp4"
    report.write_text("# Segment")
    raw0.write_bytes(b"raw-segment-0")
    raw1.write_bytes(b"raw-segment-1")
    overlay.write_bytes(b"overlay-bytes")

    assert append_segment(archive, report, raw0, overlay, [0.0, 0.1]) == 0
    assert append_segment(archive, report, raw1, overlay, [1.0, 1.1]) == 1

    extracted0 = tmp_path / "extracted0.mp4"
    sha0 = extract_segment_video(archive, 0, extracted0)
    assert extracted0.read_bytes() == b"raw-segment-0"
    assert sha0 == file_sha256(raw0)
    sha1 = file_sha256(raw1)

    store_movement_analysis(archive, 0, sha0, {"segment_index": 0, "pivots": [{"id": 0}]})
    store_movement_analysis(archive, 1, sha1, {"segment_index": 1, "pivots": [{"id": 1}]})

    assert read_movement_analysis(archive, 0) == {"segment_index": 0, "pivots": [{"id": 0}]}
    assert read_movement_analysis(archive, 1) == {"segment_index": 1, "pivots": [{"id": 1}]}

    # Replacing segment 0 movement analysis with mismatched source hash is rejected and keeps old data
    with pytest.raises(ValueError, match="changed before movement analysis could be committed"):
        store_movement_analysis(archive, 0, "wrong-sha", {"segment_index": 0, "pivots": []})
    assert read_movement_analysis(archive, 0) == {"segment_index": 0, "pivots": [{"id": 0}]}
    assert read_movement_analysis(archive, 1) == {"segment_index": 1, "pivots": [{"id": 1}]}
    assert len(read_manifest(archive)["segments"]) == 2
