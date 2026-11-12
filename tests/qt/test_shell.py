"""Headless Qt integration tests for MainWindow, Router, and Screen components."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from tango_posture.config import Config, export_toml
from tango_posture.core.entities import ReferenceSummary
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
    assert window.minimumSize().width() == 1024
    assert window.minimumSize().height() == 700

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


@pytest.mark.parametrize("size", [(1024, 700), (1280, 800)])
def test_shell_supports_specified_window_sizes(qapp, size):
    window = MainWindow(Config())
    window.resize(*size)
    window.show()
    qapp.processEvents()

    assert (window.width(), window.height()) == size
    window.close()


def test_navigation_shortcut_does_not_interrupt_text_entry(qapp):
    window = MainWindow(Config())
    window.show()
    window.router.navigate(Route.SETTINGS)
    qapp.processEvents()

    settings = window.screens[Route.SETTINGS]
    settings.ollama_url.setFocus()
    qapp.processEvents()
    window._navigate_shortcut(Route.HOME)
    assert window.router.current == Route.SETTINGS

    window.nav_buttons[Route.HOME].setFocus()
    qapp.processEvents()
    window._navigate_shortcut(Route.HOME)
    assert window.router.current == Route.HOME
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


def test_settings_screen_update(qapp, tmp_path, monkeypatch):
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
    assert settings_screen.smooth_spin.value() == Config().smoothing
    assert settings_screen.mirror_cb.isChecked() == Config().mirror

    source = Config(
        camera=3,
        side="right",
        facing="left",
        mirror=True,
        smoothing=0.8,
        head_down_angle=-20.0,
        shoulder_raise_threshold=17.0,
        beat_tracking=False,
        tango_bpm=135.0,
        ollama_base_url="http://localhost:11435",
        ollama_model="custom-model",
    )
    settings_path = tmp_path / "imported.toml"
    settings_path.write_text(export_toml(source), encoding="utf-8")
    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.settings.QFileDialog.getOpenFileName",
        lambda *args: (str(settings_path), "TOML (*.toml)"),
    )
    settings_screen._import_toml()
    assert settings_screen.cam_spin.value() == 3
    assert settings_screen.side_combo.currentText() == "right"
    assert settings_screen.facing_combo.currentText() == "left"
    assert settings_screen.mirror_cb.isChecked()
    assert settings_screen.smooth_spin.value() == 0.8
    assert settings_screen.head_angle_spin.value() == -20.0
    assert settings_screen.shoulder_spin.value() == 17.0
    assert not settings_screen.beat_cb.isChecked()
    assert settings_screen.bpm_spin.value() == 135.0
    assert settings_screen.ollama_url.text() == "http://localhost:11435"
    assert settings_screen.ollama_model.text() == "custom-model"
    window.close()


def test_invalid_settings_import_is_reported(qapp, tmp_path, monkeypatch):
    window = MainWindow(Config())
    settings_path = tmp_path / "invalid.toml"
    settings_path.write_text("camera = [", encoding="utf-8")
    monkeypatch.setattr(
        "tango_posture.desktop_qt.screens.settings.QFileDialog.getOpenFileName",
        lambda *args: (str(settings_path), "TOML (*.toml)"),
    )

    window.screens[Route.SETTINGS]._import_toml()

    assert window.bridge.config.camera == 0
    assert window.status_strip._status_label.text().startswith("Could not import settings:")
    window.close()


def test_reference_reuse_requires_camera_confirmation(qapp):
    window = MainWindow(Config())
    screen = window.screens[Route.REFERENCES]
    screen.selected_ref = ReferenceSummary(
        id="reference-id",
        name="Reference",
        path=Path("reference.tpc"),
        side="right",
        facing="left",
        camera="2",
    )

    screen._match_camera_settings()
    assert window.bridge.config.camera == 0
    assert "Confirm" in window.status_strip._status_label.text()

    screen.confirm_camera_cb.setChecked(True)
    screen._match_camera_settings()
    assert window.bridge.config.camera == 2
    assert window.bridge.config.side == "right"
    assert window.bridge.config.facing == "left"
    screen.table.clearSelection()
    screen._on_selection_changed()
    assert screen.selected_ref is None
    assert not screen.match_btn.isEnabled()
    window.close()
