"""Headless Qt integration tests for MainWindow, Router, and Screen components."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from tango_posture import desktop
from tango_posture.config import Config, export_toml
from tango_posture.core.entities import ReferenceSummary
from tango_posture.desktop_qt.components.beat_strip import BeatStrip
from tango_posture.desktop_qt.components.status_strip import StatusStrip
from tango_posture.desktop_qt.components.video_stage import VideoStage
from tango_posture.desktop_qt.main import SPLASH_DURATION_MS, show_startup_splash
from tango_posture.desktop_qt.router import Route, Router
from tango_posture.desktop_qt.screens.help import HelpScreen
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


def test_packaged_smoke_initializes_qt_and_runs_inference(qapp, tmp_path, monkeypatch, capsys):
    for name in ("TangoPosture_logo_under_1MB.png", "TANGO_POSTURE_MANUAL.md"):
        (tmp_path / name).write_bytes(b"resource fixture")
    model = tmp_path / "model.task"
    events = []

    class Estimator:
        def __init__(self, path):
            assert path == model

        def estimate(self, image, timestamp):
            assert image.shape == (64, 64, 3)
            events.append("estimate")

        def close(self):
            events.append("close")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desktop, "files", lambda _: tmp_path)
    monkeypatch.setattr(desktop, "bundled_model", lambda: model)
    monkeypatch.setattr("tango_posture.models.verify", lambda _: events.append("verify"))
    monkeypatch.setattr("tango_posture.pose.mediapipe_backend.MediaPipeEstimator", Estimator)
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(__version__="fixture"))
    desktop.smoke_test()
    assert events == ["verify", "estimate", "close"]
    assert "Checking Qt desktop" in capsys.readouterr().out


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

    window.nav_buttons[Route.LIBRARY].click()
    assert window.stack.currentWidget() == window.screens[Route.LIBRARY]
    assert window.nav_buttons[Route.LIBRARY].isChecked()

    window.router.navigate(Route.SETTINGS)
    assert window.stack.currentWidget() == window.screens[Route.SETTINGS]

    window.router.navigate(Route.HELP)
    assert window.stack.currentWidget() == window.screens[Route.HELP]

    window.close()


def test_about_button_opens_one_branded_dialog(qapp):
    window = MainWindow(Config())

    window.about_btn.click()
    qapp.processEvents()
    dialog = window.about_dialog

    assert dialog is not None
    assert dialog.windowTitle() == "About TangoPosture"
    assert dialog.version_label.text().startswith("Version ")
    assert dialog.license_label.text() == "License: Apache-2.0"
    assert "not a medical assessment" in dialog.disclaimer_label.text()
    assert "github.com/derruba2000/TangoPosture" in dialog.repo_link.text()
    window.about_btn.click()
    assert window.about_dialog is dialog
    window.close()


def test_startup_splash_delays_main_window_for_three_seconds(qapp, monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        "tango_posture.desktop_qt.main.QTimer.singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )
    window = MainWindow(Config())

    splash = show_startup_splash(window, QPixmap())
    qapp.processEvents()

    assert splash.isVisible()
    assert not window.isVisible()
    assert scheduled[0][0] == 3000 == SPLASH_DURATION_MS
    scheduled[0][1]()
    qapp.processEvents()
    assert window.isVisible()
    assert not splash.isVisible()
    window.close()


def test_help_renders_styled_manual_with_hierarchical_contents_and_search(qapp):
    window = MainWindow(Config())
    help_screen = window.screens[Route.HELP]
    assert isinstance(help_screen, HelpScreen)

    assert help_screen.toc_list.topLevelItemCount() > 0
    assert help_screen.toc_list.topLevelItem(0).childCount() > 0
    assert "TANGO POSTURE / LEARN &amp; PRACTISE" in help_screen.manual_html
    assert "<table>" in help_screen.manual_html
    assert "Tango Posture Coach Manual" in help_screen.viewer.toPlainText()

    help_screen.search_input.setText("confidence")
    help_screen._search_manual()
    assert help_screen.search_matches
    assert "matches" in help_screen.search_status.text()
    help_screen._next_match()
    assert "of" in help_screen.search_status.text()

    help_screen.search_input.setText("no such manual phrase")
    help_screen._search_manual()
    assert help_screen.search_status.text() == "No matches"
    window.close()


def test_navigation_buttons_have_icons_and_accessible_labels(qapp):
    window = MainWindow(Config())

    assert len(window.nav_buttons) == 8
    assert Route.REFERENCES not in window.nav_buttons
    for button in window.nav_buttons.values():
        assert not button.icon().isNull()
        assert button.iconSize().width() == 20
        assert button.accessibleName() == button.text()
        assert button.toolTip() == button.text()

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
    settings_screen.field_widgets["calibration_duration"].setValue(15.0)
    settings_screen.field_widgets["ollama_num_ctx"].setValue(8192)
    settings_screen.field_widgets["head_forward.entry"].setValue(0.1)
    assert settings_screen._save_settings()
    assert window.bridge.config.calibration_duration == 15.0
    assert window.bridge.config.ollama_num_ctx == 8192
    assert window.bridge.config.head_forward.entry == 0.1

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
        calibration_duration=15.0,
        calibration_valid_duration=12.0,
        ollama_num_ctx=8192,
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
    assert settings_screen.field_widgets["calibration_duration"].value() == 15.0
    assert settings_screen.field_widgets["ollama_num_ctx"].value() == 8192
    assert settings_screen.tabs.count() == 12
    window.close()


def test_settings_screen_exposes_all_shared_setting_fields(qapp):
    from tango_posture.config import SETTING_GROUPS

    window = MainWindow(Config())
    settings_screen = window.screens[Route.SETTINGS]

    assert set(settings_screen.field_widgets) == {
        field for fields in SETTING_GROUPS.values() for field in fields
    }
    window.close()


def test_invalid_settings_are_not_applied(qapp):
    window = MainWindow(Config())
    settings_screen = window.screens[Route.SETTINGS]
    original_config = window.bridge.config.model_dump()
    settings_screen.field_widgets["ollama_model"].setText("")

    assert not settings_screen._save_settings()
    assert window.bridge.config.model_dump() == original_config
    assert window.status_strip._status_label.text().startswith("Could not apply settings:")
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
