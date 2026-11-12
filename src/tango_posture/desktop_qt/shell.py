"""Main window shell with left navigation rail and stacked content screens."""

from __future__ import annotations

from importlib.resources import files as package_files
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from .components.status_strip import StatusStrip
from .router import Route, Router
from .screens.analytics import AnalyticsScreen
from .screens.coach import CoachScreen
from .screens.help import HelpScreen
from .screens.home import HomeScreen
from .screens.library import LibraryScreen
from .screens.practice import PracticeScreen
from .screens.references import ReferencesScreen
from .screens.review import ReviewScreen
from .screens.settings import SettingsScreen
from .service_bridge import ServiceBridge
from .theme import (
    STYLESHEET,
)


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, parent: QWidget | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.bridge = ServiceBridge(cfg, self)
        self.router = Router(self)

        self.setWindowTitle("TangoPosture Studio")
        self.resize(1200, 800)
        self.setMinimumSize(1024, 680)
        self.setStyleSheet(STYLESHEET)

        # Main Root Widget
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Body: Left Nav + Central Screen Stack
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # 1. Left Navigation Rail
        self.nav_rail = QFrame()
        self.nav_rail.setObjectName("navRail")
        self.nav_rail.setFixedWidth(210)
        nav_layout = QVBoxLayout(self.nav_rail)
        nav_layout.setContentsMargins(12, 18, 12, 14)
        nav_layout.setSpacing(4)

        # Brand Header
        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        logo_lbl = QLabel()
        logo_data = self._load_logo()
        if logo_data:
            pixmap = QPixmap()
            pixmap.loadFromData(logo_data)
            logo_lbl.setPixmap(pixmap.scaled(28, 28, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        brand_title = QLabel("TangoPosture")
        brand_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #FFFFFF;")
        brand_row.addWidget(logo_lbl)
        brand_row.addWidget(brand_title)
        brand_row.addStretch()
        nav_layout.addLayout(brand_row)
        nav_layout.addSpacing(18)

        # Nav Buttons Group
        self.btn_group = QButtonGroup(self)
        self.btn_group.setExclusive(True)
        self.nav_buttons: dict[Route, QPushButton] = {}

        nav_items = [
            (Route.HOME, "⌂  Home"),
            (Route.PRACTICE, "●  Live Practice"),
            (Route.REFERENCES, "◎  References"),
            (Route.LIBRARY, "▤  Library"),
            (Route.REVIEW, "▶  Review"),
            (Route.ANALYTICS, "📈 Analytics"),
            (Route.COACH, "✦  AI Coach"),
            (Route.SETTINGS, "⚙  Settings"),
            (Route.HELP, "?  Help & Guide"),
        ]

        for route, label in nav_items:
            btn = QPushButton(label)
            btn.setProperty("class", "nav-btn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _, r=route: self.router.navigate(r))
            self.btn_group.addButton(btn)
            self.nav_buttons[route] = btn
            nav_layout.addWidget(btn)

        nav_layout.addStretch()
        body_layout.addWidget(self.nav_rail)

        # 2. Central Stacked Content Screens
        self.stack = QStackedWidget()
        self.screens: dict[Route, QWidget] = {
            Route.HOME: HomeScreen(self.bridge, self.router),
            Route.PRACTICE: PracticeScreen(self.bridge, self.router),
            Route.REFERENCES: ReferencesScreen(self.bridge, self.router),
            Route.LIBRARY: LibraryScreen(self.bridge, self.router),
            Route.REVIEW: ReviewScreen(self.bridge, self.router),
            Route.ANALYTICS: AnalyticsScreen(self.bridge, self.router),
            Route.COACH: CoachScreen(self.bridge, self.router),
            Route.SETTINGS: SettingsScreen(self.bridge, self.router),
            Route.HELP: HelpScreen(self.bridge, self.router),
        }

        for route, screen in self.screens.items():
            self.stack.addWidget(screen)

        body_layout.addWidget(self.stack, 1)
        root_layout.addWidget(body, 1)

        # 3. Bottom Status Strip
        self.status_strip = StatusStrip(self)
        root_layout.addWidget(self.status_strip)

        # Wire Signals
        self.router.route_changed.connect(self._on_route_changed)
        self.bridge.status_message.connect(self.status_strip.set_status)

        # Setup Shortcuts (Ctrl+1 to Ctrl+9 / Cmd+1 to Cmd+9)
        for idx, (route, _) in enumerate(nav_items, start=1):
            sc = QShortcut(QKeySequence(f"Ctrl+{idx}"), self)
            sc.activated.connect(lambda r=route: self.router.navigate(r))

        # Initial Route
        self.nav_buttons[Route.HOME].setChecked(True)
        self.stack.setCurrentWidget(self.screens[Route.HOME])

    def _load_logo(self) -> bytes | None:
        try:
            res = package_files("tango_posture").joinpath("TangoPosture_logo_under_1MB.png")
            return res.read_bytes()
        except (OSError, ValueError, FileNotFoundError):
            path = Path(__file__).resolve().parents[3] / "logos" / "TangoPosture_logo_under_1MB.png"
            if path.is_file():
                return path.read_bytes()
        return None

    def _on_route_changed(self, route: Route, payload: object) -> None:
        if route in self.nav_buttons:
            self.nav_buttons[route].setChecked(True)

        screen = self.screens.get(route)
        if screen:
            self.stack.setCurrentWidget(screen)
            if hasattr(screen, "refresh"):
                screen.refresh()
            if route is Route.REVIEW and isinstance(payload, Path) and hasattr(screen, "load_archive"):
                screen.load_archive(payload)
