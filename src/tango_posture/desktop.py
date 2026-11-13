"""Entry point for the packaged desktop application."""

import os
import sys
from importlib.resources import files
from pathlib import Path


def data_directory() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TangoPosture"
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "TangoPosture"
    raise RuntimeError(f"Unsupported desktop platform: {sys.platform}")


def bundled_model() -> Path:
    return Path(str(files("tango_posture").joinpath("pose_landmarker_lite.task")))


def smoke_test() -> None:
    print("Importing packaged dependencies", flush=True)
    from importlib.metadata import version

    import cv2
    import numpy as np
    import sounddevice
    from PySide6.QtWidgets import QApplication

    from .config import Config
    from .desktop_qt.shell import MainWindow
    from .models import verify
    from .pose.mediapipe_backend import MediaPipeEstimator

    print("Checking Qt desktop", flush=True)
    app = QApplication.instance()
    if app is None:
        app = QApplication(["TangoPosture", "-platform", "offscreen"])
    elif not isinstance(app, QApplication):
        raise RuntimeError("A non-GUI Qt application is already running")
    window = MainWindow(Config())
    try:
        app.processEvents()
    finally:
        window.close()
    version("tango-posture")
    print("Verifying packaged resources and pose model", flush=True)
    for name in ("TangoPosture_logo_under_1MB.png", "TANGO_POSTURE_MANUAL.md"):
        if not files("tango_posture").joinpath(name).read_bytes():
            raise ValueError(f"Empty packaged resource: {name}")
    model = bundled_model()
    verify(model)
    print("Creating MediaPipe estimator", flush=True)
    estimator = MediaPipeEstimator(model)
    try:
        print("Running blank-frame inference", flush=True)
        estimator.estimate(np.zeros((64, 64, 3), dtype=np.uint8), 0.0)
    finally:
        print("Closing MediaPipe estimator", flush=True)
        estimator.close()
    print(f"Desktop smoke test passed (OpenCV {cv2.__version__}, sounddevice {sounddevice.__version__})")


def main() -> int:
    if sys.argv[1:] == ["--smoke-test"]:
        smoke_test()
        return 0
    if sys.argv[1:]:
        raise ValueError("The desktop application does not accept command-line arguments")

    from .config import load
    from .desktop_qt import launch_qt

    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    os.chdir(directory)
    return launch_qt(load(None, {"model_path": str(bundled_model())}))
