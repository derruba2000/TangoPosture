"""Qt Library discovers persisted reports and opens source files without copying them."""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from tango_posture.archive import append_segment, read_manifest, save_calibration_archive
from tango_posture.config import Config
from tango_posture.desktop_qt.router import Route, Router
from tango_posture.desktop_qt.screens.library import LibraryScreen
from tango_posture.desktop_qt.service_bridge import ServiceBridge
from tango_posture.references.archives import promote_reference
from tango_posture.references.errors import ImportCancelledError
from tango_posture.references.results import ImportedClipResult


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication(["-platform", "offscreen"])


def test_library_lists_archives_and_opens_them_in_review(qapp, tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    report = reports / "report_20261112_120000.md"
    report.write_text("# Saved report\n", encoding="utf-8")
    archive = tmp_path / "practice" / "practice.tp"
    append_segment(archive, report, None, None, [])
    router = Router()
    navigations = []
    router.route_changed.connect(lambda route, payload: navigations.append((route, payload)))
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), router)

    assert screen.table.rowCount() == 1
    assert screen.table.item(0, 0).text() == "practice"
    assert screen.table.item(0, 1).text() == "1"
    open_button = screen.table.cellWidget(0, 3).findChildren(QPushButton)[0]
    open_button.click()
    assert navigations[-1] == (Route.REVIEW, archive.resolve())
    screen.close()


def test_library_has_no_legacy_migration_control(qapp, tmp_path):
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    assert all(
        button.text() != "Migrate Legacy Data"
        for button in screen.findChildren(QPushButton)
    )
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


def test_import_success_navigates_to_generated_archive(qapp, tmp_path):
    router = Router()
    navigations = []
    router.route_changed.connect(lambda route, payload: navigations.append((route, payload)))
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), router)
    archive_path = tmp_path / "practice" / "imported.tp"
    screen._import_job_id = "job-1"
    screen._set_import_busy(True)

    screen._handle_import_event(("archive_done", "job-1", archive_path))

    assert navigations[-1] == (Route.REVIEW, archive_path)
    assert "indexed analytics" in screen.import_status.text()
    assert screen.import_btn.isEnabled()
    assert screen.cancel_import_btn.isEnabled() is False
    screen.close()


def test_rename_persists_manifest_name_without_breaking_path(qapp, tmp_path, monkeypatch):
    report = tmp_path / "report.md"
    report.write_text("# Report\n")
    path = tmp_path / "practice" / "original.tp"
    append_segment(path, report, None, None, [])
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.library.QInputDialog.getText",
        lambda *args, **kwargs: ("My renamed practice", True),
    )
    screen._rename(path, "original")
    assert path.is_file()
    assert read_manifest(path)["name"] == "My renamed practice"
    second = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    assert second.table.item(0, 0).text() == "My renamed practice"
    screen.close()
    second.close()


@pytest.mark.parametrize("suffix", [".tp", ".tpr", ".tpc"])
def test_delete_requires_confirmation_and_uses_trash(
    qapp, tmp_path, monkeypatch, reference, suffix
):
    report = tmp_path / "report.md"
    report.write_text("# Report\n")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture video")
    path = tmp_path / "practice" / "practice.tp"
    append_segment(path, report, video, None, [])
    if suffix == ".tpr":
        destination = tmp_path / "references" / "reference.tpr"
        promote_reference(path, destination, "Reference")
        path = destination
    elif suffix == ".tpc":
        path = tmp_path / "calibrations" / "calibration.tpc"
        save_calibration_archive(path, reference, video, video, [])
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    trash = tmp_path / "trash"
    trash.mkdir()
    moved = []

    def move_to_trash(filename):
        source = Path(filename)
        moved.append(str(source))
        source.replace(trash / source.name)
        return True

    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.library.QFile",
        lambda filename: SimpleNamespace(
            moveToTrash=lambda: move_to_trash(filename),
            errorString=lambda: "Permission denied",
        ),
    )
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No,
    )
    screen._delete(path)
    assert not moved
    assert path.is_file()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes,
    )
    row = next(i for i, item in enumerate(screen.practices) if item["path"] == path.resolve())
    delete = next(
        button for button in screen.table.cellWidget(row, 3).findChildren(QPushButton)
        if button.text() == "Delete"
    )
    delete.click()
    assert moved == [str(path)]
    assert not path.exists()
    assert (trash / path.name).is_file()
    assert all(item["path"] != path.resolve() for item in screen.practices)
    assert "moved to Trash" in screen.import_status.text()
    assert report.is_file()
    (trash / path.name).replace(path)
    screen.refresh()
    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.library.QFile",
        lambda filename: SimpleNamespace(
            moveToTrash=lambda: False, errorString=lambda: "Permission denied",
        ),
    )
    screen._delete(path)
    assert "Could not move" in screen.import_status.text()
    assert "Permission denied" in screen.import_status.text()
    assert path.is_file()
    screen.close()


def test_library_type_filter_combines_with_search_and_survives_refresh(qapp, tmp_path, monkeypatch):
    bridge = ServiceBridge(Config(data_dir=str(tmp_path)))
    entries = [
        {"name": name, "path": tmp_path / f"{name}{suffix}",
         "segments_count": 0 if suffix == ".tpc" else 1, "created_at": ""}
        for suffix in (".tpr", ".tpc", ".tp")
        for name in ("Walking", "Turn")
    ]
    monkeypatch.setattr(bridge, "list_library_archives", lambda: entries)
    screen = LibraryScreen(bridge, Router())
    assert screen.table.rowCount() == 6
    for suffix in (".tpr", ".tpc", ".tp"):
        screen.type_filter.setCurrentIndex(screen.type_filter.findData(suffix))
        assert screen.table.rowCount() == 2
        assert all(screen.table.item(row, 4).text() == suffix for row in range(2))
        screen.search_input.setText(" WALK ")
        assert screen.table.rowCount() == 1
        assert screen.table.item(0, 0).text() == "Walking"
        screen.refresh()
        assert screen.type_filter.currentData() == suffix
        assert screen.table.rowCount() == 1
        screen.search_input.setText("missing")
        assert screen.table.rowCount() == 0
        screen.search_input.clear()
    screen.type_filter.setCurrentIndex(0)
    assert screen.table.rowCount() == 6
    screen.close()


@pytest.mark.parametrize("accepted", [False, True])
def test_library_picker_only_offers_practice_and_reference_archives(
    qapp, tmp_path, reference, monkeypatch, accepted
):
    report = tmp_path / "report.md"
    report.write_text("# Report\n")
    practice = tmp_path / "practice" / "practice.tp"
    append_segment(practice, report, None, None, [])
    video = tmp_path / "loose-video.mp4"
    video.write_bytes(b"fixture")
    calibration = tmp_path / "calibrations" / "reference.tpc"
    save_calibration_archive(calibration, reference, video, video, [0.0])
    choices = []
    def choose(parent, title, label, items, *args):
        choices.extend(items)
        return items[-1], accepted
    monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getItem", choose)
    bridge = ServiceBridge(Config(data_dir=str(tmp_path)))
    selected = bridge.choose_library_archive(None, "Open archive")
    assert selected == (calibration.resolve() if accepted else None)
    assert len(choices) == 2
    assert all(".mp4" not in label and ".md" not in label for label in choices)


def test_closing_library_cancels_owned_packaging_thread(qapp, tmp_path, monkeypatch):
    started = threading.Event()
    cancelled = threading.Event()
    def package(path, cfg, cancel_check, *, as_reference=False):
        started.set()
        while not cancel_check():
            cancelled.wait(0.01)
        cancelled.set()
        raise ImportCancelledError("Cancelled")
    monkeypatch.setattr("tango_posture.desktop_qt.screens.library.archive_report", package)
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    result = object.__new__(ImportedClipResult)
    object.__setattr__(result, "report_path", tmp_path / "report.md")
    screen._import_job_id = "test-package"
    screen._handle_import_event(("import_done", "test-package", result))
    assert started.wait(1)
    screen.close()
    assert cancelled.is_set()
    assert not screen._package_thread.is_alive()


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


@pytest.mark.parametrize(
    "url,field",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube_url"),
        ("https://www.facebook.com/reel/123456789012345", "facebook_url"),
    ],
)
def test_online_import_uses_shared_download_analysis_worker(
    qapp, tmp_path, monkeypatch, url, field
):
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    started = []
    monkeypatch.setattr(
        screen._import_worker, "start",
        lambda **kwargs: started.append(kwargs) or "url-job",
    )
    screen._start_online_import(url, 10.0, 30.0, "Study clip")
    assert started[0][field]
    assert started[0]["video_path"] == ""
    assert started[0]["selected_in"] == 10.0
    assert started[0]["selected_out"] == 30.0
    assert not screen.url_import_btn.isEnabled()
    assert screen.cancel_import_btn.isEnabled()
    screen._handle_import_event(("import_error", "url-job", "Download unavailable"))
    assert "Download unavailable" in screen.import_status.text()
    assert screen.url_import_btn.isEnabled()
    screen.close()


def test_invalid_online_import_does_not_start_worker(qapp, tmp_path, monkeypatch):
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    started = []
    monkeypatch.setattr(screen._import_worker, "start", lambda **kwargs: started.append(kwargs))
    screen._start_online_import("https://example.com/video", 0, 20, "")
    assert not started
    assert "valid HTTPS" in screen.import_status.text()
    screen._start_online_import("https://youtu.be/dQw4w9WgXcQ", 0, 121, "")
    assert not started
    assert "120 seconds" in screen.import_status.text()
    screen.close()


def test_online_import_auto_end_uses_downloaded_duration(qapp, tmp_path, monkeypatch):
    screen = LibraryScreen(ServiceBridge(Config(data_dir=str(tmp_path))), Router())
    started = []
    monkeypatch.setattr(
        screen._import_worker, "start", lambda **kwargs: started.append(kwargs) or "job"
    )
    screen._start_online_import("https://youtu.be/dQw4w9WgXcQ", 0.0, None, "")
    assert started[0]["selected_out"] is None
    screen.close()
