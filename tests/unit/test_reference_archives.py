from zipfile import ZipFile

import pytest

from tango_posture.archive import (
    append_segment,
    read_analytics,
    read_manifest,
    save_calibration_archive,
)
from tango_posture.config import Config
from tango_posture.desktop_qt.library.archives import migrate_legacy_archives
from tango_posture.references.archives import promote_reference


def test_promote_selected_recording_retains_practice_original(tmp_path):
    source, video, report = (tmp_path / name for name in ("practice.tp", "video.mp4", "report.md"))
    report.write_text("# Practice")
    for index in range(2):
        video.write_bytes(f"raw {index}".encode())
        append_segment(source, report, video, None, [0, 0.1], analytics={
            "frames": [{"poses": []}, {"poses": []}],
        })
    before = source.read_bytes()
    destination = tmp_path / "Reference.tpr"
    promote_reference(source, destination, "My walking reference", 1)
    assert source.read_bytes() == before
    assert read_manifest(destination)["kind"] == "reference"
    assert read_analytics(destination, 0)["segment_index"] == 0
    with ZipFile(destination) as archive:
        assert archive.read("video_00.mp4") == b"raw 1"
        assert "reference.json" in archive.namelist()
    with pytest.raises(FileExistsError):
        promote_reference(source, destination, "Do not replace")


def test_legacy_migration_copies_reference_and_keeps_neutral_baseline(tmp_path, reference):
    folder = tmp_path / "calibrations"
    folder.mkdir()
    old, video, overlay, report = (
        folder / "Legacy.tpc", tmp_path / "video.mp4", tmp_path / "overlay.mp4", tmp_path / "report.md"
    )
    video.write_bytes(b"raw")
    overlay.write_bytes(b"overlay")
    report.write_text("# Legacy neutral reference")
    save_calibration_archive(old, reference, video, overlay, [0, 0.1])
    before = old.read_bytes()
    cfg = Config(data_dir=str(tmp_path))
    count, errors = migrate_legacy_archives(cfg, lambda: False)
    assert (count, errors) == (1, [])
    assert old.read_bytes() == before
    destination = tmp_path / "references" / "Legacy.tpr"
    assert read_manifest(destination)["kind"] == "reference"
    assert read_manifest(old)["kind"] == "calibration"
    assert migrate_legacy_archives(cfg, lambda: False) == (0, [])
