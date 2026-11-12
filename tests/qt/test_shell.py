"""Headless Qt integration tests for MainWindow, Router, and Screen components."""

import pytest
from PySide6.QtWidgets import QApplication

from tango_posture.config import Config
from tango_posture.desktop_qt.components.beat_strip import BeatStrip
from tango_posture.desktop_qt.components.status_strip import StatusStrip
from tango_posture.desktop_qt.components.video_stage import VideoStage
from tango_posture.desktop_qt.router import Route, Router
from tango_posture.desktop_qt.shell import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(["-platform", "offscreen"])
    return app


def test_router_navigation():
    router = Router()
    assert router.current == Route.HOME

    visited = []
    router.route_changed.connect(lambda r, p: visited.append(r))

    router.navigate(Route.PRACTICE)
    assert router.current == Route.PRACTICE
    assert visited == [Route.PRACTICE]

    router.navigate(Route.REVIEW)
    assert router.current == Route.REVIEW

    assert router.go_back()
    assert router.current == Route.PRACTICE

    assert router.go_back()
    assert router.current == Route.HOME

    # At root, cannot go back further
    assert not router.go_back()


def test_main_window_instantiation_and_navigation(qapp):
    cfg = Config()
    window = MainWindow(cfg)
    assert window is not None
    assert window.windowTitle() == "TangoPosture Studio"

    # Verify all screens are instantiated in stack
    for route in Route:
        assert route in window.screens

    # Verify switching route updates current screen
    window.router.navigate(Route.PRACTICE)
    assert window.stack.currentWidget() == window.screens[Route.PRACTICE]
    assert window.nav_buttons[Route.PRACTICE].isChecked()

    window.router.navigate(Route.REFERENCES)
    assert window.stack.currentWidget() == window.screens[Route.REFERENCES]

    window.router.navigate(Route.LIBRARY)
    assert window.stack.currentWidget() == window.screens[Route.LIBRARY]

    window.router.navigate(Route.SETTINGS)
    assert window.stack.currentWidget() == window.screens[Route.SETTINGS]

    window.router.navigate(Route.HELP)
    assert window.stack.currentWidget() == window.screens[Route.HELP]

    window.close()


def test_status_strip(qapp):
    strip = StatusStrip()
    strip.set_status("Session active")
    assert strip._status_label.text() == "Session active"

    strip.set_session_label("Recording", active=True, recording=True)
    assert strip._session_badge.text() == "RECORDING"

    strip.set_reference("Test Ref")
    assert "Test Ref" in strip._reference_label.text()


def test_beat_strip(qapp):
    strip = BeatStrip()
    strip.update_beat(125.0, 3, is_active=True)
    assert strip._bpm == 125.0
    assert strip._current_beat == 3
    assert strip._bpm_label.text() == "125 BPM"

    strip.update_beat(120.0, 1, is_active=False)
    assert strip._bpm_label.text() == "Silent"


def test_video_stage_rendering(qapp):
    stage = VideoStage("Waiting for camera")
    stage.resize(640, 480)
    stage.set_badge("● REC", "#772D43")
    assert stage._badge_text == "● REC"

    stage.clear_badge()
    assert stage._badge_text == ""


def test_settings_screen_update(qapp):
    cfg = Config()
    window = MainWindow(cfg)
    settings_screen = window.screens[Route.SETTINGS]

    settings_screen.cam_spin.setValue(2)
    settings_screen.bpm_spin.setValue(130.0)
    settings_screen._save_settings()

    assert window.bridge.config.camera == 2
    assert window.bridge.config.tango_bpm == 130.0

    settings_screen._reset_defaults()
    assert window.bridge.config.camera == 0
    assert window.bridge.config.tango_bpm == 120.0
    window.close()
