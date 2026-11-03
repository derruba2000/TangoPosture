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
    if not cfg.allow_all_angles:
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

    # All-angle mode (front, side, 360 pivots, and side steps)
    if not good("nose"):
        return unknown("Keep head/nose visible in frame")
    vis_shoulders = [side for side in ("left", "right") if good(f"{side}_shoulder")]
    vis_hips = [side for side in ("left", "right") if good(f"{side}_hip")]
    if not vis_shoulders or not vis_hips:
        return unknown("Keep shoulders and hips visible in frame")
    points = {k: pixel(v, frame.width, frame.height) for k, v in p.items()}
    if len(vis_shoulders) == 2 and len(vis_hips) == 2:
        s = 0.5 * (points["left_shoulder"] + points["right_shoulder"])
        h = 0.5 * (points["left_hip"] + points["right_hip"])
    else:
        s_side = cfg.side if cfg.side in vis_shoulders else vis_shoulders[0]
        h_side = cfg.side if cfg.side in vis_hips else vis_hips[0]
        s, h = points[f"{s_side}_shoulder"], points[f"{h_side}_hip"]
    length = float(np.linalg.norm(s - h))
    if length < cfg.min_torso * frame.height:
        return unknown("Torso too small or degenerate")
    sh_span = (
        float(np.linalg.norm(points["left_shoulder"] - points["right_shoulder"]))
        if len(vis_shoulders) == 2
        else 0.0
    )
    width = sh_span / length
    t = (s - h) / length
    trunk = math.degrees(math.atan2((1 if cfg.facing == "right" else -1) * t[0], t[1]))
    values = dict.fromkeys(METRICS)
    values["trunk_inclination"] = trunk
    quality = {m: QualityResult(True, view_width=width) for m in METRICS}
    vis_ears = [side for side in ("left", "right") if good(f"{side}_ear")]
    if not vis_ears:
        for m in METRICS[:2]:
            quality[m] = QualityResult(False, "Cannot assess: ear not visible", width)
        return MetricObservation(frame.timestamp, values, quality, width)
    if len(vis_ears) == 2 and width > 0.25:
        ear_pt = 0.5 * (points["left_ear"] + points["right_ear"])
    else:
        e_side = cfg.side if cfg.side in vis_ears else vis_ears[0]
        ear_pt = points[f"{e_side}_ear"]
    if np.linalg.norm(ear_pt - s) < cfg.min_head * frame.height:
        for m in METRICS[:2]:
            quality[m] = QualityResult(
                False, "Cannot assess: shoulder-to-ear segment too short", width
            )
    else:
        values.update(calculate(ear_pt, s, h, cfg.facing))
    return MetricObservation(frame.timestamp, values, quality, width)
