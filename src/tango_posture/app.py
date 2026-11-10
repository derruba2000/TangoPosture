"""Desktop controller. Headless and GUI use the same observation/session pipeline."""

import json
import platform
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import suppress
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

import cv2
import numpy as np

from .archive import (
    append_segment,
    practice_directory,
    practice_path,
)
from .beat import BeatTracker
from .calibration import calibrate, compatible, profile, save_calibration_file
from .capture import Capture
from .config import Config
from .display import PracticeDisplay
from .domain import Calibration, FrameObservation, MetricObservation, PoseEstimator
from .geometry import (
    AnkleStepTracker,
    AxisCurveTracker,
    EmbraceTracker,
    PoseSmoother,
    RoleTracker,
    axis_curve_alignment,
    calculate,
    detect_role,
    head_horizontal_alignment,
    knee_bend_alignment,
    pixel,
    shoulder_tension_alignment,
)
from .metrics import measure
from .models import verify
from .movement import render_floor_guide
from .overlay import MESSAGES, draw
from .pose.mediapipe_backend import MediaPipeEstimator
from .reports import write_markdown_report
from .session import Session
from .storage import Storage

GUIDANCE = (
    "Fixed, level camera; whole body in frame; adequate light; uncluttered background.\n"
    "Stand side-on; fitted clothing helps. Keep the selected anatomical side visible.\n"
    "Facing means direction in the ORIGINAL UNMIRRORED image; setup is unmirrored.\n"
    "C: calibrate (3s countdown + configured capture window); A: accept; R: record video; S: start scoring;\n"
    "P: pause/resume; E: end/save; V: toggle verbose skeleton; Y: toggle vertical axis; Q: quit/save; X: abort.\n"
    "Measurements are relative to your reference, not medical or technique judgements."
)


def runtime_versions(model_hash: str) -> dict:
    return {
        "python": platform.python_version(),
        "os": platform.platform(),
        "hardware": platform.machine(),
        "processor": platform.processor(),
        "model_hash": model_hash,
        **{
            p: version(p)
            for p in ("tango-posture", "mediapipe", "opencv-python", "numpy", "pydantic")
        },
    }


def _video_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    if not path.is_absolute() and path.parts[:1] != ("videos",):
        return Path("videos") / path
    return path


def _open_writer(path: Path, width: int, height: int, fps: float) -> cv2.VideoWriter:
    path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = "MJPG" if path.suffix.lower() == ".avi" else "mp4v"
    rate = fps if np.isfinite(fps) and fps > 0 else 15.0
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*fourcc), rate, (width, height))
    if not writer.isOpened():
        raise OSError(f"Cannot open video writer for {path}")
    return writer


class Application:
    def __init__(
        self,
        cfg: Config,
        storage: Storage,
        video: str | None = None,
        headless: bool = False,
        reference: Calibration | None = None,
        interval: tuple[float, float] | None = None,
        name: str = "My reference",
        review: dict | None = None,
        estimator: PoseEstimator | None = None,
        capture: Any = None,
        record_video: Path | None = None,
        record_overlay: Path | None = None,
        display: PracticeDisplay | None = None,
        practice_name: str | None = None,
        calibration_only: bool = False,
    ):
        self.cfg, self.storage, self.video, self.headless = cfg, storage, video, headless
        self.reference, self.interval, self.name, self.review = reference, interval, name, review
        self.estimator = estimator
        self.capture = capture
        self.display = display
        self.practice_name = practice_name
        self.calibration_only = calibration_only
        self.workspace_path = Path(mkdtemp(prefix="tango-recording-")) if display is not None else None
        self.practice_file = (
            practice_path(
                practice_directory(cfg.data_dir),
                practice_name
                or f"Practice_{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns() % 1000000:06d}",
            )
            if display is not None
            else None
        )
        self.aborted = False
        self.recording = False
        self.recording_finished = False
        self.recordings_started = 0
        self.stop_recording_requested = False
        self.record_video = None if display is not None else _video_path(record_video)
        self.record_overlay = None if display is not None else _video_path(record_overlay)
        self.session: Session | None = None
        self.state = "READY"
        self.last_timestamp = 0.0
        self.rows: list[dict] = []
        self.samples: list[MetricObservation] = []
        self.cal_head_angles: list[float] = []
        self.cal_shoulder_clearances: list[float] = []
        self.cal_axis_offsets: list[float] = []
        self.cal_start: float | None = None
        self.last_beep_sec: int | None = None
        self.candidate: Calibration | None = None
        self.saved_calibration_path: Path | None = None
        self.notice = "C: select reference; evaluation requires an accepted reference"
        self.latencies: list[float] = []
        self.inference_times: list[float] = []
        self.outputs: list[Path] = []
        self.model_hash = ""
        self.capture_profile: dict = {}
        self.versions: dict = {}
        self.setup_confirmed = headless
        self.verbose_skeleton = True
        self.show_vertical_axis = True
        self.worker: ThreadPoolExecutor | None = None
        self.window_open = False
        self.head_down_since: float | None = None
        self.head_smoothed: float | None = None
        self.head_last_ts: float | None = None
        self.shoulder_tension_since: float | None = None
        self.shoulder_smoothed: float | None = None
        self.shoulder_last_ts: float | None = None
        self.knee_straight_since: float | None = None
        self.knee_smoothed: float | None = None
        self.knee_last_ts: float | None = None
        self.pose_smoother = PoseSmoother(
            smoothing_sec=self.cfg.overlay_smoothing,
            max_gap=self.cfg.max_gap,
        )
        self.axis_tracker = AxisCurveTracker(
            entry=self.cfg.axis_curve_entry,
            clear=self.cfg.axis_curve_clear,
            entry_duration=self.cfg.axis_curve_duration,
            clear_duration=self.cfg.axis_curve_clear_duration,
            smoothing=self.cfg.smoothing,
            max_gap=self.cfg.max_gap,
        )
        self.relaxed_clearances: list[float] = []
        self.role_tracker = RoleTracker(
            assign_ms=self.cfg.role_assign_ms, hold_ms=self.cfg.role_hold_ms
        )
        self.embrace_tracker = EmbraceTracker(
            closed_threshold=self.cfg.embrace_closed_threshold,
            switch_sec=self.cfg.embrace_hysteresis_seconds,
        )
        self.ankle_tracker = AnkleStepTracker(
            high_lift_ratio=self.cfg.ankle_high_lift_ratio,
            max_consecutive_steps=self.cfg.max_consecutive_high_ankle_steps,
            slow_tango_max_bpm=self.cfg.slow_tango_max_bpm,
            max_gap=self.cfg.max_gap,
        )
        self.role = "NONE"
        self.embrace_type = "NONE"
        self.frame_log: list[dict[str, Any]] = []
        self.last_summary: dict[str, Any] | None = None
        self.report_written = False
        self.beat_tracker: BeatTracker | None = None

    def start_session(self, timestamp: float) -> None:
        if self.reference is None:
            self.notice = "Record and accept a reference first (C)"
            return
        self.session = Session(
            self.reference,
            self.cfg,
            self.storage,
            timestamp,
            "video" if self.video else "webcam",
            self.versions,
        )
        self.state = "RUNNING"
        self.notice = ""
        self.latencies.clear()
        self.inference_times.clear()

    def performance(self) -> dict:
        return {
            **self.versions,
            "resolution": [self.capture_profile.get("width"), self.capture_profile.get("height")],
            "processed_fps": 1 / float(np.mean(self.inference_times))
            if self.inference_times
            else None,
            "capture_to_overlay_ms_median": float(np.median(self.latencies))
            if self.latencies
            else None,
            "capture_to_overlay_ms_p95": float(np.percentile(self.latencies, 95))
            if self.latencies
            else None,
            "latency_scope": "decode-to-processing"
            if self.headless
            else "capture-to-overlay submission",
            "dropped_frames": self.capture.dropped,
            "timestamp_fallback": self.capture.fallback,
            "timestamp_fallback_offset": getattr(self.capture, "fallback_offset", None),
        }

    def finish(self, timestamp: float, status: str) -> None:
        if self.session and not self.session.finished:
            summary = self.session.finish(timestamp, status, self.performance())
            self.last_summary = summary
            output = self.storage.export(
                self.session.id, Path(self.cfg.data_dir) / "exports" / self.session.id
            )
            self.outputs.append(output)
            print(f"Session {self.session.id}: {status}; active {summary['active_duration']:.2f}s")
            for metric, data in summary["metrics"].items():
                percent = data["confirmed_percent_of_valid"]
                display = "N/A" if percent is None else f"{percent:.1f}%"
                print(
                    f"  {metric}: {data['event_count']} events; {display} of "
                    f"{data['valid_duration']:.2f}s valid; unknown {data['unknown_duration']:.2f}s"
                )
            print(f"Exports: {output}")
            self.notice = f"Saved {status}; active {summary['active_duration']:.1f}s. Exports printed in terminal."
            self.state = "FINISHED"
        if self.frame_log and not self.report_written:
            report_dir = (
                self.workspace_path
                if self.workspace_path is not None
                else Path("reports")
                if self.cfg.data_dir == "data"
                else Path(self.cfg.data_dir) / "reports"
            )
            events = (
                [asdict(e) for v in self.session.evaluators.values() for e in v.events]
                if self.session
                else None
            )
            report_path = write_markdown_report(
                report_dir=report_dir,
                cfg=self.cfg,
                source=f"video:{self.video}" if self.video else f"camera:{self.cfg.camera}",
                status=status,
                frame_log=self.frame_log,
                performance=self.performance(),
                reference=self.reference,
                session_id=self.session.id if self.session else None,
                session_summary=self.last_summary,
                session_events=events,
                recorded_video=self.record_video,
                recorded_overlay=self.record_overlay,
            )
            self.report_written = True
            if self.record_overlay is not None and self.frame_log:
                notes_path = self.record_overlay.with_suffix(".notes.jsonl")
                notes_path.write_text(
                    "".join(
                        json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n"
                        for row in self.frame_log
                    ),
                    encoding="utf-8",
                )
            if (
                self.display is not None
                and self.practice_file is not None
                and (
                    self.recording_finished
                    or (
                        self.session is not None
                        and self.session.finished
                        and self.recordings_started == 0
                    )
                )
            ):
                seg_idx = append_segment(
                    self.practice_file,
                    report_path,
                    self.record_video,
                    self.record_overlay,
                    [float(row["timestamp"]) for row in self.frame_log],
                )
                self.display.report_ready(self.practice_file)
                if (
                    not self.calibration_only
                    and self.practice_file.suffix.lower() == ".tp"
                    and self.record_video is not None
                    and hasattr(self.display, "segment_saved")
                ):
                    self.display.segment_saved(self.practice_file, seg_idx)
                print(f"Saved practice: {self.practice_file}")
            else:
                print(f"Saved report: {report_path} (latest: {report_dir / 'latest_report.md'})")

    def _audio_cues_enabled(self) -> bool:
        return (
            sys.platform == "darwin"
            and not self.headless
            and isinstance(self.estimator, MediaPipeEstimator)
        )

    def _speak(self, text: str) -> None:
        if not self._audio_cues_enabled():
            return
        with suppress(OSError):
            subprocess.Popen(
                ["/usr/bin/say", text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _beep(self) -> None:
        if not self._audio_cues_enabled():
            return
        with suppress(OSError):
            subprocess.Popen(
                ["/usr/bin/afplay", "/System/Library/Sounds/Ping.aiff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def accept(self) -> None:
        if self.candidate:
            self.storage.save_calibration(self.candidate)
            cal_dir = (
                Path("calibrations")
                if self.cfg.data_dir == "data"
                else Path(self.cfg.data_dir) / "calibrations"
            )
            self.saved_calibration_path = save_calibration_file(self.candidate, cal_dir)
            self.reference = self.candidate
            self.candidate = None
            print(
                f"Calibration completed & saved: {self.saved_calibration_path} "
                f"(id: {self.reference.id}, {self.reference.review['label']})"
            )
            self.notice = (
                f"Calibration completed! Saved to {self.saved_calibration_path.name}. "
                f"Press S to start session"
            )
            self.state = "READY"

    def key(self, key: int, timestamp: float) -> bool:
        if key == ord("x"):
            self.aborted = True
            return False
        if key == ord("q"):
            return False
        if key == ord("r") and self.display is not None and not self.calibration_only:
            self.start_recording()
            return True
        if key == ord("c") and self.display is not None and not self.calibration_only:
            return True
        if key in (ord("v"), ord("V")):
            self.setup_confirmed = True
            self.verbose_skeleton = not self.verbose_skeleton
            state_lbl = "ON (green lines & orange points)" if self.verbose_skeleton else "OFF (circles only)"
            self.notice = f"Verbose skeleton: {state_lbl}"
        if key in (ord("y"), ord("Y")):
            self.show_vertical_axis = not self.show_vertical_axis
            state_lbl = "ON" if self.show_vertical_axis else "OFF"
            self.notice = f"Vertical axis display: {state_lbl}"
        if key == ord("b") and self.beat_tracker:
            self.beat_tracker.tap(timestamp)
        if key in (ord("c"), ord("r")) and (
            self.calibration_only or self.display is None
        ) and self.state not in ("RUNNING", "PAUSED"):
            self.samples.clear()
            self.cal_head_angles.clear()
            self.cal_shoulder_clearances.clear()
            self.cal_axis_offsets.clear()
            self.candidate = None
            self.cal_start = timestamp + 3
            self.last_beep_sec = None
            self.state = "CALIBRATING"
            self.setup_confirmed = True
            self.notice = "Calibration starting: 3.0s to get into position"
            print(self.notice)
            self._speak("Calibration starting")
        if key == ord("a") and self.candidate:
            self.accept()
            if self.calibration_only:
                return False
        if key == ord("s") and self.state in ("READY", "FINISHED") and not self.candidate:
            self.setup_confirmed = True
            self.start_session(timestamp)
        if key == ord("p") and self.session:
            if self.state == "RUNNING":
                self.session.pause(timestamp)
                self.state = "PAUSED"
                self.rows = []
            elif self.state == "PAUSED":
                self.session.resume(timestamp)
                self.state = "RUNNING"
        if key == ord("e"):
            if self.recording:
                self.stop_recording_requested = True
            else:
                self.notice = "No recording is active; camera remains open"
            return True
        return True

    def start_recording(self) -> None:
        if self.display is None or self.calibration_only or self.recording:
            return
        if self.recordings_started >= self.cfg.max_recordings:
            self.notice = f"Recording limit reached ({self.cfg.max_recordings})"
            self.display.recording_states.put("limit")
            return
        assert self.workspace_path is not None
        index = self.recordings_started
        self.record_video = self.workspace_path / f"video_{index:02d}.mp4"
        self.record_overlay = self.workspace_path / f"overlay_{index:02d}.mp4"
        self.frame_log.clear()
        self.report_written = False
        self.recording_finished = False
        self.recording = True
        self.recordings_started += 1
        self.display.recording.set()
        self.display.recording_states.put("recording")
        self.notice = "Recording video; E saves, Q saves and quits"

    def poll_key(self, quit_only: bool = False) -> int:
        if self.display is not None:
            return self.display.poll_key(quit_only)
        return cv2.waitKey(1) & 0xFF

    def estimate_responsively(self, image: Any, timestamp: float):
        assert self.estimator is not None
        if self.worker is None:
            return self.estimator.estimate(image, timestamp)
        future = self.worker.submit(self.estimator.estimate, image, timestamp)
        while True:
            try:
                return future.result(timeout=0.02)
            except TimeoutError:
                # Only quit is processed during inference; other controls apply to completed frames.
                if self.poll_key(quit_only=True) in (ord("q"), ord("x")):
                    raise KeyboardInterrupt
                if (
                    self.display is None
                    and
                    self.window_open
                    and cv2.getWindowProperty("Tango Posture", cv2.WND_PROP_VISIBLE) < 1
                ):
                    raise KeyboardInterrupt

    def run(self) -> list[Path]:
        status = "completed"
        raw_writer: cv2.VideoWriter | None = None
        overlay_writer: cv2.VideoWriter | None = None
        overlay_lines_writer: cv2.VideoWriter | None = None
        if self.display is None:
            print(GUIDANCE)
        elif self.calibration_only:
            print("C: calibrate or retry; A: accept; Q: close the calibration camera.")
        else:
            print(
                "R: record; S: start scoring; P: pause/resume; E: stop/save; Q: quit. "
                "To calibrate, use References > Open calibration camera."
            )
        try:
            self.model_hash = verify(Path(self.cfg.model_path))
            self.versions = runtime_versions(self.model_hash)
            if self.estimator is None:
                self.estimator = MediaPipeEstimator(Path(self.cfg.model_path))
            if self.capture is None:
                self.capture = Capture(self.cfg, self.video)
            if self.cfg.beat_tracking and self.beat_tracker is None:
                audio_out = (
                    self.record_video.with_suffix(".wav")
                    if self.record_video is not None
                    else None
                )
                self.beat_tracker = BeatTracker(
                    self.cfg.tango_bpm,
                    enable_audio=not self.headless and self.video is None,
                    analysis_seconds=self.cfg.beat_analysis_seconds,
                    audio_save_path=audio_out,
                )
            if self.interval:
                self.cal_start = self.interval[0]
                self.state = "CALIBRATING"
            if not self.headless:
                self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pose-inference")
            initialized = False
            while True:
                tick = time.monotonic()
                captured = self.capture.read()
                if captured is None:
                    if self.video:
                        if not initialized:
                            raise ValueError("Video contains no decodable frames")
                        if self.cal_start is not None:
                            raise ValueError("Video ended before the reference interval completed")
                        break
                    if not self.headless and not self.key(
                        self.poll_key(), self.last_timestamp
                    ):
                        break
                    continue
                timestamp = captured.timestamp
                self.last_timestamp = timestamp
                self.capture.last = timestamp
                if (
                    self.display is not None
                    and self.display.recording.is_set()
                    and not self.recording
                ):
                    self.start_recording()
                frame = self.estimate_responsively(captured.image, timestamp)
                if self.record_video:
                    if raw_writer is None:
                        raw_writer = _open_writer(
                            self.record_video, frame.width, frame.height, self.capture.fps
                        )
                    raw_writer.write(captured.image)
                current_profile = profile(
                    self.cfg,
                    frame.width,
                    frame.height,
                    "video" if self.video else f"camera:{self.cfg.camera}",
                )
                if initialized and current_profile != self.capture_profile:
                    raise ValueError("Capture profile changed mid-run; recalibrate")
                self.capture_profile = current_profile
                if not initialized:
                    if self.reference:
                        compatible(self.reference, self.capture_profile, self.model_hash)
                    if self.headless and self.reference and not self.interval:
                        self.start_session(0.0 if self.video else timestamp)
                    initialized = True
                view_range = tuple(self.reference.quality["view_range"]) if self.reference else None
                observation = measure(
                    frame, self.cfg, view_range if self.cal_start is None else None
                )
                if self.cal_start is not None:
                    end = self.cal_start + self.cfg.calibration_duration
                    if self.cal_start <= timestamp <= end:
                        self.samples.append(observation)
                        sec_idx = int(timestamp - self.cal_start)
                        if sec_idx != self.last_beep_sec and timestamp < end:
                            self.last_beep_sec = sec_idx
                            self._beep()
                    if timestamp >= end:
                        try:
                            self.candidate = calibrate(
                                self.samples,
                                self.cal_start,
                                self.cfg,
                                self.capture_profile,
                                self.model_hash,
                                self.name,
                                self.review,
                            )
                            if self.cal_head_angles:
                                self.candidate.quality["head_neutral_offset"] = float(
                                    np.median(self.cal_head_angles)
                                )
                            if self.cal_shoulder_clearances:
                                self.candidate.quality["shoulder_neutral_clearance"] = float(
                                    np.median(self.cal_shoulder_clearances)
                                )
                            if self.cal_axis_offsets:
                                self.candidate.quality["axis_neutral_offset"] = float(
                                    np.median(self.cal_axis_offsets)
                                )
                            self.accept()
                            self._speak("Calibration completed")
                            if self.headless:
                                self.start_session(timestamp)
                        except ValueError as error:
                            if self.headless:
                                raise
                            err_msg = str(error)
                            self.notice = f"Calibration failed: {err_msg}; R: retry"
                            print(self.notice)
                            if "Framing inadequate" in err_msg:
                                spoken_reason = "Framing inadequate. Keep your full body from head to ankles inside the camera frame."
                            elif "Side-on view" in err_msg:
                                spoken_reason = "Side on view unreliable. Stand in profile to the camera."
                            elif "unstable" in err_msg:
                                spoken_reason = "Too much movement during calibration. Please hold still."
                            elif "No person" in err_msg:
                                spoken_reason = "No person detected in the frame."
                            else:
                                spoken_reason = "Not enough valid frames captured."
                            self._speak(f"Calibration failed. {spoken_reason}")
                        self.cal_start = None
                        self.last_beep_sec = None
                        self.state = "RUNNING" if self.headless else "READY"
                    else:
                        remaining = self.cal_start - timestamp
                        self.notice = (
                            f"Calibration starting: position yourself ({remaining:.1f}s)"
                            if remaining > 0
                            else f"Calibrating (beep): {end - timestamp:.1f}s remaining"
                        )
                elif self.state == "RUNNING" and self.session:
                    self.rows = self.session.update(observation)
                head_angle = None
                shoulder_elev_pct = None
                knee_bend_deg = None
                axis_dev_raw = None
                upper_body = None
                raw_role = "NONE"
                neutral_offset = (
                    float(self.reference.quality["head_neutral_offset"])
                    if self.reference and "head_neutral_offset" in self.reference.quality
                    else self.cfg.head_neutral_offset
                )
                sh_neutral_base = (
                    float(self.reference.quality["shoulder_neutral_clearance"])
                    if self.reference and "shoulder_neutral_clearance" in self.reference.quality
                    else self.cfg.shoulder_neutral_clearance
                )
                sh_neutral = (
                    max(sh_neutral_base, float(np.percentile(self.relaxed_clearances, 75)))
                    if len(self.relaxed_clearances) >= 15
                    else sh_neutral_base
                )
                axis_neutral = (
                    float(self.reference.quality["axis_neutral_offset"])
                    if self.reference and "axis_neutral_offset" in self.reference.quality
                    else self.cfg.axis_neutral_offset
                )
                if len(frame.poses) == 1:
                    pose = frame.poses[0]
                    raw_role = detect_role(pose)
                    metric_tau = max(1e-4, self.cfg.overlay_smoothing or self.cfg.smoothing)
                    knee_info = knee_bend_alignment(
                        pose, frame.width, frame.height, side=self.cfg.side
                    )
                    if knee_info is not None:
                        raw_knee_deg = knee_info[0]
                        if (
                            self.knee_smoothed is None
                            or self.knee_last_ts is None
                            or (timestamp - self.knee_last_ts) > self.cfg.max_gap
                        ):
                            self.knee_smoothed = raw_knee_deg
                        else:
                            dt_kn = max(0.0, timestamp - self.knee_last_ts)
                            alpha_kn = 1.0 - np.exp(-dt_kn / metric_tau)
                            self.knee_smoothed += float(alpha_kn) * (
                                raw_knee_deg - self.knee_smoothed
                            )
                        self.knee_last_ts = timestamp
                        knee_bend_deg = self.knee_smoothed
                    axis_info = axis_curve_alignment(
                        pose,
                        frame.width,
                        frame.height,
                        facing=self.cfg.facing,
                        side=self.cfg.side,
                        neutral_offset=axis_neutral,
                    )
                    if axis_info is not None:
                        axis_dev_raw = axis_info[0]
                        raw_axis_offset = axis_info[1]
                        if (
                            self.state == "CALIBRATING"
                            and self.cal_start is not None
                            and timestamp >= self.cal_start
                        ):
                            self.cal_axis_offsets.append(raw_axis_offset)
                    raw_head_info = head_horizontal_alignment(
                        pose, frame.width, frame.height, neutral_offset=0.0
                    )
                    if (
                        self.state == "CALIBRATING"
                        and self.cal_start is not None
                        and timestamp >= self.cal_start
                        and raw_head_info is not None
                    ):
                        self.cal_head_angles.append(raw_head_info[0])
                    head_info = head_horizontal_alignment(
                        pose, frame.width, frame.height, neutral_offset=neutral_offset
                    )
                    if head_info is not None:
                        raw_head_deg = head_info[0]
                        if (
                            self.cfg.overlay_smoothing <= 0
                            or self.head_smoothed is None
                            or self.head_last_ts is None
                            or (timestamp - self.head_last_ts) > self.cfg.max_gap
                        ):
                            self.head_smoothed = raw_head_deg
                        else:
                            dt_hd = max(0.0, timestamp - self.head_last_ts)
                            alpha_hd = 1.0 - np.exp(-dt_hd / metric_tau)
                            self.head_smoothed += float(alpha_hd) * (
                                raw_head_deg - self.head_smoothed
                            )
                        self.head_last_ts = timestamp
                        head_angle = self.head_smoothed
                    sh_info = shoulder_tension_alignment(
                        pose, frame.width, frame.height, neutral_clearance=sh_neutral
                    )
                    if sh_info is not None:
                        raw_elev_pct = sh_info[0]
                        raw_clear = sh_info[1]
                        if (
                            self.state == "CALIBRATING"
                            and self.cal_start is not None
                            and timestamp >= self.cal_start
                        ):
                            self.cal_shoulder_clearances.append(raw_clear)
                        elif len(self.relaxed_clearances) < 600 and 0.65 <= raw_clear <= 1.50:
                            self.relaxed_clearances.append(raw_clear)
                        if (
                            self.shoulder_smoothed is None
                            or self.shoulder_last_ts is None
                            or (timestamp - self.shoulder_last_ts) > self.cfg.max_gap
                        ):
                            self.shoulder_smoothed = raw_elev_pct
                        else:
                            dt_sh = max(0.0, timestamp - self.shoulder_last_ts)
                            alpha_sh = 1.0 - np.exp(-dt_sh / metric_tau)
                            self.shoulder_smoothed += float(alpha_sh) * (
                                raw_elev_pct - self.shoulder_smoothed
                            )
                        self.shoulder_last_ts = timestamp
                        shoulder_elev_pct = self.shoulder_smoothed
                    if all(observation.values.get(m) is not None for m in observation.values):
                        upper_body = {
                            m: float(v)
                            for m, v in observation.values.items()
                            if v is not None
                        }
                    else:
                        for side_cand in (self.cfg.side, "left", "right"):
                            ear_lm = pose.get(f"{side_cand}_ear")
                            sh_lm = pose.get(f"{side_cand}_shoulder")
                            hip_lm = pose.get(f"{side_cand}_hip")
                            if all(
                                lm is not None
                                and 0 <= lm.x <= 1
                                and 0 <= lm.y <= 1
                                and (lm.visibility is None or lm.visibility >= 0.5)
                                for lm in (ear_lm, sh_lm, hip_lm)
                            ):
                                assert (
                                    ear_lm is not None and sh_lm is not None and hip_lm is not None
                                )
                                try:
                                    upper_body = calculate(
                                        pixel(ear_lm, frame.width, frame.height),
                                        pixel(sh_lm, frame.width, frame.height),
                                        pixel(hip_lm, frame.width, frame.height),
                                        self.cfg.facing,
                                    )
                                    break
                                except ValueError:
                                    upper_body = None
                self.role = self.role_tracker.update(raw_role, timestamp)
                self.embrace_type = self.embrace_tracker.update(
                    frame.poses[0] if len(frame.poses) == 1 else None,
                    self.role,
                    timestamp,
                )
                axis_track = self.axis_tracker.update(axis_dev_raw, timestamp)
                axis_curve_smoothed = (
                    float(axis_track["smoothed"])
                    if axis_track["smoothed"] is not None
                    else None
                )
                axis_curve_state = str(axis_track["state"])
                axis_curve_elapsed = float(axis_track["elapsed"] or 0.0)
                bad_axis = bool(axis_track["alert"])

                quality_reason = next(
                    (q.reason for q in observation.quality.values() if not q.valid), ""
                )
                beat_state = self.beat_tracker.state_at(timestamp) if self.beat_tracker else None
                has_counted_beat = bool(beat_state and beat_state["beat"] > 0)
                active_bpm = (
                    float(beat_state["bpm"])
                    if beat_state and beat_state.get("bpm") is not None
                    else self.cfg.tango_bpm
                )
                ankle_track = self.ankle_tracker.update(
                    frame.poses[0] if len(frame.poses) == 1 else None,
                    timestamp,
                    bpm=active_bpm,
                    width=frame.width,
                    height=frame.height,
                )
                ankle_lift_ratio = (
                    float(ankle_track["smoothed_ratio"])
                    if ankle_track["smoothed_ratio"] is not None
                    else None
                )
                consecutive_high_ankle_steps = int(ankle_track["consecutive_high_steps"] or 0)
                bad_ankles = bool(ankle_track["alert"])
                self.frame_log.append(
                    {
                        "timestamp": timestamp,
                        "head_angle": head_angle,
                        "shoulder_elev_pct": shoulder_elev_pct,
                        "knee_bend_deg": knee_bend_deg,
                        "ankle_lift_ratio": ankle_lift_ratio,
                        "consecutive_high_ankle_steps": consecutive_high_ankle_steps,
                        "ankle_high_alert": bad_ankles,
                        "axis_curve_ratio": axis_curve_smoothed,
                        "axis_curve_state": axis_curve_state,
                        "axis_curve_alert": bad_axis,
                        "role": self.role,
                        "embrace_type": self.embrace_type,
                        "upper_body": upper_body,
                        "quality_reason": quality_reason,
                        "beat": beat_state["beat"] if has_counted_beat and beat_state else None,
                        "phrase": beat_state["phrase"] if has_counted_beat and beat_state else None,
                        "bpm": beat_state["bpm"] if beat_state else None,
                    }
                )
                if head_angle is not None and head_angle < self.cfg.head_down_angle:
                    if self.head_down_since is None:
                        self.head_down_since = timestamp
                    head_down_elapsed = timestamp - self.head_down_since
                else:
                    self.head_down_since = None
                    head_down_elapsed = 0.0
                bad_head = head_down_elapsed >= self.cfg.head_down_duration

                sh_clear_threshold = max(4.0, self.cfg.shoulder_raise_threshold * 0.65)
                if shoulder_elev_pct is not None:
                    if shoulder_elev_pct >= self.cfg.shoulder_raise_threshold:
                        if self.shoulder_tension_since is None:
                            self.shoulder_tension_since = timestamp
                        shoulder_tension_elapsed = timestamp - self.shoulder_tension_since
                    elif (
                        self.shoulder_tension_since is not None
                        and shoulder_elev_pct >= sh_clear_threshold
                    ):
                        shoulder_tension_elapsed = timestamp - self.shoulder_tension_since
                    else:
                        self.shoulder_tension_since = None
                        shoulder_tension_elapsed = 0.0
                else:
                    if (
                        self.shoulder_last_ts is None
                        or (timestamp - self.shoulder_last_ts) > self.cfg.max_gap
                    ):
                        self.shoulder_tension_since = None
                        shoulder_tension_elapsed = 0.0
                    elif self.shoulder_tension_since is not None:
                        shoulder_tension_elapsed = timestamp - self.shoulder_tension_since
                    else:
                        shoulder_tension_elapsed = 0.0
                bad_shoulders = shoulder_tension_elapsed >= self.cfg.shoulder_tension_duration

                knee_clear_deg = self.cfg.closed_embrace_min_knee_bend + 2.5
                if knee_bend_deg is not None:
                    if knee_bend_deg < self.cfg.closed_embrace_min_knee_bend:
                        if self.knee_straight_since is None:
                            self.knee_straight_since = timestamp
                        knee_straight_elapsed = timestamp - self.knee_straight_since
                    elif (
                        self.knee_straight_since is not None
                        and knee_bend_deg < knee_clear_deg
                    ):
                        knee_straight_elapsed = timestamp - self.knee_straight_since
                    else:
                        self.knee_straight_since = None
                        knee_straight_elapsed = 0.0
                else:
                    if (
                        self.knee_last_ts is None
                        or (timestamp - self.knee_last_ts) > self.cfg.max_gap
                    ):
                        self.knee_straight_since = None
                        knee_straight_elapsed = 0.0
                    elif self.knee_straight_since is not None:
                        knee_straight_elapsed = timestamp - self.knee_straight_since
                    else:
                        knee_straight_elapsed = 0.0
                bad_knees = knee_straight_elapsed >= self.cfg.knee_straight_duration
                if self.frame_log:
                    self.frame_log[-1]["alerts"] = {
                        "head": bool(bad_head),
                        "shoulders": bool(bad_shoulders),
                        "knees": bool(bad_knees),
                        "axis": bool(bad_axis),
                    }
                    self.frame_log[-1]["clip_timestamp"] = float(
                        timestamp - self.frame_log[0]["timestamp"]
                    )

                sh_disp_pct = (
                    shoulder_elev_pct
                    if shoulder_elev_pct is not None
                    else (
                        self.shoulder_smoothed
                        if self.shoulder_smoothed is not None
                        else self.cfg.shoulder_raise_threshold
                    )
                )
                head_disp_deg = head_angle if head_angle is not None else self.cfg.head_down_angle
                knee_disp_deg = (
                    knee_bend_deg
                    if knee_bend_deg is not None
                    else self.cfg.closed_embrace_min_knee_bend
                )
                message = self.notice
                if bad_ankles:
                    lift_pct = (ankle_lift_ratio or self.cfg.ankle_high_lift_ratio) * 100.0
                    message = (
                        f"KEEP FEET CLOSE TO FLOOR (SLOW TANGO <={self.cfg.slow_tango_max_bpm:.0f} BPM): "
                        f"ankle raised too high ({lift_pct:.1f}%) for {consecutive_high_ankle_steps} "
                        f"consecutive steps (>={self.cfg.max_consecutive_high_ankle_steps})"
                    )
                elif bad_axis and axis_curve_smoothed is not None:
                    message = (
                        f"HIP OFFSET: hip/pelvis +{axis_curve_smoothed * 100.0:.1f}% "
                        f"past ankle-to-head line ({axis_curve_elapsed:.1f}s)"
                    )
                elif bad_knees:
                    message = (
                        f"BEND YOUR KNEES: knee bend {knee_disp_deg:.1f} deg "
                        f"< {self.cfg.closed_embrace_min_knee_bend:.1f} deg ({knee_straight_elapsed:.1f}s)"
                    )
                elif bad_head and bad_shoulders:
                    message = (
                        f"BAD HEAD ALIGNMENT ({head_disp_deg:+.1f} deg) & "
                        f"LOWER YOUR SHOULDERS ({sh_disp_pct:+.0f}% for {shoulder_tension_elapsed:.1f}s)"
                    )
                elif bad_shoulders:
                    message = (
                        f"LOWER YOUR SHOULDERS: shoulder tension detected "
                        f"({sh_disp_pct:+.0f}% raised for {shoulder_tension_elapsed:.1f}s)"
                    )
                elif bad_head:
                    message = (
                        f"BAD HEAD ALIGNMENT: head pointing down ({head_disp_deg:+.1f} deg for "
                        f"{head_down_elapsed:.1f}s)"
                    )
                elif self.state == "RUNNING":
                    message = next(
                        (q.reason for q in observation.quality.values() if not q.valid), ""
                    )
                    if not message:
                        message = next(
                            (MESSAGES[r["metric"]] for r in self.rows if r["state"] == "DEVIATION"),
                            "Within reference / pending: experimental thresholds",
                        )
                if not self.headless or self.record_overlay:
                    active = 0.0
                    if self.session:
                        active = sum(b - a for a, b in self.session.spans)
                        if self.session.start is not None and not self.session.finished:
                            active += timestamp - self.session.start
                    fps = (
                        1 / float(np.mean(self.inference_times[-30:]))
                        if self.inference_times
                        else 0
                    )
                    head_str = "N/A" if head_angle is None else f"{head_angle:+.1f} deg"
                    sh_str = (
                        "N/A"
                        if shoulder_elev_pct is None
                        else (
                            f"HIGH {shoulder_elev_pct:+.0f}%"
                            if shoulder_elev_pct >= self.cfg.shoulder_raise_threshold
                            else f"OK ({shoulder_elev_pct:+.0f}%)"
                        )
                    )
                    if beat_state is None:
                        beat_str = "disabled"
                    elif not beat_state["playing"]:
                        beat_str = "Waiting for song to begin..."
                    elif beat_state["analyzing"]:
                        beat_str = (
                            f"Understanding beats ({beat_state['analyzed_seconds']:.0f}/"
                            f"{beat_state['analysis_target_seconds']:.0f}s) | "
                            f"Phrase {beat_state['phrase']} Beat {beat_state['beat']}/8 "
                            f"({beat_state['bpm']:.0f} BPM)"
                        )
                    else:
                        beat_str = (
                            f"Phrase {beat_state['phrase']} Beat {beat_state['beat']}/8 "
                            f"({beat_state['bpm']:.0f} BPM, {beat_state['source']})"
                        )
                    role_disp = (
                        f"{self.role} ({self.embrace_type})"
                        if self.role in ("LEADER", "FOLLOWER")
                        and self.embrace_type in ("CLOSED", "OPEN")
                        else self.role
                    )
                    knee_str = (
                        "N/A"
                        if knee_bend_deg is None
                        else (
                            f"STRAIGHT {knee_bend_deg:.1f} deg"
                            if bad_knees
                            or knee_bend_deg < self.cfg.closed_embrace_min_knee_bend
                            else f"OK ({knee_bend_deg:.1f} deg)"
                        )
                    )
                    axis_str = (
                        "N/A"
                        if axis_curve_smoothed is None
                        else (
                            f"HIGH {axis_curve_smoothed * 100.0:+.1f}% [DEVIATION]"
                            if bad_axis
                            else f"PENDING {axis_curve_smoothed * 100.0:+.1f}%"
                            if axis_curve_state == "PENDING"
                            else f"OK ({axis_curve_smoothed * 100.0:+.1f}%)"
                        )
                    )
                    feet_str = (
                        "N/A"
                        if ankle_lift_ratio is None
                        else (
                            f"HIGH {ankle_lift_ratio * 100.0:.1f}% ({consecutive_high_ankle_steps}/{self.cfg.max_consecutive_high_ankle_steps} steps)"
                            if bad_ankles or bool(ankle_track["high_now"])
                            else f"OK ({ankle_lift_ratio * 100.0:.1f}%, {consecutive_high_ankle_steps}/{self.cfg.max_consecutive_high_ankle_steps})"
                        )
                    )
                    lines = [
                        (
                            f"{self.state} | active {active:.1f}s | processed {fps:.1f} FPS | "
                            f"side {self.cfg.side}, facing {self.cfg.facing}"
                        ),
                        (
                            f"Role: {role_disp} | Beat: {beat_str} | "
                            f"Head: {head_str} | Shoulders: {sh_str} | Knees: {knee_str} | Feet: {feet_str} | Hip offset: {axis_str}"
                        ),
                        (
                            f"Reference: {self.reference.name if self.reference else 'NONE'} "
                            f"({self.reference.review['label'] if self.reference else 'not calibrated'})"
                        ),
                        (
                            "C calibrate/retry | A accept | Q close"
                            if self.calibration_only
                            else "R record | S score | P pause | E stop/save | V skeleton | Y torso axis | B beat-1 | Q quit"
                            if self.display is not None
                            else "C calibrate | A accept | R retry | S start | P pause | E end | V verbose | Y torso/vertical | B beat-1 | Q quit"
                        ),
                    ]
                    if not self.setup_confirmed:
                        lines.append(
                            "SETUP UNMIRRORED: check facing direction; V confirms / enables display mirror"
                        )
                    for r in self.rows:
                        val = "N/A" if r["smoothed"] is None else f"{r['smoothed']:.3f}"
                        delta = "N/A" if r["delta"] is None else f"{r['delta']:+.3f}"
                        lines.append(f"{r['metric']}: {val} | delta {delta} | {r['state']}")
                    if self.state != "RUNNING" and not self.candidate:
                        reason = next(
                            (q.reason for q in observation.quality.values() if not q.valid),
                            "Image quality: VALID (side-on heuristic)",
                        )
                        lines.append(reason)
                    if self.display is not None:
                        self.display.notes = "\n\n".join([message, *lines])
                    display_frame = FrameObservation(
                        frame.timestamp,
                        frame.width,
                        frame.height,
                        self.pose_smoother.update(frame.poses, timestamp),
                    )
                    panel = draw(
                        captured.image,
                        display_frame,
                        self.cfg.mirror and self.setup_confirmed,
                        lines,
                        message,
                        self.cfg.confidence,
                        head_down_angle=self.cfg.head_down_angle,
                        head_down_duration=self.cfg.head_down_duration,
                        head_down_elapsed=head_down_elapsed,
                        head_angle_smoothed=head_angle,
                        role=self.role,
                        beat_state=beat_state,
                        head_neutral_offset=neutral_offset,
                        shoulder_raise_threshold=self.cfg.shoulder_raise_threshold,
                        shoulder_tension_duration=self.cfg.shoulder_tension_duration,
                        shoulder_tension_elapsed=shoulder_tension_elapsed,
                        shoulder_neutral_clearance=sh_neutral,
                        shoulder_elev_smoothed=shoulder_elev_pct,
                        embrace_type=self.embrace_type,
                        embrace_closed_threshold=self.cfg.embrace_closed_threshold,
                        closed_embrace_min_knee_bend=self.cfg.closed_embrace_min_knee_bend,
                        knee_straight_duration=self.cfg.knee_straight_duration,
                        knee_straight_elapsed=knee_straight_elapsed,
                        knee_bend_smoothed=knee_bend_deg,
                        facing=self.cfg.facing,
                        side=self.cfg.side,
                        axis_neutral_offset=axis_neutral,
                        axis_curve_entry=self.cfg.axis_curve_entry,
                        axis_curve_clear=self.cfg.axis_curve_clear,
                        axis_curve_duration=self.cfg.axis_curve_duration,
                        axis_curve_elapsed=axis_curve_elapsed,
                        axis_curve_smoothed=axis_curve_smoothed,
                        axis_curve_state=axis_curve_state,
                        ankle_high_lift_ratio=self.cfg.ankle_high_lift_ratio,
                        ankle_lift_smoothed=ankle_lift_ratio,
                        consecutive_high_ankle_steps=consecutive_high_ankle_steps,
                        max_consecutive_high_ankle_steps=self.cfg.max_consecutive_high_ankle_steps,
                        slow_tango_max_bpm=self.cfg.slow_tango_max_bpm,
                        ankle_high_alert=bad_ankles,
                        verbose_skeleton=self.verbose_skeleton,
                        show_vertical_axis=self.show_vertical_axis,
                        show_footer=self.display is None,
                    )
                    if self.record_overlay:
                        if overlay_writer is None:
                            overlay_writer = _open_writer(
                                self.record_overlay,
                                panel.shape[1],
                                panel.shape[0],
                                self.capture.fps,
                            )
                        overlay_writer.write(panel)
                        if self.display is not None and self.recording:
                            lines_panel = draw(
                                render_floor_guide(captured.image),
                                display_frame,
                                self.cfg.mirror and self.setup_confirmed,
                                [],
                                "",
                                self.cfg.confidence,
                                verbose_skeleton=True,
                                show_vertical_axis=True,
                                show_footer=False,
                            )
                            if overlay_lines_writer is None:
                                lines_path = self.record_overlay.with_name(
                                    self.record_overlay.stem + "_lines.mp4"
                                )
                                overlay_lines_writer = _open_writer(
                                    lines_path,
                                    lines_panel.shape[1],
                                    lines_panel.shape[0],
                                    self.capture.fps,
                                )
                            overlay_lines_writer.write(lines_panel)
                    if not self.headless:
                        if self.display is not None:
                            self.display.show(panel)
                        else:
                            cv2.imshow("Tango Posture", panel)
                        self.window_open = True
                        if not self.key(self.poll_key(), timestamp):
                            break
                        if (
                            self.display is None
                            and cv2.getWindowProperty("Tango Posture", cv2.WND_PROP_VISIBLE) < 1
                        ):
                            status = "stopped"
                            break
                if self.stop_recording_requested:
                    self.stop_recording_requested = False
                    for writer in (raw_writer, overlay_writer, overlay_lines_writer):
                        if writer is not None:
                            writer.release()
                    raw_writer = overlay_writer = overlay_lines_writer = None
                    self.recording = False
                    self.recording_finished = True
                    if self.display is not None:
                        self.display.recording.clear()
                        self.display.recording_states.put("ready")
                    self.finish(timestamp, "completed")
                    self.record_video = self.record_overlay = None
                    self.recording_finished = False
                    self.frame_log.clear()
                self.latencies.append((time.monotonic() - captured.captured_at) * 1000)
                self.inference_times.append(time.monotonic() - tick)
        except KeyboardInterrupt:
            status = "stopped"
        except Exception:
            status = "interrupted"
            raise
        finally:
            try:
                end_time = self.last_timestamp
                if self.capture and not self.video:
                    end_time = max(end_time, time.monotonic() - self.capture.origin)
                elif self.capture and self.video and status == "completed" and self.capture.fps > 0:
                    end_time += 1 / self.capture.fps
                if raw_writer:
                    raw_writer.release()
                    print(f"Saved raw video: {self.record_video}")
                    raw_writer = None
                if overlay_writer:
                    overlay_writer.release()
                    print(f"Saved overlay video: {self.record_overlay}")
                    overlay_writer = None
                if overlay_lines_writer:
                    overlay_lines_writer.release()
                    overlay_lines_writer = None
                if self.beat_tracker:
                    self.beat_tracker.close()
                    if not self.aborted:
                        recorded_vids = [
                            p for p in (self.record_video, self.record_overlay) if p is not None
                        ]
                        muxed = self.beat_tracker.mux_into_videos(recorded_vids)
                        for mp in muxed:
                            print(f"Embedded microphone audio into: {mp}")
                    self.beat_tracker = None
                if self.aborted:
                    if self.session and not self.session.finished:
                        self.storage.discard_running_session(self.session.id)
                        self.session.buffer.clear()
                else:
                    self.recording_finished = self.recording_finished or self.recording
                    self.recording = False
                    if self.display is not None:
                        self.display.recording.clear()
                    self.finish(end_time, status)
            finally:
                try:
                    if self.beat_tracker:
                        self.beat_tracker.close()
                    if self.capture:
                        self.capture.close()
                finally:
                    try:
                        if self.worker:
                            self.worker.shutdown(wait=True, cancel_futures=True)
                        if self.estimator:
                            self.estimator.close()
                    finally:
                        if self.display is not None:
                            self.display.close()
                        elif not self.headless:
                            cv2.destroyAllWindows()
                        if self.workspace_path is not None and (self.aborted or self.report_written):
                            shutil.rmtree(self.workspace_path, ignore_errors=True)
        return self.outputs
