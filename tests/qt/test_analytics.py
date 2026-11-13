"""Portable recording analytics load from indexed archives, without a database."""

import socket
import sqlite3
from zipfile import ZipFile

import pytest
from PySide6.QtWidgets import QApplication

from tango_posture.archive import append_segment, read_analytics, store_analytics
from tango_posture.config import Config
from tango_posture.desktop_qt.analytics_data import (
    load_archive_dataset,
    load_session,
    session_payload,
)
from tango_posture.desktop_qt.library_archives import migrate_legacy_archives
from tango_posture.desktop_qt.router import Router
from tango_posture.desktop_qt.screens.analytics import AnalyticsScreen
from tango_posture.desktop_qt.service_bridge import ServiceBridge
from tango_posture.domain import METRICS, MetricObservation, QualityResult
from tango_posture.session import Session
from tango_posture.storage import Storage


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication(["-platform", "offscreen"])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Analytics must not use the network")
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def payload(tmp_path, reference, cfg):
    storage = Storage(tmp_path)
    storage.save_calibration(reference)
    session = Session(reference, cfg, storage, 10.0, "fixture", {"metric_version": "1.0"})
    for timestamp in (10.0, 10.1, 10.2, 10.3, 11.0, 11.1):
        valid = timestamp != 10.2
        session.update(MetricObservation(
            timestamp,
            {metric: reference.baseline[metric] - 0.2 if valid else None for metric in METRICS},
            {metric: QualityResult(valid, "" if valid else "occluded") for metric in METRICS},
        ))
    summary = session.finish(11.2)
    data = session_payload(tmp_path, session.id)
    stored = load_session(tmp_path, session.id)
    storage.close()
    return data, stored, summary


def make_archive(root, payload, *, count=1):
    path = root / "practice" / "practice.tp"
    report = root / "report.md"
    report.write_text(f"| Session ID | `{payload['id']}` |\n")
    for _ in range(count):
        append_segment(
            path, report, None, None, [10.0, 10.1, 10.2, 10.3, 11.0, 11.1],
            analytics={"session": payload},
        )
    return path


def test_missing_archives_does_not_create_storage(qapp, tmp_path):
    root = tmp_path / "not-created"
    screen = AnalyticsScreen(ServiceBridge(Config(data_dir=str(root))), Router())
    assert not root.exists()
    assert "No Library practice archives" in screen.state_label.text()
    assert screen.dataset is None
    assert not screen.align_btn.isEnabled()
    screen.close()


def test_indexed_analytics_survive_portable_copy_without_database(tmp_path, payload, monkeypatch):
    data, original, summary = payload
    path = make_archive(tmp_path, data, count=2)
    copied = tmp_path / "portable.tp"
    copied.write_bytes(path.read_bytes())
    def no_database(*args):
        pytest.fail("Displaying archived analytics must not read the database")
    monkeypatch.setattr("tango_posture.desktop_qt.analytics_data._connect", no_database)
    for index in (0, 1):
        loaded = load_archive_dataset(copied, index)
        assert loaded.identifier == original.identifier
        for metric in METRICS:
            assert loaded.metrics[metric] == original.metrics[metric]
            assert [len(segment) for segment in loaded.metrics[metric].segments] == [2, 1, 2]
        assert loaded.active_duration == summary["active_duration"]
    with ZipFile(copied) as archive:
        assert {"analytics_00.json", "analytics_01.json"} <= set(archive.namelist())


def test_screen_selects_archive_recording_and_seeks_its_timestamp(qapp, tmp_path, payload):
    data, _, _ = payload
    path = make_archive(tmp_path, data, count=2)
    router = Router()
    navigations = []
    router.route_changed.connect(lambda route, payload: navigations.append((route, payload)))
    screen = AnalyticsScreen(ServiceBridge(Config(data_dir=str(tmp_path))), router)
    assert screen.dataset is None
    screen.session_combo.setCurrentIndex(2)
    assert screen.dataset is not None
    screen._seek_to_recording(10.1)
    assert navigations[-1][1] == {
        "archive_path": path.resolve(), "segment_index": 1, "timestamp": 10.1,
    }
    assert "Delta samples: 5/6" in screen.coverage_labels["head_forward"].text()
    screen.close()


def test_saved_frame_measurements_display_without_reference(qapp, tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Imported clip\n")
    path = tmp_path / "practice" / "import.tp"
    append_segment(path, report, None, None, [0.0, 0.1, 0.2], analytics={
        "max_gap": 0.5,
        "frame_timestamps": [0.0, 0.1, 0.2],
        "coach_data": {"frames": {
            "0": {"head_angle": -5, "knee_bend_deg": 8, "axis_curve_smoothed": 0.02},
            "1": {"head_angle": None, "knee_bend_deg": None},
            "2": {"head_angle": -4, "knee_bend_deg": 10},
        }},
    })
    screen = AnalyticsScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    screen.session_combo.setCurrentIndex(1)
    assert screen.dataset is not None
    assert screen.dataset.metrics["head_angle"].points == [(0, -5), (0.1, None), (0.2, -4)]
    assert [len(part) for part in screen.dataset.metrics["head_angle"].segments] == [1, 1]
    assert screen.dataset.metrics["axis_curve_smoothed"].points[0] == (0, 2)
    assert not screen.charts["head_forward"].isEnabled()
    assert "Stored measurement samples: 2/3" in screen.coverage_labels["knee_bend_deg"].text()
    screen.close()


def test_reanalysis_preserves_other_indices_and_original_session(tmp_path, payload):
    data, _, _ = payload
    path = make_archive(tmp_path, data, count=2)
    before = read_analytics(path, 1)
    store_analytics(path, 0, {"coach_data": {"frames": {"0": {"head_angle": 3}}}})
    assert read_analytics(path, 1) == before
    assert read_analytics(path, 0)["session"] == data


def test_legacy_database_evidence_migrates_without_removing_originals(tmp_path, payload):
    data, _, _ = payload
    path = make_archive(tmp_path, data)
    with ZipFile(path) as archive:
        report_text = archive.read("report_00.md").decode()
    legacy = tmp_path / "practice" / "legacy.tp"
    report = tmp_path / "legacy-report.md"
    report.write_text(report_text)
    append_segment(legacy, report, None, None, [10.0, 10.1])
    _, errors = migrate_legacy_archives(Config(data_dir=str(tmp_path)), lambda: False)
    assert errors == []
    assert read_analytics(legacy, 0)["session"]["id"] == data["id"]
    assert (tmp_path / "tango.sqlite3").is_file()
    assert report.is_file()
    before = legacy.read_bytes()
    assert migrate_legacy_archives(Config(data_dir=str(tmp_path)), lambda: False) == (0, [])
    assert legacy.read_bytes() == before


def test_unanalyzed_archive_shows_actionable_status(qapp, tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Old recording\n")
    append_segment(tmp_path / "practice" / "old.tp", report, None, None, [])
    screen = AnalyticsScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    screen.session_combo.setCurrentIndex(1)
    assert screen.dataset is None
    assert "Analyze in Review or migrate" in screen.state_label.text()
    screen.close()


@pytest.mark.parametrize(("statement", "reason"), [
    ("UPDATE sessions SET status='running'", "not finalized"),
    ("UPDATE calibrations SET metric_version='2.0'", "metric semantics"),
    ('UPDATE sessions SET versions=\'{"metric_version":"2.0"}\'', "metric semantics"),
    ('UPDATE sessions SET summary=\'{"schema_version":2}\'', "summary schema"),
    ("UPDATE sessions SET config='[]'", "Invalid persisted session metadata"),
    ("UPDATE observations SET metric='legacy_head_angle'", "no compatible persisted"),
    ("UPDATE observations SET timestamp=NULL", "invalid observation timestamp"),
    ("UPDATE observations SET timestamp=10", "duplicate observation timestamps"),
])
def test_migration_rejects_incompatible_database_evidence(tmp_path, payload, statement, reason):
    data, _, _ = payload
    with sqlite3.connect(tmp_path / "tango.sqlite3") as db:
        db.execute(statement)
    with pytest.raises((ValueError, TypeError), match=reason):
        session_payload(tmp_path, data["id"])


def test_legacy_projection_keeps_unknown_nonfinite_and_zero_distinct(tmp_path, payload):
    data, _, _ = payload
    with sqlite3.connect(tmp_path / "tango.sqlite3") as db:
        db.execute("UPDATE observations SET quality_state='UNKNOWN' WHERE timestamp=10.1")
        db.execute("UPDATE observations SET delta=? WHERE timestamp=11.0", (float("inf"),))
        db.execute("UPDATE observations SET delta=0 WHERE timestamp=11.1")
    for metric in load_session(tmp_path, data["id"]).metrics.values():
        assert metric.points[1][1] is None
        assert metric.points[4][1] is None
        assert metric.points[5][1] == 0
        assert [len(segment) for segment in metric.segments] == [1, 1, 1]
