"""Desktop controller. Headless and GUI use the same observation/session pipeline."""

import platform
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import suppress
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .beat import BeatTracker
from .calibration import calibrate, compatible, profile, save_calibration_file
from .capture import Capture
from .config import Config
from .domain import Calibration, MetricObservation, PoseEstimator
from .geometry import (
    RoleTracker,
    calculate,
    detect_role,
    head_horizontal_alignment,
    pixel,
    shoulder_tension_alignment,
)
from .metrics import measure
from .models import verify
from .overlay import MESSAGES, draw
from .pose.mediapipe_backend import MediaPipeEstimator
from .reports import write_markdown_report
from .session import Session
from .storage import Storage

GUIDANCE = (
    "Fixed, level camera; whole body in frame; adequate light; uncluttered background.\n"
    "Stand side-on; fitted clothing helps. Keep the selected anatomical side visible.\n"
    "Facing means direction in the ORIGINAL UNMIRRORED image; setup is unmirrored.\n"
    "C: calibrate (3s countdown + 12s capture); A: accept; R: retry; S: start;\n"
    "P: pause/resume; E: end/save; Q: quit/save partial.\n"
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
    ):
        self.cfg, self.storage, self.video, self.headless = cfg, storage, video, headless
        self.reference, self.interval, self.name, self.review = reference, interval, name, review
        self.estimator = estimator
        self.capture = capture
        self.record_video = _video_path(record_video)
        self.record_overlay = _video_path(record_overlay)
        self.session: Session | None = None
        self.state = "READY"
        self.last_timestamp = 0.0
        self.rows: list[dict] = []
        self.samples: list[MetricObservation] = []
        self.cal_head_angles: list[float] = []
        self.cal_shoulder_clearances: list[float] = []
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
        self.worker: ThreadPoolExecutor | None = None
        self.window_open = False
        self.head_down_since: float | None = None
        self.shoulder_tension_since: float | None = None
        self.relaxed_clearances: list[float] = []
        self.role_tracker = RoleTracker(
            assign_ms=self.cfg.role_assign_ms, hold_ms=self.cfg.role_hold_ms
        )
        self.role = "NONE"
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
                Path("reports")
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
        if key == ord("q"):
            self.finish(timestamp, "stopped")
            return False
        if key == ord("v"):
            self.setup_confirmed = True
        if key == ord("b") and self.beat_tracker:
            self.beat_tracker.tap(timestamp)
        if key in (ord("c"), ord("r")) and self.state not in ("RUNNING", "PAUSED"):
            self.samples.clear()
            self.cal_head_angles.clear()
            self.cal_shoulder_clearances.clear()
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
        if key == ord("e") and self.state in ("RUNNING", "PAUSED"):
            self.finish(timestamp, "completed")
        return True

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
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    raise KeyboardInterrupt
                if (
                    self.window_open
                    and cv2.getWindowProperty("Tango Posture", cv2.WND_PROP_VISIBLE) < 1
                ):
                    raise KeyboardInterrupt

    def run(self) -> list[Path]:
        status = "completed"
        raw_writer: cv2.VideoWriter | None = None
        overlay_writer: cv2.VideoWriter | None = None
        print(GUIDANCE)
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
                        cv2.waitKey(1) & 0xFF, self.last_timestamp
                    ):
                        break
                    continue
                timestamp = captured.timestamp
                self.last_timestamp = timestamp
                self.capture.last = timestamp
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
                upper_body = None
                raw_role = "NONE"
                neutral_offset = (
                    float(self.reference.quality["head_neutral_offset"])
                    if self.reference and "head_neutral_offset" in self.reference.quality
                    else self.cfg.head_neutral_offset
                )
                sh_neutral = (
                    float(self.reference.quality["shoulder_neutral_clearance"])
                    if self.reference and "shoulder_neutral_clearance" in self.reference.quality
                    else self.cfg.shoulder_neutral_clearance
                )
                if len(frame.poses) == 1:
                    pose = frame.poses[0]
                    raw_role = detect_role(pose)
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
                        head_angle = head_info[0]
                    sh_info = shoulder_tension_alignment(
                        pose, frame.width, frame.height, neutral_clearance=sh_neutral
                    )
                    if sh_info is not None:
                        shoulder_elev_pct = sh_info[0]
                        raw_clear = sh_info[1]
                        if (
                            self.state == "CALIBRATING"
                            and self.cal_start is not None
                            and timestamp >= self.cal_start
                        ):
                            self.cal_shoulder_clearances.append(raw_clear)
                        elif len(self.relaxed_clearances) < 600 and 0.45 <= raw_clear <= 1.60:
                            self.relaxed_clearances.append(raw_clear)
                    ear_lm = pose.get(f"{self.cfg.side}_ear")
                    sh_lm = pose.get(f"{self.cfg.side}_shoulder")
                    hip_lm = pose.get(f"{self.cfg.side}_hip")
                    if all(
                        lm is not None
                        and 0 <= lm.x <= 1
                        and 0 <= lm.y <= 1
                        and (lm.visibility is None or lm.visibility >= 0.5)
                        for lm in (ear_lm, sh_lm, hip_lm)
                    ):
                        assert ear_lm is not None and sh_lm is not None and hip_lm is not None
                        try:
                            upper_body = calculate(
                                pixel(ear_lm, frame.width, frame.height),
                                pixel(sh_lm, frame.width, frame.height),
                                pixel(hip_lm, frame.width, frame.height),
                                self.cfg.facing,
                            )
                        except ValueError:
                            upper_body = None
                self.role = self.role_tracker.update(raw_role, timestamp)
                quality_reason = next(
                    (q.reason for q in observation.quality.values() if not q.valid), ""
                )
                beat_state = self.beat_tracker.state_at(timestamp) if self.beat_tracker else None
                has_counted_beat = bool(beat_state and beat_state["beat"] > 0)
                self.frame_log.append(
                    {
                        "timestamp": timestamp,
                        "head_angle": head_angle,
                        "shoulder_elev_pct": shoulder_elev_pct,
                        "role": self.role,
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

                if (
                    shoulder_elev_pct is not None
                    and shoulder_elev_pct >= self.cfg.shoulder_raise_threshold
                ):
                    if self.shoulder_tension_since is None:
                        self.shoulder_tension_since = timestamp
                    shoulder_tension_elapsed = timestamp - self.shoulder_tension_since
                else:
                    self.shoulder_tension_since = None
                    shoulder_tension_elapsed = 0.0
                bad_shoulders = shoulder_tension_elapsed >= self.cfg.shoulder_tension_duration

                message = self.notice
                if bad_head and bad_shoulders:
                    message = (
                        f"BAD HEAD ALIGNMENT ({head_angle:+.1f} deg) & "
                        f"LOWER YOUR SHOULDERS ({shoulder_elev_pct:+.0f}% for {shoulder_tension_elapsed:.1f}s)"
                    )
                elif bad_shoulders:
                    message = (
                        f"LOWER YOUR SHOULDERS: shoulder tension detected "
                        f"({shoulder_elev_pct:+.0f}% raised for {shoulder_tension_elapsed:.1f}s)"
                    )
                elif bad_head:
                    message = (
                        f"BAD HEAD ALIGNMENT: head pointing down ({head_angle:+.1f} deg for "
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
                    lines = [
                        (
                            f"{self.state} | active {active:.1f}s | processed {fps:.1f} FPS | "
                            f"side {self.cfg.side}, facing {self.cfg.facing}"
                        ),
                        (
                            f"Role: {self.role} | Beat: {beat_str} | "
                            f"Head: {head_str} | Shoulders: {sh_str}"
                        ),
                        (
                            f"Reference: {self.reference.name if self.reference else 'NONE'} "
                            f"({self.reference.review['label'] if self.reference else 'not calibrated'})"
                        ),
                        "C calibrate | A accept | R retry | S start | P pause | E end | B beat-1 | Q quit",
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
                    panel = draw(
                        captured.image,
                        frame,
                        self.cfg.mirror and self.setup_confirmed,
                        lines,
                        message,
                        self.cfg.confidence,
                        head_down_angle=self.cfg.head_down_angle,
                        head_down_duration=self.cfg.head_down_duration,
                        head_down_elapsed=head_down_elapsed,
                        role=self.role,
                        beat_state=beat_state,
                        head_neutral_offset=neutral_offset,
                        shoulder_raise_threshold=self.cfg.shoulder_raise_threshold,
                        shoulder_tension_duration=self.cfg.shoulder_tension_duration,
                        shoulder_tension_elapsed=shoulder_tension_elapsed,
                        shoulder_neutral_clearance=sh_neutral,
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
                    if not self.headless:
                        cv2.imshow("Tango Posture", panel)
                        self.window_open = True
                        if not self.key(cv2.waitKey(1) & 0xFF, timestamp):
                            break
                        if cv2.getWindowProperty("Tango Posture", cv2.WND_PROP_VISIBLE) < 1:
                            status = "stopped"
                            break
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
                self.finish(end_time, status)
            finally:
                try:
                    if self.beat_tracker:
                        self.beat_tracker.close()
                    if raw_writer:
                        raw_writer.release()
                        print(f"Saved raw video: {self.record_video}")
                    if overlay_writer:
                        overlay_writer.release()
                        print(f"Saved overlay video: {self.record_overlay}")
                    if self.beat_tracker:
                        recorded_vids = [
                            p for p in (self.record_video, self.record_overlay) if p is not None
                        ]
                        muxed = self.beat_tracker.mux_into_videos(recorded_vids)
                        for mp in muxed:
                            print(f"Embedded microphone audio into: {mp}")
                    if self.capture:
                        self.capture.close()
                finally:
                    try:
                        if self.worker:
                            self.worker.shutdown(wait=True, cancel_futures=True)
                        if self.estimator:
                            self.estimator.close()
                    finally:
                        if not self.headless:
                            cv2.destroyAllWindows()
        return self.outputs
