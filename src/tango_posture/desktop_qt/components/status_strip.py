"""Status strip and beat indicator widgets."""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from ..theme import (
    COLOR_BORDER,
    COLOR_BURGUNDY,
    COLOR_COPPER,
    COLOR_ELEVATED,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
)


class StatusStrip(QFrame):
    """Bottom persistent application status bar."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("statusStrip")
        self.setFixedHeight(34)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(16)

        self._status_label = QLabel("Ready")
        self._status_label.setStyleSheet(f"color: {COLOR_TEXT_PRIMARY}; font-size: 12px;")

        self._session_badge = QLabel("IDLE")
        self._session_badge.setStyleSheet(
            f"background-color: {COLOR_ELEVATED}; color: {COLOR_TEXT_MUTED}; "
            f"border: 1px solid {COLOR_BORDER}; border-radius: 3px; padding: 2px 8px; font-size: 11px; font-weight: 600;"
        )

        self._reference_label = QLabel("Reference: None")
        self._reference_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")

        layout.addWidget(self._status_label, 1)
        layout.addWidget(self._reference_label, 0)
        layout.addWidget(self._session_badge, 0)

    def set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def set_session_label(self, label: str, active: bool = False, recording: bool = False) -> None:
        self._session_badge.setText(label.upper())
        if recording:
            self._session_badge.setStyleSheet(
                f"background-color: {COLOR_BURGUNDY}; color: #FFFFFF; "
                f"border-radius: 3px; padding: 2px 8px; font-size: 11px; font-weight: bold;"
            )
        elif active:
            self._session_badge.setStyleSheet(
                f"background-color: {COLOR_COPPER}; color: #15191F; "
                f"border-radius: 3px; padding: 2px 8px; font-size: 11px; font-weight: bold;"
            )
        else:
            self._session_badge.setStyleSheet(
                f"background-color: {COLOR_ELEVATED}; color: {COLOR_TEXT_MUTED}; "
                f"border: 1px solid {COLOR_BORDER}; border-radius: 3px; padding: 2px 8px; font-size: 11px; font-weight: 600;"
            )

    def set_reference(self, name: str) -> None:
        self._reference_label.setText(f"Reference: {name}")


