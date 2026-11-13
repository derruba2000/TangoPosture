import csv
import json
import socket
from pathlib import Path

import pytest
from conftest import frame

from tango_posture.metrics import measure
from tango_posture.session import Session
from tango_posture.storage import Storage


def run_session(root, reference, cfg):
    db = Storage(root)
    db.save_calibration(reference)
    session = Session(
        reference, cfg, db, 0, "synthetic", {"fixture": "synthetic, not real inference"}
    )
    for i in range(51):
        f = frame(i / 10, forward=0.2 if i >= 10 else 0)
        if 40 <= i < 44:
            f.poses.clear()
        session.update(measure(f, cfg))
    session.pause(5.1)
    session.resume(7)
    for t in [7.1, 7.2, 9, 9.1]:
        session.update(measure(frame(t), cfg))
    summary = session.finish(9.5)
    out = db.export(session.id, root / "exports")
    return db, session, summary, out


def test_roundtrip_accounting_determinism(tmp_path, reference, cfg, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network attempted during analysis")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    db, session, summary, out = run_session(tmp_path / "one", reference, cfg)
    other, _, again, _ = run_session(tmp_path / "two", reference, cfg)
    try:
        assert summary == again
        assert summary["active_duration"] == pytest.approx(7.6)
        assert summary["pause_duration"] == pytest.approx(1.9)
        for data in summary["metrics"].values():
            assert data["valid_duration"] + data["unknown_duration"] == pytest.approx(7.6)
            assert data["unknown_duration"] >= 2.1
        exported = json.loads((out / "summary.json").read_text())
        assert exported["summary"] == summary
        assert exported["units"]["head_forward"] == "torso_lengths"
        assert exported["reference"]["id"] == reference.id
        with (out / "observations.csv").open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == len(session.rows)
        assert any(r["delta"] == "" and r["state"] == "UNKNOWN" for r in rows)
        with (out / "events.csv").open() as f:
            events = list(csv.DictReader(f))
        assert len(events) == sum(m["event_count"] for m in summary["metrics"].values())
        assert all(e["end_reason"] == "tracking_lost" for e in events)
    finally:
        db.close()
        other.close()


def test_zero_valid_and_endpoints(tmp_path, reference, cfg):
    db = Storage(tmp_path)
    try:
        db.save_calibration(reference)
        s = Session(reference, cfg, db, 0, "synthetic", {})
        for t in [1, 2]:
            f = frame(t)
            f.poses.clear()
            s.update(measure(f, cfg))
        summary = s.finish(3)
        for m in summary["metrics"].values():
            assert m["unknown_duration"] == 3
            assert m["confirmed_percent_of_valid"] is None
            assert m["median_delta"] is None
    finally:
        db.close()


def test_sample_export(tmp_path, reference, cfg):
    db, _, _, _ = run_session(tmp_path, reference, cfg)
    # Re-saving an existing calibration (e.g., via --calibration latest) must succeed
    # even when referenced by existing sessions.
    db.save_calibration(reference)
    assert db.calibration(reference.id).id == reference.id
    db.close()


if __name__ == "__main__":
    from tango_posture.calibration.baseline import calibrate, profile
    from tango_posture.config import Config
    from tango_posture.models import MANIFEST

    cfg = Config()
    ref = calibrate(
        [measure(frame(i / 10), cfg) for i in range(121)],
        0,
        cfg,
        profile(cfg, 640, 480, "video"),
        MANIFEST["sha256"],
        "SYNTHETIC TEST ONLY",
    )
    db, _, _, out = run_session(Path("/tmp/tango-sample-export"), ref, cfg)
    import shutil

    shutil.copytree(out, Path("samples/synthetic"), dirs_exist_ok=True)
    db.close()
