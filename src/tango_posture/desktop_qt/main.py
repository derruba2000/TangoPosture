"""Application launcher and entry point for the PySide6 Qt desktop interface."""

from __future__ import annotations

import sys
from importlib.resources import files as package_files
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication

from ..config import Config
from .screens.splash import SplashScreen
from .shell import MainWindow

SPLASH_DURATION_MS = 3000


def _load_logo() -> QPixmap:
    data: bytes | None
    try:
        resource = package_files("tango_posture").joinpath("TangoPosture_logo_under_1MB.png")
        data = resource.read_bytes()
    except (OSError, ValueError, FileNotFoundError):
        path = Path(__file__).resolve().parents[3] / "logos" / "TangoPosture_logo_under_1MB.png"
        data = path.read_bytes() if path.is_file() else None

    pixmap = QPixmap()
    if data:
        pixmap.loadFromData(data)
    return pixmap


def show_startup_splash(window: MainWindow, logo: QPixmap) -> SplashScreen:
    splash = SplashScreen(logo)
    splash.show()

    def finish_startup() -> None:
        window.show()
        window.raise_()
        splash.close()

    QTimer.singleShot(SPLASH_DURATION_MS, finish_startup)
    return splash


def launch_qt(cfg: Config) -> int:
    """Launch the modern PySide6 Qt desktop interface."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    elif not isinstance(app, QApplication):
        raise RuntimeError("A non-GUI Qt application is already running")

    app.setApplicationName("TangoPosture Studio")
    app.setOrganizationName("TangoPosture")

    logo = _load_logo()
    if not logo.isNull():
        pixmap = logo
        app.setWindowIcon(QIcon(pixmap))

    window = MainWindow(cfg)
    show_startup_splash(window, logo)
    return app.exec()
