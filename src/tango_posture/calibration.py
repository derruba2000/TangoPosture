import itertools
from datetime import UTC, datetime
from uuid import uuid4

import numpy as np

from .config import Config
from .domain import METRIC_VERSION, METRICS, Calibration, MetricObservation
from .geometry import wrapped


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def profile(cfg: Config, width: int, height: int, source: str) -> dict:
    return {
        "side": cfg.side,
        "facing": cfg.facing,
        "width": width,
        "height": height,
        "aspect_ratio": width / height,
        "capture_profile": cfg.capture_profile,
        "source": source,
    }


def compatible(ref: Calibration, current: dict, model_hash: str) -> None:
    if ref.model_hash != model_hash or ref.metric_version != METRIC_VERSION:
        raise ValueError("Reference model/metric version changed; recalibrate")
    if ref.profile != current:
        raise ValueError(
            "Reference capture profile, resolution, side or direction changed; recalibrate"
        )


def calibrate(
    samples: list[MetricObservation],
    start: float,
    cfg: Config,
    capture_profile: dict,
    model_hash: str,
    name: str,
    review: dict | None = None,
) -> Calibration:
    end = start + cfg.calibration_duration
    window = [s for s in samples if start <= s.timestamp <= end]
    valid = [s for s in window if all(s.quality[m].valid for m in METRICS)]
    duration = sum(
        b.timestamp - a.timestamp
        for a, b in itertools.pairwise(window)
        if 0 < b.timestamp - a.timestamp <= cfg.max_gap
        and all(a.quality[m].valid and b.quality[m].valid for m in METRICS)
    )
    if len(valid) < cfg.calibration_samples or duration + 1e-8 < cfg.calibration_valid_duration:
        raise ValueError(
            f"Calibration rejected: {duration:.2f}s valid, {len(valid)} samples; "
            f"need {cfg.calibration_valid_duration}s and {cfg.calibration_samples}"
        )
    baseline, mad = {}, {}
    for m in METRICS:
        values = np.array([s.values[m] for s in valid], dtype=float)
        anchor = float(values[0])
        if m != "head_forward":
            values = np.array([anchor + wrapped(float(x) - anchor) for x in values])
        med = float(np.median(values))
        mad[m] = float(np.median(np.abs(values - med)))
        baseline[m] = med if m == "head_forward" else wrapped(med)
        if mad[m] > (cfg.offset_mad if m == "head_forward" else cfg.angle_mad):
            raise ValueError(f"Calibration rejected: unstable {m} (MAD {mad[m]:.3f})")
    widths = [s.view_width for s in valid if s.view_width is not None]
    return Calibration(
        str(uuid4()),
        utc_now(),
        name,
        capture_profile,
        baseline,
        {
            "mad": mad,
            "valid_duration": duration,
            "valid_coverage": duration / cfg.calibration_duration,
            "samples": len(valid),
            "view_range": [min(widths), max(widths)],
        },
        model_hash,
        review=review or {"label": "self-selected"},
    )
