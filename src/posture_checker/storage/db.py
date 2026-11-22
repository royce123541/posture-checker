"""SQLite storage.

Privacy rule: this database holds numbers and short strings only: landmark
coordinates, derived features, scores, timings. There are no BLOB columns and
no image ever reaches this module (``tests/test_privacy.py`` checks both).
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from posture_checker import paths
from posture_checker.calibration import Calibration

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calibrations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    baseline_json   TEXT NOT NULL,
    spread_json     TEXT NOT NULL,
    n_samples       INTEGER NOT NULL,
    mean_confidence REAL NOT NULL
);

-- One row per processed frame. model_version and calibration_id give every
-- prediction its lineage.
CREATE TABLE IF NOT EXISTS samples (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    state          TEXT NOT NULL,      -- present | low_confidence | away
    label          TEXT,               -- model output, NULL when away
    confidence     REAL NOT NULL,
    reasons        TEXT NOT NULL,      -- comma-separated quality flags
    scores_json    TEXT,
    features_json  TEXT,               -- relative features fed to the model
    landmarks_json TEXT,               -- 13 upper-body points (x, y, z, visibility)
    brightness     REAL,
    model_version  TEXT,
    pose_model     TEXT,
    calibration_id INTEGER REFERENCES calibrations(id),
    latency_ms     REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    label          TEXT NOT NULL,
    bad_fraction   REAL NOT NULL,
    window_s       REAL NOT NULL,
    model_version  TEXT NOT NULL,
    calibration_id INTEGER REFERENCES calibrations(id),
    feedback       TEXT,               -- NULL | 'wrong'
    feedback_ts    REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT
);

-- User-given labels ("I'm slouching now" in the tray), for training in Stage 5.
-- sample_id links to the latest stored sample when there was one recently, with
-- the label the model predicted for it, so false negatives can be counted.
CREATE TABLE IF NOT EXISTS labels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    label           TEXT NOT NULL,
    source          TEXT NOT NULL,     -- 'tray'
    sample_id       INTEGER REFERENCES samples(id),
    model_version   TEXT,
    predicted_label TEXT
);
CREATE INDEX IF NOT EXISTS idx_labels_ts ON labels(ts);

-- Labelled samples from guided recording sessions: the label is the posture
-- the user was asked to hold. Raw features are kept so relative features can
-- be recomputed against any calibration later.
CREATE TABLE IF NOT EXISTS recorded_samples (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session        TEXT NOT NULL,
    ts             REAL NOT NULL,
    step           INTEGER NOT NULL,
    label          TEXT NOT NULL,
    confidence     REAL NOT NULL,
    raw_json       TEXT NOT NULL,
    features_json  TEXT NOT NULL,
    landmarks_json TEXT NOT NULL,
    calibration_id INTEGER REFERENCES calibrations(id)
);
CREATE INDEX IF NOT EXISTS idx_recorded_session ON recorded_samples(session);
"""


def _to_json(values: Mapping[str, float] | list[Any] | None) -> str | None:
    """Serialise numbers as strict JSON: NaN (a missing measurement) becomes null."""
    if values is None:
        return None
    if isinstance(values, Mapping):
        clean: Any = {
            k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in values.items()
        }
    else:
        clean = values
    return json.dumps(clean, allow_nan=False)


def _from_json(text: str) -> dict[str, float]:
    return {k: (math.nan if v is None else v) for k, v in json.loads(text).items()}


class Database:
    """Thin wrapper over one SQLite file.

    Toast-button callbacks arrive on a different thread from the monitor loop,
    so every statement runs under a lock on a shared connection.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = str(path) if path is not None else str(paths.db_path())
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            # WAL lets the report and trainer read while the monitor writes.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- calibrations -------------------------------------------------------
    def insert_calibration(self, calibration: Calibration) -> int:
        return self._execute(
            "INSERT INTO calibrations(ts, baseline_json, spread_json, n_samples, mean_confidence)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                calibration.ts,
                _to_json(calibration.baseline),
                _to_json(calibration.spread),
                calibration.n_samples,
                calibration.mean_confidence,
            ),
        )

    def latest_calibration(self) -> Calibration | None:
        rows = self.query("SELECT * FROM calibrations ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        row = rows[0]
        return Calibration(
            id=row["id"],
            ts=row["ts"],
            baseline=_from_json(row["baseline_json"]),
            spread=_from_json(row["spread_json"]),
            n_samples=row["n_samples"],
            mean_confidence=row["mean_confidence"],
        )

    # --- samples ------------------------------------------------------------
    def insert_sample(
        self,
        *,
        ts: float,
        state: str,
        confidence: float,
        reasons: tuple[str, ...] = (),
        label: str | None = None,
        scores: Mapping[str, float] | None = None,
        features: Mapping[str, float] | None = None,
        landmarks: list[list[float]] | None = None,
        brightness: float | None = None,
        model_version: str | None = None,
        pose_model: str | None = None,
        calibration_id: int | None = None,
        latency_ms: float | None = None,
    ) -> int:
        return self._execute(
            "INSERT INTO samples(ts, state, label, confidence, reasons, scores_json,"
            " features_json, landmarks_json, brightness, model_version, pose_model,"
            " calibration_id, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                state,
                label,
                confidence,
                ",".join(reasons),
                _to_json(scores),
                _to_json(features),
                _to_json(landmarks),
                brightness,
                model_version,
                pose_model,
                calibration_id,
                latency_ms,
            ),
        )

    # --- alerts and feedback ------------------------------------------------
    def insert_alert(
        self,
        *,
        ts: float,
        label: str,
        bad_fraction: float,
        window_s: float,
        model_version: str,
        calibration_id: int | None,
    ) -> int:
        return self._execute(
            "INSERT INTO alerts(ts, label, bad_fraction, window_s, model_version, calibration_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ts, label, bad_fraction, window_s, model_version, calibration_id),
        )

    def mark_alert_wrong(self, alert_id: int, ts: float) -> bool:
        """Record the user's "wrong alert" feedback. Returns False if no such alert."""
        if not self.query("SELECT 1 FROM alerts WHERE id = ?", (alert_id,)):
            return False
        self._execute(
            "UPDATE alerts SET feedback = 'wrong', feedback_ts = ? WHERE id = ?", (ts, alert_id)
        )
        return True

    def last_alert(self) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM alerts ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    # --- labels -------------------------------------------------------------
    def latest_sample_since(self, ts: float) -> sqlite3.Row | None:
        """The newest stored sample at or after ``ts``, if any."""
        rows = self.query(
            "SELECT * FROM samples WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT 1", (ts,)
        )
        return rows[0] if rows else None

    def insert_label(
        self,
        *,
        ts: float,
        label: str,
        source: str,
        sample_id: int | None,
        model_version: str | None,
        predicted_label: str | None,
    ) -> int:
        return self._execute(
            "INSERT INTO labels(ts, label, source, sample_id, model_version, predicted_label)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ts, label, source, sample_id, model_version, predicted_label),
        )

    # --- events -------------------------------------------------------------
    def log_event(self, ts: float, kind: str, detail: str | None = None) -> int:
        return self._execute(
            "INSERT INTO events(ts, kind, detail) VALUES (?, ?, ?)", (ts, kind, detail)
        )

    # --- guided recordings --------------------------------------------------
    def insert_recorded_sample(
        self,
        *,
        session: str,
        ts: float,
        step: int,
        label: str,
        confidence: float,
        raw: Mapping[str, float],
        features: Mapping[str, float],
        landmarks: list[list[float]],
        calibration_id: int | None,
    ) -> int:
        return self._execute(
            "INSERT INTO recorded_samples(session, ts, step, label, confidence, raw_json,"
            " features_json, landmarks_json, calibration_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session,
                ts,
                step,
                label,
                confidence,
                _to_json(raw),
                _to_json(features),
                _to_json(landmarks),
                calibration_id,
            ),
        )

    def recorded_sessions(self) -> list[str]:
        """Recording session ids, oldest first."""
        rows = self.query("SELECT session FROM recorded_samples GROUP BY session ORDER BY MIN(ts)")
        return [row["session"] for row in rows]

    def recorded_samples(self, sessions: list[str] | None = None) -> list[dict[str, Any]]:
        """Labelled samples in time order, with features decoded (missing -> NaN)."""
        sql = "SELECT * FROM recorded_samples"
        params: tuple[Any, ...] = ()
        if sessions:
            sql += f" WHERE session IN ({','.join('?' * len(sessions))})"
            params = tuple(sessions)
        rows = self.query(sql + " ORDER BY ts", params)
        return [
            {
                "session": row["session"],
                "ts": row["ts"],
                "step": row["step"],
                "label": row["label"],
                "confidence": row["confidence"],
                "features": _from_json(row["features_json"]),
            }
            for row in rows
        ]
