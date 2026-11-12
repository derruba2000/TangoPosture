"""Qt Library discovers persisted reports and opens source files without copying them."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from tango_posture.config import Config
from tango_posture.desktop_qt.router import Route, Router
from tango_posture.desktop_qt.screens.library import ImportedClipResult, LibraryScreen
from tango_posture.desktop_qt.service_bridge import ServiceBridge


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication(["-platform", "offscreen"])


def test_library_lists_saved_reports_and_opens_them_in_review(qapp, tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    report = reports / "report_20261112_120000.md"
    report.write_text("# Saved report\n", encoding="utf-8")
    router = Router()
    navigations = []
    router.route_changed.connect(lambda route, payload: navigations.append((route, payload)))
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), router)

    assert screen.table.rowCount() == 1
    assert screen.table.item(0, 0).text() == report.stem
    assert screen.table.item(0, 1).text() == "Report"
    open_button = screen.table.cellWidget(0, 3)
    open_button.click()
    assert navigations[-1] == (Route.REVIEW, report.resolve())
    screen.close()


def test_cancelled_video_selection_does_not_navigate(qapp, tmp_path, monkeypatch):
    router = Router()
    navigations = []
    router.route_changed.connect(lambda route, payload: navigations.append((route, payload)))
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), router)
    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.library.QFileDialog.getOpenFileName",
        lambda *args: ("", ""),
    )

    screen._import_video()

    assert navigations == []
    screen.close()


def test_import_success_navigates_to_generated_report(qapp, tmp_path):
    router = Router()
    navigations = []
    router.route_changed.connect(lambda route, payload: navigations.append((route, payload)))
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), router)
    report_path = tmp_path / "imported-report.md"
    result = object.__new__(ImportedClipResult)
    object.__setattr__(result, "report_path", report_path)
    object.__setattr__(result, "source_sha256", "abc123")
    screen._import_job_id = "job-1"
    screen._set_import_busy(True)

    screen._handle_import_event(("import_done", "job-1", result))

    assert navigations[-1] == (Route.REVIEW, report_path)
    assert screen.import_status.text() == "Imported report saved with source SHA-256 abc123."
    assert screen.import_btn.isEnabled()
    assert screen.cancel_import_btn.isEnabled() is False
    screen.close()


def test_started_import_can_be_cancelled_and_recovers_controls(qapp, tmp_path, monkeypatch):
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    started = []
    cancelled = []
    monkeypatch.setattr(
        screen._import_worker,
        "start",
        lambda **kwargs: started.append(kwargs) or "job-1",
    )
    monkeypatch.setattr(
        screen._import_worker, "cancel", lambda: cancelled.append(True) or True
    )

    screen._start_import(Path("dance.mp4"), 2.0, 12.0, "Practice clip")

    assert started[0]["video_path"] == Path("dance.mp4")
    assert started[0]["selected_in"] == 2.0
    assert started[0]["selected_out"] == 12.0
    assert started[0]["analysis_mode"] == "movement"
    assert started[0]["analyse_soundtrack"] is False
    assert screen.import_btn.isEnabled() is False
    screen._cancel_import()
    assert cancelled == [True]

    screen._handle_import_event(("import_cancelled", "job-1"))

    assert screen.import_status.text() == "Import cancelled. You can select another video."
    assert screen.import_btn.isEnabled()
    assert screen.cancel_import_btn.isEnabled() is False
    screen.close()


def test_unexpected_import_event_recovers_controls(qapp, tmp_path):
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    screen._import_job_id = "job-1"
    screen._set_import_busy(True)

    screen._handle_import_event(("unexpected", "job-1"))

    assert "unexpected event" in screen.import_status.text()
    assert screen.import_btn.isEnabled()
    screen.close()
