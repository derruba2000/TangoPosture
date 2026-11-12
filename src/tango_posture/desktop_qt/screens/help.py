"""Searchable, styled offline manual shared with the Tk desktop interface."""

from __future__ import annotations

from importlib.resources import files as package_files
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...tutorial_rendering import STYLE, render_tutorial
from ..router import Router
from ..service_bridge import ServiceBridge


class HelpScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.headings: list[tuple[int, str, str]] = []
        self.search_matches: list[QTextCursor] = []
        self.search_match_index = 0
        self._search_query = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Tango Posture Manual & Guide")
        title.setProperty("class", "title")
        subtitle = QLabel("Styled offline manual, searchable topics, and linked contents.")
        subtitle.setProperty("class", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        header.addWidget(QLabel("Search manual"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Enter a word or phrase…")
        self.search_input.setAccessibleName("Search manual")
        self.search_input.returnPressed.connect(self._search_manual)
        header.addWidget(self.search_input, 1)
        self.find_button = QPushButton("Find")
        self.find_button.clicked.connect(self._search_manual)
        header.addWidget(self.find_button)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._next_match)
        header.addWidget(self.next_button)
        self.search_status = QLabel()
        header.addWidget(self.search_status)
        layout.addLayout(header)

        splitter = QSplitter()
        self.toc_list = QTreeWidget()
        self.toc_list.setHeaderHidden(True)
        self.toc_list.setAccessibleName("Manual contents")
        self.toc_list.itemClicked.connect(self._on_toc_clicked)
        splitter.addWidget(self.toc_list)

        self.viewer = QTextBrowser()
        self.viewer.setReadOnly(True)
        self.viewer.setOpenExternalLinks(True)
        self.viewer.setAccessibleName("Manual")
        self.viewer.document().setDefaultStyleSheet(STYLE)
        splitter.addWidget(self.viewer)
        splitter.setSizes([260, 700])
        layout.addWidget(splitter, 1)

        self._load_manual()

    def _load_manual(self) -> None:
        content = ""
        try:
            manual = package_files("tango_posture").joinpath("TANGO_POSTURE_MANUAL.md")
            content = manual.read_text(encoding="utf-8")
        except (OSError, ValueError, FileNotFoundError):
            path = Path(__file__).resolve().parents[4] / "docs" / "TANGO_POSTURE_GUIDE.md"
            if path.is_file():
                content = path.read_text(encoding="utf-8")

        if not content:
            content = "# TangoPosture Guide\n\nManual file could not be loaded."

        self.manual_html, self.headings, _ = render_tutorial(content)
        self.viewer.setHtml(self.manual_html)
        self._populate_contents()

    def _populate_contents(self) -> None:
        self.toc_list.clear()
        parents: list[tuple[int, QTreeWidgetItem]] = []
        for level, title, anchor in self.headings:
            while parents and parents[-1][0] >= level:
                parents.pop()
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.ItemDataRole.UserRole, anchor)
            if parents:
                parents[-1][1].addChild(item)
            else:
                self.toc_list.addTopLevelItem(item)
            parents.append((level, item))
        self.toc_list.expandAll()

    def _on_toc_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        anchor = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(anchor, str):
            self.viewer.scrollToAnchor(anchor)

    def _search_manual(self) -> None:
        query = self.search_input.text().strip()
        self._search_query = query
        self.search_matches.clear()
        self.search_match_index = 0
        if not query:
            self._update_search_highlights()
            self.search_status.clear()
            return

        document = self.viewer.document()
        position = 0
        while position < document.characterCount():
            cursor = QTextCursor(document)
            cursor.setPosition(position)
            match = document.find(query, cursor)
            if match.isNull() or match.selectionEnd() <= position:
                break
            self.search_matches.append(match)
            position = match.selectionEnd()

        if not self.search_matches:
            self.search_status.setText("No matches")
            self._update_search_highlights()
            return

        self._update_search_highlights()
        self._show_search_match()
        self.search_status.setText(f"{len(self.search_matches)} matches")

    def _next_match(self) -> None:
        if self.search_input.text().strip() != self._search_query:
            self._search_manual()
            return
        if not self.search_matches:
            return
        self.search_match_index = (self.search_match_index + 1) % len(self.search_matches)
        self._update_search_highlights()
        self._show_search_match()
        self.search_status.setText(
            f"{self.search_match_index + 1} of {len(self.search_matches)}"
        )

    def _update_search_highlights(self) -> None:
        selections: list[QTextEdit.ExtraSelection] = []
        for index, cursor in enumerate(self.search_matches):
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor  # type: ignore[attr-defined]
            text_format = QTextCharFormat()
            text_format.setBackground(
                QColor("#f5b841" if index == self.search_match_index else "#ffe89c")
            )
            text_format.setForeground(QColor("#152d45"))
            selection.format = text_format  # type: ignore[attr-defined]
            selections.append(selection)
        self.viewer.setExtraSelections(selections)

    def _show_search_match(self) -> None:
        cursor = QTextCursor(self.search_matches[self.search_match_index])
        cursor.clearSelection()
        self.viewer.setTextCursor(cursor)
        self.viewer.ensureCursorVisible()
