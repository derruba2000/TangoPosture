"""Beat strip indicator widget."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ..theme import (
    COLOR_BORDER,
    COLOR_BURGUNDY,
    COLOR_COPPER,
    COLOR_PANEL,
    COLOR_TEXT_MUTED,
)


class BeatStrip(QWidget):
    """Visual 1-8 tango musical beat strip."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self._bpm: float = 120.0
        self._current_beat: int = 1
        self._is_active: bool = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._bpm_label = QLabel("120 BPM")
        self._bpm_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px; font-weight: 600;")
        layout.addWidget(self._bpm_label)

        self._beat_indicators: list[QLabel] = []
        for i in range(1, 9):
            lbl = QLabel(str(i))
            lbl.setFixedSize(22, 22)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(self._indicator_style(False, False))
            self._beat_indicators.append(lbl)
            layout.addWidget(lbl)

    def _indicator_style(self, active: bool, is_downbeat: bool) -> str:
        if active:
            bg = COLOR_BURGUNDY if is_downbeat else COLOR_COPPER
            fg = "#FFFFFF" if is_downbeat else "#15191F"
            return f"background-color: {bg}; color: {fg}; border-radius: 11px; font-size: 11px; font-weight: bold;"
        return (
            f"background-color: {COLOR_PANEL}; color: {COLOR_TEXT_MUTED}; "
            f"border: 1px solid {COLOR_BORDER}; border-radius: 11px; font-size: 10px;"
        )

    def update_beat(self, bpm: float, beat_number: int, is_active: bool) -> None:
        self._bpm = bpm
        self._bpm_label.setText(f"{int(bpm)} BPM" if is_active else "Silent")
        self._is_active = is_active
        self._current_beat = max(1, min(8, beat_number))

        for idx, lbl in enumerate(self._beat_indicators, start=1):
            is_current = is_active and (idx == self._current_beat)
            lbl.setStyleSheet(self._indicator_style(is_current, idx in (1, 5)))
