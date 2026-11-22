"""Stage 3 acceptance tests (spec: docs/specs/stage-3.md, behaviours 1-17).

Interfaces the implementer must build (imported lazily so a missing module
fails the test, not collection). Where the spec is silent the shape below is
this file's reading of it; all tests use fakes, no psutil and no webcam.

posture_checker.config.Config
    new field ``metrics_interval_s: float = 60``.
posture_checker.storage.db
    new table ``metrics`` (created by ``Database``) with exactly these columns
    (an ``id`` primary key is allowed): ts, interval_s, cpu_percent, rss_mb,
    n_present, n_low_confidence, n_away, latency_p50_ms, latency_p95_ms,
    mean_confidence, battery_percent, on_ac, camera_mode, paused_reasons,
    model_version, calibration_id, metrics_cost_ms. No BLOB or JSON columns.
    ``Database.prune_metrics(now: float, max_age_days: int = 90,
    max_rows: int = 200_000) -> int``: delete rows older than max_age_days
    (by ts), then delete the oldest until at most max_rows remain.
posture_checker.metrics
    ``MetricsRecorder(db, config, probe, clock, *, timer=time.perf_counter,
    paused_reasons=lambda: (), model_version=lambda: None,
    calibration_id=lambda: None)`` (all keyword arguments; the last three are
    zero-argument callables read when a row is written). Started at
    construction: the first window begins at ``clock()``.
      .observe(sample)  take one ``monitor.Sample`` (any state, PAUSED
                        included); kept in memory, bucketed by ``sample.ts``.
      .poll()           write one row per elapsed ``metrics_interval_s`` window.
                        Row ``ts`` = window end; window = (end - interval, end];
                        only samples in the window are counted.
      .start()          start-up hook: prune (behaviour 6).
      .stop()           write the final partial row with its real ``interval_s``
                        (now - end of the last row, or - start if none).
      .prune()          same prune as ``Database.prune_metrics`` at ``clock()``.
    Row content: n_present / n_low_confidence / n_away count samples by state;
    latency percentiles use the stored samples' latency (numpy linear
    percentile; the tests choose data where common definitions agree);
    mean_confidence is over present samples only (NULL if none);
    on_ac is 1/0, NULL when ``battery().percent`` is None; paused_reasons is
    comma-separated; metrics_cost_ms is measured with ``timer`` (seconds).
    Probe (injected, see FakeSystemProbe in tests/acceptance/conftest.py):
      probe.cpu_percent() -> float | None, probe.rss_mb() -> float,
      probe.battery() -> posture_checker.policy.pausing.BatteryStatus.
    A probe that raises gives NULL for that field and one WARNING+ log line per
    failing field in total, not one per minute.
posture_checker.monitor.Monitor
    new optional kwarg ``recorder``; ``step()`` calls ``recorder.observe(sample)``
    then ``recorder.poll()`` for every sample (paused ones too).
posture_checker.app.App
    new optional kwargs ``recorder`` (``run()`` calls ``recorder.stop()`` when
    it ends and ``recorder.start()`` when it begins) and ``report_opener:
    Callable[[Path], None]`` (default: os.startfile). New method
    ``open_report()``: writes ``paths.app_dir() / "report.html"`` for today
    (local time of ``self.clock``) and calls the opener with that path.
    ``tray_actions()`` has the key ``tray.OPEN_REPORT``.
posture_checker.tray
    ``OPEN_REPORT`` action id; ``menu_model`` has a top-level item labelled
    "Open report" directly above "Open data folder".
posture_checker.cli
    ``report [--out PATH] [--week YYYY-MM-DD]`` (``--week`` = the last day of
    the 7-day period). Default path ``paths.app_dir() / "report.html"``. Returns 0.
posture_checker.report
    ``generate_report(db, *, day=None, now=None, tz=None) -> str`` (HTML);
    ``day`` is a ``datetime.date`` (default: local date of ``now``), ``now``
    epoch seconds (default: time.time()), ``tz`` a tzinfo (default: local).
    The day runs from local midnight; the week is the 7 days ending ``day``.
    ``write_report(db, path, *, day=None, now=None, tz=None) -> Path``.
    No network module imported.
    HTML contract (so figures can be asserted without scraping prose): every
    figure is an element with ``data-figure="<key>"`` and ``data-scope``;
    its text is what the reader sees, and ``data-value`` holds the plain number
    (seconds, fractions 0-1, counts, %/h) or is absent when there is no data.
    Scopes: "day" (the chosen day), "week" (7 days ending it), "page" (page-wide).
    Keys per scope day/week:
      monitored_s, good_share, alerts_total, alerts_wrong, alerts_per_hour,
      false_alert_rate, alerts_label:<label>, labels:<label>,
      pause_s:<reason>,
      cpu_mean:<mode>, cpu_p95:<mode>, rss_mean_mb:<mode>, rss_max_mb:<mode>,
      latency_p50_ms:<mode>, latency_p95_ms:<mode>, mean_confidence:<mode>,
      drain_pct_per_h:<mode>      where <mode> is "monitoring" or "paused".
    Scope "page": model_versions, calibration_ids (ids and their dates
    YYYY-MM-DD), app_version, generated_at ("YYYY-MM-DD HH:MM" local),
    period_end (YYYY-MM-DD).
    No data reads "not measured yet" or "no data" (never 0). Rates with a
    denominator under 1 h or under 5 alerts show raw counts in the text
    ("1 of 2 alerts"; "2 alerts" for alerts per hour).
    The weekly charts are ``<svg data-chart="posture_share|alerts_per_hour|
    false_alert_rate">``, each with 7 ``<rect class="bar">`` elements.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from html.parser import HTMLParser
from importlib import import_module
from pathlib import Path

import numpy as np
import pytest

from acceptance.conftest import (
    METRIC_COLUMNS,
    FakeSystemProbe,
    insert_metrics,
    local_ts,
    make_recorder,
    seed_report_db,
)
from conftest import FakeClock
from posture_checker import paths
from posture_checker.config import Config

START = 1_742_000_000.0  # a real date (March 2025), so local-time conversion is safe
TZ = timezone(timedelta(hours=2))
DAY = date(2025, 3, 12)
NOW = local_ts(TZ, DAY, 15.0)
NO_DATA = ("not measured yet", "no data")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


def report_mod():
    return import_module("posture_checker.report")


def html_for(db, **kw) -> str:
    kw.setdefault("day", DAY)
    kw.setdefault("now", NOW)
    kw.setdefault("tz", TZ)
    return report_mod().generate_report(db, **kw)


# --- HTML helpers --------------------------------------------------------------


@dataclass
class Figure:
    text: str
    value: str | None


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.figures: dict[tuple[str, str], Figure] = {}
        self.bars: dict[str, int] = {}
        self.style_text = ""
        self._active: list[list] = []  # [tag, depth, text parts, (scope, key), value]
        self._chart: str | None = None
        self._in_style = False

    def handle_starttag(self, tag, attrs) -> None:
        a = dict(attrs)
        self.tags.append((tag, a))
        for item in self._active:
            if item[0] == tag:
                item[1] += 1
        if "data-figure" in a:
            self._active.append(
                [
                    tag,
                    1,
                    [],
                    (a.get("data-scope") or "", a["data-figure"] or ""),
                    a.get("data-value"),
                ]
            )
        if tag == "svg":
            self._chart = a.get("data-chart")
        if tag == "rect" and self._chart and "bar" in (a.get("class") or "").split():
            self.bars[self._chart] = self.bars.get(self._chart, 0) + 1
        self._in_style = tag == "style"

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag) -> None:
        if tag == "svg":
            self._chart = None
        if tag == "style":
            self._in_style = False
        for item in list(self._active):
            if item[0] == tag:
                item[1] -= 1
                if item[1] == 0:
                    text = " ".join("".join(item[2]).split())
                    self.figures.setdefault(item[3], Figure(text, item[4]))
                    self._active.remove(item)

    def handle_data(self, data) -> None:
        if self._in_style:
            self.style_text += data
        for item in self._active:
            item[2].append(data)


def parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    page.close()
    return page


def figure(page: _Page, scope: str, key: str) -> Figure:
    assert (scope, key) in page.figures, f"no figure {key!r} in scope {scope!r}"
    return page.figures[(scope, key)]


def value(page: _Page, scope: str, key: str) -> float:
    fig = figure(page, scope, key)
    assert fig.value not in (None, ""), f"{scope}/{key} has no data-value (text: {fig.text!r})"
    return float(fig.value)  # type: ignore[arg-type]


# --- shared setups ---------------------------------------------------------------


def minute_samples(db, start: float, end: float):
    return db.query("SELECT * FROM samples WHERE ts > ? AND ts <= ? ORDER BY ts", (start, end))


def mixed_script() -> list[tuple[str, float]]:
    """30 samples: 15 present, 9 low confidence, 6 away.

    Latencies sorted: 5..16, 20 x6, 25..33, 50 x3, so p50 is 20 and p95 is 50
    under every common percentile definition.
    """
    latencies = (
        [5.0 + i for i in range(12)] + [20.0] * 6 + [25.0 + i for i in range(9)] + [50.0] * 3
    )
    kinds = ["present"] * 15 + ["low"] * 9 + ["away"] * 6
    rng = np.random.default_rng(3)
    rng.shuffle(latencies)
    rng.shuffle(kinds)
    return list(zip(kinds, latencies, strict=True))


# === Metrics recorder ===============================================================


def test_b1_one_row_every_interval_with_real_interval_s(make_metrics_rig) -> None:
    """Behaviour 1: a row every 60 s measured by the injected clock; Config default 60."""
    assert Config().metrics_interval_s == 60
    mr = make_metrics_rig()
    mr.drive(3)  # 180 fake seconds
    rows = mr.rows()
    assert len(rows) == 3
    assert [r["interval_s"] for r in rows] == [60, 60, 60]
    for i, row in enumerate(rows, start=1):
        assert row["ts"] == pytest.approx(START + 60 * i, abs=1.0)
    assert [rows[i + 1]["ts"] - rows[i]["ts"] for i in range(2)] == [
        pytest.approx(60.0),
        pytest.approx(60.0),
    ]


def test_b1_rows_are_still_written_while_paused(make_metrics_rig) -> None:
    """Behaviour 1: paused minutes still get a row, carrying the pause reason."""
    mr = make_metrics_rig()
    mr.rig.supervisor.pause_manual(None)
    mr.drive(3)
    rows = mr.rows()
    assert len(rows) == 3
    assert all(r["interval_s"] == 60 for r in rows)
    assert all(r["paused_reasons"] == "manual" for r in rows)
    assert all(r["n_present"] == r["n_low_confidence"] == r["n_away"] == 0 for r in rows)


def test_b2_row_values_equal_probe_values_and_sample_statistics(make_metrics_rig, db) -> None:
    """Behaviour 2: column set, probe values, battery, mode, lineage ids."""
    mr = make_metrics_rig(script=mixed_script())
    mr.probe.cpu, mr.probe.rss = 12.5, 180.0
    mr.probe.set_battery(64, plugged=False)
    mr.drive(1)
    cols = {r["name"] for r in db.query("PRAGMA table_info(metrics)")} - {"id"}
    assert cols == {c.strip() for c in METRIC_COLUMNS.split(",")}
    (row,) = mr.rows()
    stored = minute_samples(db, START, START + 60)
    assert row["interval_s"] == 60
    assert row["cpu_percent"] == pytest.approx(12.5)
    assert row["rss_mb"] == pytest.approx(180.0)
    assert row["battery_percent"] == 64
    assert row["on_ac"] == 0
    assert row["camera_mode"] == mr.rig.config.camera_mode
    assert row["paused_reasons"] == ""
    assert row["model_version"] == mr.rig.monitor.model.version
    assert row["calibration_id"] == mr.rig.monitor.calibration.id
    present = [s for s in stored if s["state"] == "present"]
    assert row["n_present"] == len(present) == 15
    assert row["n_low_confidence"] == sum(s["state"] == "low_confidence" for s in stored) == 9
    assert row["n_away"] == sum(s["state"] == "away" for s in stored) == 6
    assert row["mean_confidence"] == pytest.approx(np.mean([s["confidence"] for s in present]))


def test_b2_on_ac_and_battery_null_without_a_battery(make_metrics_rig) -> None:
    """Behaviour 2: no battery gives NULL battery_percent and NULL on_ac; plugged in gives 1."""
    mr = make_metrics_rig()  # default probe: no battery
    mr.drive(1)
    (row,) = mr.rows()
    assert row["battery_percent"] is None and row["on_ac"] is None

    mr2 = make_metrics_rig()
    mr2.probe.set_battery(90, plugged=True)
    mr2.drive(1)
    assert mr2.rows()[-1]["on_ac"] == 1


def test_b2_mean_confidence_is_null_without_present_samples(make_metrics_rig) -> None:
    """Behaviour 2: mean_confidence is NULL when the minute had no present samples."""
    mr = make_metrics_rig(script=[("away", 4.0)] * 40)
    mr.drive(1)
    (row,) = mr.rows()
    assert row["n_present"] == 0 and row["mean_confidence"] is None


def test_b3_counts_and_statistics_match_the_samples_table(make_metrics_rig, db) -> None:
    """Behaviour 3: 30 mixed samples; row counts, p50, p95 and mean match the samples rows."""
    mr = make_metrics_rig(script=mixed_script())
    mr.drive(1)
    (row,) = mr.rows()
    stored = minute_samples(db, START, START + 60)
    assert len(stored) == 30
    lat = [s["latency_ms"] for s in stored]
    assert row["latency_p50_ms"] == pytest.approx(np.percentile(lat, 50)) == pytest.approx(20.0)
    assert row["latency_p95_ms"] == pytest.approx(np.percentile(lat, 95)) == pytest.approx(50.0)
    assert (row["n_present"], row["n_low_confidence"], row["n_away"]) == (15, 9, 6)
    present_conf = [s["confidence"] for s in stored if s["state"] == "present"]
    assert row["mean_confidence"] == pytest.approx(np.mean(present_conf))


def test_b4_failing_probes_store_null_log_once_and_keep_monitoring(
    make_metrics_rig, db, caplog
) -> None:
    """Behaviour 4: CPU and battery raise; NULLs, few log lines, samples keep coming."""
    caplog.set_level(logging.DEBUG)
    mr = make_metrics_rig()
    mr.probe.cpu_error = RuntimeError("cpu probe failed")
    mr.probe.battery_error = RuntimeError("battery probe failed")
    mr.drive(3)
    rows = mr.rows()
    assert len(rows) == 3
    for row in rows:
        assert row["cpu_percent"] is None
        assert row["battery_percent"] is None and row["on_ac"] is None
        assert row["rss_mb"] == pytest.approx(180.0)  # the other readings still work
    assert db.query("SELECT COUNT(*) AS n FROM samples")[0]["n"] == 91
    warnings = [
        r for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith("posture")
    ]
    assert 1 <= len(warnings) <= 2, [
        r.getMessage() for r in warnings
    ]  # once per field, not per minute


def test_b5_recorder_cost_is_stored_and_small(make_metrics_rig) -> None:
    """Behaviour 5: metrics_cost_ms is present and under 50 ms with 30 samples (real timer)."""
    mr = make_metrics_rig(script=mixed_script())
    mr.drive(1)
    (row,) = mr.rows()
    assert row["metrics_cost_ms"] is not None
    assert 0 <= row["metrics_cost_ms"] < 50


def test_b6_prune_removes_rows_older_than_90_days(file_db) -> None:
    """Behaviour 6: rows 100 and 10 days old; only the 10-day row remains."""
    now = START
    insert_metrics(file_db, now - 100 * 86400)
    insert_metrics(file_db, now - 10 * 86400)
    file_db.prune_metrics(now)
    rows = file_db.query("SELECT ts FROM metrics")
    assert [r["ts"] for r in rows] == [pytest.approx(now - 10 * 86400)]


def test_b6_prune_caps_rows_deleting_oldest_first(file_db) -> None:
    """Behaviour 6: with the cap at 100 and 150 rows, the 100 newest remain."""
    now = START
    for i in range(150):
        insert_metrics(file_db, now - (150 - i) * 60)  # i=149 is the newest
    file_db.prune_metrics(now, max_rows=100)
    ts = sorted(r["ts"] for r in file_db.query("SELECT ts FROM metrics"))
    assert len(ts) == 100
    assert ts[0] == pytest.approx(now - 100 * 60)
    assert ts[-1] == pytest.approx(now - 60)


def test_b6_recorder_prunes_at_startup_and_once_a_day(file_db, clock) -> None:
    """Behaviour 6: ``start()`` prunes, and a day later the next poll prunes again."""
    probe = FakeSystemProbe()
    insert_metrics(file_db, clock.now - 100 * 86400)
    recorder = make_recorder(file_db, Config(), probe, clock)
    recorder.start()
    assert file_db.query("SELECT COUNT(*) AS n FROM metrics")[0]["n"] == 0

    insert_metrics(file_db, clock.now - 100 * 86400)  # stale, planted after start-up
    insert_metrics(file_db, clock.now - 10 * 86400)
    clock.advance(25 * 3600)
    recorder.poll()
    ages = [(clock.now - r["ts"]) / 86400 for r in file_db.query("SELECT ts FROM metrics")]
    assert ages and all(age < 90 for age in ages)
    assert any(age == pytest.approx(10 + 25 / 24, abs=0.01) for age in ages)


def test_b7_stopping_writes_a_final_partial_row(db, clock) -> None:
    """Behaviour 7: stop after 25 fake seconds writes one row with interval_s 25."""
    recorder = make_recorder(db, Config(), FakeSystemProbe(), clock)
    clock.advance(25)
    recorder.stop()
    rows = db.query("SELECT * FROM metrics")
    assert len(rows) == 1
    assert rows[0]["interval_s"] == pytest.approx(25)
    assert rows[0]["ts"] == pytest.approx(START + 25)


def test_b7_app_run_ends_with_the_partial_row(make_metrics_rig, db, clock) -> None:
    """Behaviour 7: ``App.run`` stopping cleanly after 25 s records the partial minute."""
    from posture_checker.app import App

    mr = make_metrics_rig()
    app = App(
        monitor=mr.rig.monitor,
        supervisor=mr.rig.supervisor,
        db=db,
        notifier=mr.rig.notifier,
        clock=clock,
        recorder=mr.recorder,
    )

    class StopAfter25:
        flag = False

        def is_set(self) -> bool:
            return self.flag

        def wait(self, timeout: float | None = None) -> bool:
            clock.advance(25)
            self.flag = True
            return True

    app.run(StopAfter25())  # type: ignore[arg-type]
    rows = mr.rows()
    assert len(rows) == 1
    assert rows[0]["interval_s"] == pytest.approx(25)


# === Report: opening and generating =====================================================


def test_b8_tray_has_open_report_above_open_data_folder() -> None:
    """Behaviour 8: the menu lists "Open report" directly above "Open data folder"."""
    from posture_checker import tray

    labels = [item.label for item in tray.menu_model(tray.TrayState())]
    assert "Open report" in labels
    assert labels.index("Open report") == labels.index("Open data folder") - 1
    item = next(i for i in tray.menu_model(tray.TrayState()) if i.label == "Open report")
    assert item.action == tray.OPEN_REPORT and item.enabled


def test_b8_open_report_writes_the_file_and_calls_the_opener(make_rig, db, clock) -> None:
    """Behaviour 8: the tray action writes report.html in the data folder, then opens it."""
    from posture_checker import tray
    from posture_checker.app import App

    rig = make_rig()
    opened: list[Path] = []
    app = App(
        monitor=rig.monitor,
        supervisor=rig.supervisor,
        db=db,
        notifier=rig.notifier,
        clock=clock,
        report_opener=opened.append,
    )
    app.tray_actions()[tray.OPEN_REPORT]()
    expected = paths.app_dir() / "report.html"
    assert [Path(p) for p in opened] == [expected]
    assert expected.exists()
    assert "<html" in expected.read_text(encoding="utf-8").lower()


def test_b8_cli_report_out_writes_the_file(tmp_path) -> None:
    """Behaviour 8: ``posture-checker report --out PATH`` writes the file."""
    from posture_checker import cli

    out = tmp_path / "elsewhere" / "r.html"
    out.parent.mkdir()
    assert cli.main(["report", "--out", str(out)]) == 0
    assert "<html" in out.read_text(encoding="utf-8").lower()


def test_b8_cli_report_defaults_to_the_data_folder_and_takes_week(capsys) -> None:
    """Behaviour 8: no --out writes report.html in the data folder; --week ends the period."""
    from posture_checker import cli

    assert cli.main(["report", "--week", "2025-03-12"]) == 0
    text = (paths.app_dir() / "report.html").read_text(encoding="utf-8")
    page = parse(text)
    assert "2025-03-12" in figure(page, "page", "period_end").text


# === Report: content ====================================================================


def external(value: str | None) -> bool:
    return bool(value and re.match(r"\s*(https?:)?//", value, re.IGNORECASE))


def test_b9_report_is_self_contained(file_db, calibration) -> None:
    """Behaviour 9: no external URL, script src, image or link tag; renders offline."""
    seed_report_db(file_db, calibration, TZ, DAY)
    html = html_for(file_db)
    page = parse(html)
    names = [tag for tag, _ in page.tags]
    assert "img" not in names and "link" not in names and "iframe" not in names
    for tag, attrs in page.tags:
        assert not (tag == "script" and attrs.get("src")), "script src"
        for attr in ("src", "href", "action", "data", "poster"):
            assert not external(attrs.get(attr)), f"external {attr} on <{tag}>"
        assert "url(" not in (attrs.get("style") or "").lower()
    assert "@import" not in page.style_text.lower()
    assert "url(" not in page.style_text.lower()
    assert not re.search(r"@import|<link\b|<img\b|<script[^>]*\ssrc=", html, re.IGNORECASE)
    assert "<svg" in html  # charts are inline SVG


def test_b10_day_and_week_figures_are_computed_from_stored_rows(file_db, calibration) -> None:
    """Behaviour 10: monitored time, posture share, alerts, false rate, labels, pauses."""
    seed_report_db(file_db, calibration, TZ, DAY)
    page = parse(html_for(file_db))
    expected = {
        "day": {
            "monitored_s": 4 * 3600,
            "good_share": 0.7,
            "alerts_total": 3,
            "alerts_wrong": 1,
            "alerts_per_hour": 3 / 4,
            "false_alert_rate": 1 / 3,
            "alerts_label:slouch": 2,
            "alerts_label:forward_head": 1,
            "labels:slouch": 1,
            "labels:forward_head": 1,
            "pause_s:in_call:meet": 1800,
        },
        "week": {
            "monitored_s": 6 * 3600,
            "good_share": 17 / 20,
            "alerts_total": 4,
            "alerts_wrong": 1,
            "alerts_per_hour": 4 / 6,
            "false_alert_rate": 1 / 4,
            "alerts_label:slouch": 2,
            "alerts_label:forward_head": 1,
            "alerts_label:too_close": 1,
            "labels:slouch": 1,
            "labels:forward_head": 1,
            "pause_s:in_call:meet": 1800,
        },
    }
    for scope, figures in expected.items():
        for key, want in figures.items():
            assert value(page, scope, key) == pytest.approx(want), f"{scope}/{key}"
    # Small denominators carry their raw counts (behaviour 13).
    assert "1 of 3" in figure(page, "day", "false_alert_rate").text
    assert "1 of 4" in figure(page, "week", "false_alert_rate").text


def test_b11_weekly_charts_have_seven_bars(file_db, calibration) -> None:
    """Behaviour 11: posture share, alerts per hour and false-alert rate, one bar per day."""
    seed_report_db(file_db, calibration, TZ, DAY)
    page = parse(html_for(file_db))
    for chart in ("posture_share", "alerts_per_hour", "false_alert_rate"):
        assert page.bars.get(chart) == 7, f"chart {chart}: {page.bars}"


def test_b12_system_health_for_monitoring_and_paused_minutes(file_db) -> None:
    """Behaviour 12: CPU mean/p95, memory mean/max, latency, confidence, split by mode."""
    for i in range(21):  # 21 values: mean 11 and p95 20 under any common definition
        ts = local_ts(TZ, DAY, 8.0 + i / 6)
        insert_metrics(file_db, ts, 600.0, cpu=1.0 + i, rss=100.0 + i, confidence=0.8)
        file_db.insert_sample(
            ts=ts - 5, state="present", confidence=0.8, label="good", latency_ms=1.0 + i,
            model_version="rules-v9-test",
        )  # fmt: skip
    for i in range(5):
        insert_metrics(
            file_db, local_ts(TZ, DAY, 13.0 + i / 6), 600.0, cpu=1.0, rss=50.0, reasons="locked"
        )
    page = parse(html_for(file_db))
    for scope in ("day", "week"):
        assert value(page, scope, "cpu_mean:monitoring") == pytest.approx(11.0)
        assert value(page, scope, "cpu_p95:monitoring") == pytest.approx(20.0)
        assert value(page, scope, "rss_mean_mb:monitoring") == pytest.approx(110.0)
        assert value(page, scope, "rss_max_mb:monitoring") == pytest.approx(120.0)
        assert value(page, scope, "latency_p50_ms:monitoring") == pytest.approx(11.0)
        assert value(page, scope, "latency_p95_ms:monitoring") == pytest.approx(20.0)
        assert value(page, scope, "mean_confidence:monitoring") == pytest.approx(0.8)
        assert value(page, scope, "cpu_mean:paused") == pytest.approx(1.0)
        assert value(page, scope, "rss_max_mb:paused") == pytest.approx(50.0)


def test_b12_battery_drain_uses_only_consecutive_on_battery_rows(file_db) -> None:
    """Behaviour 12: 80% to 70% over 2 on-battery hours is 5.0 %/h; AC rows are excluded."""
    rows = [  # (hour, percent, on_ac)
        (8, 100, 1),  # AC, then unplugged: the step 100 -> 80 is not drain
        (9, 80, 0),
        (10, 75, 0),
        (11, 70, 0),
        (12, 100, 1),  # plugged in again
        (13, 100, 1),
        (14, 90, 0),  # a lone on-battery row has no consecutive partner
    ]
    for hour, percent, on_ac in rows:
        insert_metrics(file_db, local_ts(TZ, DAY, hour), 3600.0, battery=percent, on_ac=on_ac)
    page = parse(html_for(file_db))
    assert value(page, "day", "drain_pct_per_h:monitoring") == pytest.approx(5.0)


def test_b13_empty_database_reads_no_data_everywhere(db) -> None:
    """Behaviour 13: nothing stored; every figure says "not measured yet" or "no data"."""
    page = parse(html_for(db))
    keys = [
        "monitored_s",
        "good_share",
        "alerts_per_hour",
        "false_alert_rate",
        *(f"{m}:{mode}" for mode in ("monitoring", "paused") for m in (
            "cpu_mean", "cpu_p95", "rss_mean_mb", "rss_max_mb",
            "latency_p50_ms", "latency_p95_ms", "mean_confidence", "drain_pct_per_h",
        )),
    ]  # fmt: skip
    for scope in ("day", "week"):
        for key in keys:
            fig = figure(page, scope, key)
            assert any(s in fig.text.lower() for s in NO_DATA), f"{scope}/{key}: {fig.text!r}"
            assert fig.value in (None, ""), f"{scope}/{key} carries a number"


def test_b13_small_denominators_show_raw_counts(file_db) -> None:
    """Behaviour 13: under 1 h monitored or under 5 alerts, counts sit beside the rate."""
    insert_metrics(file_db, local_ts(TZ, DAY, 10.0), 1800.0)  # 30 min monitored
    ids = [
        file_db.insert_alert(
            ts=local_ts(TZ, DAY, 9.6 + i / 10), label="slouch", bad_fraction=0.9, window_s=60.0,
            model_version="rules-v9-test", calibration_id=None,
        )
        for i in range(2)
    ]  # fmt: skip
    file_db.mark_alert_wrong(ids[0], local_ts(TZ, DAY, 9.9))
    page = parse(html_for(file_db))
    assert "1 of 2 alerts" in figure(page, "day", "false_alert_rate").text
    assert "2 alerts" in figure(page, "day", "alerts_per_hour").text


def test_b14_page_shows_versions_calibration_app_and_generation_time(file_db, calibration) -> None:
    """Behaviour 14: model version, calibration id and date, app version, generated time."""
    import dataclasses
    from importlib.metadata import version

    file_db.insert_calibration(dataclasses.replace(calibration, ts=local_ts(TZ, DAY, 1.0, -30)))
    seeded = seed_report_db(file_db, calibration, TZ, DAY)
    page = parse(html_for(file_db))
    assert seeded["model_version"] in figure(page, "page", "model_versions").text
    cal_text = figure(page, "page", "calibration_ids").text
    assert re.search(rf"\b{seeded['calibration_id']}\b", cal_text)
    assert "2025-03-12" in cal_text
    assert version("posture-checker") in figure(page, "page", "app_version").text
    assert "2025-03-12 15:00" in figure(page, "page", "generated_at").text  # local time, not UTC


@pytest.mark.slow
def test_b15_generating_a_week_of_data_takes_under_five_seconds(file_db) -> None:
    """Behaviour 15: about 10,000 metrics rows and 300,000 samples."""
    conn = sqlite3.connect(file_db.path)
    start = local_ts(TZ, DAY, 0.0, -6)
    n_samples = 301_000
    step = 7 * 86400 / n_samples
    states = ("present", "present", "present", "low_confidence", "away")
    conn.executemany(
        "INSERT INTO samples(ts, state, label, confidence, reasons, model_version, latency_ms)"
        " VALUES (?, ?, ?, ?, '', 'rules-v9-test', ?)",
        (
            (
                start + i * step,
                states[i % 5],
                None if states[i % 5] == "away" else ("good" if i % 7 else "slouch"),
                0.9,
                5.0 + i % 9,
            )
            for i in range(n_samples)
        ),
    )
    conn.executemany(
        f"INSERT INTO metrics({METRIC_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            (
                start + (i + 1) * 60, 60, 3.0 + i % 5, 150.0, 20, 5, 5, 5.0, 12.0, 0.9,
                80 - i // 200, 0, "burst", "" if i % 10 else "locked", "rules-v9-test", None, 2.0,
            )
            for i in range(10_080)
        ),
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO alerts(ts, label, bad_fraction, window_s, model_version)"
        " VALUES (?, 'slouch', 0.9, 60, 'rules-v9-test')",
        ((start + i * 2000.0,) for i in range(300)),
    )
    conn.commit()
    conn.close()
    began = time.perf_counter()
    html = html_for(file_db)
    took = time.perf_counter() - began
    assert "<html" in html.lower()
    assert took < 5.0, f"took {took:.1f} s"


def test_b16_days_run_from_local_midnight(file_db) -> None:
    """Behaviour 16: 23:30 and 00:30 local fall on different days (timezone fixed at +02:00)."""
    for ts in (local_ts(TZ, DAY, 23.5), local_ts(TZ, DAY, 0.5, 1)):
        file_db.insert_alert(
            ts=ts, label="slouch", bad_fraction=0.9, window_s=60.0,
            model_version="rules-v9-test", calibration_id=None,
        )  # fmt: skip
    # Both fall on the same UTC day (21:30 and 22:30 UTC), so a UTC report would merge them.
    first = parse(html_for(file_db, day=DAY))
    second = parse(html_for(file_db, day=DAY + timedelta(days=1)))
    assert value(first, "day", "alerts_total") == 1
    assert value(second, "day", "alerts_total") == 1
    assert value(first, "week", "alerts_total") == 1
    assert value(second, "week", "alerts_total") == 2


# === Privacy ============================================================================


def test_b17_metrics_table_holds_numbers_and_short_labels_only(db) -> None:
    """Behaviour 17: no BLOB or JSON columns, nothing named like an image or landmark."""
    info = db.query("PRAGMA table_info(metrics)")
    assert info, "the metrics table does not exist"
    for col in info:
        assert "blob" not in (col["type"] or "").lower()
        assert not re.search(r"json|blob|landmark|feature|frame|image|pixel", col["name"], re.I)


def test_b17_report_has_no_landmark_or_feature_fields(file_db, calibration) -> None:
    """Behaviour 17: stored landmarks and features never reach the page."""
    seed_report_db(file_db, calibration, TZ, DAY)
    file_db.insert_sample(
        ts=local_ts(TZ, DAY, 9.9), state="present", confidence=0.9, label="good",
        features={"head_forward_ratio": 0.123456}, landmarks=[[0.111111, 0.222222, 0.3, 0.9]],
        scores={"slouch": 0.1}, latency_ms=5.0, model_version="rules-v9-test",
    )  # fmt: skip
    html = html_for(file_db)
    for name in ("landmarks", "features_json", "scores_json", "raw_json", "head_forward_ratio"):
        assert name not in html
    assert "0.111111" not in html and "0.123456" not in html


def test_b17_report_code_imports_no_network_module() -> None:
    """Behaviour 17: the report (and recorder) modules never import a network library."""
    network = re.compile(
        r"^\s*(import|from)\s+(socket|requests|httpx|aiohttp|urllib|http|ftplib|smtplib|webbrowser)\b",
        re.MULTILINE,
    )
    for name in ("posture_checker.report", "posture_checker.metrics"):
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin, f"{name} does not exist"
        assert not network.search(Path(spec.origin).read_text(encoding="utf-8")), name


def test_b17_report_is_written_only_to_the_data_folder_or_out(tmp_path) -> None:
    """Behaviour 17: a default run leaves report.html in the data folder and nowhere else."""
    from posture_checker import cli

    home = paths.app_dir()
    cwd_before = sorted(p.name for p in Path.cwd().iterdir())
    assert cli.main(["report"]) == 0
    assert (home / "report.html").exists()
    assert sorted(p.name for p in Path.cwd().iterdir()) == cwd_before
