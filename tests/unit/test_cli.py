import pytest

from tango_posture.archive import save_calibration_archive
from tango_posture.cli import main
from tango_posture.models import MANIFEST
from tango_posture.storage import Storage


def test_manifest_copy():
    import json
    from pathlib import Path

    assert json.loads(Path("models/manifest.json").read_text()) == MANIFEST


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--side", "left"],
        ["--side", "left", "--facing", "right", "--headless"],
        ["--side", "left", "--facing", "right", "--headless", "--video", "missing"],
        [
            "--side",
            "left",
            "--facing",
            "right",
            "--video",
            "missing",
            "--reference-interval",
            "0",
            "4",
        ],
    ],
)
def test_invalid_run_flags(tmp_path, flags):
    assert main(["--data-dir", str(tmp_path), "run", *flags]) == 2


def test_reference_confirmation(tmp_path, reference):
    db = Storage(tmp_path)
    db.save_calibration(reference)
    db.close()
    assert (
        main(
            [
                "--data-dir",
                str(tmp_path),
                "run",
                "--video",
                "missing",
                "--headless",
                "--calibration",
                reference.id,
            ]
        )
        == 2
    )


def test_coach_loads_reference_json_from_calibration_archive(tmp_path, reference, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / "Reference.tpc"
    raw, overlay = tmp_path / "raw.mp4", tmp_path / "overlay.mp4"
    raw.write_bytes(b"raw")
    overlay.write_bytes(b"overlay")
    save_calibration_archive(archive, reference, raw, overlay, [0.0])
    received = []

    class Coach:
        def __init__(self, *args, **kwargs):
            received.append(args[4])

        def run(self):
            pass

    monkeypatch.setattr("tango_posture.app.Application", Coach)
    assert main([
        "--data-dir", str(tmp_path), "run", "--video", "fixture", "--headless",
        "--calibration", str(archive), "--confirm-camera-unchanged",
    ]) == 0
    assert received == [reference]
