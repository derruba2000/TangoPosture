"""Display boundary shared by the desktop controls and the observation loop."""

from pathlib import Path
from threading import Event
from typing import Any, Protocol


class PracticeDisplay(Protocol):
    recording: Event

    def show(self, image: Any) -> None: ...
    def poll_key(self, quit_only: bool = False) -> int: ...
    def close(self) -> None: ...
    def report_ready(self, path: Path) -> None: ...
