"""Screen routes and navigation state for the desktop application."""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QObject, Signal


class Route(StrEnum):
    HOME = "home"
    PRACTICE = "practice"
    REFERENCES = "references"
    LIBRARY = "library"
    REVIEW = "review"
    ANALYTICS = "analytics"
    COACH = "coach"
    SETTINGS = "settings"
    HELP = "help"


class Router(QObject):
    route_changed = Signal(Route, object)  # route, optional payload

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._current_route = Route.HOME
        self._history: list[Route] = [Route.HOME]

    @property
    def current(self) -> Route:
        return self._current_route

    def navigate(self, target: Route | str, payload: object = None) -> None:
        route = Route(target)
        if route != self._current_route or payload is not None:
            self._current_route = route
            self._history.append(route)
            self.route_changed.emit(route, payload)

    def go_back(self) -> bool:
        if len(self._history) > 1:
            self._history.pop()  # pop current
            prev = self._history[-1]
            self._current_route = prev
            self.route_changed.emit(prev, None)
            return True
        return False
