"""Branded About dialog for the Qt desktop application."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class AboutDialog(QDialog):
    def __init__(self, logo: QPixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About TangoPosture")
        self.setModal(False)
        self.setFixedWidth(390)

        try:
            app_version = version("tango-posture")
        except PackageNotFoundError:
            app_version = "development"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)

        if not logo.isNull():
            logo_label = QLabel()
            logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            logo_label.setPixmap(
                logo.scaled(
                    92,
                    92,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            layout.addWidget(logo_label)

        title = QLabel("TangoPosture")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        self.version_label = QLabel(f"Version {app_version}")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.version_label)

        self.license_label = QLabel("License: Apache-2.0")
        self.license_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.license_label)

        self.repo_link = QLabel(
            '<a href="https://github.com/derruba2000/TangoPosture">GitHub repository</a>'
        )
        self.repo_link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.repo_link.setOpenExternalLinks(True)
        layout.addWidget(self.repo_link)

        self.disclaimer_label = QLabel(
            "Experimental movement observation, not a medical assessment."
        )
        self.disclaimer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.disclaimer_label.setWordWrap(True)
        layout.addWidget(self.disclaimer_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.hide)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
