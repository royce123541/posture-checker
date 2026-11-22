"""Calibration: record what "good posture" looks like for this desk setup.

The user sits upright for a few seconds; the baseline is the median of each raw
feature over that period. Medians (and MAD for spread) shrug off the odd bad
frame.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from posture_checker import features as feat
from posture_checker.capture import quality as qual
from posture_checker.capture.camera import FrameSource
from posture_checker.capture.pose import PoseBackend
from posture_checker.features import RAW_FEATURES

MIN_SAMPLES = 10
MIN_MEAN_CONFIDENCE = 0.6
#: Reject the calibration if the face size varied more than this (relative
#: spread): the user was moving, so there is no single baseline to record.
MAX_FACE_SCALE_SPREAD = 0.08
#: Shoulder features enter the baseline only if most samples had shoulders.
MIN_SHOULDER_SHARE = 0.5

_SHOULDER_FEATURES = (
    "shoulder_width",
    "shoulder_y",
    "shoulder_tilt_deg",
    "neck_ratio",
    "head_forward_z",
)


#: Samples below this confidence are left out of the baseline.
SAMPLE_MIN_CONFIDENCE = 0.6


class CalibrationError(Exception):
    """The captured samples are not good enough to calibrate from."""


@dataclass(frozen=True)
class Calibration:
    baseline: dict[str, float]
    spread: dict[str, float]
    n_samples: int
    mean_confidence: float
    ts: float
    id: int | None = None

    @property
    def has_shoulders(self) -> bool:
        return not math.isnan(self.baseline.get("shoulder_width", math.nan))


def build_calibration(
    raw_samples: Sequence[Mapping[str, float]],
    confidences: Sequence[float],
    ts: float,
) -> Calibration:
    """Combine captured samples into a baseline, or raise ``CalibrationError``."""
    if len(raw_samples) != len(confidences):
        raise ValueError("raw_samples and confidences must have the same length")
    if len(raw_samples) < MIN_SAMPLES:
        raise CalibrationError(
            f"only {len(raw_samples)} usable samples (need {MIN_SAMPLES}); "
            "make sure your face is visible and well lit"
        )
    mean_confidence = float(np.mean(confidences))
    if mean_confidence < MIN_MEAN_CONFIDENCE:
        raise CalibrationError(
            f"detection confidence was too low ({mean_confidence:.2f}); "
            "improve the lighting or face the camera"
        )

    shoulder_share = float(np.mean([s.get("shoulders_ok", 0.0) >= 0.5 for s in raw_samples]))
    use_shoulders = shoulder_share >= MIN_SHOULDER_SHARE

    baseline: dict[str, float] = {}
    spread: dict[str, float] = {}
    for name in RAW_FEATURES:
        if name in _SHOULDER_FEATURES and not use_shoulders:
            baseline[name] = spread[name] = math.nan
            continue
        values = np.array([s.get(name, math.nan) for s in raw_samples], dtype=float)
        values = values[~np.isnan(values)]
        median = float(np.median(values))
        baseline[name] = median
        # 1.4826 * MAD estimates the standard deviation for normal data.
        spread[name] = float(1.4826 * np.median(np.abs(values - median)))
    baseline["shoulders_ok"] = 1.0 if use_shoulders else 0.0

    relative_spread = spread["face_scale"] / baseline["face_scale"]
    if relative_spread > MAX_FACE_SCALE_SPREAD:
        raise CalibrationError(
            "you moved too much during calibration; sit still in your good posture and retry"
        )

    return Calibration(
        baseline=baseline,
        spread=spread,
        n_samples=len(raw_samples),
        mean_confidence=round(mean_confidence, 4),
        ts=ts,
    )


def capture_calibration(
    source: FrameSource,
    pose: PoseBackend,
    duration_s: float = 8.0,
    interval_s: float = 0.25,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[float, int], None] | None = None,
) -> Calibration:
    """Watch the user sit in good posture for ``duration_s`` and build a baseline.

    The camera must already be open. ``on_progress`` receives the elapsed
    fraction (0..1) and the number of usable samples so far.
    """
    raw_samples: list[dict[str, float]] = []
    confidences: list[float] = []
    start = clock()
    while (elapsed := clock() - start) < duration_s:
        frame = source.read()
        if frame is not None:
            height, width = frame.shape[:2]
            brightness, contrast = qual.frame_stats(frame)
            result = pose.estimate(frame)
            del frame
            if result.landmarks is not None:
                raw = feat.raw_features(result.landmarks, aspect=width / height)
                quality = qual.assess(
                    result.landmarks,
                    brightness,
                    contrast,
                    yaw_asymmetry=raw["yaw_asymmetry"],
                    shoulders_ok=raw["shoulders_ok"] >= 0.5,
                )
                if quality.confidence >= SAMPLE_MIN_CONFIDENCE:
                    raw_samples.append(raw)
                    confidences.append(quality.confidence)
        if on_progress is not None:
            on_progress(min(elapsed / duration_s, 1.0), len(raw_samples))
        sleep(interval_s)
    return build_calibration(raw_samples, confidences, ts=clock())
