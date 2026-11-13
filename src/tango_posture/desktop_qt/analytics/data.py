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

from ...archive import practice_directory, read_analytics, read_manifest
from ...calibration.models import METRIC_DETAILS, RANGE_VERSION
from ...domain import METRIC_VERSION, METRICS

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
            "WHERE session_id=? ORDER BY timestamp,metric",
            (identifier,),
        ).fetchall()
        events = db.execute(
            "SELECT id,metric,onset,detection,end,peak_deviation,end_reason "
            "FROM events WHERE session_id=? ORDER BY onset,id",
            (identifier,),
        ).fetchall()

    return project_session(
        identifier, row["status"], rows, events, summary, max_gap,
    )


def project_session(
    identifier: str, status: str, rows: list[Any], events: list[Any],
    summary: dict[str, Any], max_gap: float | None,
) -> SessionData:
    if any(finite_number(observation["timestamp"]) is None for observation in rows):
        raise ValueError("Selected session contains an invalid observation timestamp.")
    rows = sorted(rows, key=lambda observation: (observation["timestamp"], observation["metric"]))
    if not rows:
        raise ValueError("Selected session has no compatible persisted metric observations.")
    origin = finite_number(rows[0]["timestamp"])
    if origin is None:
        raise ValueError("Selected session contains an invalid observation timestamp.")
    if summary.get("measurement_version", RANGE_VERSION) != RANGE_VERSION:
        raise ValueError("Incompatible range measurement version")
    keys = list(dict.fromkeys([*METRICS, *(row["metric"] for row in rows)]))
    if any(key not in METRIC_DETAILS for key in keys):
        raise ValueError("Selected session has no compatible persisted observations: unsupported metric")
    points: dict[str, list[Point]] = {metric: [] for metric in keys}
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
    counts: dict[str, tuple[int, int]] = {}
    for event in persisted_events:
        total, opened = counts.get(event.metric, (0, 0))
        counts[event.metric] = total + 1, opened + int(event.end is None)
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
        status,
        metrics,
        finite_number(summary.get("active_duration")),
        max_gap,
        origin,
        persisted_events,
    )


def session_payload(data_dir: Path, identifier: str) -> dict[str, Any]:
    dataset = load_session(data_dir, identifier)
    with closing(_connect(data_dir)) as db:
        row = db.execute("SELECT summary FROM sessions WHERE id=?", (identifier,)).fetchone()
        observations = [
            dict(item) for item in db.execute(
                "SELECT timestamp,metric,delta,quality_state,state FROM observations "
                "WHERE session_id=? ORDER BY timestamp,metric", (identifier,),
            )
        ]
    return {
        "id": identifier, "status": dataset.status,
        "observations": observations, "summary": _object(row["summary"]),
        "max_gap": dataset.max_gap,
        "events": [
            {"id": event.identifier, "metric": event.metric, "onset": event.onset,
             "detection": event.detection, "end": event.end,
             "peak_deviation": event.peak_deviation, "end_reason": event.end_reason}
            for event in dataset.events
        ],
    }


def load_archive_session(path: Path, index: int) -> SessionData:
    payload = read_analytics(path, index)
    if payload is None:
        raise ValueError("This recording has no stored analytics. Analyze it in Review or migrate legacy data.")
    session = payload.get("session")
    if not isinstance(session, dict):
        raise TypeError("No reference-relative analytics were stored for this recording.")
    if not isinstance(session.get("status"), str) or session["status"] == "running":
        raise ValueError("Archived session is not finalized or has an invalid status")
    summary = session.get("summary") or {}
    if not isinstance(summary, dict) or summary.get("schema_version", 1) != 1:
        raise ValueError("Unsupported archived analytics summary")
    rows = session.get("observations")
    events = session.get("events") or []
    if not isinstance(rows, list) or not isinstance(events, list):
        raise TypeError("Invalid archived analytics observations or events")
    return project_session(
        str(session.get("id") or f"{path.name}:{index}"),
        str(session.get("status", "completed")), rows, events, summary,
        gap if (gap := finite_number(session.get("max_gap"))) is not None and gap > 0 else None,
    )


FRAME_METRICS = {
    "head_angle": ("Head alignment", "degrees", 1.0),
    "shoulder_elev_pct": ("Shoulder height", "% raised", 1.0),
    "knee_bend_deg": ("Knee bend", "degrees", 1.0),
    "axis_curve_smoothed": ("Torso / hip offset", "%", 100.0),
}


def load_archive_dataset(path: Path, index: int) -> SessionData:
    payload = read_analytics(path, index)
    if payload is None:
        raise ValueError("No stored analytics for this recording. Analyze in Review or migrate legacy data.")
    if isinstance(payload.get("session"), dict):
        dataset = load_archive_session(path, index)
    else:
        dataset = SessionData(
            f"{path.name}:{index:02d}", "archived",
            {key: MetricData([], [], {}, 0, 0) for key in METRICS},
            None, finite_number(payload.get("max_gap")), 0.0, [],
        )
    coach = payload.get("coach_data")
    entries = coach.get("frames", {}) if isinstance(coach, dict) else {}
    if not entries:
        entries = {str(i): entry for i, entry in enumerate(payload.get("frames") or [])}
    timestamps = payload.get("frame_timestamps") or []
    origin = (
        dataset.origin_timestamp if isinstance(payload.get("session"), dict)
        else float(timestamps[0]) if timestamps else dataset.origin_timestamp
    )
    for key, (_, _, scale) in FRAME_METRICS.items():
        points: list[Point] = []
        for frame, entry in sorted(entries.items(), key=lambda item: int(item[0])):
            if not isinstance(entry, dict):
                raise TypeError("Invalid archived frame analytics")
            frame_index = int(frame)
            timestamp = (
                finite_number(timestamps[frame_index]) if frame_index < len(timestamps)
                else finite_number(entry.get("timestamp"))
            )
            value = finite_number(entry.get(key))
            if key == "axis_curve_smoothed" and value is None:
                value = finite_number(entry.get("axis_curve_ratio"))
            if timestamp is not None:
                points.append((timestamp - origin, value * scale if value is not None else None))
        segments: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for timestamp, value in points:
            if value is None:
                current = []
                continue
            if (
                not current or dataset.max_gap is None
                or timestamp - current[-1][0] > dataset.max_gap
            ):
                current = []
                segments.append(current)
            current.append((timestamp, value))
        dataset.metrics[key] = MetricData(points, segments, {}, 0, 0)
    if not isinstance(payload.get("session"), dict):
        return SessionData(
            dataset.identifier, dataset.status, dataset.metrics, dataset.active_duration,
            dataset.max_gap, origin, dataset.events,
        )
    return dataset
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
