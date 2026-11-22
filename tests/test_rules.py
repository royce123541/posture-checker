from __future__ import annotations

import pytest

from posture_checker import features as feat
from posture_checker.inference import FORWARD_HEAD, GOOD, LABELS, SLOUCH, TOO_CLOSE
from posture_checker.inference.rules import RulesModel
from posture_checker.synthetic import make_landmarks


def predict(baseline, sensitivity: float = 1.0, **pose):
    rel = feat.relative_features(feat.raw_features(make_landmarks(**pose)), baseline)
    return RulesModel(sensitivity).predict(rel)


def test_calibrated_posture_is_good(baseline) -> None:
    prediction = predict(baseline)
    assert prediction.label == GOOD
    assert prediction.model_version == "rules-v0"
    assert set(prediction.scores) == set(LABELS)
    assert prediction.scores[GOOD] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("pose", "expected"),
    [
        ({"closeness": 1.35}, TOO_CLOSE),
        ({"head_forward": 0.18}, FORWARD_HEAD),
        ({"head_drop": 0.7, "shoulder_drop": 0.3}, SLOUCH),
        ({"closeness": 1.05}, GOOD),  # small shifts are normal
        ({"head_drop": 0.2}, GOOD),
    ],
)
def test_each_posture_gets_its_label(baseline, pose, expected) -> None:
    assert predict(baseline, **pose).label == expected


def test_moving_the_chair_in_is_not_called_forward_head(baseline) -> None:
    # 15% closer overall: under the too-close threshold, and the head did not
    # move relative to the shoulders, so nothing should fire.
    assert predict(baseline, closeness=1.15).label == GOOD


def test_sensitivity_scales_thresholds(baseline) -> None:
    pose = {"head_forward": 0.08}
    assert predict(baseline, sensitivity=1.0, **pose).label == GOOD
    assert predict(baseline, sensitivity=2.0, **pose).label == FORWARD_HEAD
    strong = {"head_forward": 0.15}
    assert predict(baseline, sensitivity=1.0, **strong).label == FORWARD_HEAD
    assert predict(baseline, sensitivity=0.5, **strong).label == GOOD


def test_firing_point_maps_to_score_half(baseline) -> None:
    below = predict(baseline, closeness=1.20).scores[TOO_CLOSE]
    above = predict(baseline, closeness=1.30).scores[TOO_CLOSE]
    assert below < 0.5 < above


def test_face_only_fallback_when_shoulders_are_hidden(baseline) -> None:
    # Without shoulders a lean-in can only be seen as a larger face.
    prediction = predict(baseline, head_forward=0.18, shoulder_visibility=0.1)
    assert prediction.label == FORWARD_HEAD
    assert predict(baseline, shoulder_visibility=0.1).label == GOOD


def test_rejects_non_positive_sensitivity() -> None:
    with pytest.raises(ValueError):
        RulesModel(0)
