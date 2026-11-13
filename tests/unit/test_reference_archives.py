from zipfile import ZipFile

import pytest

from tango_posture.archive import (
    append_segment,
    read_analytics,
    read_manifest,
)
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
