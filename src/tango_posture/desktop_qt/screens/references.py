"""References management and calibration screen."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..router import Route, Router
from ..service_bridge import ServiceBridge
from ..theme import COLOR_TEXT_MUTED


class ReferencesScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.selected_ref = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Header with actions
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Movement References")
        title.setProperty("class", "title")
        subtitle = QLabel("Standing posture calibration baselines used for real-time comparison.")
        subtitle.setProperty("class", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)

        cal_btn = QPushButton("New Calibration")
        cal_btn.setProperty("class", "primary")
        cal_btn.clicked.connect(lambda: self.router.navigate(Route.PRACTICE))

        header.addWidget(refresh_btn)
        header.addWidget(cal_btn)
        layout.addLayout(header)

        # Splitter: Table on left, Details on right
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        # Left: Reference Table
        table_frame = QFrame()
        table_frame.setProperty("class", "card")
        tf_layout = QVBoxLayout(table_frame)
        tf_layout.setContentsMargins(12, 12, 12, 12)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Side", "Camera", "Type"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        tf_layout.addWidget(self.table)
        splitter.addWidget(table_frame)

        # Right: Detail Card
        detail_frame = QFrame()
        detail_frame.setProperty("class", "card")
        detail_frame.setMinimumWidth(300)
        detail_frame.setMaximumWidth(400)
        df_layout = QVBoxLayout(detail_frame)
        df_layout.setContentsMargins(18, 16, 18, 16)
        df_layout.setSpacing(14)

        d_title = QLabel("Reference Details")
        d_title.setStyleSheet("font-size: 15px; font-weight: bold;")
        df_layout.addWidget(d_title)

        self.detail_name = QLabel("No reference selected")
        self.detail_name.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.detail_meta = QLabel("Select a reference from the table to view details.")
        self.detail_meta.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        self.detail_meta.setWordWrap(True)

        self.confirm_camera_cb = QCheckBox("I confirm the physical camera placement is unchanged")

        self.match_btn = QPushButton("Match Camera Settings")
        self.match_btn.setProperty("class", "accent")
        self.match_btn.clicked.connect(self._match_camera_settings)

        df_layout.addWidget(self.detail_name)
        df_layout.addWidget(self.detail_meta)
        df_layout.addWidget(self.confirm_camera_cb)
        df_layout.addStretch()
        df_layout.addWidget(self.match_btn)
        splitter.addWidget(detail_frame)
        splitter.setSizes([600, 360])

        self.refresh()

    def refresh(self) -> None:
        refs = self.bridge.list_references()
        self.table.setRowCount(0)
        for row_idx, ref in enumerate(refs):
            self.table.insertRow(row_idx)
            name_item = QTableWidgetItem(ref.name)
            name_item.setData(Qt.ItemDataRole.UserRole, ref)
            self.table.setItem(row_idx, 0, name_item)
            self.table.setItem(row_idx, 1, QTableWidgetItem(ref.side.capitalize()))
            self.table.setItem(row_idx, 2, QTableWidgetItem(f"Device {ref.camera}"))
            self.table.setItem(row_idx, 3, QTableWidgetItem(".tpc" if ref.is_archive else "Legacy JSON"))

        if refs:
            self.table.selectRow(0)

    def _on_selection_changed(self) -> None:
        selected_rows = self.table.selectedItems()
        if not selected_rows:
            return
        ref = self.table.item(selected_rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        self.selected_ref = ref
        self.detail_name.setText(ref.name)
        self.detail_meta.setText(
            f"ID: {ref.id[:8]}...\n"
            f"Measurement side: {ref.side.capitalize()}\n"
            f"Facing: {ref.facing.capitalize()}\n"
            f"Camera device: {ref.camera}\n"
            f"File: {ref.path.name}"
        )
        self.confirm_camera_cb.setChecked(False)

    def _match_camera_settings(self) -> None:
        if not self.selected_ref:
            return
        cfg = self.bridge.config
        try:
            cam_idx = int(self.selected_ref.camera)
            cfg.camera = cam_idx
        except ValueError:
            pass
        if self.selected_ref.side in ("left", "right"):
            cfg.side = self.selected_ref.side  # type: ignore
        self.bridge.update_config(cfg)
        self.detail_meta.setText(self.detail_meta.text() + "\n\n✓ Camera settings matched successfully!")
