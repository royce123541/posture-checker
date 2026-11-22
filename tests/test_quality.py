from __future__ import annotations

import numpy as np
import pytest

from posture_checker.capture import quality as qual
from posture_checker.synthetic import make_landmarks

GOOD_LIGHT = {"brightness": 120.0, "contrast": 45.0}


def test_clear_frontal_face_is_fully_trusted() -> None:
    quality = qual.assess(make_landmarks(), **GOOD_LIGHT)
    assert quality.confidence == 1.0
    assert quality.reasons == ()


def test_no_person_has_zero_confidence() -> None:
    quality = qual.assess(None, **GOOD_LIGHT)
    assert quality.confidence == 0.0
    assert quality.reasons == ("no_person",)


@pytest.mark.parametrize(
    ("brightness", "reason"),
    [(25.0, "too_dark"), (240.0, "too_bright")],
)
def test_bad_lighting_lowers_confidence(brightness, reason) -> None:
    quality = qual.assess(make_landmarks(), brightness=brightness, contrast=45.0)
    assert quality.confidence < 0.6
    assert reason in quality.reasons


def test_flat_image_lowers_confidence() -> None:
    quality = qual.assess(make_landmarks(), brightness=120.0, contrast=5.0)
    assert quality.confidence < 0.6
    assert "low_contrast" in quality.reasons


def test_turned_head_lowers_confidence() -> None:
    quality = qual.assess(make_landmarks(), **GOOD_LIGHT, yaw_asymmetry=0.55)
    assert quality.confidence < 0.6
    assert "head_turned" in quality.reasons


def test_small_head_turn_is_tolerated() -> None:
    assert qual.assess(make_landmarks(), **GOOD_LIGHT, yaw_asymmetry=0.15).confidence == 1.0


def test_unclear_face_lowers_confidence() -> None:
    quality = qual.assess(make_landmarks(face_visibility=0.6), **GOOD_LIGHT)
    assert quality.confidence < 0.6
    assert "face_not_clear" in quality.reasons


def test_face_at_frame_edge_is_flagged() -> None:
    landmarks = make_landmarks()
    landmarks[:, 1] -= 0.39  # push the eyes to the very top of the frame
    quality = qual.assess(landmarks, **GOOD_LIGHT)
    assert "face_at_edge" in quality.reasons
    assert quality.confidence < 0.6


def test_missing_shoulders_reduce_but_do_not_block() -> None:
    quality = qual.assess(make_landmarks(), **GOOD_LIGHT, shoulders_ok=False)
    assert "shoulders_not_visible" in quality.reasons
    assert 0.6 <= quality.confidence < 1.0


def test_frame_stats_on_known_images() -> None:
    dark = np.full((480, 640, 3), 10, dtype=np.uint8)
    brightness, contrast = qual.frame_stats(dark)
    assert brightness == pytest.approx(10.0, abs=0.5)
    assert contrast == pytest.approx(0.0, abs=0.5)

    half = np.zeros((480, 640, 3), dtype=np.uint8)
    half[:, 320:] = 200
    brightness, contrast = qual.frame_stats(half)
    assert brightness == pytest.approx(100.0, abs=2.0)
    assert contrast == pytest.approx(100.0, abs=2.0)


def test_empty_dark_frame_is_cannot_see_not_nobody_there() -> None:
    assert qual.assess(None, brightness=12.0, contrast=3.0).reasons == ("no_person", "too_dark")
    assert qual.assess(None, brightness=250.0, contrast=3.0).reasons == ("no_person", "too_bright")
    assert qual.assess(None, **GOOD_LIGHT).reasons == ("no_person",)
