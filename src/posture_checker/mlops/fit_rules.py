"""Fit rule thresholds to the owner's labelled recordings, and evaluate them.

Method, kept deliberately simple and explainable:

1. **Time-blocked split.** Each prompted step is a contiguous block of samples.
   The first 60% of every block is used for fitting, the last 40% for testing.
   Neighbouring frames are nearly identical, so a random split would leak and
   overstate accuracy.
2. **One threshold at a time.** Each rule compares one deviation against one
   threshold. For each, pick the cut-off that best separates the samples that
   rule should fire on from those it should not (maximum balanced accuracy).
   When a range of cut-offs scores equally, take the middle of the range for
   the widest safety margin on both sides.
3. **Gate.** The fitted thresholds are deployed only if they do at least as
   well as the current rules on the held-out test blocks.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from posture_checker.inference import FORWARD_HEAD, GOOD, LABELS, SLOUCH, TOO_CLOSE
from posture_checker.inference.rules import RulesModel, Thresholds, deviations

TRAIN_FRACTION = 0.6
#: Fewer fitting samples than this on either side and a threshold keeps its old value.
MIN_SAMPLES_PER_SIDE = 5

#: For each threshold: the labels it should fire on, and the labels it must not.
#: Labels left out of both are ignored for that threshold (e.g. "too close"
#: also enlarges the face, but the too-close rule takes priority over it).
TARGETS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "too_close_excess": (frozenset({TOO_CLOSE}), frozenset({GOOD, SLOUCH, FORWARD_HEAD})),
    "forward_vs_shoulders_excess": (frozenset({FORWARD_HEAD}), frozenset({GOOD, SLOUCH})),
    "forward_face_only_excess": (frozenset({FORWARD_HEAD}), frozenset({GOOD, SLOUCH})),
    "slouch_face_drop": (frozenset({SLOUCH}), frozenset({GOOD})),
    "slouch_neck_shrink": (frozenset({SLOUCH}), frozenset({GOOD})),
}


@dataclass(frozen=True)
class ThresholdFit:
    name: str
    old: float
    new: float
    fitted: bool
    n_positive: int
    n_negative: int
    train_balanced_accuracy: float | None
    note: str = ""


@dataclass(frozen=True)
class Evaluation:
    n: int
    accuracy: float
    #: Share of truly good samples labelled bad: what drives false alerts.
    false_bad_rate: float
    #: Share of truly bad samples labelled as any bad posture.
    bad_detection_rate: float
    per_class_recall: dict[str, float]
    #: confusion[true][predicted] = count
    confusion: dict[str, dict[str, int]]


def time_blocked_split(
    samples: Sequence[Mapping[str, Any]], train_fraction: float = TRAIN_FRACTION
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split each (session, step) block into an early train part and a late test part."""
    blocks: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for sample in samples:
        blocks.setdefault((sample["session"], sample["step"]), []).append(sample)
    train: list[Mapping[str, Any]] = []
    test: list[Mapping[str, Any]] = []
    for block in blocks.values():
        block = sorted(block, key=lambda s: s["ts"])
        cut = round(len(block) * train_fraction)
        train.extend(block[:cut])
        test.extend(block[cut:])
    return train, test


def best_threshold(positives: Sequence[float], negatives: Sequence[float]) -> tuple[float, float]:
    """Cut-off t (fire when value >= t) maximising balanced accuracy.

    Returns ``(threshold, balanced_accuracy)``. Among equally good cut-offs the
    middle of the plateau is chosen.
    """
    values = sorted(set(positives) | set(negatives))
    candidates = [(a + b) / 2 for a, b in itertools.pairwise(values)]
    if not candidates:
        candidates = [values[0]]

    def score(t: float) -> float:
        tpr = sum(v >= t for v in positives) / len(positives)
        tnr = sum(v < t for v in negatives) / len(negatives)
        return (tpr + tnr) / 2

    scored = [(score(t), t) for t in candidates]
    best = max(s for s, _ in scored)
    plateau = [t for s, t in scored if s >= best - 1e-12]
    # Middle of the best range: as far as possible from both classes' edges.
    return (min(plateau) + max(plateau)) / 2, best


def fit_thresholds(
    train: Iterable[Mapping[str, Any]],
    current: Thresholds,
    keep: Collection[str] = (),
) -> tuple[Thresholds, list[ThresholdFit]]:
    """Fit every threshold in ``TARGETS`` except those named in ``keep``.

    Kept thresholds stay at their current value, for rules whose signal is too
    weak to fit reliably (e.g. forward head from a front-facing camera).
    """
    unknown = set(keep) - set(TARGETS)
    if unknown:
        raise ValueError(f"unknown threshold(s): {', '.join(sorted(unknown))}")
    rows = [(s["label"], deviations(s["features"])) for s in train]
    fitted: dict[str, float] = {}
    report: list[ThresholdFit] = []
    for name, (fire_on, quiet_on) in TARGETS.items():
        old = getattr(current, name)
        if name in keep:
            report.append(ThresholdFit(name, old, old, False, 0, 0, None, "kept by request"))
            continue
        pos = [d[name] for label, d in rows if label in fire_on and not math.isnan(d[name])]
        neg = [d[name] for label, d in rows if label in quiet_on and not math.isnan(d[name])]
        if len(pos) < MIN_SAMPLES_PER_SIDE or len(neg) < MIN_SAMPLES_PER_SIDE:
            report.append(
                ThresholdFit(
                    name, old, old, False, len(pos), len(neg), None, "not enough samples; kept"
                )
            )
            continue
        threshold, balanced = best_threshold(pos, neg)
        if threshold <= 0:
            # A non-positive cut-off would fire on the calibrated posture itself.
            report.append(
                ThresholdFit(
                    name,
                    old,
                    old,
                    False,
                    len(pos),
                    len(neg),
                    balanced,
                    "fit would fire on the calibrated posture; kept",
                )
            )
            continue
        fitted[name] = round(threshold, 4)
        report.append(
            ThresholdFit(name, old, fitted[name], True, len(pos), len(neg), round(balanced, 4))
        )
    return replace(current, **fitted), report


def evaluate(model: RulesModel, samples: Iterable[Mapping[str, Any]]) -> Evaluation:
    confusion: dict[str, Counter[str]] = {label: Counter() for label in LABELS}
    for sample in samples:
        predicted = model.predict(sample["features"]).label
        confusion[sample["label"]][predicted] += 1
    n = sum(sum(c.values()) for c in confusion.values())
    if n == 0:
        raise ValueError("no samples to evaluate")
    correct = sum(confusion[label][label] for label in LABELS)
    good_total = sum(confusion[GOOD].values())
    bad_labels = [label for label in LABELS if label != GOOD]
    bad_total = sum(sum(confusion[label].values()) for label in bad_labels)
    false_bad = good_total - confusion[GOOD][GOOD]
    bad_detected = sum(sum(confusion[b].values()) - confusion[b][GOOD] for b in bad_labels)
    recall = {
        label: round(confusion[label][label] / total, 4)
        for label in LABELS
        if (total := sum(confusion[label].values()))
    }
    return Evaluation(
        n=n,
        accuracy=round(correct / n, 4),
        false_bad_rate=round(false_bad / good_total, 4) if good_total else math.nan,
        bad_detection_rate=round(bad_detected / bad_total, 4) if bad_total else math.nan,
        per_class_recall=recall,
        confusion={t: {p: confusion[t][p] for p in LABELS} for t in LABELS},
    )


def passes_gate(candidate: Evaluation, current: Evaluation) -> bool:
    """Deploy only if the candidate is at least as accurate and not more alarm-prone."""
    no_more_false_alerts = (
        math.isnan(candidate.false_bad_rate)
        or math.isnan(current.false_bad_rate)
        or candidate.false_bad_rate <= current.false_bad_rate + 0.02
    )
    return candidate.accuracy >= current.accuracy and no_more_false_alerts


def next_version(current_version: str) -> str:
    """rules-v0 -> rules-v1 -> rules-v2 ..."""
    prefix, _, number = current_version.rpartition("-v")
    if prefix != "rules" or not number.isdigit():
        return "rules-v1"
    return f"rules-v{int(number) + 1}"


def evaluation_dict(evaluation: Evaluation) -> dict[str, Any]:
    return asdict(evaluation)
