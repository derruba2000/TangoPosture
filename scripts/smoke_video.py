"""Run real-model inference on a user-supplied, rights-cleared video; save no images."""

import argparse
import json
import time
from pathlib import Path

import numpy as np

from tango_posture.app import runtime_versions
from tango_posture.beat import BeatTracker
from tango_posture.capture import Capture
from tango_posture.config import load
from tango_posture.domain import METRICS
from tango_posture.geometry import (
    RoleTracker,
    calculate,
    detect_role,
    head_horizontal_alignment,
    pixel,
)
from tango_posture.metrics import measure
from tango_posture.pose.mediapipe_backend import MediaPipeEstimator
from tango_posture.reports import write_markdown_report

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("video")
p.add_argument("--side", required=True, choices=["left", "right"])
p.add_argument("--facing", required=True, choices=["left", "right"])
p.add_argument("--model-path", default="models/pose_landmarker_lite.task")
p.add_argument("--output", type=Path, default=Path("data/real-video-smoke.json"))
a = p.parse_args()
cfg = load(None, {"side": a.side, "facing": a.facing, "model_path": a.model_path})
beat_tracker = BeatTracker(bpm=cfg.tango_bpm, enable_audio=False) if cfg.beat_tracking else None
role_tracker = RoleTracker(assign_ms=cfg.role_assign_ms, hold_ms=cfg.role_hold_ms)
capture = None
estimator = None
count = 0
valid = dict.fromkeys(METRICS, 0)
reasons = {}
latencies = []
frame_log = []
first = None
last = None
try:
    estimator = MediaPipeEstimator(Path(cfg.model_path))
    capture = Capture(cfg, a.video)
    started = time.monotonic()
    while (frame := capture.read()) is not None:
        t = time.monotonic()
        observation = estimator.estimate(frame.image, frame.timestamp)
        metrics = measure(observation, cfg)
        latencies.append((time.monotonic() - t) * 1000)
        first = frame.timestamp if first is None else first
        last = frame.timestamp
        count += 1
        for m, q in metrics.quality.items():
            valid[m] += int(q.valid)
            if not q.valid:
                reasons[q.reason] = reasons.get(q.reason, 0) + 1
        head_angle = None
        raw_role = "NONE"
        upper_body = None
        if len(observation.poses) == 1:
            pose = observation.poses[0]
            raw_role = detect_role(pose)
            head_info = head_horizontal_alignment(pose, observation.width, observation.height)
            if head_info is not None:
                head_angle = head_info[0]
            ear_lm = pose.get(f"{cfg.side}_ear")
            sh_lm = pose.get(f"{cfg.side}_shoulder")
            hip_lm = pose.get(f"{cfg.side}_hip")
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
                        pixel(ear_lm, observation.width, observation.height),
                        pixel(sh_lm, observation.width, observation.height),
                        pixel(hip_lm, observation.width, observation.height),
                        cfg.facing,
                    )
                except ValueError:
                    upper_body = None
        role = role_tracker.update(raw_role, frame.timestamp)
        frame_log.append(
            {
                "timestamp": frame.timestamp,
                "head_angle": head_angle,
                "role": role,
                "upper_body": upper_body,
                "quality_reason": next(
                    (q.reason for q in metrics.quality.values() if not q.valid), ""
                ),
                "beat": beat_tracker.state_at(frame.timestamp) if beat_tracker else None,
            }
        )
    elapsed = time.monotonic() - started
    if not count:
        raise ValueError("No decodable frames")
    result = {
        **runtime_versions(estimator.model_hash),
        "fixture": "user-supplied; source path omitted",
        "config": cfg.model_dump(),
        "frames": count,
        "source_start": first,
        "source_end": last,
        "resolution": [observation.width, observation.height],
        "valid_frame_coverage": {m: n / count for m, n in valid.items()},
        "abstentions_by_metric_frame": reasons,
        "processed_fps": count / elapsed,
        "decode_to_measurement_ms_median": float(np.median(latencies)),
        "decode_to_measurement_ms_p95": float(np.percentile(latencies, 95)),
        "dropped_frames": capture.dropped,
        "timestamp_fallback": capture.fallback,
        "limitation": "File throughput, not webcam capture-to-overlay latency or technique accuracy",
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, indent=2) + "\n")
    report_path = write_markdown_report(
        report_dir=Path("reports"),
        cfg=cfg,
        source=f"video:{a.video}",
        status="completed",
        frame_log=frame_log,
        performance={
            "processed_fps": result["processed_fps"],
            "capture_to_overlay_ms_median": result["decode_to_measurement_ms_median"],
            "dropped_frames": result["dropped_frames"],
        },
        recorded_video=Path(a.video),
    )
    print(f"Saved JSON: {a.output}")
    print(f"Saved report: {report_path} (latest: reports/latest_report.md)")
finally:
    if capture:
        capture.close()
    if estimator:
        estimator.close()
