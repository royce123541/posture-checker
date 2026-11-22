from __future__ import annotations

import math

import numpy as np
import pytest

from conftest import FakeCamera, FakeClock, FakePose
from posture_checker import features as feat
from posture_checker.calibration import (
    CalibrationError,
    build_calibration,
    capture_calibration,
)
from posture_checker.synthetic import make_landmarks


def raws(n: int, **pose) -> list[dict[str, float]]:
    rng = np.random.default_rng(3)
    return [feat.raw_features(make_landmarks(noise=0.001, rng=rng, **pose)) for _ in range(n)]


def test_baseline_is_the_median_of_the_samples() -> None:
    calibration = build_calibration(raws(30), [0.9] * 30, ts=123.0)
    assert calibration.n_samples == 30
    assert calibration.ts == 123.0
    assert calibration.baseline["face_scale"] == pytest.approx(0.14, rel=0.02)
    assert calibration.spread["face_scale"] < 0.01
    assert calibration.has_shoulders


def test_outlier_frames_do_not_move_the_baseline() -> None:
    samples = raws(30)
    samples[5] = feat.raw_features(make_landmarks(closeness=2.0))  # one wild frame
    calibration = build_calibration(samples, [0.9] * 30, ts=0.0)
    assert calibration.baseline["face_scale"] == pytest.approx(0.14, rel=0.02)


def test_too_few_samples_is_rejected() -> None:
    with pytest.raises(CalibrationError, match="usable samples"):
        build_calibration(raws(4), [0.9] * 4, ts=0.0)


def test_low_confidence_is_rejected() -> None:
    with pytest.raises(CalibrationError, match="confidence"):
        build_calibration(raws(20), [0.4] * 20, ts=0.0)


def test_moving_during_calibration_is_rejected() -> None:
    moving = raws(10) + raws(10, closeness=1.4)
    with pytest.raises(CalibrationError, match="moved"):
        build_calibration(moving, [0.9] * 20, ts=0.0)


def test_hidden_shoulders_give_a_face_only_baseline() -> None:
    calibration = build_calibration(raws(20, shoulder_visibility=0.1), [0.9] * 20, ts=0.0)
    assert not calibration.has_shoulders
    assert math.isnan(calibration.baseline["neck_ratio"])
    assert calibration.baseline["face_scale"] > 0


def test_capture_runs_against_fakes() -> None:
    clock = FakeClock()
    camera = FakeCamera()
    camera.open()
    progress: list[tuple[float, int]] = []
    calibration = capture_calibration(
        camera,
        FakePose(make_landmarks),
        duration_s=5.0,
        interval_s=0.25,
        clock=clock,
        sleep=clock.advance,
        on_progress=lambda fraction, usable: progress.append((fraction, usable)),
    )
    assert calibration.n_samples == 20
    assert progress[-1][1] == 20
    assert 0 < progress[0][0] <= progress[-1][0] <= 1.0 or progress[0][0] == 0


def test_capture_with_nobody_there_fails_cleanly() -> None:
    clock = FakeClock()
    camera = FakeCamera()
    camera.open()
    with pytest.raises(CalibrationError):
        capture_calibration(
            camera, FakePose(lambda: None), duration_s=2.0, clock=clock, sleep=clock.advance
        )
