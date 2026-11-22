"""Stage 3 coverage addition (spec: docs/specs/stage-3-coverage.md, C1-C6).

Uses the data-figure / data-scope / data-value contract of test_stage_3.py
(see its header) and the same ``generate_report`` and seeding helpers.
New figure keys the implementer must emit, for scopes "day" and "week":
    at_desk_s   seconds at the desk (replaces monitored_s)
    away_s      seconds away
    paused_s    total pause seconds
    excluded    only present when something was left out; its text is exactly
                "N alerts and M samples before recording started or during gaps
                are not counted." (no data-value needed). Absent otherwise.
Reading of the spec: a moment is covered when it falls in (ts - interval_s, ts]
of some metrics row; the period's alerts/samples/labels outside every window
are excluded from all posture figures and counted in ``excluded``.
Time at the desk is computed per monitoring row (C3); away_s is
interval_s - at-desk time for rows that have samples.
"""

from __future__ import annotations

import re

import pytest

from acceptance.conftest import insert_metrics, local_ts, seed_report_db
from acceptance.test_stage_3 import DAY, NOW, TZ, figure, parse, value
from posture_checker.report import generate_report

EXCLUDED = re.compile(
    r"(\d+) alerts? and (\d+) samples? before recording started or during gaps are not counted\."
)


def page_for(db):
    return parse(generate_report(db, day=DAY, now=NOW, tz=TZ))


def alert(db, hour: float, label: str) -> None:
    db.insert_alert(
        ts=local_ts(TZ, DAY, hour), label=label, bad_fraction=0.9, window_s=60.0,
        model_version="rules-v9-test", calibration_id=None,
    )  # fmt: skip


def sample(db, hour: float, label: str) -> None:
    db.insert_sample(
        ts=local_ts(TZ, DAY, hour), state="present", confidence=0.9, label=label,
        latency_ms=5.0, model_version="rules-v9-test",
    )  # fmt: skip


def label(db, hour: float, name: str) -> None:
    db.insert_label(
        ts=local_ts(TZ, DAY, hour), label=name, source="tray", sample_id=None,
        model_version=None, predicted_label=None,
    )  # fmt: skip


def test_c1_c5_uncovered_alerts_and_samples_are_excluded_and_counted(file_db) -> None:
    """C1, C5: before the first row and in a gap, nothing counts; the line gives the counts."""
    insert_metrics(file_db, local_ts(TZ, DAY, 10.0), 3600.0)  # covers (09:00, 10:00]
    insert_metrics(file_db, local_ts(TZ, DAY, 12.0), 3600.0)  # covers (11:00, 12:00]; gap 10-11
    alert(file_db, 8.5, "slouch")  # before the first row
    alert(file_db, 9.5, "too_close")
    alert(file_db, 10.5, "slouch")  # in the gap
    alert(file_db, 11.5, "too_close")
    sample(file_db, 8.5, "slouch")
    sample(file_db, 9.5, "good")
    sample(file_db, 10.5, "slouch")
    sample(file_db, 11.5, "good")
    label(file_db, 8.0, "slouch")  # uncovered
    label(file_db, 9.6, "forward_head")
    page = page_for(file_db)
    for scope in ("day", "week"):
        assert value(page, scope, "alerts_total") == 2
        assert value(page, scope, "alerts_label:too_close") == 2
        slouch = page.figures.get((scope, "alerts_label:slouch"))
        assert slouch is None or slouch.value in (None, "", "0", "0.0")
        assert value(page, scope, "good_share") == pytest.approx(1.0)
        assert value(page, scope, "labels:forward_head") == 1
        labels_slouch = page.figures.get((scope, "labels:slouch"))
        assert labels_slouch is None or labels_slouch.value in (None, "", "0", "0.0")
        assert value(page, scope, "alerts_per_hour") == pytest.approx(2 / 2)  # 2 at-desk hours
        text = figure(page, scope, "excluded").text
        match = EXCLUDED.fullmatch(text)
        assert match, text
        assert (match.group(1), match.group(2)) == ("2", "2")


def test_c2_c3_a_row_with_10_present_and_10_away_splits_the_time(file_db) -> None:
    """C2, C3: 60 s with 10 present and 10 away samples is 30 s at the desk, 30 s away."""
    insert_metrics(file_db, local_ts(TZ, DAY, 10.0), 60.0, n_present=10, n_away=10)
    page = page_for(file_db)
    for scope in ("day", "week"):
        assert value(page, scope, "at_desk_s") == pytest.approx(30.0)
        assert value(page, scope, "away_s") == pytest.approx(30.0)
        assert "monitored_s" not in {key for (sc, key) in page.figures if sc == scope}


def test_c2_at_desk_plus_away_equals_monitoring_time_and_paused_is_separate(file_db) -> None:
    """C2, C3: low-confidence counts as at the desk; paused rows are neither."""
    insert_metrics(file_db, local_ts(TZ, DAY, 9.0), 600.0, n_present=3, n_low=3, n_away=6)
    insert_metrics(file_db, local_ts(TZ, DAY, 10.0), 3600.0, n_present=20)
    insert_metrics(file_db, local_ts(TZ, DAY, 11.0), 1800.0, n_present=0, n_away=10)
    insert_metrics(file_db, local_ts(TZ, DAY, 12.0), 900.0, reasons="locked")
    page = page_for(file_db)
    for scope in ("day", "week"):
        at_desk = value(page, scope, "at_desk_s")
        away = value(page, scope, "away_s")
        assert at_desk == pytest.approx(300 + 3600)
        assert away == pytest.approx(300 + 1800)
        assert at_desk + away == pytest.approx(600 + 3600 + 1800)
        assert value(page, scope, "paused_s") == pytest.approx(900)


def test_c4_alerts_per_hour_uses_at_desk_time_and_its_raw_count_rule(file_db) -> None:
    """C4: 2 h covered but 30 min at the desk; 6 alerts give 12 per hour with raw counts."""
    insert_metrics(file_db, local_ts(TZ, DAY, 12.0), 7200.0, n_present=5, n_away=15)
    for i in range(6):
        alert(file_db, 11.0 + i / 100, "slouch")
    page = page_for(file_db)
    for scope in ("day", "week"):
        assert value(page, scope, "at_desk_s") == pytest.approx(1800)
        assert value(page, scope, "alerts_per_hour") == pytest.approx(6 / 0.5)
        # Under 1 h at the desk: the raw count sits beside the rate, though 6 >= 5 alerts.
        assert "6 alerts" in figure(page, scope, "alerts_per_hour").text


def test_c5_no_excluded_line_when_everything_is_covered(file_db, calibration) -> None:
    """C5: the seeded week has no uncovered alerts or samples, so no line is shown."""
    seed_report_db(file_db, calibration, TZ, DAY)
    page = page_for(file_db)
    for scope in ("day", "week"):
        assert (scope, "excluded") not in page.figures
    assert "not counted" not in generate_report(file_db, day=DAY, now=NOW, tz=TZ)


def test_c6_health_figures_ignore_coverage(file_db) -> None:
    """C6: CPU and memory figures are unchanged by the coverage rules."""
    for i, cpu in enumerate((2.0, 4.0)):
        insert_metrics(file_db, local_ts(TZ, DAY, 9.0 + i), 3600.0, cpu=cpu, rss=100.0 + 20 * i)
    page = page_for(file_db)
    assert value(page, "day", "cpu_mean:monitoring") == pytest.approx(3.0)
    assert value(page, "day", "rss_max_mb:monitoring") == pytest.approx(120.0)
