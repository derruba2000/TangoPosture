"""Home screen dashboard with quick start and recent practice history."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..router import Route, Router
from ..service_bridge import ServiceBridge
from ..theme import COLOR_TEXT_MUTED


class HomeScreen(QScrollArea):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        self.setWidget(container)
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(32, 28, 32, 28)
        main_layout.setSpacing(24)

        # Header
        header = QVBoxLayout()
        header.setSpacing(4)
        title = QLabel("TangoPosture Studio")
        title.setProperty("class", "title")
        subtitle = QLabel("Local, reference-relative movement observation for Argentine Tango dancers.")
        subtitle.setProperty("class", "subtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        main_layout.addLayout(header)

        # Action Cards Row
        cards_grid = QGridLayout()
        cards_grid.setSpacing(16)

        # 1. Quick Start Practice Card
        practice_card = QFrame()
        practice_card.setProperty("class", "card")
        p_layout = QVBoxLayout(practice_card)
        p_layout.setContentsMargins(20, 18, 20, 18)
        p_layout.setSpacing(12)

        p_title = QLabel("Live Practice")
        p_title.setStyleSheet("font-size: 15px; font-weight: bold;")
        cfg = self.bridge.config
        self.setup_summary = QLabel(
            f"Camera {cfg.camera} · {cfg.width}×{cfg.height} · {cfg.side} side · {cfg.facing} facing"
        )
        self.setup_summary.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")

        start_btn = QPushButton("Start Practice")
        start_btn.setProperty("class", "primary")
        start_btn.setFixedHeight(36)
        start_btn.clicked.connect(lambda: self.router.navigate(Route.PRACTICE))

        p_layout.addWidget(p_title)
        p_layout.addWidget(self.setup_summary)
        p_layout.addStretch()
        p_layout.addWidget(start_btn)
        cards_grid.addWidget(practice_card, 0, 0)

        # 2. Reference Card
        ref_card = QFrame()
        ref_card.setProperty("class", "card")
        r_layout = QVBoxLayout(ref_card)
        r_layout.setContentsMargins(20, 18, 20, 18)
        r_layout.setSpacing(12)

        r_title = QLabel("Active Reference")
        r_title.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.ref_summary = QLabel("Ready for comparison")
        self.ref_summary.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")

        ref_btn = QPushButton("Manage References")
        ref_btn.setProperty("class", "accent")
        ref_btn.setFixedHeight(36)
        ref_btn.clicked.connect(lambda: self.router.navigate(Route.REFERENCES))

        r_layout.addWidget(r_title)
        r_layout.addWidget(self.ref_summary)
        r_layout.addStretch()
        r_layout.addWidget(ref_btn)
        cards_grid.addWidget(ref_card, 0, 1)

        main_layout.addLayout(cards_grid)

        # Recent Practices Section
        recent_header = QHBoxLayout()
        recent_title = QLabel("Recent Practice Archives")
        recent_title.setStyleSheet("font-size: 16px; font-weight: 600;")
        view_all_btn = QPushButton("View All in Library →")
        view_all_btn.clicked.connect(lambda: self.router.navigate(Route.LIBRARY))
        recent_header.addWidget(recent_title)
        recent_header.addStretch()
        recent_header.addWidget(view_all_btn)
        main_layout.addLayout(recent_header)

        self.recent_list_container = QVBoxLayout()
        self.recent_list_container.setSpacing(10)
        main_layout.addLayout(self.recent_list_container)

        main_layout.addStretch()
        self.refresh()

    def refresh(self) -> None:
        cfg = self.bridge.config
        self.setup_summary.setText(
            f"Camera {cfg.camera} · {cfg.width}×{cfg.height} · {cfg.side} side · {cfg.facing} facing"
        )
        refs = self.bridge.list_references()
        if refs:
            latest = refs[0]
            self.ref_summary.setText(f"Active: {latest.name} ({latest.side} side)")
        else:
            self.ref_summary.setText("No saved reference found. Create one before scoring.")

        # Clear old items in recent list
        while self.recent_list_container.count():
            item = self.recent_list_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        practices = self.bridge.list_practices()
        if not practices:
            empty_lbl = QLabel("No practice archives yet. Record your first practice session!")
            empty_lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; padding: 12px;")
            self.recent_list_container.addWidget(empty_lbl)
        else:
            for item in practices[:5]:
                row = QFrame()
                row.setProperty("class", "card")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(16, 12, 16, 12)

                info_col = QVBoxLayout()
                p_name = QLabel(item["name"])
                p_name.setStyleSheet("font-weight: 600; font-size: 13px;")
                p_sub = QLabel(f"Segments: {item['segments_count']} · {item['path'].name}")
                p_sub.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
                info_col.addWidget(p_name)
                info_col.addWidget(p_sub)

                review_btn = QPushButton("Review")
                review_btn.clicked.connect(lambda _, path=item["path"]: self.router.navigate(Route.REVIEW, path))

                row_layout.addLayout(info_col)
                row_layout.addStretch()
                row_layout.addWidget(review_btn)
                self.recent_list_container.addWidget(row)
