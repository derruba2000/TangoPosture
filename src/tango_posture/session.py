from dataclasses import asdict
from uuid import uuid4

from .calibration import utc_now
from .config import Config
from .domain import METRICS, Calibration, MetricObservation
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
        self.evaluators = {m: Evaluator(m, ref.baseline[m], cfg) for m in METRICS}
        self.rows: list[dict] = []
        self.buffer: list[dict] = []
        self.spans: list[tuple[float, float]] = []
        self.start: float | None = timestamp
        self.pause_start: float | None = None
        self.pause_duration = 0.0
        self.finished = False
        storage.start(self.id, ref.id, utc_now(), source, cfg.model_dump(), versions)

    def update(self, observation: MetricObservation) -> list[dict]:
        if self.start is None:
            return []
        rows = []
        for metric, evaluator in self.evaluators.items():
            quality = observation.quality[metric]
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
        summary = summary_dict(self.rows, events, self.spans, self.pause_duration, self.cfg)
        summary["performance"] = performance or {}
        summary["thresholds_label"] = "experimental engineering defaults; not technique standards"
        self.flush()
        self.storage.finish(self.id, utc_now(), status, summary, events)
        self.finished = True
        return summary
