"""Practice options and thread-safe display transport, independent of Tk."""

import math
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config


def next_recordings(folder: Path = Path("videos")) -> tuple[str, str]:
    index = 1
    while any((folder / f"{name}_{index:02d}.mp4").exists() for name in ("practice", "overlay")):
        index += 1
    return str(folder / f"practice_{index:02d}.mp4"), str(folder / f"overlay_{index:02d}.mp4")


@dataclass
class PracticeOptions:
    source: str = "camera"
    video: str = ""
    headless: bool = False
    reference_mode: str = "new"
    calibration: str = "latest"
    confirm_camera: bool = False
    interval_start: str = "0"
    interval_end: str = "12"
    reference_name: str = "My reference"
    teacher_note: str = ""
    practice_name: str = "My practice"
    record_raw: bool = False
    raw_path: str = "videos/practice_01.mp4"
    record_overlay: bool = False
    overlay_path: str = "videos/overlay_01.mp4"

    def __post_init__(self):
        for name, field in self.__dataclass_fields__.items():
            if not isinstance(getattr(self, name), type(field.default)):
                raise TypeError(f"Invalid preset value for {name}")

    def recording_paths(self) -> list[Path]:
        from .app import _video_path

        paths = []
        for enabled, name in (
            (self.record_raw, self.raw_path),
            (self.record_overlay, self.overlay_path),
        ):
            if enabled:
                if not name.strip() or Path(name).suffix.lower() not in (".mp4", ".avi"):
                    raise ValueError("Choose an .mp4 or .avi filename for each enabled recording")
                path = _video_path(Path(name))
                assert path is not None
                paths.append(path.resolve())
        if len(paths) != len(set(paths)):
            raise ValueError("Raw and overlay recordings must use different filenames")
        if self.source == "video" and Path(self.video).resolve() in paths:
            raise ValueError("A recording must not overwrite its input video")
        return paths

    def arguments(self, cfg: Config, config_path: Path) -> list[str]:
        args = ["--config", str(config_path), "run"]
        if self.source == "video":
            if not self.video.strip() or not Path(self.video).is_file():
                raise ValueError("Select an existing input video")
            args += ["--video", self.video]
        elif self.source != "camera":
            raise ValueError("Choose camera or video as the practice source")
        if self.headless:
            if self.source != "video" or self.reference_mode == "new":
                raise ValueError(
                    "Analysis without a preview requires a video and a saved reference or interval"
                )
            args += ["--headless"]
        if not self.reference_name.strip():
            raise ValueError("Give the reference a name")
        args += ["--reference-name", self.reference_name]
        if self.reference_mode == "saved":
            if not self.calibration.strip():
                raise ValueError("Select a saved reference, its UUID, or a JSON file")
            if not self.confirm_camera:
                raise ValueError(
                    "Confirm the camera placement is unchanged, or make a new reference"
                )
            args += ["--calibration", self.calibration, "--confirm-camera-unchanged"]
        elif self.reference_mode == "interval":
            start, end = float(self.interval_start), float(self.interval_end)
            if (
                self.source != "video"
                or not all(math.isfinite(v) for v in (start, end))
                or start < 0
            ):
                raise ValueError(
                    "A reference interval requires a video and finite nonnegative times"
                )
            if abs(end - start - cfg.calibration_duration) > 1e-6:
                raise ValueError(f"Reference interval must be {cfg.calibration_duration:g} seconds")
            args += ["--reference-interval", str(start), str(end)]
        elif self.reference_mode != "new":
            raise ValueError("Choose a new, saved, or video-interval reference")
        if self.teacher_note.strip():
            args += ["--teacher-reviewed", self.teacher_note]
        from .archive import practice_path

        practice_path(Path("practice"), self.practice_name)
        args += ["--practice-name", self.practice_name]
        if self.headless:
            args += ["--record-video", "--record-overlay"]
        return args


class GuiDisplay:
    """Bounded frame mailbox and lossless controls; no Tk calls from inference threads."""

    def __init__(self):
        self.frames: queue.Queue = queue.Queue(maxsize=1)
        self.keys: queue.SimpleQueue[int] = queue.SimpleQueue()
        self.stopped = threading.Event()
        self.aborted = threading.Event()
        self.recording = threading.Event()
        self.auto_recording = threading.Event()
        self.recording_states: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.reports: queue.SimpleQueue[Path] = queue.SimpleQueue()

    def show(self, image: Any) -> None:
        try:
            self.frames.get_nowait()
        except queue.Empty:
            pass
        self.frames.put_nowait(image)

    def request(self, key: str) -> None:
        if key == "x":
            self.aborted.set()
            self.stopped.set()
        elif key == "q":
            self.stopped.set()
        else:
            self.keys.put(ord(key))

    def poll_key(self, quit_only: bool = False) -> int:
        if self.aborted.is_set():
            return ord("x")
        if self.stopped.is_set():
            return ord("q")
        if not quit_only:
            try:
                return self.keys.get_nowait()
            except queue.Empty:
                pass
        return -1

    def close(self) -> None:
        self.stopped.set()

    def report_ready(self, path: Path) -> None:
        self.reports.put(path.resolve())
