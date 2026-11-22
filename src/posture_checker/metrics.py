"""Per-minute health numbers for the ``metrics`` table (spec Stage 3, behaviours 1-7).

``MetricsRecorder`` keeps the monitor's samples for the current window in
memory and writes one row per ``Config.metrics_interval_s``. Everything it
reads from the system goes through an injected probe, so tests use a fake and
never call psutil. Only numbers and short labels are stored.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from posture_checker.config import Config
from posture_checker.monitor import (
    AWAY,
    LOW_CONFIDENCE,
    PAUSED,
    PRESENT,
    STORED_STATES,
    Sample,
)
from posture_checker.policy.pausing import BatteryStatus
from posture_checker.storage.db import Database

log = logging.getLogger(__name__)

#: Prune the metrics table this often while running (spec Stage 3, behaviour 6:
#: once a day, plus at start-up).
PRUNE_EVERY_S = 24 * 3600.0
#: Pieces of a window shorter than this (seconds) are float noise, not time
#: monitored or paused, and get no row.
MIN_SPAN_S = 1e-6


class SystemProbe(Protocol):
    def cpu_percent(self) -> float | None: ...

    def rss_mb(self) -> float: ...

    def battery(self) -> BatteryStatus: ...


class BatteryReader(Protocol):
    def status(self) -> BatteryStatus: ...


class PsutilProbe:
    """This process's CPU and memory from psutil, battery from the Stage 2 reader."""

    def __init__(self, battery: BatteryReader) -> None:
        import psutil

        self._psutil = psutil
        self._process = psutil.Process()
        self._battery = battery
        self._primed = False

    def cpu_percent(self) -> float | None:
        """CPU since the previous call, as a share of all cores (0-100).

        psutil needs two readings; the first call primes it and returns None
        (spec Stage 3, risks: the first row has no CPU value).
        """
        value = float(self._process.cpu_percent(interval=None))
        if not self._primed:
            self._primed = True
            return None
        return value / (self._psutil.cpu_count() or 1)

    def rss_mb(self) -> float:
        return float(self._process.memory_info().rss) / (1024 * 1024)

    def battery(self) -> BatteryStatus:
        return self._battery.status()


@dataclass(frozen=True)
class _Seen:
    ts: float
    state: str
    confidence: float
    latency_ms: float | None
    #: Pause reasons when the sample was taken; () while running.
    reasons: tuple[str, ...] = ()


def _none() -> None:
    return None


def _no_reasons() -> tuple[str, ...]:
    return ()


class MetricsRecorder:
    def __init__(
        self,
        *,
        db: Database,
        config: Config,
        probe: SystemProbe,
        clock: Callable[[], float] = time.time,
        timer: Callable[[], float] = time.perf_counter,
        paused_reasons: Callable[[], Iterable[str]] = _no_reasons,
        model_version: Callable[[], str | None] = _none,
        calibration_id: Callable[[], int | None] = _none,
    ) -> None:
        self.db = db
        self.config = config
        self.probe = probe
        self.clock = clock
        self.timer = timer
        self.paused_reasons = paused_reasons
        self.model_version = model_version
        self.calibration_id = calibration_id
        self._window_start = clock()
        self._last_prune = self._window_start
        self._seen: list[_Seen] = []
        self._failed: set[str] = set()
        #: Pause reasons of the last sample of the previous window (None: unknown).
        self._prev_reasons: tuple[str, ...] | None = None
        #: The next CPU reading spans a gap with no samples; store NULL instead.
        self._cpu_stale = False

    @property
    def interval_s(self) -> float:
        return float(self.config.metrics_interval_s)

    # --- hooks ----------------------------------------------------------------
    def start(self) -> None:
        self.prune()

    def stop(self) -> None:
        """Write the final partial row with its real length (behaviour 7)."""
        now = self.clock()
        if now > self._window_start:
            self._write(self._window_start, now)
            self._window_start = now

    def prune(self) -> int:
        now = self.clock()
        self._last_prune = now
        try:
            return self.db.prune_metrics(now)
        except Exception:
            log.exception("pruning the metrics table failed")
            return 0

    def observe(self, sample: Sample) -> None:
        reasons: tuple[str, ...] = ()
        if sample.state == PAUSED:
            # Read now, not at the window end, so a pause that starts or ends
            # mid-minute is split correctly (an empty answer still means paused).
            reasons = tuple(self._read("paused_reasons", self.paused_reasons) or ()) or (PAUSED,)
        self._seen.append(
            _Seen(sample.ts, sample.state, sample.confidence, sample.latency_ms, reasons)
        )

    def poll(self) -> None:
        now = self.clock()
        interval = self.interval_s
        while now >= self._window_start + interval:
            start, end = self._window_start, self._window_start + interval
            if any(start < s.ts <= end for s in self._seen):
                self._write(start, end)
            else:
                # The monitor samples at least every 30 s while the app runs, so
                # a window without a single sample means the laptop was asleep:
                # no row (suspended time is not monitored time), and the next
                # CPU reading would average over the gap, so it is dropped.
                self._cpu_stale = True
                self._prev_reasons = None
            self._window_start = end
        if now - self._last_prune >= PRUNE_EVERY_S:
            self.prune()

    # --- one row ----------------------------------------------------------------
    def _read(self, field: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception:
            if field not in self._failed:
                self._failed.add(field)
                log.warning("could not read %s for metrics; storing NULL", field, exc_info=True)
            return None

    def _split(self, start: float, end: float, window: list[_Seen]) -> tuple[float, list[str]]:
        """Seconds of (start, end] spent paused, and the reasons seen, in order.

        Time between two samples takes the state of the earlier one; time
        before the first sample takes the previous window's last state (or the
        first sample's when unknown).
        """
        current = self._prev_reasons
        if current is None:
            if window:
                current = window[0].reasons
            else:
                current = tuple(self._read("paused_reasons", self.paused_reasons) or ())
        paused_s, t = 0.0, start
        reasons: list[str] = []
        for ts, next_reasons in [*((s.ts, s.reasons) for s in window), (end, None)]:
            if current:
                paused_s += ts - t
                reasons += [r for r in current if r not in reasons]
            t = ts
            if next_reasons is not None:
                current = next_reasons
        self._prev_reasons = current
        return paused_s, reasons

    def _write(self, start: float, end: float) -> None:
        """Write the window as one row, or two when it was partly paused.

        A mixed window gives a monitoring row (``paused_reasons`` empty) and a
        paused row, each with the seconds it covered, so the report adds time
        up correctly. Both carry the same CPU, memory and battery readings.
        """
        began = self.timer()
        window = [s for s in self._seen if start < s.ts <= end]
        self._seen = [s for s in self._seen if s.ts > end]
        paused_s, reasons = self._split(start, end, window)
        present = [s for s in window if s.state == PRESENT]
        latencies = [
            s.latency_ms for s in window if s.state in STORED_STATES and s.latency_ms is not None
        ]
        cpu = self._read("cpu", self.probe.cpu_percent)
        if self._cpu_stale:
            cpu, self._cpu_stale = None, False
        battery: BatteryStatus | None = self._read("battery", self.probe.battery)
        percent = battery.percent if battery is not None else None
        common: dict[str, Any] = {
            "ts": end,
            "cpu_percent": cpu,
            "rss_mb": self._read("memory", self.probe.rss_mb),
            "battery_percent": percent,
            "on_ac": None if battery is None or percent is None else int(battery.plugged),
            "camera_mode": self.config.camera_mode,
            "model_version": self._read("model_version", self.model_version),
            "calibration_id": self._read("calibration_id", self.calibration_id),
        }
        rows: list[dict[str, Any]] = []
        monitored_s = (end - start) - paused_s
        if monitored_s > MIN_SPAN_S:
            rows.append(
                {
                    **common,
                    "interval_s": monitored_s,
                    "paused_reasons": "",
                    "n_present": len(present),
                    "n_low_confidence": sum(s.state == LOW_CONFIDENCE for s in window),
                    "n_away": sum(s.state == AWAY for s in window),
                    # numpy's default (linear) percentile, as in the acceptance tests.
                    "latency_p50_ms": float(np.percentile(latencies, 50)) if latencies else None,
                    "latency_p95_ms": float(np.percentile(latencies, 95)) if latencies else None,
                    "mean_confidence": (
                        float(np.mean([s.confidence for s in present])) if present else None
                    ),
                }
            )
        if paused_s > MIN_SPAN_S:
            rows.append(
                {
                    **common,
                    "interval_s": paused_s,
                    "paused_reasons": ",".join(reasons),
                    "n_present": 0,
                    "n_low_confidence": 0,
                    "n_away": 0,
                }
            )
        # metrics_cost_ms (behaviour 5) covers reading the probes and building
        # the rows. The insert is not included, so the cost can go into the
        # same INSERT instead of a second write.
        cost_ms = (self.timer() - began) * 1000.0
        for row in rows:
            row["metrics_cost_ms"] = cost_ms
            try:
                self.db.insert_metrics(row)
            except Exception:
                log.exception("could not write a metrics row")
