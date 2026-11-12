"""Read-only projection of schema-v1 sessions; no metric or coverage recomputation."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from ..archive import practice_directory, read_manifest
from ..domain import METRIC_VERSION, METRICS

Point = tuple[float, float | None]


@dataclass(frozen=True)
class MetricData:
    points: list[Point]
    segments: list[list[tuple[float, float]]]
    summary: dict[str, Any]
    event_count: int
    open_event_count: int


@dataclass(frozen=True)
class PersistedEvent:
    identifier: str
    metric: str
    onset: float
    detection: float
    end: float | None
    peak_deviation: float | None
    end_reason: str | None


@dataclass(frozen=True)
class SessionData:
    identifier: str
    status: str
    metrics: dict[str, MetricData]
    active_duration: float | None
    max_gap: float | None
    origin_timestamp: float
    events: list[PersistedEvent]


def finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _object(value: str | None) -> dict[str, Any]:
    if value is None:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("Invalid persisted session metadata.")
    return parsed


def _connect(data_dir: Path) -> sqlite3.Connection:
    path = data_dir / "tango.sqlite3"
    if not path.is_file():
        raise ValueError("No local session database. Save a practice session first.")
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Unsupported local session database schema (expected version 1).")
    except Exception:
        db.close()
        raise
    return db


def list_sessions(data_dir: Path) -> list[tuple[str, str]]:
    with closing(_connect(data_dir)) as db:
        return [
            (row["id"], f"{row['start_utc'] or 'Unknown date'} · {row['status']} · {row['id']}")
            for row in db.execute("SELECT id,start_utc,status FROM sessions ORDER BY rowid DESC")
        ]


def load_session(data_dir: Path, identifier: str) -> SessionData:
    """Keep persisted deltas and summaries, splitting lines at unknown/long gaps."""
    with closing(_connect(data_dir)) as db:
        # A consistent snapshot also prevents mixing a summary with newer observations.
        db.execute("BEGIN")
        row = db.execute(
            "SELECT s.*, c.metric_version FROM sessions s "
            "LEFT JOIN calibrations c ON c.id=s.calibration_id WHERE s.id=?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise ValueError("Selected session is no longer available.")
        if row["status"] == "running":
            raise ValueError("Selected session is not finalized. Save or finish it first.")
        versions = _object(row["versions"])
        if (
            row["metric_version"] != METRIC_VERSION
            or versions.get("metric_version", METRIC_VERSION) != METRIC_VERSION
        ):
            raise ValueError("Selected session has incompatible or unknown metric semantics.")
        summary = _object(row["summary"])
        if summary and summary.get("schema_version") != 1:
            raise ValueError("Unsupported persisted summary schema.")
        config = _object(row["config"])
        max_gap = finite_number(config.get("max_gap"))
        if max_gap is not None and max_gap <= 0:
            max_gap = None
        rows = db.execute(
            "SELECT timestamp,metric,delta,quality_state,state FROM observations "
            "WHERE session_id=? AND metric IN (?,?,?) ORDER BY timestamp,metric",
            (identifier, *METRICS),
        ).fetchall()
        events = db.execute(
            "SELECT id,metric,onset,detection,end,peak_deviation,end_reason "
            "FROM events WHERE session_id=? ORDER BY onset,id",
            (identifier,),
        ).fetchall()
        event_counts = db.execute(
            "SELECT metric,COUNT(*) AS total,SUM(end IS NULL) AS open_count "
            "FROM events WHERE session_id=? GROUP BY metric",
            (identifier,),
        ).fetchall()

    if not rows:
        raise ValueError("Selected session has no compatible persisted metric observations.")
    origin = finite_number(rows[0]["timestamp"])
    if origin is None:
        raise ValueError("Selected session contains an invalid observation timestamp.")
    points: dict[str, list[Point]] = {metric: [] for metric in METRICS}
    for observation in rows:
        timestamp = finite_number(observation["timestamp"])
        own = points[observation["metric"]]
        if timestamp is None or (own and timestamp - origin <= own[-1][0]):
            raise ValueError(
                "Selected session contains invalid or duplicate observation timestamps."
            )
        delta = finite_number(observation["delta"])
        if observation["quality_state"] != "VALID" or observation["state"] == "UNKNOWN":
            delta = None
        own.append((timestamp - origin, delta))

    metric_summaries = summary.get("metrics", {})
    if not isinstance(metric_summaries, dict):
        raise TypeError("Invalid persisted metric summaries.")
    persisted_events = []
    for event in events:
        onset = finite_number(event["onset"])
        detection = finite_number(event["detection"])
        end = finite_number(event["end"])
        peak = finite_number(event["peak_deviation"])
        if onset is None or detection is None or detection < onset:
            raise ValueError("Selected session contains an invalid persisted event timeline.")
        if end is not None and end < detection:
            raise ValueError("Selected session contains an invalid persisted event timeline.")
        persisted_events.append(
            PersistedEvent(
                str(event["id"]),
                str(event["metric"]),
                onset,
                detection,
                end,
                peak,
                event["end_reason"] if isinstance(event["end_reason"], str) else None,
            )
        )
    counts = {
        event["metric"]: (event["total"], event["open_count"]) for event in event_counts
    }
    metrics = {}
    for metric, own in points.items():
        segments: list[list[tuple[float, float]]] = []
        segment: list[tuple[float, float]] = []
        for timestamp, delta in own:
            if delta is None:
                segment = []
                continue
            if not segment or max_gap is None or timestamp - segment[-1][0] > max_gap:
                segment = []
                segments.append(segment)
            segment.append((timestamp, delta))
        details = metric_summaries.get(metric, {})
        if not isinstance(details, dict):
            raise TypeError("Invalid persisted metric summary.")
        count, open_count = counts.get(metric, (0, 0))
        metrics[metric] = MetricData(own, segments, details, count, open_count)
    return SessionData(
        identifier,
        row["status"],
        metrics,
        finite_number(summary.get("active_duration")),
        max_gap,
        origin,
        persisted_events,
    )


def recordings_for_session(data_dir: Path, identifier: str) -> list[tuple[Path, int, list[float]]]:
    """Find archived recording segments whose report identifies this persisted session."""
    folder = practice_directory(str(data_dir))
    if not folder.is_dir():
        return []
    session_line = re.compile(rf"^\| Session ID \| `{re.escape(identifier)}` \|$", re.MULTILINE)
    matches: list[tuple[Path, int, list[float]]] = []
    for path in sorted(folder.glob("*.tp"), key=lambda item: item.stat().st_mtime_ns, reverse=True):
        try:
            manifest = read_manifest(path)
            with ZipFile(path) as archive:
                for index, segment in enumerate(manifest["segments"]):
                    report = archive.read(segment["report"]).decode("utf-8")
                    if not session_line.search(report):
                        continue
                    metadata = json.loads(archive.read(segment["metadata"]))
                    if not isinstance(metadata, dict):
                        continue
                    timestamps = metadata.get("frame_timestamps", [])
                    if (
                        isinstance(timestamps, list)
                        and timestamps
                        and all(
                            isinstance(value, (int, float))
                            and not isinstance(value, bool)
                            and math.isfinite(value)
                            for value in timestamps
                        )
                        and all(
                            earlier <= later for earlier, later in pairwise(timestamps)
                        )
                    ):
                        matches.append((path.resolve(), index, [float(value) for value in timestamps]))
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, BadZipFile):
            continue
    return matches
