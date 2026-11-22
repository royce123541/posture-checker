"""Unit tests for the report's own logic (Stage 3, behaviours 8-16)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from posture_checker import cli, report
from posture_checker.storage.db import Database

TZ = timezone(timedelta(hours=-5))
DAY = date(2025, 3, 12)


def ts(hour: float, offset_days: int = 0) -> float:
    start = datetime(2025, 3, 12, tzinfo=TZ) + timedelta(days=offset_days)
    return start.timestamp() + hour * 3600


def metric(db: Database, when: float, interval: float = 3600.0, **kw: object) -> None:
    db.insert_metrics({"ts": when, "interval_s": interval, "paused_reasons": "", **kw} | {
        "n_present": 0, "n_low_confidence": 0, "n_away": 0,
    })  # fmt: skip


def value(html: str, scope: str, key: str) -> str | None:
    m = re.search(
        rf'data-figure="{re.escape(key)}" data-scope="{re.escape(scope)}"'
        r'(?: data-value="([^"]*)")?',
        html,
    )
    assert m, f"{scope}/{key} missing"
    return m.group(1)


def test_drain_skips_a_gap_where_the_app_was_not_running(db: Database) -> None:
    metric(db, ts(8), battery_percent=90, on_ac=0)
    metric(db, ts(9), battery_percent=85, on_ac=0)  # 5 %/h
    metric(db, ts(15), battery_percent=40, on_ac=0)  # 6 h gap: not counted
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    assert float(value(html, "day", "drain_pct_per_h:monitoring") or "nan") == pytest.approx(5.0)


def test_short_drain_shows_raw_values(db: Database) -> None:
    metric(db, ts(8), 60.0, battery_percent=90, on_ac=0)
    metric(db, ts(8) + 60, 60.0, battery_percent=89, on_ac=0)
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    assert "1 % over 1 min" in html


def test_other_days_get_their_own_scope_for_the_picker(db: Database) -> None:
    db.insert_alert(
        ts=ts(10, -2), label="slouch", bad_fraction=0.9, window_s=60, model_version="m",
        calibration_id=None,
    )  # fmt: skip
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    assert value(html, "day:2025-03-10", "alerts_total") == "1"
    assert value(html, "day", "alerts_total") is None  # no monitoring, no alerts: no data
    assert 'href="#d2025-03-10"' in html and 'id="d2025-03-10"' in html


def test_chart_draws_seven_bars_with_no_data_days(db: Database) -> None:
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    chart = re.search(r'<svg data-chart="alerts_per_hour".*?</svg>', html, re.S)
    assert chart and chart.group(0).count('class="bar"') == 7
    assert "no data" in chart.group(0)


def test_labels_and_reasons_are_escaped(db: Database) -> None:
    metric(db, ts(9), 600.0, paused_reasons="camera_in_use:<b>x</b>")
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    assert "<b>x</b>" not in html and "&lt;b&gt;" in html


def test_default_day_is_the_local_date_of_now(db: Database) -> None:
    html = report.generate_report(db, now=ts(23.9), tz=TZ)
    assert "2025-03-12" in html  # 04:54 UTC on the 13th


def test_write_report_replaces_the_file(db: Database, tmp_path) -> None:
    path = tmp_path / "r.html"
    path.write_text("old", encoding="utf-8")
    assert report.write_report(db, path, day=DAY, now=ts(16), tz=TZ) == path
    assert path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert not (tmp_path / "r.html.tmp").exists()


def test_cli_rejects_a_bad_week_date() -> None:
    with pytest.raises(SystemExit):
        cli.main(["report", "--week", "12/03/2025"])


def drain(html: str) -> float | None:
    v = value(html, "day", "drain_pct_per_h:monitoring")
    return None if v is None else float(v)


def test_drain_ignores_a_rise_and_any_pair_touching_ac(db: Database) -> None:
    metric(db, ts(8), battery_percent=80, on_ac=0)
    metric(db, ts(9), battery_percent=85, on_ac=0)  # rose: ignored
    metric(db, ts(10), battery_percent=80, on_ac=0)  # 5 %/h
    metric(db, ts(11), battery_percent=70, on_ac=1)  # AC on the later side: ignored
    metric(db, ts(12), battery_percent=60, on_ac=0)  # AC on the earlier side: ignored
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    assert drain(html) == pytest.approx(5.0)


@pytest.mark.parametrize(("gap_h", "counted"), [(1.5, True), (1.51, False)])
def test_drain_gap_factor_boundary(db: Database, gap_h: float, counted: bool) -> None:
    assert report.DRAIN_GAP_FACTOR == 1.5
    metric(db, ts(8), battery_percent=80, on_ac=0)
    metric(db, ts(8 + gap_h), battery_percent=77, on_ac=0)
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    assert (drain(html) is not None) is counted


def test_drain_treats_a_split_minute_as_one_reading(db: Database) -> None:
    metric(db, ts(8), 60.0, battery_percent=80, on_ac=0)
    metric(db, ts(8) + 60, 40.0, battery_percent=80, on_ac=0)
    metric(db, ts(8) + 60, 20.0, battery_percent=80, on_ac=0, paused_reasons="locked")
    metric(db, ts(8) + 120, 60.0, battery_percent=79, on_ac=0)
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    assert drain(html) == pytest.approx(30.0)  # 1 % over 2 minutes


@pytest.mark.parametrize(("n", "raw"), [(4, True), (5, False)])
def test_min_alerts_for_rate_boundary(db: Database, n: int, raw: bool) -> None:
    assert report.MIN_ALERTS_FOR_RATE == 5
    metric(db, ts(12), 4 * 3600.0)
    for i in range(n):
        db.insert_alert(
            ts=ts(9 + i / 10), label="slouch", bad_fraction=0.9, window_s=60,
            model_version="m", calibration_id=None,
        )  # fmt: skip
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    text = re.search(r'data-figure="false_alert_rate" data-scope="day"[^>]*>([^<]*)<', html)
    assert text and (f"of {n} alerts" in text.group(1)) is raw


def test_pause_table_notes_that_reasons_overlap(db: Database) -> None:
    metric(db, ts(9), 600.0, paused_reasons="locked,in_call:meet")
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    assert value(html, "day", "pause_s:locked") == "600"
    assert value(html, "day", "pause_s:in_call:meet") == "600"
    assert report.PAUSE_NOTE in html


def test_report_reads_through_a_read_only_connection(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "ro.db")
    metric(db, ts(9))

    def shared(*_a: object) -> None:
        raise AssertionError("report used the shared connection")

    monkeypatch.setattr(db, "query", shared)
    html = report.generate_report(db, day=DAY, now=ts(16), tz=TZ)
    assert value(html, "day", "monitored_s") == "3600"
    with db.read_only() as reader, pytest.raises(Exception, match="readonly"):
        reader.query("DELETE FROM metrics")
    db.close()


@pytest.mark.parametrize(("day", "hours"), [(date(2025, 3, 30), 23), (date(2025, 10, 26), 25)])
def test_dst_days_run_midnight_to_midnight(db: Database, day: date, hours: int) -> None:
    from zoneinfo import ZoneInfo

    london = ZoneInfo("Europe/London")
    start = report._midnight(day, london)
    end = report._midnight(day + timedelta(days=1), london)
    assert end - start == hours * 3600
    metric(db, end - 1, 3600.0)  # 23:59:59 local: still this day
    metric(db, end, 3600.0)  # next midnight: the next day
    html = report.generate_report(db, day=day, now=end + 3600, tz=london)
    assert value(html, "day", "monitored_s") == "3600"


def test_open_report_failure_shows_a_message(db, calibration, clock) -> None:  # type: ignore[no-untyped-def]
    from acceptance.conftest import Rig
    from posture_checker import app as app_mod

    rig = Rig(db, calibration, clock)

    def broken(_path: object) -> None:
        raise OSError("no browser")

    app = app_mod.App(
        monitor=rig.monitor, supervisor=rig.supervisor, db=db, notifier=rig.notifier,
        clock=clock, report_opener=broken,
    )  # fmt: skip
    app.open_report()
    assert rig.notifier.messages == [app_mod.REPORT_ERROR]
