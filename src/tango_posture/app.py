"""Desktop controller. Headless and GUI use the same observation/session pipeline."""

import platform
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .beat import BeatTracker
from .calibration import calibrate, compatible, profile
from .capture import Capture
from .config import Config
from .domain import Calibration, MetricObservation, PoseEstimator
from .geometry import calculate, detect_role, head_horizontal_alignment, pixel
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
        self.cal_start: float | None = None
        self.candidate: Calibration | None = None
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
            )
            self.report_written = True
            print(f"Saved report: {report_path} (latest: {report_dir / 'latest_report.md'})")

    def accept(self) -> None:
        if self.candidate:
            self.storage.save_calibration(self.candidate)
            self.reference = self.candidate
            self.candidate = None
            print(f"Accepted reference: {self.reference.id} ({self.reference.review['label']})")
            self.notice = "Reference accepted. S: start session"
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
            self.candidate = None
            self.cal_start = timestamp + 3
            self.state = "CALIBRATING"
            self.setup_confirmed = True
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
                self.beat_tracker = BeatTracker(
                    self.cfg.tango_bpm, enable_audio=not self.headless and self.video is None
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
                            q = self.candidate.quality
                            self.notice = (
                                f"Reference: {q['valid_duration']:.1f}s valid, "
                                f"MAD {q['mad']}. A: accept / R: retry"
                            )
                            print(self.notice)
                            if self.headless:
                                self.accept()
                                self.start_session(timestamp)
                        except ValueError as error:
                            if self.headless:
                                raise
                            self.notice = str(error) + "; R: retry"
                        self.cal_start = None
                        self.state = "RUNNING" if self.headless else "READY"
                    else:
                        remaining = self.cal_start - timestamp
                        self.notice = (
                            f"Adopt reference: countdown {remaining:.1f}s"
                            if remaining > 0
                            else f"Capturing reference: {end - timestamp:.1f}s remaining"
                        )
                elif self.state == "RUNNING" and self.session:
                    self.rows = self.session.update(observation)
                head_angle = None
                upper_body = None
                if len(frame.poses) == 1:
                    pose = frame.poses[0]
                    self.role = detect_role(pose)
                    head_info = head_horizontal_alignment(pose, frame.width, frame.height)
                    if head_info is not None:
                        head_angle = head_info[0]
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
                else:
                    self.role = "NONE"
                quality_reason = next(
                    (q.reason for q in observation.quality.values() if not q.valid), ""
                )
                beat_state = self.beat_tracker.state_at(timestamp) if self.beat_tracker else None
                self.frame_log.append(
                    {
                        "timestamp": timestamp,
                        "head_angle": head_angle,
                        "role": self.role,
                        "upper_body": upper_body,
                        "quality_reason": quality_reason,
                        "beat": beat_state["beat"] if beat_state else None,
                        "phrase": beat_state["phrase"] if beat_state else None,
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
                message = self.notice
                if bad_head:
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
                    beat_str = (
                        f"Phrase {beat_state['phrase']} Beat {beat_state['beat']}/8 "
                        f"({beat_state['bpm']:.0f} BPM, {beat_state['source']})"
                        if beat_state
                        else "disabled"
                    )
                    lines = [
                        (
                            f"{self.state} | active {active:.1f}s | processed {fps:.1f} FPS | "
                            f"side {self.cfg.side}, facing {self.cfg.facing}"
                        ),
                        (
                            f"Role: {self.role} | Beat: {beat_str} | Head horiz: {head_str}"
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
                    if raw_writer:
                        raw_writer.release()
                        print(f"Saved raw video: {self.record_video}")
                    if overlay_writer:
                        overlay_writer.release()
                        print(f"Saved overlay video: {self.record_overlay}")
                    if self.capture:
                        self.capture.close()
                    if self.beat_tracker:
                        self.beat_tracker.close()
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
