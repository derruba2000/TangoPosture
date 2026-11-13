import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from tango_posture import desktop


def test_desktop_uses_user_data_and_bundled_model(monkeypatch, tmp_path):
    from tango_posture import desktop_qt

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

    monkeypatch.setattr(desktop_qt, "launch_qt", launch)
    assert desktop.main() == 0
    assert captured == {"cwd": user_data, "model": str(model), "data": "data"}


def test_default_desktop_starts_without_tk(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", """
import importlib.abc
import sys

class NoTk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in ("tkinter", "_tkinter"):
            raise ImportError("Tk is unavailable")

sys.meta_path.insert(0, NoTk())
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from tango_posture.cli import main

app = QApplication(["TangoPosture", "-platform", "offscreen"])
timer = QTimer()
timer.setInterval(250)
timer.timeout.connect(lambda: app.exit(0))
timer.start()
result = main([])
assert "tkinter" not in sys.modules
assert "_tkinter" not in sys.modules
raise SystemExit(result)
"""],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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


@pytest.mark.parametrize("fails", [False, True])
def test_packaged_entry_logs_smoke_test_result(monkeypatch, tmp_path, fails):
    log = tmp_path / "smoke.log"
    monkeypatch.setenv("TANGOPOSTURE_SMOKE_LOG", str(log))
    monkeypatch.setattr(sys, "argv", ["TangoPosture", "--smoke-test"])
    stdout, stderr = sys.stdout, sys.stderr

    def main():
        print("Checking packaged dependencies")
        if fails:
            raise RuntimeError("Missing packaged native library")
        print("Desktop smoke test passed")
        return 0

    monkeypatch.setattr(desktop, "main", main)
    entry = Path(__file__).resolve().parents[2] / "scripts" / "desktop_entry.py"
    namespace = runpy.run_path(str(entry))
    assert namespace["run"]() == (1 if fails else 0)
    content = log.read_text(encoding="utf-8")
    assert "Checking packaged dependencies" in content
    if fails:
        assert "Traceback" in content and "Missing packaged native library" in content
        assert "Desktop smoke test passed" not in content
    else:
        assert "Desktop smoke test passed" in content
    assert sys.stdout is stdout and sys.stderr is stderr


def test_packaged_entry_preserves_normal_launch_errors(monkeypatch, tmp_path):
    log = tmp_path / "smoke.log"
    monkeypatch.setenv("TANGOPOSTURE_SMOKE_LOG", str(log))
    monkeypatch.setattr(sys, "argv", ["TangoPosture"])

    def main():
        raise RuntimeError("Normal launch failed")

    monkeypatch.setattr(desktop, "main", main)
    entry = Path(__file__).resolve().parents[2] / "scripts" / "desktop_entry.py"
    namespace = runpy.run_path(str(entry))
    with pytest.raises(RuntimeError, match="Normal launch failed"):
        namespace["run"]()
    assert not log.exists()
