"""Shared local video decoding, timing and export helpers."""

import math
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
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


def cut_video_segment(
    source_path: Path | str,
    output_path: Path | str,
    start_seconds: float,
    end_seconds: float,
) -> Path:
    """Write a new video containing the selected `[start, end)` source interval."""
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if source == output:
        raise ValueError("Choose a different output path so the source video stays unchanged")
    if output.exists():
        raise FileExistsError(f"Output file already exists: {output}")
    if (
        not math.isfinite(start_seconds)
        or not math.isfinite(end_seconds)
        or start_seconds < 0.0
        or end_seconds <= start_seconds
    ):
        raise ValueError("End time must be greater than a nonnegative start time")

    reader = VideoReader(source)
    duration = reader.duration
    fps = reader.fps
    width, height = reader.width, reader.height
    reader.close()
    if end_seconds > duration:
        raise ValueError(f"End time exceeds video duration ({duration:.2f}s)")
    output.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        command = [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-ss",
            f"{start_seconds:.6f}",
            "-i",
            str(source),
            "-t",
            f"{end_seconds - start_seconds:.6f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise ValueError(f"Could not start ffmpeg: {error}") from error
        if result.returncode != 0:
            output.unlink(missing_ok=True)
            detail = (result.stderr or "").strip().splitlines()
            raise ValueError(detail[-1] if detail else "ffmpeg could not cut this video")
        if not output.is_file() or output.stat().st_size == 0:
            raise ValueError("ffmpeg completed without creating a video file")
        return output

    capture = cv2.VideoCapture(str(source))
    writer: cv2.VideoWriter | None = None
    try:
        start_frame = min(max(0, round(start_seconds * fps)), max(0, round(duration * fps) - 1))
        end_frame = min(round(duration * fps), math.ceil(end_seconds * fps))
        capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        fourcc = "MJPG" if output.suffix.lower() == ".avi" else "mp4v"
        writer = cv2.VideoWriter(
            str(output), cv2.VideoWriter.fourcc(*fourcc), fps, (width, height)
        )
        if not writer.isOpened():
            raise OSError(f"Cannot create sliced video: {output}")
        written = 0
        for _ in range(start_frame, end_frame):
            ok, frame = capture.read()
            if not ok:
                break
            writer.write(frame)
            written += 1
        if written == 0:
            raise ValueError("The selected range contains no decodable frames")
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        capture.release()
        if writer is not None:
            writer.release()
    return output


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


def save_overlay_video(
    source_path: Path | str,
    output_path: Path | str,
    start_frame: int,
    end_frame: int,
    *,
    frame_transform: Callable[[Any, int, float], Any],
) -> tuple[Path, bool]:
    """Export transformed frames from an inclusive source-frame interval.

    Returns the output path and whether source audio was muxed into the result.
    """
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if source == output:
        raise ValueError("Choose a different output path so the source video stays unchanged")
    if output.exists():
        raise FileExistsError(f"Output file already exists: {output}")
    reader = VideoReader(source)
    fps, frame_count = reader.fps, reader.count
    reader.close()
    if not (0 <= start_frame <= end_frame < frame_count):
        raise ValueError("Selected replay range is outside the source video")

    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="tango-overlay-") as temp_dir:
        rendered_path = Path(temp_dir) / "rendered.mp4"
        capture = cv2.VideoCapture(str(source))
        writer: cv2.VideoWriter | None = None
        rendered_size: tuple[int, int] | None = None
        try:
            capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
            written = 0
            for frame_index in range(start_frame, end_frame + 1):
                ok, image = capture.read()
                if not ok or image is None:
                    break
                rendered = frame_transform(image, frame_index, fps)
                height, width = rendered.shape[:2]
                if rendered_size is None:
                    rendered_size = (width, height)
                    writer = cv2.VideoWriter(
                        str(rendered_path),
                        cv2.VideoWriter.fourcc(*"mp4v"),
                        fps,
                        rendered_size,
                    )
                    if not writer.isOpened():
                        raise OSError(f"Cannot create overlay video: {rendered_path}")
                elif rendered_size != (width, height):
                    raise ValueError("Overlay renderer changed frame dimensions during export")
                assert writer is not None
                writer.write(rendered)
                written += 1
            if written == 0:
                raise ValueError("Selected replay range contains no decodable frames")
        except Exception:
            output.unlink(missing_ok=True)
            raise
        finally:
            capture.release()
            if writer is not None:
                writer.release()

        if ffmpeg is None:
            shutil.copyfile(rendered_path, output)
            return output, False

        start_seconds = start_frame / fps
        duration_seconds = written / fps
        command = [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-i",
            str(rendered_path),
            "-ss",
            f"{start_seconds:.6f}",
            "-t",
            f"{duration_seconds:.6f}",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(output),
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise ValueError(f"Could not start ffmpeg: {error}") from error
        if result.returncode != 0:
            output.unlink(missing_ok=True)
            detail = (result.stderr or "").strip().splitlines()
            raise ValueError(detail[-1] if detail else "Could not add source audio")
        return output, True
