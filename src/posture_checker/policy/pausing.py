"""Pause supervisor: decide whether the app may use the camera right now.

Several independent things can each require a pause: another app using the
camera, a locked screen, a low battery, the user's own "Pause", or a
calibration running in a separate process. Each is read from an injected probe,
so tests drive the supervisor with fakes and a fake clock.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")

MANUAL = "manual"
CALIBRATING = "calibrating"
LOCKED = "locked"
LOW_BATTERY = "low_battery"
CAMERA_IN_USE_PREFIX = "camera_in_use:"
IN_CALL_PREFIX = "in_call:"

#: After the last other app stops using the camera, wait this long before
#: taking it back (spec behaviour 5), so a call that briefly toggles video off
#: does not have its camera grabbed mid-call.
CAMERA_GRACE_S = 10.0
#: Charge must rise this many points above the threshold before a low-battery
#: pause ends while still unplugged (spec behaviour 7: stops flapping).
BATTERY_HYSTERESIS_PCT = 5


@dataclass(frozen=True)
class BatteryStatus:
    #: Charge in percent; None means the machine has no battery.
    percent: int | None
    plugged: bool
    #: Windows Battery Saver is on.
    saver: bool = False


class CameraUsageProbe(Protocol):
    def apps_in_use(self) -> list[str]: ...


class CallProbe(Protocol):
    def apps_in_call(self) -> list[str]: ...


class LockStateProbe(Protocol):
    def is_locked(self) -> bool: ...


class BatteryProbe(Protocol):
    def status(self) -> BatteryStatus: ...


@dataclass(frozen=True)
class PauseDecision:
    reasons: list[str] = field(default_factory=list)

    @property
    def paused(self) -> bool:
        return bool(self.reasons)


class PauseSupervisor:
    """Combine the probes and manual reasons into one run/paused decision.

    Thread-safe: the monitor thread calls ``check`` while the tray thread
    changes the manual and calibrating reasons.
    """

    def __init__(
        self,
        camera_usage: CameraUsageProbe,
        lock_state: LockStateProbe,
        battery: BatteryProbe,
        clock: Callable[[], float] = time.time,
        battery_pause_percent: int = 20,
        calls: CallProbe | None = None,
        pause_in_calls: bool = True,
    ) -> None:
        self.camera_usage = camera_usage
        self.lock_state = lock_state
        self.battery = battery
        self.clock = clock
        self.battery_pause_percent = battery_pause_percent
        self.calls = calls
        #: Setting "Pause during calls" (spec C8); False skips the probe entirely.
        self.pause_in_calls = pause_in_calls
        self._lock = threading.Lock()
        #: None = no manual pause; math.inf = until resumed; else end time.
        self._manual_until: float | None = None
        self._extra: set[str] = set()
        #: App name -> last time it was seen using the camera.
        self._camera_seen: dict[str, float] = {}
        #: Call app name -> last time its meeting window was seen.
        self._call_seen: dict[str, float] = {}
        self._battery_paused = False
        self._last = PauseDecision()
        #: Probes whose failure has been logged (cleared once they work again).
        self._failed_probes: set[str] = set()

    # --- manual and other explicit reasons ----------------------------------
    def pause_manual(self, seconds: float | None) -> None:
        """Pause for ``seconds``, or until ``resume_manual`` when None."""
        with self._lock:
            self._manual_until = float("inf") if seconds is None else self.clock() + seconds
            # Show it in ``reasons`` now, so the tray updates without waiting
            # for the next check (spec behaviour 12: within 2 s).
            if MANUAL not in self._last.reasons:
                self._last = PauseDecision([MANUAL, *self._last.reasons])

    def resume_manual(self) -> None:
        """Clear only the manual reason; automatic reasons keep holding."""
        with self._lock:
            self._manual_until = None
            self._last = PauseDecision([r for r in self._last.reasons if r != MANUAL])

    @property
    def manual_active(self) -> bool:
        with self._lock:
            return self._manual_active(self.clock())

    def _manual_active(self, now: float) -> bool:
        return self._manual_until is not None and now < self._manual_until

    def add_reason(self, name: str) -> None:
        with self._lock:
            self._extra.add(name)

    def remove_reason(self, name: str) -> None:
        with self._lock:
            self._extra.discard(name)

    @property
    def reasons(self) -> list[str]:
        """Reasons from the latest ``check`` (for the tray; no probing)."""
        with self._lock:
            return list(self._last.reasons)

    # --- the decision -------------------------------------------------------
    def _probe(self, name: str, read: Callable[[], T], fallback: T) -> T:
        """Call one probe; a failure is logged once and counts as no reason.

        A broken probe must not raise out of check(): that would crash the
        monitor step and loop through the 30 s error restart forever.
        """
        try:
            value = read()
        except Exception:
            if name not in self._failed_probes:
                self._failed_probes.add(name)
                log.exception("%s probe failed; ignoring it until it works again", name)
            return fallback
        self._failed_probes.discard(name)
        return value

    def _camera_reasons(self, now: float) -> list[str]:
        no_apps: list[str] = []
        apps = self._probe("camera-in-use", self.camera_usage.apps_in_use, no_apps)
        for app in apps:
            self._camera_seen[app] = now
        for app, seen in list(self._camera_seen.items()):
            if now - seen >= CAMERA_GRACE_S:
                del self._camera_seen[app]
        return [CAMERA_IN_USE_PREFIX + app for app in sorted(self._camera_seen)]

    def _call_reasons(self, now: float) -> list[str]:
        if self.calls is None or not self.pause_in_calls:
            self._call_seen.clear()
            return []
        no_apps: list[str] = []
        for app in self._probe("in-call", self.calls.apps_in_call, no_apps):
            self._call_seen[app] = now
        # Same grace as the camera signal (spec C4), so a title flicker while a
        # tab reloads does not unpause and re-pause.
        for app, seen in list(self._call_seen.items()):
            if now - seen >= CAMERA_GRACE_S:
                del self._call_seen[app]
        return [IN_CALL_PREFIX + app for app in sorted(self._call_seen)]

    def _battery_low(self) -> bool:
        status = self._probe("battery", self.battery.status, BatteryStatus(None, True))
        if status.percent is None:
            # No battery (desktop): never pause for power.
            self._battery_paused = False
            return False
        threshold = self.battery_pause_percent
        if status.saver:
            self._battery_paused = True
        elif status.plugged:
            self._battery_paused = False
        elif self._battery_paused:
            self._battery_paused = status.percent < threshold + BATTERY_HYSTERESIS_PCT
        else:
            self._battery_paused = status.percent < threshold
        return self._battery_paused

    def check(self) -> PauseDecision:
        with self._lock:
            now = self.clock()
            reasons: list[str] = []
            if self._manual_active(now):
                reasons.append(MANUAL)
            elif self._manual_until is not None:
                self._manual_until = None  # a timed pause ran out
            reasons.extend(sorted(self._extra))
            reasons.extend(self._camera_reasons(now))
            reasons.extend(self._call_reasons(now))
            if self._probe("lock", self.lock_state.is_locked, False):
                reasons.append(LOCKED)
            if self._battery_low():
                reasons.append(LOW_BATTERY)
            self._last = PauseDecision(reasons)
            return self._last
