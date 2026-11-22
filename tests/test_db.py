from __future__ import annotations

import json
import math

from posture_checker.storage.db import Database


def test_calibration_round_trip_preserves_missing_values(db, calibration) -> None:
    baseline = {**calibration.baseline, "neck_ratio": math.nan}
    stored_id = db.insert_calibration(
        type(calibration)(**{**vars(calibration), "baseline": baseline})
    )
    loaded = db.latest_calibration()
    assert loaded is not None
    assert loaded.id == stored_id
    assert loaded.baseline["face_scale"] == calibration.baseline["face_scale"]
    assert math.isnan(loaded.baseline["neck_ratio"])
    # Stored as strict JSON (null), not the non-standard NaN token.
    raw = db.query("SELECT baseline_json FROM calibrations")[0]["baseline_json"]
    assert "NaN" not in raw
    assert json.loads(raw)["neck_ratio"] is None


def test_latest_calibration_wins(db, calibration) -> None:
    assert db.latest_calibration() is None
    db.insert_calibration(calibration)
    second = db.insert_calibration(calibration)
    assert db.latest_calibration().id == second


def test_sample_records_model_lineage(db) -> None:
    db.insert_sample(
        ts=1.0,
        state="present",
        label="slouch",
        confidence=0.9,
        reasons=("shoulders_not_visible",),
        scores={"good": 0.2, "slouch": 0.8},
        features={"face_drop": 0.6, "neck_ratio_change": math.nan},
        landmarks=[[0.5, 0.4, -0.1, 0.99]],
        brightness=118.0,
        model_version="rules-v0",
        pose_model="pose_landmarker_lite",
        calibration_id=None,
        latency_ms=21.5,
    )
    row = db.query("SELECT * FROM samples")[0]
    assert row["model_version"] == "rules-v0"
    assert row["pose_model"] == "pose_landmarker_lite"
    assert row["reasons"] == "shoulders_not_visible"
    assert json.loads(row["features_json"]) == {"face_drop": 0.6, "neck_ratio_change": None}


def test_wrong_alert_feedback(db) -> None:
    alert_id = db.insert_alert(
        ts=10.0,
        label="slouch",
        bad_fraction=0.9,
        window_s=60.0,
        model_version="rules-v0",
        calibration_id=None,
    )
    assert db.last_alert()["feedback"] is None
    assert db.mark_alert_wrong(alert_id, ts=15.0)
    alert = db.last_alert()
    assert alert["feedback"] == "wrong"
    assert alert["feedback_ts"] == 15.0
    assert not db.mark_alert_wrong(9999, ts=16.0)


def test_events_are_logged(db) -> None:
    db.log_event(1.0, "pause", "manual:1800s")
    row = db.query("SELECT kind, detail FROM events")[0]
    assert (row["kind"], row["detail"]) == ("pause", "manual:1800s")


def test_database_file_is_reopenable(tmp_path) -> None:
    path = tmp_path / "posture.db"
    first = Database(path)
    first.log_event(1.0, "start")
    first.close()
    second = Database(path)
    assert len(second.query("SELECT * FROM events")) == 1
    assert second.query("SELECT value FROM meta WHERE key='schema_version'")[0]["value"] == "1"
    second.close()
