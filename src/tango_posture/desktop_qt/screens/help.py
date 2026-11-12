"""Searchable offline manual and tutorial guide screen."""

from __future__ import annotations

import re
from importlib.resources import files as package_files
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..router import Router
from ..service_bridge import ServiceBridge


class HelpScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.headings: list[tuple[str, int]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Tango Posture Manual & Guide")
        title.setProperty("class", "title")
        subtitle = QLabel("Offline reference guide, keyboard shortcuts and camera placement documentation.")
        subtitle.setProperty("class", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search manual…")
        self.search_input.textChanged.connect(self._search_manual)
        header.addWidget(self.search_input)

        layout.addLayout(header)

        # Splitter: Outline on left, Reader on right
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        self.toc_list = QListWidget()
        self.toc_list.itemClicked.connect(self._on_toc_clicked)
        splitter.addWidget(self.toc_list)

        self.viewer = QTextEdit()
        self.viewer.setReadOnly(True)
        splitter.addWidget(self.viewer)

        splitter.setSizes([260, 700])

        self._load_manual()

    def _load_manual(self) -> None:
        content = ""
        try:
            manual = package_files("tango_posture").joinpath("TANGO_POSTURE_MANUAL.md")
            content = manual.read_text(encoding="utf-8")
        except (OSError, ValueError, FileNotFoundError):
            path = Path(__file__).resolve().parents[3] / "docs" / "TANGO_POSTURE_GUIDE.md"
            if path.is_file():
                content = path.read_text(encoding="utf-8")

        if not content:
            content = "# TangoPosture Guide\n\nManual file could not be loaded."

        self.viewer.setMarkdown(content)

        # Extract headings for TOC
        self.toc_list.clear()
        self.headings.clear()
        for idx, line in enumerate(content.splitlines()):
            m = re.match(r"^(#{1,3})\s+(.*)", line)
            if m:
                level = len(m.group(1))
                h_title = m.group(2)
                indent = "  " * (level - 1)
                self.toc_list.addItem(f"{indent}{h_title}")
                self.headings.append((h_title, idx))

    def _on_toc_clicked(self) -> None:
        row = self.toc_list.currentRow()
        if 0 <= row < len(self.headings):
            title, _ = self.headings[row]
            self.viewer.find(title)

    def _search_manual(self, query: str) -> None:
        if query.strip():
            self.viewer.find(query.strip())
