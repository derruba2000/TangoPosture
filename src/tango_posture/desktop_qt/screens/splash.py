"""Startup splash screen for the Qt desktop application."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QVBoxLayout, QWidget

from ..theme import COLOR_BASE, COLOR_BORDER, COLOR_COPPER, COLOR_TEXT_PRIMARY


class SplashScreen(QWidget):
    def __init__(self, logo: QPixmap):
        super().__init__(
            None,
            Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint,
        )
        self.setObjectName("splashWindow")
        self.setFixedSize(440, 380)
        self.setStyleSheet(
            f"QWidget#splashWindow {{ background-color: {COLOR_BASE}; "
            f"border: 1px solid {COLOR_BORDER}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)
        layout.addStretch()

        if not logo.isNull():
            logo_label = QLabel()
            logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            logo_label.setPixmap(
                logo.scaled(
                    208,
                    208,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            layout.addWidget(logo_label)

        title = QLabel("TangoPosture")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"font-size: 25px; font-weight: bold; color: {COLOR_TEXT_PRIMARY};"
        )
        layout.addWidget(title)

        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setFixedWidth(210)
        progress.setStyleSheet(
            f"QProgressBar {{ border: 0; background-color: {COLOR_BORDER}; height: 5px; }}"
            f"QProgressBar::chunk {{ background-color: {COLOR_COPPER}; }}"
        )
        progress_row = QVBoxLayout()
        progress_row.addWidget(progress, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(progress_row)
        layout.addStretch()

        screen = QApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())
