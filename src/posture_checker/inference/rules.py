"""Threshold-rule posture models.

``rules-v0`` uses hand-picked thresholds chosen from geometry. It needs no
training data, so the app works from the first calibration, and it is the
baseline every later model must beat.

``rules-v1`` (and later versions) use the same rules with thresholds fitted to
the owner's own labelled recordings by ``posture_checker.mlops.fit_rules``. The
fitted thresholds are stored as a small versioned JSON file in the app's
models folder.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from posture_checker import paths
from posture_checker.inference import (
    FORWARD_HEAD,
    GOOD,
    SLOUCH,
    TOO_CLOSE,
    Prediction,
)

BASELINE_VERSION = "rules-v0"
#: File in the models folder holding the deployed fitted thresholds.
FITTED_FILE = "rules_thresholds.json"


@dataclass(frozen=True)
class Thresholds:
    """Deviations from the calibrated baseline that count as bad posture.

    The defaults are the ``rules-v0`` starting points.
    """

    #: Face at least 25% larger than at calibration, i.e. roughly 20% closer.
    too_close_excess: float = 0.25
    #: Face grew 10% more than the shoulders did: the head moved forward alone.
    forward_vs_shoulders_excess: float = 0.10
    #: Without visible shoulders, a moderately larger face is the only lean-in cue.
    forward_face_only_excess: float = 0.12
    #: Head sits 0.45 face-widths (about 4 cm) lower in the frame.
    slouch_face_drop: float = 0.45
    #: Head has sunk 15% of the way towards the shoulder line.
    slouch_neck_shrink: float = 0.15

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not value > 0:
                raise ValueError(f"threshold {field.name} must be positive, got {value}")


# Kept as module constants for readability elsewhere.
DEFAULT_THRESHOLDS = Thresholds()


def _severity(deviation: float, threshold: float) -> float:
    """How far past the threshold a deviation is: 1.0 means exactly at it."""
    if deviation is None or math.isnan(deviation):
        return 0.0
    return max(deviation, 0.0) / threshold


def _score(severity: float) -> float:
    """Map severity to 0..1 so that the firing point (1.0) lands on 0.5."""
    return min(severity / 2.0, 1.0)


def deviations(relative: Mapping[str, float]) -> dict[str, float]:
    """The raw deviation each rule compares against its threshold.

    Shared by the model and by threshold fitting, so both measure the same thing.
    NaN means the rule cannot be evaluated for this sample (shoulders hidden).
    """
    face_excess = relative["face_scale_ratio"] - 1.0
    has_shoulders = relative.get("shoulders_ok", 0.0) >= 0.5
    return {
        "too_close_excess": face_excess,
        "forward_vs_shoulders_excess": (
            relative["head_vs_shoulder_ratio"] - 1.0 if has_shoulders else math.nan
        ),
        "forward_face_only_excess": math.nan if has_shoulders else face_excess,
        "slouch_face_drop": relative["face_drop"],
        "slouch_neck_shrink": -relative["neck_ratio_change"] if has_shoulders else math.nan,
    }


class RulesModel:
    """Threshold rules on relative features."""

    def __init__(
        self,
        sensitivity: float = 1.0,
        thresholds: Thresholds = DEFAULT_THRESHOLDS,
        version: str = BASELINE_VERSION,
    ) -> None:
        if sensitivity <= 0:
            raise ValueError("sensitivity must be positive")
        self.sensitivity = sensitivity
        self.thresholds = thresholds
        self.version = version

    def predict(self, relative: Mapping[str, float]) -> Prediction:
        s = self.sensitivity
        t = self.thresholds
        dev = deviations(relative)

        too_close = _severity(dev["too_close_excess"], t.too_close_excess / s)
        forward = max(
            _severity(dev["forward_vs_shoulders_excess"], t.forward_vs_shoulders_excess / s),
            _severity(dev["forward_face_only_excess"], t.forward_face_only_excess / s),
        )
        slouch = max(
            _severity(dev["slouch_face_drop"], t.slouch_face_drop / s),
            _severity(dev["slouch_neck_shrink"], t.slouch_neck_shrink / s),
        )

        severities = {TOO_CLOSE: too_close, FORWARD_HEAD: forward, SLOUCH: slouch}
        scores = {name: round(_score(value), 4) for name, value in severities.items()}
        scores[GOOD] = round(1.0 - max(scores.values()), 4)

        # When several rules fire, report the most specific explanation first.
        label = GOOD
        for name in (TOO_CLOSE, FORWARD_HEAD, SLOUCH):
            if severities[name] >= 1.0:
                label = name
                break
        return Prediction(label=label, scores=scores, model_version=self.version)


def save_fitted(thresholds: Thresholds, version: str, metadata: Mapping[str, object]) -> Path:
    """Deploy fitted thresholds as the active rules model."""
    path = paths.models_dir() / FITTED_FILE
    payload = {"version": version, "thresholds": asdict(thresholds), "metadata": dict(metadata)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic: a running app never reads a half-written file
    return path


def load_active(sensitivity: float = 1.0) -> RulesModel:
    """The deployed fitted rules if there are any, otherwise ``rules-v0``."""
    path = paths.models_dir() / FITTED_FILE
    if not path.exists():
        return RulesModel(sensitivity)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RulesModel(
        sensitivity, thresholds=Thresholds(**payload["thresholds"]), version=payload["version"]
    )
