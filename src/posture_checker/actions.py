"""Feedback actions shared by the tray, the toast buttons and the CLI.

None of these touch the monitor or its alert policy: recording feedback must
never change what the app alerts on (spec Stage 2, behaviour 14).
"""

from __future__ import annotations

from collections.abc import Callable

from posture_checker.storage.db import Database

#: "I'm slouching now" links to the latest sample only if it is at most this
#: old (spec Stage 2, behaviour 14); older samples describe another moment.
LABEL_SAMPLE_MAX_AGE_S = 10.0
#: "Mark latest alert wrong" is offered for alerts at most this old (spec
#: Stage 2, behaviour 11).
RECENT_ALERT_S = 30 * 60

LABEL_NAMES = {
    "slouch": "slouching",
    "forward_head": "head forward",
    "too_close": "too close",
}


def mark_latest_alert_wrong(db: Database, clock: Callable[[], float]) -> bool:
    """Mark the newest alert wrong. False if there is none or it is already marked."""
    alert = db.last_alert()
    if alert is None or alert["feedback"] is not None:
        return False
    return db.mark_alert_wrong(alert["id"], clock())


def alert_feedback_state(db: Database, clock: Callable[[], float]) -> tuple[bool, bool]:
    """(has an alert in the last 30 min, that alert is already marked)."""
    alert = db.last_alert()
    if alert is None or clock() - alert["ts"] > RECENT_ALERT_S:
        return False, False
    return True, alert["feedback"] is not None


def record_label(
    db: Database,
    clock: Callable[[], float],
    label: str,
    confirm: Callable[[str], object] | None = None,
) -> int:
    """Store a user label for this moment and return its row id."""
    now = clock()
    sample = db.latest_sample_since(now - LABEL_SAMPLE_MAX_AGE_S)
    label_id = db.insert_label(
        ts=now,
        label=label,
        source="tray",
        sample_id=sample["id"] if sample is not None else None,
        model_version=sample["model_version"] if sample is not None else None,
        predicted_label=sample["label"] if sample is not None else None,
    )
    if confirm is not None:
        confirm(f"Thanks, noted: {LABEL_NAMES.get(label, label)}.")
    return label_id
