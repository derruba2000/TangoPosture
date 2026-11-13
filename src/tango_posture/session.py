from dataclasses import asdict
from uuid import uuid4

from .calibration.baseline import utc_now
from .calibration.models import profile_from_calibration
from .config import Config
from .domain import METRICS, Calibration, MetricObservation, QualityResult
from .reports import summary_dict
from .storage import Storage
from .temporal import Evaluator


class Session:
    def __init__(
        self,
        ref: Calibration,
        cfg: Config,
        storage: Storage,
        timestamp: float,
        source: str,
        versions: dict,
    ):
        self.id = str(uuid4())
        self.cfg, self.storage = cfg, storage
        self.profile = profile_from_calibration(ref)
        self.ranges = (
            {key: (target.lower, target.upper) for key, target in self.profile.metrics.items()
             if target.camera_check_passed}
            if self.profile else None
        )
        if self.ranges == {}:
            raise ValueError("Comparison-only profile: complete the guided camera check before scoring")
        self.evaluators = (
            {key: Evaluator(key, 0, cfg, bounds=bounds) for key, bounds in self.ranges.items()}
            if self.ranges is not None else
            {m: Evaluator(m, ref.baseline[m], cfg) for m in METRICS}
        )
        self.rows: list[dict] = []
        self.buffer: list[dict] = []
        self.spans: list[tuple[float, float]] = []
        self.start: float | None = timestamp
        self.pause_start: float | None = None
        self.pause_duration = 0.0
        self.finished = False
        storage.save_calibration(ref)
        storage.start(self.id, ref.id, utc_now(), source, cfg.model_dump(), versions)

    def update(self, observation: MetricObservation) -> list[dict]:
        if self.start is None:
            return []
        rows = []
        for metric, evaluator in self.evaluators.items():
            quality = observation.quality[metric]
            if self.profile is not None:
                view = self.profile.metrics[metric].view
                width = observation.view_width
                if view != "all" and (
                    width is None or (view == "side" and width > self.cfg.max_view_width)
                    or (view == "front" and width <= self.cfg.max_view_width)
                ):
                    quality = QualityResult(False, "Camera view is not comparable to the approved range")
            row = {
                "timestamp": observation.timestamp,
                "metric": metric,
                **evaluator.update(
                    observation.timestamp, observation.values[metric] if quality.valid else None
                ),
                "quality_state": "VALID" if quality.valid else "UNKNOWN",
                "reason": quality.reason,
            }
            rows.append(row)
        self.rows.extend(rows)
        self.buffer.extend(rows)
        if len(self.buffer) >= 180:
            self.flush()
        return rows

    def flush(self) -> None:
        if self.buffer:
            self.storage.observations(self.id, self.buffer)
            self.buffer.clear()

    def pause(self, timestamp: float) -> None:
        if self.start is not None:
            self.spans.append((self.start, timestamp))
            self.start = None
            self.pause_start = timestamp
            for evaluator in self.evaluators.values():
                evaluator.close("paused", reset_filter=True)
            self.flush()

    def resume(self, timestamp: float) -> None:
        if self.pause_start is not None:
            self.pause_duration += timestamp - self.pause_start
            self.pause_start = None
            self.start = timestamp

    def finish(
        self, timestamp: float, status: str = "completed", performance: dict | None = None
    ) -> dict:
        if self.finished:
            raise ValueError("Session already finished")
        if self.start is not None:
            self.spans.append((self.start, timestamp))
        if self.pause_start is not None:
            self.pause_duration += timestamp - self.pause_start
        for evaluator in self.evaluators.values():
            evaluator.close("session_ended")
        events = [asdict(e) for v in self.evaluators.values() for e in v.events]
        summary = summary_dict(
            self.rows, events, self.spans, self.pause_duration, self.cfg, ranges=self.ranges,
        )
        if self.profile is not None:
            summary["units"] = {key: value.unit for key, value in self.profile.metrics.items()}
            summary["approved_ranges"] = {key: value.model_dump(mode="json")
                                          for key, value in self.profile.metrics.items()}
            summary["unavailable_for_scoring"] = [
                key for key, target in self.profile.metrics.items() if not target.camera_check_passed
            ]
            summary["measurement_version"] = self.profile.measurement_version
        summary["performance"] = performance or {}
        summary["thresholds_label"] = (
            "user-approved ranges; not technique standards" if self.profile else
            "experimental engineering defaults; not technique standards"
        )
        self.flush()
        self.storage.finish(self.id, utc_now(), status, summary, events)
        self.finished = True
        return summary
