"""Offline 2D display interpolation; inferred joints are never measurement evidence."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from itertools import pairwise
from typing import Literal

from ..domain import FrameObservation, Landmark
from ..quality import reliable


def _coordinate(
    frames: Sequence[FrameObservation], name: str, timestamp: float,
    anchors: list[int], axis: str,
) -> float:
    value = 0.0
    for anchor in anchors:
        component = getattr(frames[anchor].poses[0][name], axis)
        weight = 1.0
        for other in anchors:
            if other != anchor:
                weight *= (timestamp - frames[other].timestamp) / (
                    frames[anchor].timestamp - frames[other].timestamp
                )
        value += component * weight
    return value


def interpolate_poses(
    frames: Sequence[FrameObservation], *,
    method: Literal["linear", "quadratic"], max_gap: float, confidence: float,
) -> list[FrameObservation]:
    result = list(frames)
    if max_gap <= 0:
        return result
    segments: list[list[int]] = []
    for index, frame in enumerate(frames):
        if len(frame.poses) > 1 or not math.isfinite(frame.timestamp):
            segments.append([])
            continue
        if not segments or (
            segments[-1] and frame.timestamp <= frames[segments[-1][-1]].timestamp
        ):
            segments.append([])
        segments[-1].append(index)
    for segment in segments:
        names = {name for index in segment for pose in frames[index].poses for name in pose}
        for name in names:
            valid = [
                index for index in segment
                if frames[index].poses and reliable(frames[index].poses[0].get(name), confidence)
            ]
            for position, (left, right) in enumerate(pairwise(valid)):
                start, end = frames[left].timestamp, frames[right].timestamp
                if right == left + 1 or end - start > max_gap + 1e-9:
                    continue
                anchors = [left, right]
                if method == "quadratic":
                    candidates = [
                        valid[p] for p in (position - 1, position + 2)
                        if 0 <= p < len(valid)
                        and min(abs(frames[valid[p]].timestamp - start),
                                abs(frames[valid[p]].timestamp - end)) <= max_gap
                    ]
                    if candidates:
                        anchors.append(min(
                            candidates, key=lambda i: abs(frames[i].timestamp - (start + end) / 2),
                        ))
                for index in range(left + 1, right):
                    timestamp = frames[index].timestamp

                    x, y = (
                        _coordinate(frames, name, timestamp, anchors, axis) for axis in ("x", "y")
                    )
                    if not (0 <= x <= 1 and 0 <= y <= 1):
                        x, y = (
                            _coordinate(frames, name, timestamp, [left, right], axis)
                            for axis in ("x", "y")
                        )
                    pose = dict(result[index].poses[0]) if result[index].poses else {}
                    pose[name] = Landmark(
                        x=x, y=y,
                        visibility=1.0, presence=1.0,
                    )
                    result[index] = replace(result[index], poses=[pose])
    return result
