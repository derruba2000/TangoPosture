"""Sequential file decode and a latest-frame-only webcam producer."""

import ctypes
import ctypes.util
import math
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2

from .config import Config


def _mac_camera_status() -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p
        msg_str = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p
        )(("objc_msgSend", objc))
        media = msg_str(
            objc.objc_getClass(b"NSString"),
            objc.sel_registerName(b"stringWithUTF8String:"),
            b"vide",
        )
        msg_status = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(("objc_msgSend", objc))
        return int(
            msg_status(
                objc.objc_getClass(b"AVCaptureDevice"),
                objc.sel_registerName(b"authorizationStatusForMediaType:"),
                media,
            )
        )
    except (OSError, AttributeError):
        return None


@dataclass
class Captured:
    image: Any
    timestamp: float
    captured_at: float


class Capture:
    def __init__(self, cfg: Config, video: str | None):
        if (
            video is not None
            and not Path(video).is_file()
            and (Path("videos") / Path(video).name).is_file()
        ):
            video = str(Path("videos") / Path(video).name)
        self.video = video
        self.cap = cv2.VideoCapture(video if video is not None else cfg.camera)
        if not self.cap.isOpened() and video is None:
            deadline = time.monotonic() + 30
            while _mac_camera_status() == 0 and time.monotonic() < deadline:
                time.sleep(0.25)
            if _mac_camera_status() == 3:
                self.cap.release()
                self.cap = cv2.VideoCapture(cfg.camera)
        if not self.cap.isOpened():
            self.cap.release()
            if video:
                if not Path(video).is_file():
                    raise ValueError(
                        f"Cannot read video file: '{video}' does not exist "
                        f"(record one first with --camera 0 --record-video videos/{Path(video).name})"
                    )
                raise ValueError(f"Cannot read video file: '{video}'")
            raise ValueError("Camera unavailable: check device index and OS camera permissions")
        if not video:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        self.fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.frame_count = self.cap.get(cv2.CAP_PROP_FRAME_COUNT)
        self.index = 0
        self.last = -1.0
        self.fallback = False
        self.fallback_offset: float | None = None
        self.dropped = 0
        self.origin = time.monotonic()
        self.queue: queue.Queue[Captured] = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.failed = threading.Event()
        self.thread: threading.Thread | None = None
        if not video:
            self.thread = threading.Thread(target=self._produce, daemon=True)
            self.thread.start()

    def _produce(self) -> None:
        while not self.stop.is_set():
            ok, image = self.cap.read()
            now = time.monotonic()
            if not ok:
                self.failed.set()
                break
            frame = Captured(image, now - self.origin, now)
            try:
                self.queue.put_nowait(frame)
            except queue.Full:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass
                self.queue.put_nowait(frame)

    def read(self) -> Captured | None:
        if not self.video:
            if self.failed.is_set():
                raise OSError("Camera disconnected")
            try:
                frame = self.queue.get(timeout=0.03)
                if time.monotonic() - frame.captured_at > 0.5:
                    raise OSError("Camera stalled: no fresh frame")
                return frame
            except queue.Empty:
                if time.monotonic() - self.origin - max(self.last, 0) > 3:
                    raise OSError("Camera stalled or permissions denied")
                return None
        ok, image = self.cap.read()
        if not ok:
            if self.frame_count > 0 and self.index < self.frame_count - 1:
                raise OSError("Video decoding stopped before declared EOF")
            return None
        timestamp = self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
        if self.fallback or not math.isfinite(timestamp) or timestamp <= self.last:
            if not math.isfinite(self.fps) or self.fps <= 0:
                raise ValueError(
                    "Video has neither reliable presentation timestamps nor declared FPS"
                )
            if self.fallback_offset is None:
                self.fallback_offset = max(0, self.last + 1 / self.fps - self.index / self.fps)
            self.fallback = True
            timestamp = self.index / self.fps + self.fallback_offset
        self.last = timestamp
        self.index += 1
        return Captured(image, timestamp, time.monotonic())

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        self.cap.release()
        if self.thread:
            self.thread.join(timeout=1)
