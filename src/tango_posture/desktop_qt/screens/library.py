"""Library screen listing all saved practices and archives."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..router import Route, Router
from ..service_bridge import ServiceBridge


class LibraryScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.practices = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Practice Library")
        title.setProperty("class", "title")
        subtitle = QLabel("Browse, search and review recorded practice archives and imported video clips.")
        subtitle.setProperty("class", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        import_btn = QPushButton("Import Video…")
        import_btn.clicked.connect(self._import_video)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)

        header.addWidget(import_btn)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        # Search Bar
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search practices by name…")
        self.search_input.textChanged.connect(self._filter_table)
        search_row.addWidget(self.search_input)
        layout.addLayout(search_row)

        # Table
        table_frame = QFrame()
        table_frame.setProperty("class", "card")
        tf_layout = QVBoxLayout(table_frame)
        tf_layout.setContentsMargins(12, 12, 12, 12)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Segments", "Date / Created", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        tf_layout.addWidget(self.table)
        layout.addWidget(table_frame, 1)

        self.refresh()

    def refresh(self) -> None:
        self.practices = self.bridge.list_practices()
        self._filter_table(self.search_input.text())

    def _filter_table(self, query: str) -> None:
        q = query.strip().lower()
        filtered = [p for p in self.practices if q in p["name"].lower()] if q else self.practices

        self.table.setRowCount(0)
        for row_idx, p in enumerate(filtered):
            self.table.insertRow(row_idx)
            self.table.setItem(row_idx, 0, QTableWidgetItem(p["name"]))
            self.table.setItem(row_idx, 1, QTableWidgetItem(str(p["segments_count"])))
            self.table.setItem(row_idx, 2, QTableWidgetItem(p["created_at"] or "Recent"))

            btn = QPushButton("Open in Review")
            btn.clicked.connect(lambda _, path=p["path"]: self.router.navigate(Route.REVIEW, path))
            self.table.setCellWidget(row_idx, 3, btn)

    def _import_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a video to import",
            "",
            "Video files (*.mp4 *.mov *.avi *.mkv);;All files (*)",
        )
        if path:
            self.router.navigate(Route.REVIEW, Path(path))
