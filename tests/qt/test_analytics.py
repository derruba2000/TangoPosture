"""D13 analytics uses local schema-v1 evidence, never generated scores/alignment."""

import json
import socket
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from tango_posture.archive import append_segment
from tango_posture.config import Config
from tango_posture.desktop_qt.analytics_data import list_sessions, load_session
from tango_posture.desktop_qt.router import Router
from tango_posture.desktop_qt.screens.analytics import AnalyticsScreen
from tango_posture.desktop_qt.service_bridge import ServiceBridge
from tango_posture.domain import METRICS, MetricObservation, QualityResult
from tango_posture.session import Session
from tango_posture.storage import Storage


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(["-platform", "offscreen"])
    return app


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Analytics must not use the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def saved(tmp_path, reference, cfg):
    storage = Storage(tmp_path)
    storage.save_calibration(reference)
    session = Session(reference, cfg, storage, 10.0, "fixture", {"metric_version": "1.0"})
    for timestamp in (10.0, 10.1, 10.2, 10.3, 11.0, 11.1):
        valid = timestamp != 10.2
        session.update(
            MetricObservation(
                timestamp,
                {metric: reference.baseline[metric] - 0.2 if valid else None for metric in METRICS},
                {metric: QualityResult(valid, "" if valid else "occluded") for metric in METRICS},
            )
        )
    summary = session.finish(11.2)
    storage.db.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
        ("fixture-event", session.id, "head_forward", 10.0, 10.1, None, 0.2, None),
    )
    storage.db.commit()
    yield tmp_path, storage, session, summary
    storage.close()


def screen_for(root: Path) -> AnalyticsScreen:
    return AnalyticsScreen(ServiceBridge(Config(data_dir=str(root))), Router())


def select(screen: AnalyticsScreen, identifier: str) -> None:
    screen.session_combo.setCurrentIndex(screen.session_combo.findData(identifier))


def test_missing_database_does_not_create_storage(qapp, tmp_path):
    root = tmp_path / "not-created"
    screen = screen_for(root)
    assert not root.exists()
    assert "No local session database" in screen.state_label.text()
    assert screen.dataset is None
    assert not screen.align_btn.isEnabled()
    assert all(not chart.isEnabled() for chart in screen.charts.values())
    screen.close()


def test_empty_database(qapp, tmp_path):
    storage = Storage(tmp_path)
    storage.close()
    screen = screen_for(tmp_path)
    assert "No saved sessions" in screen.state_label.text()
    assert screen.session_combo.count() == 1
    assert all(chart.data is None for chart in screen.charts.values())
    screen.close()


def test_projection_keeps_deltas_nulls_and_gaps(saved):
    root, storage, session, summary = saved
    dataset = load_session(root, session.id)
    assert dataset.identifier == session.id
    assert dataset.active_duration == summary["active_duration"]
    for metric, data in dataset.metrics.items():
        persisted = storage.db.execute(
            "SELECT timestamp,delta FROM observations WHERE session_id=? AND metric=? "
            "ORDER BY timestamp",
            (session.id, metric),
        ).fetchall()
        assert data.points == [(row["timestamp"] - 10.0, row["delta"]) for row in persisted]
        assert [len(segment) for segment in data.segments] == [2, 1, 2]
        assert data.points[2][1] is None
        assert data.points[0][1] == pytest.approx(-0.2)
        assert data.summary == summary["metrics"][metric]
    assert dataset.metrics["head_forward"].event_count == 1
    assert dataset.metrics["head_forward"].open_event_count == 1
    assert dataset.metrics["trunk_inclination"].event_count == 0
    assert dataset.origin_timestamp == 10.0


def test_chart_navigation_targets_matching_archived_session(qapp, saved):
    root, _, session, _ = saved
    practice = root / "practice"
    practice.mkdir()
    report = root / "session-report.md"
    report.write_text(f"| Session ID | `{session.id}` |\n", encoding="utf-8")
    video = root / "session.mp4"
    video.write_bytes(b"fixture video")
    archive_path = practice / "session.tp"
    timestamps = [10.0 + index / 10 for index in range(12)]
    append_segment(archive_path, report, video, video, timestamps)

    router = Router()
    navigations: list[tuple[object, object]] = []
    router.route_changed.connect(lambda route, payload: navigations.append((route, payload)))
    screen = AnalyticsScreen(ServiceBridge(Config(data_dir=str(root))), router)
    select(screen, session.id)
    assert screen.dataset is not None
    assert screen.recordings == [(archive_path.resolve(), 0, timestamps)]

    screen.resize(1000, 800)
    screen.show()
    qapp.processEvents()
    chart = screen.charts["head_forward"]
    click_x = chart.width() // 2
    expected_timestamp = session.start + (
        (click_x - 62) / (chart.width() - 82) * max(
            timestamp for metric in screen.dataset.metrics.values() for timestamp, _ in metric.points
        )
    )
    QTest.mouseClick(chart, Qt.MouseButton.LeftButton, pos=QPoint(click_x, chart.height() // 2))
    assert navigations[-1][0].value == "review"
    assert navigations[-1][1]["archive_path"] == archive_path.resolve()
    assert navigations[-1][1]["segment_index"] == 0
    assert navigations[-1][1]["timestamp"] == pytest.approx(expected_timestamp)
    assert navigations[-1][1] == {
        "archive_path": archive_path.resolve(),
        "segment_index": 0,
        "timestamp": expected_timestamp,
    }
    assert screen.event_table.rowCount() == 1
    screen.event_table.cellDoubleClicked.emit(0, 0)
    assert navigations[-1][1] == {
        "archive_path": archive_path.resolve(),
        "segment_index": 0,
        "timestamp": 10.0,
    }
    screen.close()


def test_explicit_selection_units_and_no_alignment(qapp, saved):
    root, storage, session, summary = saved
    before = list(storage.db.iterdump())
    screen = screen_for(root)
    assert screen.dataset is None
    assert all(not chart.isEnabled() for chart in screen.charts.values())
    select(screen, session.id)
    assert screen.dataset is not None
    assert all(chart.isEnabled() for chart in screen.charts.values())
    assert "seconds since first persisted observation" in " ".join(
        label.text() for label in screen.findChildren(QLabel)
    )
    labels = " ".join(label.text() for label in screen.findChildren(QLabel))
    assert "Head forward — smoothed delta (torso lengths)" in labels
    assert "Head inclination — smoothed delta (degrees)" in labels
    assert "Trunk inclination — smoothed delta (degrees)" in labels
    assert "Knee" not in labels
    own = summary["metrics"]["head_forward"]
    text = screen.coverage_labels["head_forward"].text()
    assert f"Valid coverage: {own['valid_coverage']:.1%}" in text
    assert f"Unknown: {own['unknown_duration']:.2f} s" in text
    assert "Delta samples: 5/6" in text
    assert "Persisted events: 1 (1 open)" in text
    assert not screen.align_btn.isEnabled()
    screen.align_btn.click()
    assert not screen.align_btn.isEnabled()
    assert list(storage.db.iterdump()) == before
    # Exercise Qt paint paths, including disconnected single-point segments.
    screen.resize(1000, 900)
    screen.show()
    qapp.processEvents()
    assert not screen.grab().isNull()
    screen.session_combo.setCurrentIndex(0)
    assert screen.dataset is None
    assert all(chart.data is None and not chart.isEnabled() for chart in screen.charts.values())
    screen.close()


def test_unknown_summary_and_missing_metric_are_not_zero(qapp, saved):
    root, storage, session, _ = saved
    storage.db.execute("UPDATE sessions SET summary=NULL,config='{}' WHERE id=?", (session.id,))
    storage.db.execute(
        "DELETE FROM observations WHERE session_id=? AND metric='trunk_inclination'", (session.id,)
    )
    storage.db.commit()
    screen = screen_for(root)
    select(screen, session.id)
    assert screen.dataset is not None
    assert "Active duration: Unknown" in screen.state_label.text()
    assert "Tracking gap limit unknown" in screen.state_label.text()
    assert "Valid coverage: Unknown" in screen.coverage_labels["head_forward"].text()
    assert "Unknown: Unknown" in screen.coverage_labels["head_forward"].text()
    assert all(len(segment) == 1 for segment in screen.dataset.metrics["head_forward"].segments)
    assert not screen.charts["trunk_inclination"].isEnabled()
    assert "Delta samples: 0/0" in screen.coverage_labels["trunk_inclination"].text()
    screen.close()


def test_all_unknown_data_retains_zero_coverage(qapp, saved):
    root, storage, session, summary = saved
    storage.db.execute(
        "UPDATE observations SET delta=NULL,quality_state='UNKNOWN',state='UNKNOWN' "
        "WHERE session_id=?",
        (session.id,),
    )
    for metric in METRICS:
        summary["metrics"][metric].update(
            valid_coverage=0, valid_duration=0, unknown_duration=summary["active_duration"]
        )
    storage.db.execute(
        "UPDATE sessions SET summary=? WHERE id=?", (json.dumps(summary), session.id)
    )
    storage.db.commit()
    screen = screen_for(root)
    select(screen, session.id)
    assert screen.dataset is not None
    assert all(not chart.isEnabled() for chart in screen.charts.values())
    assert "Valid coverage: 0.0%" in screen.coverage_labels["head_forward"].text()
    assert "Valid: 0.00 s" in screen.coverage_labels["head_forward"].text()
    screen.show()
    qapp.processEvents()
    assert not screen.grab().isNull()
    screen.close()


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("UPDATE sessions SET status='running'", "not finalized"),
        ("UPDATE calibrations SET metric_version='2.0'", "metric semantics"),
        ('UPDATE sessions SET versions=\'{"metric_version":"2.0"}\'', "metric semantics"),
        ("UPDATE sessions SET summary='{\"schema_version\":2}'", "summary schema"),
        ("UPDATE sessions SET summary='not-json'", "No compatible dataset"),
        ("UPDATE sessions SET config='[]'", "Invalid persisted session metadata"),
        ("UPDATE observations SET metric='legacy_head_angle'", "no compatible persisted"),
        ("UPDATE observations SET timestamp=NULL", "invalid observation timestamp"),
        ("UPDATE observations SET timestamp=10", "duplicate observation timestamps"),
    ],
)
def test_incompatible_selection_clears_charts(qapp, saved, statement, reason):
    root, storage, session, _ = saved
    screen = screen_for(root)
    select(screen, session.id)
    assert screen.dataset is not None
    storage.db.execute(statement)
    storage.db.commit()
    screen.refresh()
    assert screen.dataset is None
    assert reason in screen.state_label.text()
    assert not screen.align_btn.isEnabled()
    assert all(chart.data is None and not chart.isEnabled() for chart in screen.charts.values())
    screen.close()


def test_unknown_quality_nonfinite_delta_and_zero_are_not_connected(saved):
    root, storage, session, _ = saved
    storage.db.execute(
        "UPDATE observations SET quality_state='UNKNOWN' WHERE session_id=? AND timestamp=10.1",
        (session.id,),
    )
    storage.db.execute(
        "UPDATE observations SET delta=? WHERE session_id=? AND timestamp=11.0",
        (float("inf"), session.id),
    )
    storage.db.execute(
        "UPDATE observations SET delta=0 WHERE session_id=? AND timestamp=11.1", (session.id,)
    )
    storage.db.commit()
    dataset = load_session(root, session.id)
    for data in dataset.metrics.values():
        assert data.points[1][1] is None
        assert data.points[4][1] is None
        assert data.points[5][1] == 0
        assert [len(segment) for segment in data.segments] == [1, 1, 1]


def test_deleted_session_and_unsupported_database(saved):
    root, storage, session, _ = saved
    with pytest.raises(ValueError, match="no longer available"):
        load_session(root, "missing")
    storage.db.execute("PRAGMA user_version=2")
    for action in (lambda: list_sessions(root), lambda: load_session(root, session.id)):
        with pytest.raises(ValueError, match="database schema"):
            action()
