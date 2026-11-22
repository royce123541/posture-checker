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
