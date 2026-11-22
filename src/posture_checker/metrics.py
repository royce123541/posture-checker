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
from posture_checker.monitor import AWAY, LOW_CONFIDENCE, PRESENT, STORED_STATES, Sample
from posture_checker.policy.pausing import BatteryStatus
from posture_checker.storage.db import Database

log = logging.getLogger(__name__)

#: Prune the metrics table this often while running (spec Stage 3, behaviour 6:
#: once a day, plus at start-up).
PRUNE_EVERY_S = 24 * 3600.0
#: If more windows than this elapsed between two polls, the process was not
#: running in between (laptop asleep or hibernated: the monitor polls at least
#: every 30 s otherwise). Those windows get no rows, so suspended time never
#: counts as monitored time and a wake-up does not write a burst of empty rows.
MAX_CATCH_UP_WINDOWS = 2


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
        self._seen.append(_Seen(sample.ts, sample.state, sample.confidence, sample.latency_ms))

    def poll(self) -> None:
        now = self.clock()
        interval = self.interval_s
        end = self._window_start + interval
        if now >= end:
            self._write(self._window_start, end)
            self._window_start = end
            skipped = int((now - self._window_start) // interval)
            if skipped > MAX_CATCH_UP_WINDOWS:
                log.info("no metrics for %d windows (the app was not running)", skipped)
                self._window_start += skipped * interval
                self._seen = [s for s in self._seen if s.ts > self._window_start]
            else:
                while now >= self._window_start + interval:
                    self._write(self._window_start, self._window_start + interval)
                    self._window_start += interval
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

    def _write(self, start: float, end: float) -> None:
        began = self.timer()
        window = [s for s in self._seen if start < s.ts <= end]
        self._seen = [s for s in self._seen if s.ts > end]
        present = [s for s in window if s.state == PRESENT]
        latencies = [
            s.latency_ms for s in window if s.state in STORED_STATES and s.latency_ms is not None
        ]
        battery: BatteryStatus | None = self._read("battery", self.probe.battery)
        has_battery = battery is not None and battery.percent is not None
        row: dict[str, Any] = {
            "ts": end,
            "interval_s": end - start,
            "cpu_percent": self._read("cpu", self.probe.cpu_percent),
            "rss_mb": self._read("memory", self.probe.rss_mb),
            "n_present": len(present),
            "n_low_confidence": sum(s.state == LOW_CONFIDENCE for s in window),
            "n_away": sum(s.state == AWAY for s in window),
            # numpy's default (linear) percentile, as in the acceptance tests.
            "latency_p50_ms": float(np.percentile(latencies, 50)) if latencies else None,
            "latency_p95_ms": float(np.percentile(latencies, 95)) if latencies else None,
            "mean_confidence": (
                float(np.mean([s.confidence for s in present])) if present else None
            ),
            "battery_percent": battery.percent if battery is not None and has_battery else None,
            "on_ac": (int(battery.plugged) if battery is not None and has_battery else None),
            "camera_mode": self.config.camera_mode,
            "paused_reasons": ",".join(self._read("paused_reasons", self.paused_reasons) or ()),
            "model_version": self._read("model_version", self.model_version),
            "calibration_id": self._read("calibration_id", self.calibration_id),
        }
        try:
            row_id = self.db.insert_metrics(row)
            # Cost covers gathering plus the insert (behaviour 5), so it is known
            # only afterwards and is filled in with a small update.
            self.db.set_metrics_cost(row_id, (self.timer() - began) * 1000.0)
        except Exception:
            log.exception("could not write a metrics row")
