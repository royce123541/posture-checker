"""Turn pose landmarks into posture measurements.

Two layers:

* **Raw features** describe one frame on its own (how big the face is, where
  it sits in the frame, how far the head is above the shoulders, ...).
* **Relative features** compare a frame with the user's calibrated "good
  posture" baseline. Models and rules only ever see relative features, so a
  recalibration adapts them to a new desk without retraining.

A single front-facing webcam cannot see the neck from the side, so nothing
here measures a true neck angle. Forward head and slouch are inferred from
proxies: the face growing relative to the shoulders, the head dropping in the
frame, and the head sinking towards the shoulder line.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np

# BlazePose landmark indices (the 13 upper-body points are all we use/store).
NOSE = 0
LEFT_EYE_OUTER = 3
RIGHT_EYE_OUTER = 6
LEFT_EAR = 7
RIGHT_EAR = 8
LEFT_SHOULDER = 11
RIGHT_SHOULDER = 12
UPPER_BODY_COUNT = 13

X, Y, Z, VIS = 0, 1, 2, 3

#: Shoulders below this visibility are treated as not measured.
SHOULDER_MIN_VISIBILITY = 0.5

RAW_FEATURES = (
    "face_scale",
    "face_x",
    "face_y",
    "pitch_ratio",
    "yaw_asymmetry",
    "head_roll_deg",
    "shoulders_ok",
    "shoulder_width",
    "shoulder_y",
    "shoulder_tilt_deg",
    "neck_ratio",
    "head_forward_z",
)

RELATIVE_FEATURES = (
    "face_scale_ratio",
    "face_drop",
    "pitch_delta",
    "yaw_asymmetry",
    "head_roll_delta",
    "shoulders_ok",
    "shoulder_width_ratio",
    "head_vs_shoulder_ratio",
    "shoulder_drop",
    "shoulder_tilt_delta",
    "neck_ratio_change",
    "head_forward_z_delta",
)

#: Value used for shoulder-based features when the shoulders are not visible.
MISSING = float("nan")


def _isotropic(landmarks: np.ndarray, aspect: float) -> np.ndarray:
    """Rescale x (and z) so distances use one unit: the image height."""
    pts = landmarks[:, :3].astype(float).copy()
    pts[:, X] *= aspect
    pts[:, Z] *= aspect
    return pts


def _line_angle_deg(right: np.ndarray, left: np.ndarray) -> float:
    """Tilt of the line from the person's right point to their left point.

    In an unmirrored camera image the person's left side has the larger x, so
    a level line gives 0 degrees.
    """
    return math.degrees(math.atan2(left[Y] - right[Y], left[X] - right[X]))


def raw_features(landmarks: np.ndarray, aspect: float = 4 / 3) -> dict[str, float]:
    """Measure one frame.

    ``landmarks`` is an ``(N>=13, 4)`` array of ``x, y, z, visibility`` with x
    and y normalised to the image size (y grows downwards). ``aspect`` is the
    frame's width divided by its height.
    """
    if landmarks.ndim != 2 or landmarks.shape[0] < UPPER_BODY_COUNT or landmarks.shape[1] < 4:
        raise ValueError(f"expected at least a (13, 4) landmark array, got {landmarks.shape}")

    pts = _isotropic(landmarks, aspect)
    nose = pts[NOSE]
    eye_l, eye_r = pts[LEFT_EYE_OUTER], pts[RIGHT_EYE_OUTER]
    eye_mid = (eye_l + eye_r) / 2
    face_scale = float(np.linalg.norm(eye_l[:2] - eye_r[:2]))
    if face_scale <= 1e-6:
        raise ValueError("degenerate face: eye landmarks coincide")

    # Yaw: when the head turns, the nose moves towards one eye in the image.
    to_left = abs(eye_l[X] - nose[X])
    to_right = abs(nose[X] - eye_r[X])
    yaw_asymmetry = (to_left - to_right) / max(to_left + to_right, 1e-6)

    features: dict[str, float] = {
        "face_scale": face_scale,
        "face_x": float(eye_mid[X] / aspect),
        "face_y": float(eye_mid[Y]),
        "pitch_ratio": float((nose[Y] - eye_mid[Y]) / face_scale),
        "yaw_asymmetry": float(yaw_asymmetry),
        "head_roll_deg": _line_angle_deg(eye_r, eye_l),
    }

    sh_l, sh_r = pts[LEFT_SHOULDER], pts[RIGHT_SHOULDER]
    shoulder_vis = min(landmarks[LEFT_SHOULDER, VIS], landmarks[RIGHT_SHOULDER, VIS])
    shoulder_width = float(np.linalg.norm(sh_l[:2] - sh_r[:2]))
    shoulders_ok = shoulder_vis >= SHOULDER_MIN_VISIBILITY and shoulder_width > 1e-6
    features["shoulders_ok"] = 1.0 if shoulders_ok else 0.0

    if shoulders_ok:
        sh_mid = (sh_l + sh_r) / 2
        ear_mid = (pts[LEFT_EAR] + pts[RIGHT_EAR]) / 2
        features.update(
            shoulder_width=shoulder_width,
            shoulder_y=float(sh_mid[Y]),
            shoulder_tilt_deg=_line_angle_deg(sh_r, sh_l),
            # How far the ears sit above the shoulder line, in shoulder widths.
            neck_ratio=float((sh_mid[Y] - ear_mid[Y]) / shoulder_width),
            # BlazePose depth estimate of nose vs shoulders (negative = nose
            # closer to the camera). Noisy, but it is the only depth cue.
            head_forward_z=float((nose[Z] - sh_mid[Z]) / shoulder_width),
        )
    else:
        for name in (
            "shoulder_width",
            "shoulder_y",
            "shoulder_tilt_deg",
            "neck_ratio",
            "head_forward_z",
        ):
            features[name] = MISSING
    return features


def _ok(value: float) -> bool:
    return value is not None and not math.isnan(value)


def relative_features(raw: Mapping[str, float], baseline: Mapping[str, float]) -> dict[str, float]:
    """Compare one frame's raw features with the calibrated baseline.

    Distances are expressed in "baseline face widths" (the outer-eye distance
    at calibration, roughly 9-10 cm in the real world), which keeps the numbers
    comparable across cameras and seating distances.
    """
    base_scale = baseline["face_scale"]
    if not _ok(base_scale) or base_scale <= 0:
        raise ValueError("baseline has no usable face_scale; recalibrate")

    face_scale_ratio = raw["face_scale"] / base_scale
    rel: dict[str, float] = {
        # > 1 means the face is bigger than at calibration, i.e. closer.
        "face_scale_ratio": face_scale_ratio,
        # > 0 means the head sits lower in the frame than at calibration.
        "face_drop": (raw["face_y"] - baseline["face_y"]) / base_scale,
        "pitch_delta": raw["pitch_ratio"] - baseline["pitch_ratio"],
        "yaw_asymmetry": raw["yaw_asymmetry"],
        "head_roll_delta": raw["head_roll_deg"] - baseline["head_roll_deg"],
    }

    both_have_shoulders = (
        raw.get("shoulders_ok", 0.0) >= 0.5
        and _ok(baseline.get("shoulder_width", MISSING))
        and baseline["shoulder_width"] > 0
        and _ok(baseline.get("neck_ratio", MISSING))
    )
    rel["shoulders_ok"] = 1.0 if both_have_shoulders else 0.0
    if both_have_shoulders:
        shoulder_width_ratio = raw["shoulder_width"] / baseline["shoulder_width"]
        base_neck = baseline["neck_ratio"]
        rel.update(
            shoulder_width_ratio=shoulder_width_ratio,
            # > 1 means the head came closer to the camera than the shoulders
            # did: the signature of a forward head rather than a moved chair.
            head_vs_shoulder_ratio=face_scale_ratio / shoulder_width_ratio,
            shoulder_drop=(raw["shoulder_y"] - baseline["shoulder_y"]) / base_scale,
            shoulder_tilt_delta=raw["shoulder_tilt_deg"] - baseline["shoulder_tilt_deg"],
            # < 0 means the head has sunk towards the shoulder line.
            neck_ratio_change=(raw["neck_ratio"] / base_neck - 1.0)
            if abs(base_neck) > 1e-6
            else 0.0,
            head_forward_z_delta=raw["head_forward_z"] - baseline["head_forward_z"],
        )
    else:
        for name in (
            "shoulder_width_ratio",
            "head_vs_shoulder_ratio",
            "shoulder_drop",
            "shoulder_tilt_delta",
            "neck_ratio_change",
            "head_forward_z_delta",
        ):
            rel[name] = MISSING
    return rel


def upper_body(landmarks: np.ndarray, decimals: int = 4) -> list[list[float]]:
    """The 13 upper-body landmarks, rounded, for storage.

    Keeping these numbers (never the image) lets features be recomputed later
    when the feature code changes.
    """
    return np.round(landmarks[:UPPER_BODY_COUNT, :4].astype(float), decimals).tolist()
