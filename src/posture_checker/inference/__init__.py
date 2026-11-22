"""Posture models: the interface they share and the labels they predict."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

GOOD = "good"
SLOUCH = "slouch"
FORWARD_HEAD = "forward_head"
TOO_CLOSE = "too_close"
LABELS = (GOOD, SLOUCH, FORWARD_HEAD, TOO_CLOSE)


@dataclass(frozen=True)
class Prediction:
    label: str
    #: Score per label in 0..1. For a bad-posture label, 0.5 is the firing point.
    scores: dict[str, float]
    #: Recorded with every stored sample and alert, so any prediction can be
    #: traced back to the exact model that made it.
    model_version: str


class PostureModel(Protocol):
    version: str

    def predict(self, relative: Mapping[str, float]) -> Prediction: ...
