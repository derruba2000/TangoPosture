import math

import numpy as np

from .config import Config
from .domain import METRICS, FrameObservation, MetricObservation, QualityResult
from .geometry import calculate, pixel
from .quality import reliable


def measure(
    frame: FrameObservation, cfg: Config, view_range: tuple[float, float] | None = None
) -> MetricObservation:
    def unknown(reason: str) -> MetricObservation:
        return MetricObservation(
            frame.timestamp,
            dict.fromkeys(METRICS),
            {m: QualityResult(False, reason) for m in METRICS},
        )

    if len(frame.poses) != 1:
        return unknown("One-person mode only" if len(frame.poses) > 1 else "No person detected")
    if frame.width <= 0 or frame.height <= 0 or not math.isfinite(frame.timestamp):
        return unknown("Invalid frame geometry or timestamp")
    p = frame.poses[0]
    good = lambda name: reliable(p.get(name), cfg.confidence)
    if not good("nose") or not any(good(s + "_ankle") for s in ("left", "right")):
        return unknown("Keep nose and at least one ankle visible in frame")
    pair = [s + "_" + joint for s in ("left", "right") for joint in ("shoulder", "hip")]
    if not all(good(k) for k in pair):
        return unknown("Side-on view UNKNOWN: shoulders/hips not reliable")
    points = {k: pixel(v, frame.width, frame.height) for k, v in p.items()}
    s, h = points[cfg.side + "_shoulder"], points[cfg.side + "_hip"]
    length = float(np.linalg.norm(s - h))
    if length < cfg.min_torso * frame.height:
        return unknown("Torso too small or degenerate")
    width = (
        max(
            float(np.linalg.norm(points["left_" + j] - points["right_" + j]))
            for j in ("shoulder", "hip")
        )
        / length
    )
    if width > cfg.max_view_width or (
        view_range
        and not view_range[0] - cfg.view_change_tolerance
        <= width
        <= view_range[1] + cfg.view_change_tolerance
    ):
        return unknown("Return to the selected side-on view")
    t = (s - h) / length
    trunk = math.degrees(math.atan2((1 if cfg.facing == "right" else -1) * t[0], t[1]))
    values: dict[str, float | None] = dict.fromkeys(METRICS)
    values["trunk_inclination"] = trunk
    quality = {m: QualityResult(True, view_width=width) for m in METRICS}
    reason = ""
    if not good(cfg.side + "_ear"):
        reason = "Cannot assess: ear not visible"
    elif np.linalg.norm(points[cfg.side + "_ear"] - s) < cfg.min_head * frame.height:
        reason = "Cannot assess: shoulder-to-ear segment too short"
    if reason:
        for m in METRICS[:2]:
            quality[m] = QualityResult(False, reason, width)
    else:
        values.update(calculate(points[cfg.side + "_ear"], s, h, cfg.facing))
    return MetricObservation(frame.timestamp, values, quality, width)
