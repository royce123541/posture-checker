"""Unit tests for the metrics recorder and psutil probe (Stage 3, behaviours 1-7)."""

from __future__ import annotations

import sys
import types

import pytest

from conftest import FakeClock
from posture_checker import metrics
from posture_checker.config import Config
from posture_checker.monitor import AWAY, PAUSED, PRESENT, Sample
from posture_checker.policy.pausing import BatteryStatus


class Probe:
    def cpu_percent(self) -> float | None:
        return 3.0

    def rss_mb(self) -> float:
        return 100.0

    def battery(self) -> BatteryStatus:
        return BatteryStatus(percent=None, plugged=True)


def recorder(db, clock, **kw):  # type: ignore[no-untyped-def]
    return metrics.MetricsRecorder(db=db, config=Config(), probe=Probe(), clock=clock, **kw)


def rows(db):  # type: ignore[no-untyped-def]
    return db.query("SELECT * FROM metrics ORDER BY ts")


def test_samples_on_the_window_end_belong_to_that_window(db) -> None:
    clock = FakeClock(0.0)
    rec = recorder(db, clock)
    clock.advance(60)
    rec.observe(Sample(ts=60.0, state=PRESENT, confidence=0.9, latency_ms=4.0))
    rec.poll()
    clock.advance(1)
    rec.observe(Sample(ts=61.0, state=AWAY, latency_ms=2.0))
    rec.stop()
    first, last = rows(db)
    assert (first["n_present"], first["n_away"]) == (1, 0)
    assert (last["n_present"], last["n_away"], last["interval_s"]) == (0, 1, 1.0)


def test_paused_samples_are_not_counted(db) -> None:
    clock = FakeClock(0.0)
    rec = recorder(db, clock, paused_reasons=lambda: ("locked", "in_call:zoom"))
    clock.advance(60)
    rec.observe(Sample(ts=60.0, state=PAUSED))
    rec.poll()
    (row,) = rows(db)
    assert row["paused_reasons"] == "locked,in_call:zoom"
    assert row["latency_p50_ms"] is None and row["mean_confidence"] is None


def test_a_long_gap_writes_no_rows_for_suspended_time(db) -> None:
    clock = FakeClock(0.0)
    rec = recorder(db, clock)
    clock.advance(10 * 3600)  # laptop asleep
    rec.poll()
    assert len(rows(db)) == 1  # only the window that had started before the gap
    clock.advance(60)
    rec.poll()
    assert [r["interval_s"] for r in rows(db)] == [60, 60]


def test_a_short_catch_up_writes_each_window(db) -> None:
    clock = FakeClock(0.0)
    rec = recorder(db, clock)
    clock.advance(180)
    rec.poll()
    assert [r["ts"] for r in rows(db)] == [60, 120, 180]


def test_stop_twice_writes_one_partial_row(db) -> None:
    clock = FakeClock(0.0)
    rec = recorder(db, clock)
    clock.advance(10)
    rec.stop()
    rec.stop()
    assert len(rows(db)) == 1


def test_prune_is_daily_not_every_poll(db, monkeypatch) -> None:
    calls: list[float] = []
    monkeypatch.setattr(db, "prune_metrics", lambda now: calls.append(now) or 0)
    clock = FakeClock(0.0)
    rec = recorder(db, clock)
    rec.start()
    for _ in range(3):
        clock.advance(60)
        rec.poll()
    assert calls == [0.0]
    clock.advance(metrics.PRUNE_EVERY_S)
    rec.poll()
    assert len(calls) == 2


def test_psutil_probe_first_cpu_reading_is_none_then_per_core(monkeypatch) -> None:
    class Proc:
        def cpu_percent(self, interval=None):  # type: ignore[no-untyped-def]
            return 80.0

        def memory_info(self):  # type: ignore[no-untyped-def]
            return types.SimpleNamespace(rss=200 * 1024 * 1024)

    fake = types.SimpleNamespace(Process=Proc, cpu_count=lambda: 8)
    monkeypatch.setitem(sys.modules, "psutil", fake)

    class Battery:
        def status(self) -> BatteryStatus:
            return BatteryStatus(percent=50, plugged=False)

    probe = metrics.PsutilProbe(Battery())
    assert probe.cpu_percent() is None
    assert probe.cpu_percent() == pytest.approx(10.0)
    assert probe.rss_mb() == pytest.approx(200.0)
    assert probe.battery().percent == 50


def test_insert_failure_is_logged_not_raised(db, monkeypatch, caplog) -> None:
    def boom(row):  # type: ignore[no-untyped-def]
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "insert_metrics", boom)
    clock = FakeClock(0.0)
    rec = recorder(db, clock)
    clock.advance(60)
    rec.poll()
    assert "could not write a metrics row" in caplog.text


def test_config_rejects_non_positive_metrics_interval() -> None:
    with pytest.raises(ValueError):
        Config(metrics_interval_s=0).validate()
