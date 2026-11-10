import sys
from pathlib import Path

import pytest

from tango_posture import desktop


def test_desktop_uses_user_data_and_bundled_model(monkeypatch, tmp_path):
    from tango_posture import gui

    user_data = tmp_path / "Application Support" / "TangoPosture"
    model = tmp_path / "installed app" / "model.task"
    captured = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["TangoPosture"])
    monkeypatch.setattr(desktop, "data_directory", lambda: user_data)
    monkeypatch.setattr(desktop, "bundled_model", lambda: model)

    def launch(cfg):
        captured["cwd"] = Path.cwd()
        captured["model"] = cfg.model_path
        captured["data"] = cfg.data_dir
        return 0

    monkeypatch.setattr(gui, "launch", launch)
    assert desktop.main() == 0
    assert captured == {"cwd": user_data, "model": str(model), "data": "data"}


def test_desktop_rejects_unexpected_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["TangoPosture", "--invalid"])
    with pytest.raises(ValueError, match="does not accept"):
        desktop.main()


def test_windows_desktop_data_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert desktop.data_directory() == tmp_path / "TangoPosture"


def test_macos_desktop_data_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert desktop.data_directory() == tmp_path / "Library" / "Application Support" / "TangoPosture"
