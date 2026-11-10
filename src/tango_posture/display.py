"""Display boundary shared by the desktop controls and the observation loop."""

from pathlib import Path
from queue import SimpleQueue
from threading import Event
from typing import Any, Protocol


class PracticeDisplay(Protocol):
    recording: Event
    aborted: Event
    auto_recording: Event
    recording_states: SimpleQueue[str]
    notes: list[str] | str

    def show(self, image: Any) -> None: ...
    def poll_key(self, quit_only: bool = False) -> int: ...
    def close(self) -> None: ...
    def report_ready(self, path: Path) -> None: ...
    def segment_saved(self, path: Path, index: int) -> None: ...
