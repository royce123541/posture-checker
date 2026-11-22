"""Guided recording and threshold fitting, end to end with fakes."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from conftest import FakeCamera, FakeClock, FakePose
from posture_checker import features as feat
from posture_checker import paths
from posture_checker.cli import main
from posture_checker.inference import FORWARD_HEAD, GOOD, SLOUCH, TOO_CLOSE
from posture_checker.inference.rules import (
    BASELINE_VERSION,
    FITTED_FILE,
    RulesModel,
    Thresholds,
    deviations,
    load_active,
    save_fitted,
)
from posture_checker.mlops import fit_rules as fr
from posture_checker.recording import Step, run_recording
from posture_checker.storage.db import Database
from posture_checker.synthetic import make_landmarks

# A person whose "good" sits 0.4 face-widths below calibration (as measured on
# the owner's desk) and whose slouch is only a little lower: the case the
# default 0.45 threshold handles badly.
POSES = {
    GOOD: {"head_drop": 0.40},
    SLOUCH: {"head_drop": 0.75, "shoulder_drop": 0.2},
    FORWARD_HEAD: {"head_forward": 0.16, "head_drop": 0.4},
    TOO_CLOSE: {"closeness": 1.35, "head_drop": 0.4},
}
STEPS = (
    Step(GOOD, "good", 20),
    Step(SLOUCH, "slouch", 20),
    Step(FORWARD_HEAD, "forward", 20),
    Step(TOO_CLOSE, "close", 20),
    Step(GOOD, "good again", 20),
)


def record(db: Database, calibration, steps=STEPS, noise: float = 0.003, seed: int = 0) -> str:
    rng = np.random.default_rng(seed)
    clock = FakeClock()
    current: dict[str, dict] = {"pose": {}}

    def on_step(index: int, step: Step) -> None:
        current["pose"] = POSES[step.label]

    camera = FakeCamera()
    camera.open()
    summary = run_recording(
        camera,
        FakePose(lambda: make_landmarks(noise=noise, rng=rng, **current["pose"])),
        db,
        calibration,
        steps=steps,
        interval_s=0.5,
        settle_s=4.0,
        clock=clock,
        sleep=clock.advance,
        on_step=on_step,
    )
    return summary.session


def _rel(landmarks) -> dict[str, float]:
    base = feat.raw_features(make_landmarks())
    return feat.relative_features(feat.raw_features(landmarks), base)


@pytest.fixture
def recorded(db, calibration):
    db.insert_calibration(calibration)
    session = record(db, db.latest_calibration())
    return db, session


def test_recording_stores_labelled_numbers_after_settling(recorded) -> None:
    db, session = recorded
    rows = db.recorded_samples([session])
    counts = {label: sum(r["label"] == label for r in rows) for label in POSES}
    # 20 s per step at 0.5 s, minus 4 s of settling = 32 samples per step.
    assert counts == {GOOD: 64, SLOUCH: 32, FORWARD_HEAD: 32, TOO_CLOSE: 32}
    assert db.recorded_sessions() == [session]
    stored = db.query("SELECT raw_json, landmarks_json FROM recorded_samples LIMIT 1")[0]
    assert "face_scale" in json.loads(stored["raw_json"])
    assert len(json.loads(stored["landmarks_json"])) == 13
    assert [r["kind"] for r in db.query("SELECT kind FROM events")] == ["recording"]


def test_recording_skips_low_confidence_samples(db, calibration) -> None:
    db.insert_calibration(calibration)
    camera = FakeCamera(brightness=20)  # too dark to trust
    camera.open()
    clock = FakeClock()
    summary = run_recording(
        camera,
        FakePose(make_landmarks),
        db,
        db.latest_calibration(),
        steps=(Step(GOOD, "x", 10),),
        clock=clock,
        sleep=clock.advance,
    )
    assert summary.counts == {}
    assert summary.skipped_low_confidence > 0
    assert db.recorded_samples() == []


def test_time_blocked_split_keeps_early_for_fitting_and_late_for_testing(recorded) -> None:
    db, _ = recorded
    samples = db.recorded_samples()
    train, test = fr.time_blocked_split(samples)
    assert len(train) + len(test) == len(samples)
    for step in range(len(STEPS)):
        block_train = [s["ts"] for s in train if s["step"] == step]
        block_test = [s["ts"] for s in test if s["step"] == step]
        assert max(block_train) < min(block_test)
        total = len(block_train) + len(block_test)
        assert len(block_train) == pytest.approx(0.6 * total, abs=1)


def test_best_threshold_takes_the_middle_of_the_gap() -> None:
    threshold, balanced = fr.best_threshold([0.7, 0.8, 0.9], [0.3, 0.4, 0.5])
    assert balanced == 1.0
    assert threshold == pytest.approx(0.6)


def test_best_threshold_on_overlapping_classes() -> None:
    threshold, balanced = fr.best_threshold([0.45, 0.6, 0.7, 0.8], [0.3, 0.4, 0.5, 0.55])
    assert 0.5 < balanced < 1.0
    assert 0.4 < threshold < 0.7


def test_fitting_fixes_the_too_low_slouch_threshold(recorded) -> None:
    db, _ = recorded
    train, test = fr.time_blocked_split(db.recorded_samples())
    fitted, report = fr.fit_thresholds(train, Thresholds())
    # Moved up into the gap between good (0.40) and slouch (0.75).
    assert 0.45 < fitted.slouch_face_drop < 0.75
    by_name = {f.name: f for f in report}
    assert by_name["slouch_face_drop"].fitted
    # No shoulder-less samples, so that rule keeps its default.
    assert not by_name["forward_face_only_excess"].fitted
    assert fitted.forward_face_only_excess == Thresholds().forward_face_only_excess

    before = fr.evaluate(RulesModel(), test)
    after = fr.evaluate(RulesModel(thresholds=fitted, version="rules-v1"), test)
    assert after.accuracy > before.accuracy
    assert after.false_bad_rate < before.false_bad_rate
    assert fr.passes_gate(after, before)
    assert not fr.passes_gate(before, after)


def test_evaluation_counts() -> None:
    samples = [
        {"label": GOOD, "features": _rel(make_landmarks())},
        {"label": GOOD, "features": _rel(make_landmarks(head_drop=0.8))},
        {"label": SLOUCH, "features": _rel(make_landmarks(head_drop=0.8))},
        {"label": SLOUCH, "features": _rel(make_landmarks())},
    ]
    ev = fr.evaluate(RulesModel(), samples)
    assert ev.n == 4
    assert ev.accuracy == 0.5
    assert ev.false_bad_rate == 0.5
    assert ev.bad_detection_rate == 0.5
    assert ev.confusion[SLOUCH][GOOD] == 1
    with pytest.raises(ValueError):
        fr.evaluate(RulesModel(), [])


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("rules-v0", "rules-v1"),
        ("rules-v1", "rules-v2"),
        ("rules-v12", "rules-v13"),
        ("other", "rules-v1"),
    ],
)
def test_next_version(current, expected) -> None:
    assert fr.next_version(current) == expected


def test_thresholds_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Thresholds(slouch_face_drop=0)


def test_save_and_load_active_rules() -> None:
    assert load_active().version == BASELINE_VERSION
    save_fitted(Thresholds(slouch_face_drop=0.6), "rules-v1", {"sessions": ["s"]})
    model = load_active(sensitivity=1.5)
    assert model.version == "rules-v1"
    assert model.thresholds.slouch_face_drop == 0.6
    assert model.sensitivity == 1.5
    payload = json.loads((paths.models_dir() / FITTED_FILE).read_text())
    assert payload["metadata"] == {"sessions": ["s"]}


def test_fitted_rules_tag_their_predictions() -> None:
    save_fitted(Thresholds(), "rules-v3", {})
    assert load_active().predict(_rel(make_landmarks())).model_version == "rules-v3"


def test_fit_rules_command_reports_then_deploys(calibration, capsys) -> None:
    db = Database()
    db.insert_calibration(calibration)
    record(db, db.latest_calibration())
    db.close()

    assert main(["fit-rules"]) == 0
    out = capsys.readouterr().out
    assert "slouch_face_drop" in out
    assert "Candidate passes the gate" in out
    assert load_active().version == BASELINE_VERSION  # not deployed without --deploy

    assert main(["fit-rules", "--deploy"]) == 0
    assert "Deployed rules-v1" in capsys.readouterr().out
    deployed = load_active()
    assert deployed.version == "rules-v1"
    assert deployed.thresholds.slouch_face_drop > 0.45
    metadata = json.loads((paths.models_dir() / FITTED_FILE).read_text())["metadata"]
    assert metadata["previous_version"] == BASELINE_VERSION
    assert metadata["held_out_after"]["accuracy"] > metadata["held_out_before"]["accuracy"]


def test_gate_rejects_a_worse_candidate(calibration, capsys, monkeypatch) -> None:
    db = Database()
    db.insert_calibration(calibration)
    record(db, db.latest_calibration())
    db.close()
    assert main(["fit-rules", "--deploy"]) == 0
    capsys.readouterr()

    # Sabotage fitting: the candidate keeps a slouch threshold far too low.
    def bad_fit(train, current):
        return Thresholds(slouch_face_drop=0.05), []

    monkeypatch.setattr(fr, "fit_thresholds", bad_fit)
    assert main(["fit-rules", "--deploy"]) == 2
    assert "keeping rules-v1" in capsys.readouterr().out
    assert load_active().version == "rules-v1"


def test_fit_rules_without_recordings(capsys) -> None:
    assert main(["fit-rules"]) == 1
    assert "No recordings" in capsys.readouterr().err


def test_missing_shoulder_rules_are_nan_safe() -> None:
    dev = deviations(_rel(make_landmarks(shoulder_visibility=0.1)))
    assert math.isnan(dev["forward_vs_shoulders_excess"])
    assert not math.isnan(dev["forward_face_only_excess"])
