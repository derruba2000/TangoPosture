"""Local video replay, with a Tk player and optional ffplay audio output."""

import math
import os
import shutil
import subprocess
import sys
import time
import tkinter as tk
from collections.abc import Callable
from functools import partial
from pathlib import Path
from tkinter import ttk
from typing import Any

import cv2


def parse_time(value: str) -> float:
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("Use seconds, MM:SS, or HH:MM:SS")
    result = 0.0
    for part in parts:
        number = float(part)
        if not math.isfinite(number) or number < 0:
            raise ValueError("Replay time must be finite and nonnegative")
        result = result * 60 + number
    return result


def time_label(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


class VideoReader:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        if not self.path.is_file():
            raise ValueError(f"Video not found: {self.path}")
        self.cap = cv2.VideoCapture(str(self.path))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS)
        count = self.cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if (
            not self.cap.isOpened()
            or not math.isfinite(self.fps)
            or self.fps <= 0
            or not math.isfinite(count)
            or count < 1
        ):
            self.cap.release()
            raise ValueError(f"Cannot decode video or read its timing: {self.path}")
        self.count = int(count)
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.index = -1

    @property
    def duration(self) -> float:
        return self.count / self.fps

    def frame(self, index: int) -> Any:
        index = min(max(0, index), self.count - 1)
        if index != self.index + 1 and not self.cap.set(cv2.CAP_PROP_POS_FRAMES, index):
            raise ValueError("This video codec does not support seeking")
        ok, image = self.cap.read()
        if not ok:
            raise ValueError(f"Cannot decode video frame {index}")
        self.index = index
        return image

    def close(self):
        self.cap.release()


class ReplayPlayer(ttk.Frame):
    def __init__(self, parent, audio: tk.Variable):
        super().__init__(parent, padding=8)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.comparisons: list[tuple[VideoReader, tk.Label]] = []
        self.notes: list[Any] = []
        self.on_frame: Callable[[int], None] | None = None
        self.frame_transform: Callable[[Any, int, float], Any] | None = None
        self.reader: VideoReader | None = None
        self.playing = False
        self.closed = False
        self.timer: str | None = None
        self.sound: subprocess.Popen | None = None
        self.audio = audio
        self.photo: tk.PhotoImage | None = None
        self.raw_frame: Any = None
        self.last_image: Any = None
        self.speed = tk.StringVar(value="1")
        self.position = tk.DoubleVar(value=0)
        self.jump = tk.StringVar(value="00:00.0")
        self.status = tk.StringVar(value="Choose a report video, or open a video file.")
        self.clock = tk.StringVar(value="00:00.0 / 00:00.0")
        self.name = tk.StringVar(value="No video selected")
        ttk.Label(self, textvariable=self.name, wraplength=850).grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        self.images = ttk.Frame(self)
        self.images.grid(row=1, column=0, sticky="nsew")
        self.image = tk.Label(
            self.images, text="No video selected", background="#151a1d", foreground="white"
        )
        self.images.rowconfigure(0, weight=1)
        self.images.columnconfigure(0, weight=1, uniform="video")
        self.image.grid(row=0, column=0, sticky="nsew")
        self.image.bind("<Configure>", lambda _: self._render())
        self.seekbar = ttk.Scale(self, from_=0, to=1, variable=self.position)
        self.seekbar.grid(row=2, column=0, sticky="ew", pady=6)
        self.seekbar.bind("<ButtonPress-1>", lambda _: self.pause())
        self.seekbar.bind(
            "<ButtonRelease-1>", lambda _: self._guard(lambda: self.seek(self.position.get()))
        )
        controls = ttk.Frame(self)
        controls.grid(row=3, column=0, sticky="ew")
        self.play_button = ttk.Button(
            controls, text="Play", command=lambda: self._guard(self.toggle)
        )
        self.play_button.pack(side="left")
        for label, amount in (("< Frame", -1), ("Frame >", 1)):
            ttk.Button(
                controls,
                text=label,
                command=partial(self._guard, partial(self.step, amount)),
            ).pack(side="left", padx=3)
        ttk.Label(controls, text="Speed").pack(side="left", padx=(12, 4))
        speed = ttk.Combobox(
            controls,
            textvariable=self.speed,
            values=("0.25", "0.5", "1", "1.5", "2"),
            width=5,
            state="readonly",
        )
        speed.pack(side="left")
        speed.bind("<<ComboboxSelected>>", lambda _: self._guard(self._restart_clock))
        ttk.Checkbutton(
            controls,
            text="Sound (ffplay)",
            variable=audio,
            command=lambda: self._guard(self._start_sound),
        ).pack(side="left", padx=12)
        ttk.Label(controls, textvariable=self.clock).pack(side="right")
        jumps = ttk.Frame(self)
        jumps.grid(row=4, column=0, sticky="ew", pady=6)
        ttk.Entry(jumps, textvariable=self.jump, width=13).pack(side="left")
        ttk.Button(
            jumps,
            text="Go to time",
            command=lambda: self._guard(lambda: self.seek(parse_time(self.jump.get()))),
        ).pack(side="left", padx=5)
        ttk.Button(
            jumps, text="Open in system player", command=lambda: self._guard(self.external)
        ).pack(side="left", padx=5)
        ttk.Label(self, textvariable=self.status, wraplength=850).grid(row=5, column=0, sticky="w")

    def _guard(self, action):
        try:
            action()
        except (ValueError, OSError, cv2.error) as error:
            self.pause()
            self.status.set(str(error))

    def load(self, path: Path, seconds: float = 0):
        reader = VideoReader(path)
        self.pause()
        if self.reader:
            self.reader.close()
        self.reader = reader
        self.name.set(str(reader.path))
        self.seekbar.configure(to=max(0, (reader.count - 1) / reader.fps))
        self.seek(seconds)
        self.status.set(
            "Replay ready. Enable Sound for audio via ffplay, or use your system player."
        )

    def compare(self, paths, refresh=True):
        for reader, label in self.comparisons:
            reader.close()
            self.images.columnconfigure(int(label.grid_info()["column"]), weight=0, uniform="")
            label.destroy()
        self.comparisons = []
        for path in paths:
            reader = VideoReader(path)
            label = tk.Label(self.images, background="#151a1d")
            column = len(self.comparisons) + 1
            self.images.columnconfigure(column, weight=1, uniform="video")
            label.grid(row=0, column=column, sticky="nsew")
            self.comparisons.append((reader, label))
        if self.reader and refresh and paths:
            self._frame(self.reader.index)

    def _render(self):
        if self.last_image is None or self.closed:
            return
        height, width = self.last_image.shape[:2]
        scale = min(
            max(1, self.image.winfo_width()) / width,
            max(1, self.image.winfo_height()) / height,
            1.0,
        )
        rgb = cv2.cvtColor(self.last_image, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (max(1, round(width * scale)), max(1, round(height * scale))))
        h, w = rgb.shape[:2]
        self.photo = tk.PhotoImage(
            master=self, data=f"P6\n{w} {h}\n255\n".encode() + rgb.tobytes(), format="PPM"
        )
        self.image.configure(image=self.photo, text="")

    def refresh_frame(self):
        if self.reader is None or self.raw_frame is None or self.closed:
            return
        base = self.raw_frame.copy()
        self.last_image = (
            self.frame_transform(base, self.reader.index, self.reader.fps)
            if self.frame_transform is not None
            else base
        )
        self._render()

    def _frame(self, index: int):
        assert self.reader is not None
        decoded = self.reader.frame(index)
        self.raw_frame = decoded.copy()
        self.last_image = (
            self.frame_transform(decoded.copy(), self.reader.index, self.reader.fps)
            if self.frame_transform is not None
            else decoded
        )
        seconds = self.reader.index / self.reader.fps
        self.position.set(seconds)
        self.clock.set(f"{time_label(seconds)} / {time_label(self.reader.duration)}")
        self._render()
        for reader, label in self.comparisons:
            frame = reader.frame(round(seconds * reader.fps))
            h, w = frame.shape[:2]
            scale = min(max(1, label.winfo_width()) / w, max(1, label.winfo_height()) / h)
            rgb = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB),
                             (max(1, round(w * scale)), max(1, round(h * scale))))
            h, w = rgb.shape[:2]
            label.photo = tk.PhotoImage(master=self,
                data=f"P6\n{w} {h}\n255\n".encode() + rgb.tobytes(), format="PPM")
            label.configure(image=label.photo)
        if self.on_frame:
            self.on_frame(self.reader.index)

    def seek(self, seconds: float):
        if not self.reader:
            raise ValueError("Open a video first")
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Replay time must be finite and nonnegative")
        self._frame(round(seconds * self.reader.fps))
        self._restart_clock()

    def step(self, amount: int):
        if not self.reader:
            raise ValueError("Open a video first")
        self.pause()
        self._frame(self.reader.index + amount)

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            if not self.reader:
                raise ValueError("Open a video first")
            if self.reader.index >= self.reader.count - 1:
                self._frame(0)
            self.playing = True
            self.play_button.configure(text="Pause")
            self._restart_clock()
            self._tick()

    def _restart_clock(self):
        if self.reader:
            self._clock_anchor = (self.reader.index, time.monotonic())
        self._start_sound()

    def _tick(self):
        self.timer = None
        if not self.playing or not self.reader or self.closed:
            return
        index = self._clock_anchor[0] + int(
            (time.monotonic() - self._clock_anchor[1]) * self.reader.fps * float(self.speed.get())
        )
        try:
            if index != self.reader.index:
                self._frame(index)
            if index >= self.reader.count - 1:
                self.pause()
                return
            if self.sound and self.sound.poll() is not None:
                if self.sound.returncode:
                    self.status.set(
                        "Sound unavailable for this file/device. Video playback continues."
                    )
                self.sound = None
        except (ValueError, OSError, cv2.error) as error:
            self.pause()
            self.status.set(str(error))
            return
        self.timer = self.after(15, self._tick)

    def _stop_sound(self):
        if self.sound:
            if self.sound.poll() is None:
                self.sound.terminate()
                try:
                    self.sound.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    self.sound.kill()
                    self.sound.wait()
            self.sound = None

    def _start_sound(self):
        self._stop_sound()
        if not self.playing or not self.reader or not self.audio.get():
            return
        executable = shutil.which("ffplay")
        if not executable:
            self.status.set(
                "Install ffplay (FFmpeg) for sound, or open this video in your system player."
            )
            return
        speed = float(self.speed.get())
        tempo = "atempo=0.5,atempo=0.5" if speed == 0.25 else f"atempo={speed}"
        self.sound = subprocess.Popen(
            [
                executable,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-ss",
                str(self.position.get()),
                "-vn",
                "-af",
                tempo,
                "-i",
                str(self.reader.path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def pause(self):
        self.playing = False
        self.play_button.configure(text="Play")
        if self.timer:
            self.after_cancel(self.timer)
            self.timer = None
        self._stop_sound()

    def external(self):
        if not self.reader:
            raise ValueError("Open a video first")
        self.pause()
        if sys.platform == "win32":
            os.startfile(str(self.reader.path))
        else:
            subprocess.Popen(
                ["open" if sys.platform == "darwin" else "xdg-open", str(self.reader.path)]
            )

    def close(self):
        if not self.closed:
            self.unload()
            self.closed = True

    def unload(self):
        self.pause()
        self.compare([], refresh=False)
        if self.reader:
            self.reader.close()
            self.reader = None
        self.raw_frame = self.last_image = self.photo = None
        self.image.configure(image="", text="No video selected")
        self.name.set("No video selected")
        self.position.set(0)
        self.clock.set("00:00.0 / 00:00.0")
