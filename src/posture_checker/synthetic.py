"""Synthetic pose landmarks.

Tests and the CI pipeline need realistic landmark data but can use neither a
webcam nor recorded images. This module builds the 33-point BlazePose array for
a simple geometric "person" whose posture is controlled by a few parameters.
"""

from __future__ import annotations

import numpy as np

from posture_checker.features import (
    LEFT_EAR,
    LEFT_EYE_OUTER,
    LEFT_SHOULDER,
    NOSE,
    RIGHT_EAR,
    RIGHT_EYE_OUTER,
    RIGHT_SHOULDER,
)

ASPECT = 4 / 3
# Baseline geometry in image-height units (x is converted on the way out).
_FACE_SCALE = 0.14  # outer-eye distance
_EYE_Y = 0.40
_CENTRE_X = 0.5 * ASPECT
_SHOULDER_WIDTH = 4.3 * _FACE_SCALE
_SHOULDER_DY = 1.9 * _FACE_SCALE  # shoulder line below the eye line
_FACE_POINTS = (NOSE, 1, 2, LEFT_EYE_OUTER, 4, 5, RIGHT_EYE_OUTER, LEFT_EAR, RIGHT_EAR, 9, 10)


def make_landmarks(
    closeness: float = 1.0,
    head_forward: float = 0.0,
    head_drop: float = 0.0,
    shoulder_drop: float = 0.0,
    yaw: float = 0.0,
    face_visibility: float = 0.99,
    shoulder_visibility: float = 0.95,
    noise: float = 0.0,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Build a ``(33, 4)`` landmark array.

    * ``closeness``: 1.0 is the calibrated distance; 1.3 means the whole body
      appears 30% larger (the chair moved in).
    * ``head_forward``: extra growth of the face only (0.15 = head 15% closer
      than the shoulders), as in a forward-head posture.
    * ``head_drop`` / ``shoulder_drop``: how far the head / shoulders sit lower
      in the frame, in face widths.
    * ``yaw``: -1..1, moves the nose towards one eye as when turning the head.
    """
    pts = np.zeros((33, 4), dtype=np.float64)
    pts[:, 3] = 0.1  # hips, arms, legs: not visible at a desk

    eye_mid = np.array([_CENTRE_X, _EYE_Y])
    half = _FACE_SCALE / 2

    def at(dx: float, dy: float) -> np.ndarray:
        """A point offset from the midpoint between the eyes."""
        return eye_mid + np.array([dx, dy])

    face = {
        LEFT_EYE_OUTER: at(half, 0.0),
        RIGHT_EYE_OUTER: at(-half, 0.0),
        2: at(half * 0.65, 0.0),
        5: at(-half * 0.65, 0.0),
        1: at(half * 0.35, 0.0),
        4: at(-half * 0.35, 0.0),
        NOSE: at(yaw * half * 0.8, 0.5 * _FACE_SCALE),
        LEFT_EAR: at(0.75 * _FACE_SCALE, 0.05 * _FACE_SCALE),
        RIGHT_EAR: at(-0.75 * _FACE_SCALE, 0.05 * _FACE_SCALE),
        9: at(0.25 * _FACE_SCALE, 0.9 * _FACE_SCALE),
        10: at(-0.25 * _FACE_SCALE, 0.9 * _FACE_SCALE),
    }
    # Forward head: the face grows about its own centre and drops with the head.
    for index, point in face.items():
        grown = eye_mid + (point - eye_mid) * (1.0 + head_forward)
        grown[1] += head_drop * _FACE_SCALE
        pts[index, :2] = grown
        pts[index, 2] = -0.6 - head_forward
        pts[index, 3] = face_visibility

    shoulder_y = _EYE_Y + _SHOULDER_DY + shoulder_drop * _FACE_SCALE
    pts[LEFT_SHOULDER, :2] = [_CENTRE_X + _SHOULDER_WIDTH / 2, shoulder_y]
    pts[RIGHT_SHOULDER, :2] = [_CENTRE_X - _SHOULDER_WIDTH / 2, shoulder_y]
    pts[[LEFT_SHOULDER, RIGHT_SHOULDER], 2] = -0.2
    pts[[LEFT_SHOULDER, RIGHT_SHOULDER], 3] = shoulder_visibility

    # Moving closer scales the whole body about the image centre.
    centre = np.array([_CENTRE_X, 0.5])
    upper = [*_FACE_POINTS, LEFT_SHOULDER, RIGHT_SHOULDER]
    pts[upper, :2] = centre + (pts[upper, :2] - centre) * closeness

    if noise > 0:
        rng = rng or np.random.default_rng(0)
        pts[upper, :3] += rng.normal(0.0, noise, size=(len(upper), 3))

    # Convert x and z from image-height units to MediaPipe's width-normalised x.
    pts[:, 0] /= ASPECT
    pts[:, 2] /= ASPECT
    return pts.astype(np.float32)
