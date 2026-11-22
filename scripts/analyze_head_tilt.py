"""Does head tilt (chin tucked vs untucked) separate postures in the recordings?

Exploratory analysis, no model changes. For every guided recording it computes
head-tilt measures from the stored landmarks (numbers only, never images) and
compares how well each one separates good posture from forward head and from
slouch, next to the measures the current rules already use.

Each comparison fits one cut-off on the early 60% of every prompted block and
scores it on the late 40% (the same time-blocked split as ``fit-rules``), so
the numbers are held-out, not fitted-on.

Usage:  uv run python scripts/analyze_head_tilt.py [--session ID ...]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from posture_checker import features as feat
from posture_checker.inference import FORWARD_HEAD, GOOD, SLOUCH
from posture_checker.inference.rules import deviations
from posture_checker.mlops.fit_rules import best_threshold, time_blocked_split
from posture_checker.storage.db import Database

MOUTH_LEFT, MOUTH_RIGHT = 9, 10  # MediaPipe pose landmark indices


def tilt_measures(landmarks: list[list[float]]) -> dict[str, float]:
    """Head-tilt proxies, in face widths (outer-eye distance). Larger = nose lower."""
    lm = np.asarray(landmarks, dtype=float)
    pts = lm[:, :3].copy()
    pts[:, 0] *= 4 / 3  # recordings are 640x480
    nose = pts[feat.NOSE]
    eye_l, eye_r = pts[feat.LEFT_EYE_OUTER], pts[feat.RIGHT_EYE_OUTER]
    eye_mid = (eye_l + eye_r) / 2
    ear_mid = (pts[feat.LEFT_EAR] + pts[feat.RIGHT_EAR]) / 2
    mouth_mid = (pts[MOUTH_LEFT] + pts[MOUTH_RIGHT]) / 2
    scale = float(np.linalg.norm(eye_l[:2] - eye_r[:2]))
    return {
        # Chin tucked tips the nose down below the ears; untucked lifts it.
        "tilt_nose_vs_ears": float((nose[1] - ear_mid[1]) / scale),
        "tilt_eyes_vs_ears": float((eye_mid[1] - ear_mid[1]) / scale),
        "tilt_mouth_vs_ears": float((mouth_mid[1] - ear_mid[1]) / scale),
        # Already computed by the app (pitch_ratio) but unused by the rules.
        "tilt_nose_vs_eyes": float((nose[1] - eye_mid[1]) / scale),
    }


def current_measures(features: Mapping[str, float]) -> dict[str, float]:
    dev = deviations(features)
    return {
        "rule_face_drop": dev["slouch_face_drop"],
        "rule_neck_shrink": dev["slouch_neck_shrink"],
        "rule_forward_vs_shoulders": dev["forward_vs_shoulders_excess"],
    }


def held_out_separation(
    train: Sequence[Mapping[str, Any]],
    test: Sequence[Mapping[str, Any]],
    measure: str,
    positive: str,
    negative: str,
) -> tuple[float, float, str] | None:
    """Fit a cut-off on train, return (held-out balanced accuracy, cut-off, direction)."""

    def values(rows: Sequence[Mapping[str, Any]], label: str, sign: float) -> list[float]:
        return [
            sign * r["m"][measure]
            for r in rows
            if r["label"] == label and np.isfinite(r["m"][measure])
        ]

    best: tuple[float, float, str] | None = None
    for sign, direction in ((1.0, "higher"), (-1.0, "lower")):
        pos, neg = values(train, positive, sign), values(train, negative, sign)
        tpos, tneg = values(test, positive, sign), values(test, negative, sign)
        if min(len(pos), len(neg), len(tpos), len(tneg)) < 5:
            return None
        cut, _ = best_threshold(pos, neg)
        score = (
            sum(v >= cut for v in tpos) / len(tpos) + sum(v < cut for v in tneg) / len(tneg)
        ) / 2
        if best is None or score > best[0]:
            best = (score, sign * cut, direction)
    return best


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session", action="append", help="recording session id (repeatable)")
    args = parser.parse_args(argv)

    db = Database()
    sessions = args.session or db.recorded_sessions()
    if not sessions:
        print("No recordings yet.", file=sys.stderr)
        return 1
    rows = db.query(
        f"SELECT session, ts, step, label, features_json, landmarks_json FROM recorded_samples "
        f"WHERE session IN ({','.join('?' * len(sessions))}) ORDER BY ts",
        tuple(sessions),
    )
    db.close()

    samples = []
    for row in rows:
        features = {
            k: (np.nan if v is None else v) for k, v in json.loads(row["features_json"]).items()
        }
        m = tilt_measures(json.loads(row["landmarks_json"])) | current_measures(features)
        samples.append(
            {
                "session": row["session"],
                "ts": row["ts"],
                "step": row["step"],
                "label": row["label"],
                "m": m,
            }
        )
    names = list(samples[0]["m"]) if samples else []

    comparisons = ((FORWARD_HEAD, GOOD), (SLOUCH, GOOD), (FORWARD_HEAD, SLOUCH))
    for session in sessions:
        own = [s for s in samples if s["session"] == session]
        if not own:
            continue
        train, test = time_blocked_split(own)
        print(f"\n=== {session}  ({len(own)} samples)")
        print(
            f"{'measure':<27}"
            + "".join(f"{lbl:>14}" for lbl in (GOOD, FORWARD_HEAD, SLOUCH))
            + "   (median)"
        )
        for name in names:
            meds = []
            for lbl in (GOOD, FORWARD_HEAD, SLOUCH):
                vals = [
                    s["m"][name] for s in own if s["label"] == lbl and np.isfinite(s["m"][name])
                ]
                meds.append(f"{statistics.median(vals):>14.3f}" if vals else f"{'-':>14}")
            print(f"{name:<27}" + "".join(meds))
        print(
            f"\n{'held-out balanced accuracy':<27}"
            + "".join(f"{p + ' vs ' + n:>22}" for p, n in comparisons)
        )
        for name in names:
            cells = []
            for p, n in comparisons:
                r = held_out_separation(train, test, name, p, n)
                cells.append(f"{'-':>22}" if r is None else f"{r[0]:.2f} ({r[2]})".rjust(22))
            print(f"{name:<27}" + "".join(cells))
    print("\n1.00 = perfect split, 0.50 = chance. 'higher': the first label has larger values.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
