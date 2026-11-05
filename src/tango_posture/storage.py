import csv
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .domain import Calibration


class Storage:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "tango.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError(f"Unsupported database schema version {version}")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS calibrations (
          id TEXT PRIMARY KEY, created_at_utc TEXT, name TEXT, profile TEXT, baseline TEXT,
          quality TEXT, model_hash TEXT, metric_version TEXT, review TEXT);
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY, calibration_id TEXT REFERENCES calibrations(id),
          start_utc TEXT, end_utc TEXT, source_type TEXT, status TEXT, active_duration REAL,
          config TEXT, versions TEXT, summary TEXT);
        CREATE TABLE IF NOT EXISTS observations (
          session_id TEXT REFERENCES sessions(id), timestamp REAL, metric TEXT,
          raw REAL, smoothed REAL, delta REAL, quality_state TEXT, reason TEXT, state TEXT);
        CREATE INDEX IF NOT EXISTS observations_session_time ON observations(session_id,timestamp);
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id), metric TEXT,
          onset REAL, detection REAL, end REAL, peak_deviation REAL, end_reason TEXT);
        PRAGMA user_version=1;
        """)

    def save_calibration(self, calibration: Calibration) -> None:
        d = asdict(calibration)
        for key in ("profile", "baseline", "quality", "review"):
            d[key] = json.dumps(d[key], allow_nan=False)
        # Use INSERT OR REPLACE only when no sessions reference this calibration;
        # otherwise update in-place to avoid foreign-key conflicts.
        has_sessions = (
            self.db.execute(
                "SELECT 1 FROM sessions WHERE calibration_id=? LIMIT 1", (calibration.id,)
            ).fetchone()
            is not None
        )
        with self.db:
            if has_sessions:
                self.db.execute(
                    "UPDATE calibrations SET created_at_utc=:created_at_utc, name=:name, "
                    "profile=:profile, baseline=:baseline, quality=:quality, "
                    "model_hash=:model_hash, metric_version=:metric_version, review=:review "
                    "WHERE id=:id",
                    d,
                )
            else:
                self.db.execute(
                    "INSERT OR REPLACE INTO calibrations VALUES (:id,:created_at_utc,:name,:profile,"
                    ":baseline,:quality,:model_hash,:metric_version,:review)",
                    d,
                )

    def calibration(self, identifier: str) -> Calibration:
        row = self.db.execute("SELECT * FROM calibrations WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Reference not found; use calibrations list")
        d = dict(row)
        for key in ("profile", "baseline", "quality", "review"):
            d[key] = json.loads(d[key])
        return Calibration(**d)

    def start(
        self, identifier: str, ref: str, start: str, source: str, config: dict, versions: dict
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO sessions VALUES (?,?,?,NULL,?,'running',0,?,?,NULL)",
                (identifier, ref, start, source, json.dumps(config), json.dumps(versions)),
            )

    def observations(self, identifier: str, rows: list[dict]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO observations VALUES "
                "(:session_id,:timestamp,:metric,:raw,:smoothed,:delta,"
                ":quality_state,:reason,:state)",
                [{**r, "session_id": identifier} for r in rows],
            )

    def finish(
        self, identifier: str, end: str, status: str, summary: dict, events: list[dict]
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sessions SET end_utc=?,status=?,active_duration=?,summary=? WHERE id=?",
                (
                    end,
                    status,
                    summary["active_duration"],
                    json.dumps(summary, allow_nan=False),
                    identifier,
                ),
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO events VALUES "
                "(:id,:session_id,:metric,:onset,:detection,:end,:peak_deviation,:end_reason)",
                [{**e, "session_id": identifier} for e in events],
            )

    def discard_running_session(self, identifier: str) -> None:
        with self.db:
            row = self.db.execute("SELECT status FROM sessions WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return
            if row["status"] != "running":
                raise ValueError("Cannot discard a saved session")
            self.db.execute("DELETE FROM events WHERE session_id=?", (identifier,))
            self.db.execute("DELETE FROM observations WHERE session_id=?", (identifier,))
            self.db.execute("DELETE FROM sessions WHERE id=? AND status='running'", (identifier,))

    def list(self, table: str) -> list[dict]:
        if table not in ("sessions", "calibrations"):
            raise ValueError("Invalid table")
        return [dict(r) for r in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC")]

    def export(self, identifier: str, output: Path) -> Path:
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Session not found")
        session = dict(row)
        for key in ("config", "versions", "summary"):
            session[key] = json.loads(session[key]) if session[key] else None
        session["reference"] = asdict(self.calibration(session["calibration_id"]))
        session["export_schema_version"] = 1
        from .domain import UNITS

        session["units"] = UNITS
        output.mkdir(parents=True, exist_ok=True)
        (output / "summary.json").write_text(
            json.dumps(session, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        for table, order in (("observations", "timestamp,metric"), ("events", "onset,metric")):
            cursor = self.db.execute(
                f"SELECT * FROM {table} WHERE session_id=? ORDER BY {order}", (identifier,)
            )
            with (output / f"{table}.csv").open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([d[0] for d in cursor.description])
                for record in cursor:
                    writer.writerow(
                        [format(x, ".12g") if isinstance(x, float) else x for x in record]
                    )
        return output

    def close(self) -> None:
        self.db.close()
