"""Application launcher and entry point for the PySide6 Qt desktop interface."""

from __future__ import annotations

import sys
from importlib.resources import files as package_files
from pathlib import Path

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication

from ..config import Config
from .shell import MainWindow


def launch_qt(cfg: Config) -> int:
    """Launch the modern PySide6 Qt desktop interface."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    elif not isinstance(app, QApplication):
        raise RuntimeError("A non-GUI Qt application is already running")

    app.setApplicationName("TangoPosture Studio")
    app.setOrganizationName("TangoPosture")

    # Set Application Icon
    data: bytes | None
    try:
        res = package_files("tango_posture").joinpath("TangoPosture_logo_under_1MB.png")
        data = res.read_bytes()
    except (OSError, ValueError, FileNotFoundError):
        path = Path(__file__).resolve().parents[3] / "logos" / "TangoPosture_logo_under_1MB.png"
        data = path.read_bytes() if path.is_file() else None

    if data:
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        app.setWindowIcon(QIcon(pixmap))

    window = MainWindow(cfg)
    window.show()
    return app.exec()
