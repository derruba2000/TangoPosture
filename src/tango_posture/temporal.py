"""Timestamp-driven filtering and hysteresis; no capture or GUI dependencies."""

import math

from .config import Config
from .domain import DeviationEvent
from .geometry import wrapped


class Evaluator:
    def __init__(self, metric: str, baseline: float, cfg: Config):
        self.metric, self.baseline, self.cfg = metric, baseline, cfg
        self.threshold = getattr(cfg, metric)
        self.smoothed: float | None = None
        self.last_valid: float | None = None
        self.last_timestamp: float | None = None
        self.pending: float | None = None
        self.clearing: float | None = None
        self.event: DeviationEvent | None = None
        self.events: list[DeviationEvent] = []
        self.pending_peak = 0.0

    def close(self, reason: str, reset_filter: bool = False) -> None:
        if self.event:
            self.event.end = self.last_valid
            self.event.end_reason = reason
            self.event = None
        self.pending = self.clearing = None
        self.pending_peak = 0
        if reset_filter:
            self.smoothed = self.last_valid = None

    def update(self, timestamp: float, raw: float | None) -> dict:
        if not math.isfinite(timestamp) or (
            self.last_timestamp is not None and timestamp <= self.last_timestamp
        ):
            raise ValueError("Observation timestamps must be finite and strictly increasing")
        self.last_timestamp = timestamp
        if self.last_valid is not None and timestamp - self.last_valid > self.cfg.max_gap:
            self.close("tracking_lost", reset_filter=True)
        if raw is None or not math.isfinite(raw):
            self.close("tracking_lost")
            return {"raw": None, "smoothed": None, "delta": None, "state": "UNKNOWN"}
        if self.smoothed is None or self.last_valid is None:
            self.smoothed = raw
        else:
            alpha = 1 - math.exp(-(timestamp - self.last_valid) / self.cfg.smoothing)
            step = raw - self.smoothed
            if self.metric != "head_forward":
                step = wrapped(step)
            self.smoothed += alpha * step
        self.last_valid = timestamp
        delta = self.smoothed - self.baseline
        if self.metric != "head_forward":
            delta = wrapped(delta)
        magnitude = delta if self.metric == "head_forward" else abs(delta)
        if self.event:
            self.event.peak_deviation = max(self.event.peak_deviation, magnitude)
            if magnitude < self.threshold.clear:
                if self.clearing is None:
                    self.clearing = timestamp
                if timestamp - self.clearing + 1e-9 >= self.cfg.clear_duration:
                    self.close("cleared")
            else:
                self.clearing = None
        elif magnitude > self.threshold.entry:
            if self.pending is None:
                self.pending = timestamp
            self.pending_peak = max(self.pending_peak, magnitude)
            if timestamp - self.pending + 1e-9 >= self.cfg.entry_duration:
                self.event = DeviationEvent(self.metric, self.pending, timestamp, self.pending_peak)
                self.events.append(self.event)
                self.pending = None
        else:
            self.pending = None
            self.pending_peak = 0
        state = (
            "DEVIATION"
            if self.event
            else "PENDING"
            if self.pending is not None
            else "WITHIN_REFERENCE"
        )
        return {"raw": raw, "smoothed": self.smoothed, "delta": delta, "state": state}
