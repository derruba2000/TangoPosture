"""Owned local replay resources for the Qt review screen."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from pathlib import Path

from ..archive import PracticeArchive
from ..replay import VideoReader
from ..review import ReportRecord, read_report

VIDEO_SUFFIXES = (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm")


class ReviewPlayback:
    def __init__(self, reader_factory: Callable[[Path], VideoReader] = VideoReader) -> None:
        self._reader_factory = reader_factory
        self.reader: VideoReader | None = None
        self.archive: PracticeArchive | None = None
        self.variants: dict[str, Path] = {}
        self.report = ""
        self.warning = ""
        self._report: ReportRecord | None = None
        self._selected_variant = ""
        self._record_timestamps: list[float] = []
        self.selected_segment_index: int | None = None
        self.timestamps: list[float] = []
        self._relative_timestamps: list[float] = []

    @property
    def segment_count(self) -> int:
        return len(self.archive.manifest["segments"]) if self.archive else 0

    def open(self, path: Path) -> None:
        self.close()
        path = path.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"File not found: {path}")
        if path.suffix.lower() in (".tp", ".tpc"):
            self.archive = PracticeArchive(path)
            if not self.segment_count:
                self.close()
                raise ValueError("This archive contains no recordings")
        elif path.suffix.lower() in VIDEO_SUFFIXES:
            self.variants = {"Local video": path}
        elif path.suffix.lower() == ".md":
            report = read_report(path)
            self._report = report
            self.report = report.markdown
            self.warning = report.warning
            self._record_timestamps = report.timestamps
            for label, report_label in (
                ("Overlay video", "Overlay"),
                ("Raw video", "Raw video"),
                ("Source video", "Source video"),
            ):
                video = report.videos.get(report_label)
                if video is not None and video.is_file():
                    self.variants[label] = video
                elif video is not None:
                    message = f"linked {report_label.lower()} is missing; relink the report video"
                    self.warning = f"{self.warning}; {message}" if self.warning else message
        else:
            raise ValueError(f"Unsupported replay file type: {path.suffix or '(none)'}")

    def select_segment(self, index: int) -> None:
        self.close_reader()
        self.variants = {}
        self.report = ""
        self.warning = ""
        self._report = None
        self._selected_variant = ""
        self._record_timestamps = []
        self.timestamps = []
        self._relative_timestamps = []
        self.selected_segment_index = index
        if self.archive is None or not 0 <= index < self.segment_count:
            self.selected_segment_index = None
            raise ValueError("Recording not found in archive")
        segment = self.archive.manifest["segments"][index]
        # Manifest members are validated by PracticeArchive; do not follow external report links.
        for label, key in (("Overlay video", "overlay"), ("Raw video", "video")):
            if segment.get(key):
                path = self.archive.folder / segment[key]
                if not path.is_file():
                    raise ValueError(f"Missing archived {label.lower()}")
                self.variants[label] = path
        overlay_member = segment.get("overlay")
        lines_member = segment.get("lines")
        if lines_member == f"lines_{index:02d}.mp4":
            lines_path = self.archive.folder / lines_member
            if lines_path.is_file():
                self.variants["Full lines + messages"] = lines_path
        if overlay_member:
            lines_path = self.archive.folder / f"{Path(overlay_member).stem}_lines.mp4"
            if lines_path.is_file():
                self.variants["Full lines + messages"] = lines_path
        record = read_report(self.archive.report(index))
        self._report = record
        self.report = record.markdown
        self.warning = record.warning
        self._record_timestamps = record.timestamps

    def select_variant(self, label: str) -> VideoReader:
        self.close_reader()
        if label not in self.variants:
            raise ValueError(f"Video variant unavailable: {label}")
        self.reader = self._reader_factory(self.variants[label])
        self._selected_variant = label
        self.timestamps = self._record_timestamps
        if self.timestamps and len(self.timestamps) != self.reader.count:
            self.timestamps = []
            message = (
                "frame timestamps do not match the video frame count; "
                "using the video's FPS timeline"
            )
            self.warning = f"{self.warning}; {message}" if self.warning else message
        self._relative_timestamps = (
            [timestamp - self.timestamps[0] for timestamp in self.timestamps]
            if self.timestamps
            else []
        )
        return self.reader

    def timestamp_for_frame(self, index: int) -> float:
        reader = self.reader
        if reader is None:
            return 0.0
        if self._relative_timestamps:
            return self._relative_timestamps[index]
        return index / reader.fps

    def frame_for_time(self, seconds: float) -> int:
        reader = self.reader
        if reader is None:
            return 0
        if self._relative_timestamps:
            return min(max(0, bisect_right(self._relative_timestamps, seconds) - 1), reader.count - 1)
        return min(max(0, int(seconds * reader.fps)), reader.count - 1)

    def frame_for_timestamp(self, timestamp: float) -> int:
        reader = self.reader
        if reader is None:
            raise ValueError("Open a video stream before seeking to a timestamp")
        if not self.timestamps:
            raise ValueError("This recording has no compatible frame timestamps")
        return min(max(0, bisect_right(self.timestamps, timestamp) - 1), reader.count - 1)

    def duration(self) -> float:
        reader = self.reader
        if reader is None:
            return 0.0
        if self._relative_timestamps:
            return self.timestamp_for_frame(reader.count - 1) + 1 / reader.fps
        return reader.duration

    def frame_for_report_time(self, seconds: float) -> int:
        reader = self.reader
        if reader is None:
            raise ValueError("Open a video stream before seeking from the report")
        if self._report is None:
            raise ValueError("This video has no linked report timeline")
        if (
            self._record_timestamps
            and not self.timestamps
            and self._report is not None
            and not self._report.is_imported_reference
        ):
            raise ValueError(
                "Report timestamps do not match this video's frame count; seek is unavailable"
            )
        report_stream = "Overlay" if self._selected_variant == "Overlay video" else self._selected_variant
        video_seconds = self._report.recording_seconds(seconds, reader.fps, report_stream)
        return min(max(round(video_seconds * reader.fps), 0), reader.count - 1)

    def close_reader(self) -> None:
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.close()

    def close(self) -> None:
        try:
            self.close_reader()
        finally:
            archive, self.archive = self.archive, None
            if archive is not None:
                archive.close()
            self.variants = {}
            self.report = ""
            self.warning = ""
            self._report = None
            self._selected_variant = ""
            self._record_timestamps = []
            self.timestamps = []
            self._relative_timestamps = []
            self.selected_segment_index = None
