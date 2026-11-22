"""Alert policy: only complain about posture that has stayed bad for a while.

The model labels single samples; this state machine decides when that adds up
to an alert. It is pure logic with timestamps passed in, so tests drive it with
a fake clock.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass

from posture_checker.inference import GOOD

#: A silence longer than this (pause, sleep, camera lost) starts a new window.
DEFAULT_MAX_GAP_S = 30.0
#: Share of samples in the window that must be confident; below it, the app
#: does not know enough to alert.
MIN_CONFIDENT_SHARE = 0.5
#: Number of latest confident samples that must all be bad to call the
#: situation "suspect" and sample faster.
SUSPECT_STREAK = 3


@dataclass(frozen=True)
class Alert:
    ts: float
    label: str
    bad_fraction: float
    window_s: float
    n_samples: int


@dataclass(frozen=True)
class _Entry:
    ts: float
    label: str | None
    confident: bool


class AlertPolicy:
    """Fire when at least ``bad_fraction`` of the last ``window_s`` was bad."""

    def __init__(
        self,
        window_s: float = 60.0,
        bad_fraction: float = 0.8,
        cooldown_s: float = 300.0,
        min_samples: int = 5,
        max_gap_s: float = DEFAULT_MAX_GAP_S,
    ) -> None:
        self.window_s = window_s
        self.bad_fraction = bad_fraction
        self.cooldown_s = cooldown_s
        self.min_samples = min_samples
        self.max_gap_s = max_gap_s
        self._entries: deque[_Entry] = deque()
        self._window_start: float | None = None
        self._last_alert_ts: float | None = None

    def reset(self) -> None:
        """Forget the current window (the cooldown clock keeps running)."""
        self._entries.clear()
        self._window_start = None

    @property
    def suspect(self) -> bool:
        """True while the latest confident samples are all bad."""
        recent = [e for e in self._entries if e.confident][-SUSPECT_STREAK:]
        return len(recent) == SUSPECT_STREAK and all(e.label != GOOD for e in recent)

    def update(self, ts: float, label: str | None, confident: bool) -> Alert | None:
        """Record one sample and return an alert if it is time for one."""
        if self._entries and ts - self._entries[-1].ts > self.max_gap_s:
            self.reset()
        if self._window_start is None:
            self._window_start = ts
        self._entries.append(_Entry(ts, label, confident))
        while self._entries and self._entries[0].ts < ts - self.window_s:
            self._entries.popleft()

        # Not enough history yet: bad posture has not "lasted for a while".
        if ts - self._window_start < self.window_s:
            return None
        if self._last_alert_ts is not None and ts - self._last_alert_ts < self.cooldown_s:
            return None

        confident_entries = [e for e in self._entries if e.confident]
        if len(confident_entries) < self.min_samples:
            return None
        if len(confident_entries) / len(self._entries) < MIN_CONFIDENT_SHARE:
            return None

        bad = [e.label for e in confident_entries if e.label != GOOD]
        fraction = len(bad) / len(confident_entries)
        if fraction < self.bad_fraction:
            return None

        self._last_alert_ts = ts
        dominant = Counter(bad).most_common(1)[0][0]
        assert dominant is not None
        return Alert(
            ts=ts,
            label=dominant,
            bad_fraction=round(fraction, 4),
            window_s=self.window_s,
            n_samples=len(confident_entries),
        )
