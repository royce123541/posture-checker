"""Decide how much a single sample can be trusted.

Bad lighting, a turned head or a face at the edge of the frame all make the
landmarks unreliable. Rather than guess, the app scores each sample's
confidence, and low-confidence samples never count towards an alert.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from posture_checker.features import LEFT_EYE_OUTER, NOSE, RIGHT_EYE_OUTER, VIS, X, Y

# Thresholds on 0-255 grey levels, measured on a downsampled frame.
DARK_ZERO, DARK_FULL = 20.0, 60.0  # mean brightness: no trust below, full trust above
BRIGHT_FULL, BRIGHT_ZERO = 215.0, 245.0
CONTRAST_FULL = 25.0  # grey-level standard deviation giving full trust
YAW_FULL, YAW_ZERO = 0.25, 0.60  # |yaw_asymmetry|: full trust below, none above
VIS_ZERO, VIS_FULL = 0.5, 0.9
EDGE_MARGIN = 0.02
#: Below this lighting score an empty-looking frame proves nothing about presence.
BLIND_LIGHT_SCORE = 0.5
#: Without shoulders only face-based features remain, so trust a bit less.
NO_SHOULDERS_FACTOR = 0.85


@dataclass(frozen=True)
class Quality:
    confidence: float
    reasons: tuple[str, ...]


def _ramp(value: float, zero: float, full: float) -> float:
    """Linear 0..1 score: 0 at ``zero``, 1 at ``full`` (either order)."""
    if full == zero:
        return 1.0
    return float(np.clip((value - zero) / (full - zero), 0.0, 1.0))


def frame_stats(frame_bgr: np.ndarray) -> tuple[float, float]:
    """Mean brightness and contrast of a frame, from a coarse subsample."""
    small = frame_bgr[::8, ::8].astype(np.float32)
    grey = small @ np.array([0.114, 0.587, 0.299], dtype=np.float32)
    return float(grey.mean()), float(grey.std())


def assess(
    landmarks: np.ndarray | None,
    brightness: float,
    contrast: float,
    yaw_asymmetry: float = 0.0,
    shoulders_ok: bool = True,
) -> Quality:
    """Score one sample. Confidence is the product of independent checks."""
    light = min(
        _ramp(brightness, DARK_ZERO, DARK_FULL), _ramp(brightness, BRIGHT_ZERO, BRIGHT_FULL)
    )
    light_reason = "too_dark" if brightness < DARK_FULL else "too_bright"

    if landmarks is None:
        # Nobody found. If the picture is too dark or blown out to show a person
        # anyway, say so: "cannot see" is a different fact from "nobody there".
        if light < BLIND_LIGHT_SCORE:
            return Quality(0.0, ("no_person", light_reason))
        return Quality(0.0, ("no_person",))

    reasons: list[str] = []
    confidence = 1.0

    if light < 1.0:
        reasons.append(light_reason)
    confidence *= light

    contrast_score = _ramp(contrast, 0.0, CONTRAST_FULL)
    if contrast_score < 1.0:
        reasons.append("low_contrast")
    confidence *= contrast_score

    face = landmarks[[NOSE, LEFT_EYE_OUTER, RIGHT_EYE_OUTER]]
    visibility = _ramp(float(face[:, VIS].min()), VIS_ZERO, VIS_FULL)
    if visibility < 1.0:
        reasons.append("face_not_clear")
    confidence *= visibility

    inside = (
        (face[:, X] > EDGE_MARGIN)
        & (face[:, X] < 1 - EDGE_MARGIN)
        & (face[:, Y] > EDGE_MARGIN)
        & (face[:, Y] < 1 - EDGE_MARGIN)
    )
    if not bool(inside.all()):
        reasons.append("face_at_edge")
        confidence *= 0.3

    yaw = _ramp(abs(yaw_asymmetry), YAW_ZERO, YAW_FULL)
    if yaw < 1.0:
        reasons.append("head_turned")
    confidence *= yaw

    if not shoulders_ok:
        reasons.append("shoulders_not_visible")
        confidence *= NO_SHOULDERS_FACTOR

    return Quality(round(float(confidence), 4), tuple(reasons))
