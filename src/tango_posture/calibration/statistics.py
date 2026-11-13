"""Passage distributions, reliable duration and guided repeatability checks."""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

import numpy as np

from .models import Passage


def summarize_examples(
    samples: list[tuple[float, float | None]], passages: list[Passage],
    max_gap: float, lower: float | None = None, upper: float | None = None,
) -> dict[str, Any]:
    if not math.isfinite(max_gap) or max_gap <= 0:
        raise ValueError("Maximum tracking gap must be finite and positive")
    if any(not math.isfinite(t) for t, _ in samples) or any(
        b[0] <= a[0] for a, b in pairwise(samples)
    ):
        raise ValueError("Evidence timestamps must be finite and strictly increasing")
    ordered = sorted(passages, key=lambda passage: passage.start)
    if any(left.end > right.start for left, right in pairwise(ordered)):
        raise ValueError("Example passages must not overlap")
    selected = [(t, value) for t, value in samples if any(p.start <= t < p.end for p in ordered)]
    valid = [(t, value) for t, value in selected if value is not None and math.isfinite(value)]
    if len(valid) < 2:
        raise ValueError("Not enough reliable samples; choose another passage or skip this metric")
    values = [value for _, value in valid]
    p10, median, p90 = map(float, np.quantile(values, [0.1, 0.5, 0.9]))
    lo, hi = p10 if lower is None else lower, p90 if upper is None else upper
    if not math.isfinite(lo) or not math.isfinite(hi) or lo > hi:
        raise ValueError("Acceptable range must have finite, ordered limits")
    duration = sum(
        b[0] - a[0] for a, b in pairwise(selected)
        if a[1] is not None and b[1] is not None
        and math.isfinite(a[1]) and math.isfinite(b[1])
        and 0 < b[0] - a[0] <= max_gap
        and any(p.start <= a[0] <= b[0] < p.end for p in ordered)
    )
    if duration <= 0:
        raise ValueError("No continuous reliable footage; select a longer stable passage")
    return {
        "median": median, "p10": p10, "p90": p90, "lower": lo, "upper": hi,
        "valid_duration": duration, "selected_duration": sum(p.end - p.start for p in ordered),
        "samples": len(valid), "inside_fraction": sum(lo <= value <= hi for value in values) / len(values),
        "minimum_time": min(valid, key=lambda item: item[1])[0],
        "maximum_time": max(valid, key=lambda item: item[1])[0],
    }


def repeat_check(
    samples: list[tuple[float, float | None]], holds: list[Passage], max_gap: float,
) -> tuple[bool, str]:
    if len(holds) != 4:
        return False, "Capture neutral, change, return, and a fresh repetition"
    try:
        stats = [summarize_examples(samples, [hold], max_gap) for hold in holds]
    except ValueError as error:
        return False, str(error)
    medians = [item["median"] for item in stats]
    if any(item["valid_duration"] < 0.5 or
           item["valid_duration"] / item["selected_duration"] < 0.6 for item in stats):
        return False, "Too little reliable footage in a hold; adjust framing, then retry or skip"
    noise = max(item["p90"] - item["p10"] for item in stats)
    change, repeated = medians[1] - medians[0], medians[3] - medians[2]
    if min(abs(change), abs(repeated)) <= max(2 * noise, 1e-6) or change * repeated <= 0:
        return False, "Changes were not distinguishable from hold variation; adjust framing and retry"
    if abs(medians[2] - medians[0]) > max(noise * 2, abs(change) * 0.25, 1e-6):
        return False, "Return to neutral did not match the first hold; retry or skip this metric"
    return True, "Repeated change and return distinguished from within-hold variation"
