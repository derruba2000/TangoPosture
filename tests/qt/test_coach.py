"""Qt Coach tests use only a local protocol fixture, never an external model."""

import threading
import time
from zipfile import ZipFile

import pytest
from PySide6.QtWidgets import QApplication

from tango_posture.archive import append_segment
from tango_posture.config import Config
from tango_posture.desktop_qt.router import Router
from tango_posture.desktop_qt.screens.coach import CoachScreen
from tango_posture.desktop_qt.service_bridge import ServiceBridge
from tango_posture.ollama import OllamaClient
from tango_posture.review import read_report, saved_analyses


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication(["-platform", "offscreen"])


def wait_for_job(qapp, screen):
    deadline = time.monotonic() + 5
    while screen._is_busy() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert not screen._is_busy()


def make_report_archive(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(
        "# Recorded practice\n\nA report observation at `00:00.2`.\n",
        encoding="utf-8",
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"local test media")
    archive_path = tmp_path / "practice.tp"
    append_segment(archive_path, report, video, None, [0.0, 0.1, 0.2])
    return archive_path


def test_coach_does_not_contact_ollama_until_explicit_action(qapp, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Coach must not contact Ollama automatically")

    monkeypatch.setattr(OllamaClient, "models", forbidden)
    monkeypatch.setattr(OllamaClient, "analyze", forbidden)
    screen = CoachScreen(ServiceBridge(Config()), Router())
    assert screen._report is None
    assert not screen.analyze_btn.isEnabled()
    assert "manual" in screen.status_label.text().lower()
    screen.close()


def test_archive_report_analysis_streams_and_saves_provenance(qapp, tmp_path, ollama_server):
    url, server = ollama_server
    archive_path = make_report_archive(tmp_path)
    screen = CoachScreen(
        ServiceBridge(Config(ollama_base_url=url, ollama_model="test-model:latest")),
        Router(),
    )
    screen.load_report({"archive_path": archive_path, "segment_index": 0})
    assert screen._report is not None
    digest = screen._report.digest

    screen._run_analysis()
    wait_for_job(qapp, screen)

    assert "UNKNOWN" in screen.response_text.toPlainText()
    assert "Analysis saved" in screen.status_label.text()
    assert len(server.requests) == 1
    with ZipFile(archive_path) as archive:
        saved = [name for name in archive.namelist() if ".analysis-" in name]
    assert len(saved) == 1
    loaded_report = read_report(screen._report.path)
    analyses = saved_analyses(loaded_report)
    assert len(analyses) == 1
    assert analyses[0][1]["report_sha256"] == digest
    assert analyses[0][1]["model"] == "test-model:latest"
    assert analyses[0][1]["endpoint"] == url
    assert "Discuss camera framing" in analyses[0][1]["analysis"]
    screen.close()


def test_report_changed_during_analysis_is_not_saved(qapp, tmp_path, monkeypatch):
    report_path = tmp_path / "report.md"
    report_path.write_text("# Original report\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    def delayed_analysis(_client, _report, _on_chunk, _cancel):
        started.set()
        assert release.wait(5)
        return "Response for stale report", True

    monkeypatch.setattr(OllamaClient, "analyze", delayed_analysis)
    screen = CoachScreen(ServiceBridge(Config()), Router())
    screen.load_report(report_path)
    screen._run_analysis()
    assert started.wait(5)
    report_path.write_text("# Updated report\n", encoding="utf-8")
    release.set()
    wait_for_job(qapp, screen)

    assert "changed during analysis" in screen.status_label.text()
    assert not list(tmp_path.glob("*.analysis-*.json"))
    screen.close()


def test_cancelled_analysis_keeps_streamed_text_but_does_not_save(qapp, tmp_path, monkeypatch):
    report_path = tmp_path / "report.md"
    report_path.write_text("# Report\n", encoding="utf-8")
    started = threading.Event()

    def cancellable_analysis(_client, _report, on_chunk, cancel):
        on_chunk("Partial response")
        started.set()
        assert cancel.wait(5)
        raise InterruptedError("Analysis cancelled")

    monkeypatch.setattr(OllamaClient, "analyze", cancellable_analysis)
    screen = CoachScreen(ServiceBridge(Config()), Router())
    screen.load_report(report_path)
    screen._run_analysis()
    assert started.wait(5)
    screen._cancel_analysis()
    wait_for_job(qapp, screen)

    assert screen.response_text.toPlainText() == "Partial response"
    assert "cancelled" in screen.status_label.text().lower()
    assert not list(tmp_path.glob("*.analysis-*.json"))
    screen.close()
