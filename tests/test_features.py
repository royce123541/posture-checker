from __future__ import annotations

import math

import numpy as np
import pytest

from posture_checker import features as feat
from posture_checker.synthetic import make_landmarks


def test_baseline_pose_is_level_and_centred() -> None:
    raw = feat.raw_features(make_landmarks())
    assert raw["face_scale"] == pytest.approx(0.14, rel=1e-3)
    assert raw["face_x"] == pytest.approx(0.5, abs=1e-3)
    assert raw["head_roll_deg"] == pytest.approx(0.0, abs=1e-3)
    assert raw["shoulder_tilt_deg"] == pytest.approx(0.0, abs=1e-3)
    assert raw["yaw_asymmetry"] == pytest.approx(0.0, abs=1e-3)
    assert raw["shoulders_ok"] == 1.0
    assert raw["neck_ratio"] > 0


def test_same_pose_gives_neutral_relative_features(baseline) -> None:
    rel = feat.relative_features(baseline, baseline)
    assert rel["face_scale_ratio"] == pytest.approx(1.0)
    assert rel["head_vs_shoulder_ratio"] == pytest.approx(1.0)
    assert rel["face_drop"] == pytest.approx(0.0)
    assert rel["neck_ratio_change"] == pytest.approx(0.0)


def test_moving_closer_scales_face_and_shoulders_together(baseline) -> None:
    rel = feat.relative_features(feat.raw_features(make_landmarks(closeness=1.3)), baseline)
    assert rel["face_scale_ratio"] == pytest.approx(1.3, rel=1e-3)
    assert rel["shoulder_width_ratio"] == pytest.approx(1.3, rel=1e-3)
    # The whole body moved, so the head did not move relative to the shoulders.
    assert rel["head_vs_shoulder_ratio"] == pytest.approx(1.0, rel=1e-3)


def test_forward_head_grows_face_but_not_shoulders(baseline) -> None:
    rel = feat.relative_features(feat.raw_features(make_landmarks(head_forward=0.2)), baseline)
    assert rel["face_scale_ratio"] == pytest.approx(1.2, rel=1e-3)
    assert rel["shoulder_width_ratio"] == pytest.approx(1.0, rel=1e-3)
    assert rel["head_vs_shoulder_ratio"] == pytest.approx(1.2, rel=1e-3)
    assert rel["head_forward_z_delta"] < 0  # nose closer to the camera


def test_head_drop_is_measured_in_face_widths(baseline) -> None:
    rel = feat.relative_features(feat.raw_features(make_landmarks(head_drop=0.6)), baseline)
    assert rel["face_drop"] == pytest.approx(0.6, rel=1e-3)
    assert rel["neck_ratio_change"] < 0  # head sank towards the shoulders


def test_yaw_moves_asymmetry_away_from_zero() -> None:
    left = feat.raw_features(make_landmarks(yaw=0.8))["yaw_asymmetry"]
    right = feat.raw_features(make_landmarks(yaw=-0.8))["yaw_asymmetry"]
    assert abs(left) > 0.5
    assert left == pytest.approx(-right, abs=1e-6)


def test_head_roll_follows_tilted_eye_line() -> None:
    landmarks = make_landmarks()
    landmarks[feat.LEFT_EYE_OUTER, feat.Y] += 0.02  # person's left eye lower in the image
    assert feat.raw_features(landmarks)["head_roll_deg"] > 5


def test_invisible_shoulders_become_missing_not_wrong(baseline) -> None:
    raw = feat.raw_features(make_landmarks(shoulder_visibility=0.2))
    assert raw["shoulders_ok"] == 0.0
    assert math.isnan(raw["neck_ratio"])
    rel = feat.relative_features(raw, baseline)
    assert rel["shoulders_ok"] == 0.0
    assert math.isnan(rel["head_vs_shoulder_ratio"])
    assert rel["face_scale_ratio"] == pytest.approx(1.0)  # face features still work


def test_aspect_ratio_is_respected() -> None:
    # The same landmarks in a 16:9 frame describe a physically wider face.
    landmarks = make_landmarks()
    assert (
        feat.raw_features(landmarks, aspect=16 / 9)["face_scale"]
        > feat.raw_features(landmarks, aspect=4 / 3)["face_scale"]
    )


def test_rejects_malformed_input() -> None:
    with pytest.raises(ValueError):
        feat.raw_features(np.zeros((5, 4)))
    with pytest.raises(ValueError):
        feat.raw_features(np.zeros((33, 4)))  # all points coincide


def test_relative_features_need_a_usable_baseline(baseline) -> None:
    with pytest.raises(ValueError):
        feat.relative_features(baseline, {**baseline, "face_scale": math.nan})


def test_upper_body_keeps_thirteen_rounded_points() -> None:
    stored = feat.upper_body(make_landmarks())
    assert len(stored) == 13
    assert all(len(point) == 4 for point in stored)
    assert stored[0][0] == round(stored[0][0], 4)
