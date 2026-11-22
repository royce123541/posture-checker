"""The local HTML report (spec Stage 3, behaviours 8-17).

``generate_report`` reads the ``metrics``, ``samples``, ``alerts``, ``labels``
and ``calibrations`` tables and returns one self-contained HTML page: inline
CSS, inline SVG charts, no scripts, images, links or network access. Every
figure is an element with ``data-figure``, ``data-scope`` and (when there is
data) ``data-value``, so tests read the numbers without scraping prose.
"""

from __future__ import annotations

import html
import itertools
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from datetime import time as dtime
from pathlib import Path
from typing import Any

import numpy as np

from posture_checker.storage.db import Database, Reader

NO_DATA = "not measured yet"
#: Rates over less monitored time than this show their raw counts beside them
#: (spec Stage 3, behaviour 13).
MIN_HOURS_FOR_RATE = 1.0
#: Rates over fewer alerts than this show their raw counts (behaviour 13).
MIN_ALERTS_FOR_RATE = 5
#: Two on-battery rows are "consecutive" when the gap between them is at most
#: this multiple of the later row's interval; a longer gap means the app was
#: not running in between, so the drop is not counted (behaviour 12).
DRAIN_GAP_FACTOR = 1.5
#: Days in the weekly view (spec Stage 3, decision 6).
WEEK_DAYS = 7
MODES = ("monitoring", "paused")
#: Shown under pause times: a minute with two reasons counts under both.
PAUSE_NOTE = (
    "A minute paused for several reasons counts under each, so these can add up"
    " to more than the time paused."
)


# --- time helpers ------------------------------------------------------------------


def _midnight(d: date, tz: tzinfo | None) -> float:
    """Epoch seconds of local midnight starting ``d`` (system local time if tz is None)."""
    if tz is None:
        return datetime.combine(d, dtime(0)).astimezone().timestamp()
    return datetime.combine(d, dtime(0), tzinfo=tz).timestamp()


def _local(ts: float, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(ts, tz)


# --- statistics ----------------------------------------------------------------------


@dataclass
class Health:
    cpu: list[float] = field(default_factory=list)
    rss: list[float] = field(default_factory=list)
    latency: list[float] = field(default_factory=list)
    confidence: list[float] = field(default_factory=list)
    drain_pct: float = 0.0
    drain_h: float = 0.0


@dataclass
class Stats:
    has_metrics: bool = False
    at_desk_s: float = 0.0
    away_s: float = 0.0
    paused_s: float = 0.0
    excluded_alerts: int = 0
    excluded_samples: int = 0
    n_present: int = 0
    n_good: int = 0
    alerts_total: int = 0
    alerts_wrong: int = 0
    alerts_by_label: dict[str, int] = field(default_factory=dict)
    labels: dict[str, int] = field(default_factory=dict)
    pause_s: dict[str, float] = field(default_factory=dict)
    health: dict[str, Health] = field(default_factory=dict)


def _add_drain(st: Stats, rows: Sequence[Any]) -> None:
    """Battery drain from consecutive on-battery minutes (behaviour 12).

    A partly paused minute is stored as two rows with the same ``ts``; they are
    one battery reading, so rows are grouped by ``ts`` first. A pair counts
    only when both readings are on battery, the gap is no longer than
    DRAIN_GAP_FACTOR x the later minute's length, and the charge did not rise.
    The pair goes to the mode that covered most of the later minute.
    """
    groups: dict[float, list[Any]] = {}
    for row in rows:
        g = groups.setdefault(row["ts"], [0.0, 0.0, row["battery_percent"], row["on_ac"]])
        g[0] += row["interval_s"]
        if not row["paused_reasons"]:
            g[1] += row["interval_s"]
    readings = sorted(groups.items())
    for (ts0, g0), (ts1, g1) in itertools.pairwise(readings):
        if g0[3] != 0 or g1[3] != 0 or g0[2] is None or g1[2] is None:
            continue
        drop = g0[2] - g1[2]
        if drop < 0 or ts1 - ts0 > g1[0] * DRAIN_GAP_FACTOR:
            continue
        h = st.health["monitoring" if g1[1] * 2 >= g1[0] else "paused"]
        h.drain_pct += drop
        h.drain_h += (ts1 - ts0) / 3600.0


#: Slack in seconds for the coverage bounds. ``ts - interval_s`` is a float
#: subtraction, so a moment exactly at a window start can come out a few ulps
#: inside it; 1 microsecond is far above that error and far below the time
#: between two samples (at least 1 s).
COVER_EPS_S = 1e-6


def _covered(alias: str) -> str:
    """SQL condition: ``alias.ts`` lies in (ts - interval_s, ts] of some metrics row.

    The upper bound on ``m.ts`` (the longest interval stored) keeps each check
    to a short range scan of idx_metrics_ts instead of every later row.
    """
    return (
        "EXISTS (SELECT 1 FROM metrics m"
        f" WHERE m.ts >= {alias}.ts - {COVER_EPS_S}"
        f" AND m.ts < {alias}.ts + (SELECT MAX(interval_s) FROM metrics)"
        f" AND m.ts - m.interval_s < {alias}.ts - {COVER_EPS_S})"
    )


def _period_stats(db: Reader, start: float, end: float, *, health: bool = True) -> Stats:
    """Every figure for [start, end) in epoch seconds."""
    st = Stats(health={m: Health() for m in MODES})
    rows = db.query(
        "SELECT ts, interval_s, cpu_percent, rss_mb, battery_percent, on_ac, paused_reasons,"
        " n_present, n_low_confidence, n_away"
        " FROM metrics WHERE ts >= ? AND ts < ? ORDER BY ts, id",
        (start, end),
    )
    st.has_metrics = bool(rows)
    pause: dict[str, float] = defaultdict(float)
    for row in rows:
        reasons = row["paused_reasons"] or ""
        mode = "paused" if reasons else "monitoring"
        if reasons:
            st.paused_s += row["interval_s"]
            for reason in reasons.split(","):
                pause[reason] += row["interval_s"]
        else:
            # A row's time goes to the day of its ts (the window end), so a row
            # just after midnight moves at most one interval across the
            # boundary. Accepted rather than clipped: rows are a minute long.
            # Coverage spec C3: the row's time splits by its sample states;
            # low confidence still means someone was at the desk. A row with
            # none of these samples adds nothing to either.
            desk = (row["n_present"] or 0) + (row["n_low_confidence"] or 0)
            total = desk + (row["n_away"] or 0)
            if total:
                at_desk = row["interval_s"] * desk / total
                st.at_desk_s += at_desk
                st.away_s += row["interval_s"] - at_desk
        if health:
            h = st.health[mode]
            if row["cpu_percent"] is not None:
                h.cpu.append(row["cpu_percent"])
            if row["rss_mb"] is not None:
                h.rss.append(row["rss_mb"])
    if health:
        _add_drain(st, rows)
    st.pause_s = dict(pause)

    (counts,) = db.query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(label = 'good'), 0) AS good FROM samples s"
        f" WHERE ts >= ? AND ts < ? AND state = 'present' AND {_covered('s')}",
        (start, end),
    )
    st.n_present, st.n_good = int(counts["n"]), int(counts["good"])
    (excluded,) = db.query(
        "SELECT (SELECT COUNT(*) FROM alerts a WHERE a.ts >= ? AND a.ts < ?"
        f" AND NOT {_covered('a')}) AS alerts,"
        " (SELECT COUNT(*) FROM samples s WHERE s.ts >= ? AND s.ts < ?"
        f" AND NOT {_covered('s')}) AS samples",
        (start, end, start, end),
    )
    st.excluded_alerts, st.excluded_samples = int(excluded["alerts"]), int(excluded["samples"])

    for row in db.query(
        "SELECT label, COUNT(*) AS n, COALESCE(SUM(feedback = 'wrong'), 0) AS wrong"
        f" FROM alerts a WHERE ts >= ? AND ts < ? AND {_covered('a')}"
        " GROUP BY label ORDER BY label",
        (start, end),
    ):
        st.alerts_by_label[row["label"]] = int(row["n"])
        st.alerts_total += int(row["n"])
        st.alerts_wrong += int(row["wrong"])
    for row in db.query(
        "SELECT label, COUNT(*) AS n FROM labels lb WHERE ts >= ? AND ts < ?"
        f" AND {_covered('lb')} GROUP BY label ORDER BY label",
        (start, end),
    ):
        st.labels[row["label"]] = int(row["n"])

    if health:
        # Pose only runs while monitoring, so latency and confidence come from
        # the stored samples and belong to the monitoring minutes.
        h = st.health["monitoring"]
        h.latency = [
            r[0]
            for r in db.query(
                "SELECT latency_ms FROM samples s WHERE ts >= ? AND ts < ?"
                f" AND latency_ms IS NOT NULL AND {_covered('s')}",
                (start, end),
            )
        ]
        h.confidence = [
            r[0]
            for r in db.query(
                "SELECT confidence FROM samples s WHERE ts >= ? AND ts < ?"
                f" AND state = 'present' AND {_covered('s')}",
                (start, end),
            )
        ]
    return st


# --- formatting ------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def _fmt_num(value: float) -> str:
    return f"{value:.10g}"


def _figure(scope: str, key: str, text: str, value: float | None, tag: str = "span") -> str:
    attrs = f'data-figure="{html.escape(key)}" data-scope="{html.escape(scope)}"'
    if value is not None:
        attrs += f' data-value="{_fmt_num(value)}"'
    return f"<{tag} {attrs}>{html.escape(text)}</{tag}>"


def _row(label: str, cell: str) -> str:
    return f"<tr><th>{html.escape(label)}</th><td>{cell}</td></tr>"


def _no(scope: str, key: str, text: str = NO_DATA) -> str:
    return _figure(scope, key, text, None)


def _pct(fraction: float) -> str:
    return f"{fraction * 100:.0f}%"


def _summary_rows(scope: str, st: Stats) -> list[str]:
    hours = st.at_desk_s / 3600.0
    out: list[str] = []
    for key, title, seconds in (
        ("at_desk_s", "Time at the desk", st.at_desk_s),
        ("away_s", "Time away", st.away_s),
        ("paused_s", "Time paused", st.paused_s),
    ):
        cell = (
            _figure(scope, key, _fmt_duration(seconds), seconds)
            if st.has_metrics
            else _no(scope, key)
        )
        out.append(_row(title, cell))
    if st.excluded_alerts or st.excluded_samples:
        text = (
            f"{st.excluded_alerts} alerts and {st.excluded_samples} samples before recording"
            " started or during gaps are not counted."
        )
        out.append(f'<tr><td colspan="2" class="note">{_no(scope, "excluded", text)}</td></tr>')

    if st.n_present:
        share = st.n_good / st.n_present
        text = f"{_pct(share)} good ({st.n_good} of {st.n_present} present samples)"
        out.append(_row("Good posture", _figure(scope, "good_share", text, share)))
    else:
        out.append(_row("Good posture", _no(scope, "good_share")))

    no_counts = not st.has_metrics and not st.alerts_total
    for key, label, count in (
        ("alerts_total", "Alerts", st.alerts_total),
        ("alerts_wrong", "Alerts marked wrong", st.alerts_wrong),
    ):
        cell = _no(scope, key, "no data") if no_counts else _figure(scope, key, str(count), count)
        out.append(_row(label, cell))

    if hours > 0:
        rate = st.alerts_total / hours
        text = f"{rate:.2f} per hour"
        if hours < MIN_HOURS_FOR_RATE or st.alerts_total < MIN_ALERTS_FOR_RATE:
            text += f" ({st.alerts_total} alerts in {_fmt_duration(st.at_desk_s)} at the desk)"
        out.append(_row("Alerts per monitored hour", _figure(scope, "alerts_per_hour", text, rate)))
    else:
        out.append(_row("Alerts per monitored hour", _no(scope, "alerts_per_hour")))

    if st.alerts_total:
        rate = st.alerts_wrong / st.alerts_total
        text = _pct(rate)
        if st.alerts_total < MIN_ALERTS_FOR_RATE:
            text += f" ({st.alerts_wrong} of {st.alerts_total} alerts)"
        out.append(_row("False-alert rate", _figure(scope, "false_alert_rate", text, rate)))
    else:
        out.append(_row("False-alert rate", _no(scope, "false_alert_rate")))

    out.append(_count_rows(scope, "alerts_label", "Alerts by label", st.alerts_by_label))
    out.append(_count_rows(scope, "labels", '"I\'m slouching now" by label', st.labels))
    if st.pause_s:
        cells = ", ".join(
            f"{html.escape(reason)}: "
            + _figure(scope, f"pause_s:{reason}", _fmt_duration(seconds), seconds)
            for reason, seconds in sorted(st.pause_s.items())
        )
        out.append(f"<tr><th>Pause time by reason</th><td>{cells}</td></tr>")
        out.append(f'<tr><td colspan="2" class="note">{PAUSE_NOTE}</td></tr>')
    else:
        out.append(_row("Pause time by reason", "no data"))
    return out


def _count_rows(scope: str, prefix: str, title: str, counts: dict[str, int]) -> str:
    if not counts:
        return _row(title, "no data")
    cells = ", ".join(
        f"{html.escape(label)}: " + _figure(scope, f"{prefix}:{label}", str(n), n)
        for label, n in sorted(counts.items())
    )
    return f"<tr><th>{html.escape(title)}</th><td>{cells}</td></tr>"


def _stat_cell(
    scope: str, key: str, values: Sequence[float], how: str, unit: str, digits: int = 1
) -> str:
    if not values:
        return _no(scope, key)
    arr = np.asarray(values, dtype=float)
    if how == "mean":
        v = float(arr.mean())
    elif how == "max":
        v = float(arr.max())
    else:  # a percentile, numpy linear (the recorder uses the same definition)
        v = float(np.percentile(arr, float(how)))
    return _figure(scope, key, f"{v:.{digits}f}{unit}", v)


def _health_rows(scope: str, st: Stats) -> list[str]:
    out = ["<tr><th></th><th>Monitoring</th><th>Paused</th></tr>"]
    specs: list[tuple[str, str, str, str, str, int]] = [
        ("CPU mean (this process, all cores)", "cpu_mean", "cpu", "mean", " %", 1),
        ("CPU p95", "cpu_p95", "cpu", "95", " %", 1),
        ("Memory mean", "rss_mean_mb", "rss", "mean", " MB", 0),
        ("Memory max", "rss_max_mb", "rss", "max", " MB", 0),
        ("Pose latency p50", "latency_p50_ms", "latency", "50", " ms", 1),
        ("Pose latency p95", "latency_p95_ms", "latency", "95", " ms", 1),
        ("Mean confidence", "mean_confidence", "confidence", "mean", "", 2),
    ]
    for title, key, attr, how, unit, digits in specs:
        cells = "".join(
            "<td>"
            + _stat_cell(scope, f"{key}:{mode}", getattr(st.health[mode], attr), how, unit, digits)
            + "</td>"
            for mode in MODES
        )
        out.append(f"<tr><th>{html.escape(title)}</th>{cells}</tr>")
    cells = ""
    for mode in MODES:
        h = st.health[mode]
        key = f"drain_pct_per_h:{mode}"
        if h.drain_h > 0:
            rate = h.drain_pct / h.drain_h
            text = f"{rate:.1f} %/h"
            if h.drain_h < MIN_HOURS_FOR_RATE:
                text += f" ({h.drain_pct:.0f} % over {_fmt_duration(h.drain_h * 3600)})"
            cells += "<td>" + _figure(scope, key, text, rate) + "</td>"
        else:
            cells += "<td>" + _no(scope, key) + "</td>"
    out.append(f"<tr><th>Laptop drain while Hunch ran (on battery)</th>{cells}</tr>")
    return out


# --- charts -----------------------------------------------------------------------------

# Chart geometry in SVG user units (pixels at 100% zoom), chosen so 7 bars fit:
#: Chart width: 7 x (bar + gap) + one gap = 360.
CHART_W = 360
#: Chart height, room for the bars plus value and weekday labels.
CHART_H = 140
#: Width of one day's bar.
BAR_W = 40
#: Space between bars.
GAP = 10
#: Space above the bars for the value label.
TOP = 16
#: Space below the bars for the weekday label.
BOTTOM = 20
#: Floor for the alerts-per-hour scale so a week of zeros does not divide by zero.
MIN_SCALE = 1e-9


def _chart(name: str, title: str, days: list[date], values: list[float | None], unit: str) -> str:
    known = [v for v in values if v is not None]
    scale = max([*known, MIN_SCALE]) if name == "alerts_per_hour" else 1.0
    plot_h = CHART_H - TOP - BOTTOM
    parts = [
        f'<svg data-chart="{name}" width="{CHART_W}"'
        f' height="{CHART_H}" role="img" aria-label="{html.escape(title)}">'
    ]
    for i, (d, v) in enumerate(zip(days, values, strict=True)):
        x = GAP + i * (BAR_W + GAP)
        h = 0.0 if v is None else plot_h * (v / scale if scale else 0.0)
        y = TOP + plot_h - h
        label = "no data" if v is None else (f"{v * 100:.0f}%" if unit == "%" else f"{v:.2f}")
        parts.append(
            f'<rect class="bar" x="{x}" y="{y:.1f}" width="{BAR_W}" height="{h:.1f}">'
            f"<title>{d.isoformat()}: {label}</title></rect>"
            f'<text x="{x + BAR_W / 2}" y="{TOP - 4}" text-anchor="middle">'
            f"{'-' if v is None else html.escape(label)}</text>"
            f'<text x="{x + BAR_W / 2}" y="{CHART_H - 6}" text-anchor="middle">'
            f"{d.strftime('%a')}</text>"
        )
    parts.append("</svg>")
    return f"<figure><figcaption>{html.escape(title)}</figcaption>{''.join(parts)}</figure>"


# --- page -------------------------------------------------------------------------------

CSS = """
body{font-family:Segoe UI,Arial,sans-serif;margin:2em;color:#222;max-width:60em}
table{border-collapse:collapse;margin:.5em 0 1.5em}
th,td{text-align:left;padding:.2em .8em;border-bottom:1px solid #ddd;vertical-align:top}
.bar{fill:#2ea043}
svg text{font-size:11px;fill:#444}
figure{display:inline-block;margin:0 1.5em 1em 0}
.day{display:none}
.day.chosen,.day:target{display:block}
.days:has(.day:target) .day.chosen:not(:target){display:none}
nav a{margin-right:.8em}
.note{color:#666;font-size:.9em}
"""


def _lineage(db: Reader, start: float, end: float, tz: tzinfo | None) -> tuple[str, str]:
    versions: set[str] = set()
    cal_ids: set[int] = set()
    for table in ("samples", "alerts", "metrics"):
        for row in db.query(
            f"SELECT DISTINCT model_version, calibration_id FROM {table} WHERE ts >= ? AND ts < ?",
            (start, end),
        ):
            if row["model_version"]:
                versions.add(row["model_version"])
            if row["calibration_id"] is not None:
                cal_ids.add(int(row["calibration_id"]))
    cal_text = []
    for cal_id in sorted(cal_ids):
        rows = db.query("SELECT ts FROM calibrations WHERE id = ?", (cal_id,))
        when = _local(rows[0]["ts"], tz).strftime("%Y-%m-%d") if rows else "date unknown"
        cal_text.append(f"#{cal_id} (calibrated {when})")
    return (", ".join(sorted(versions)) or "no data", ", ".join(cal_text) or "no data")


def _app_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("posture-checker")
    except PackageNotFoundError:
        return "unknown"


def _section(scope: str, title: str, st: Stats) -> str:
    return (
        f"<h3>{html.escape(title)}</h3><table>{''.join(_summary_rows(scope, st))}</table>"
        f"<h4>System health</h4><table>{''.join(_health_rows(scope, st))}</table>"
    )


def generate_report(
    db: Database,
    *,
    day: date | None = None,
    now: float | None = None,
    tz: tzinfo | None = None,
) -> str:
    """The report page for ``day`` and the 7 days ending it, as HTML."""
    import time

    now = time.time() if now is None else now
    day = day or _local(now, tz).date()
    days = [day - timedelta(days=WEEK_DAYS - 1 - i) for i in range(WEEK_DAYS)]
    bounds = [_midnight(d, tz) for d in [*days, day + timedelta(days=1)]]
    week_start, week_end = bounds[0], bounds[-1]

    # A separate read-only connection, so the monitor's writes are not held up.
    with db.read_only() as reader:
        week = _period_stats(reader, week_start, week_end)
        per_day = [_period_stats(reader, bounds[i], bounds[i + 1]) for i in range(WEEK_DAYS)]
        versions, cal_ids = _lineage(reader, week_start, week_end, tz)

    share: list[float | None] = []
    per_hour: list[float | None] = []
    false_rate: list[float | None] = []
    for st in per_day:
        share.append(st.n_good / st.n_present if st.n_present else None)
        hours = st.at_desk_s / 3600.0
        per_hour.append(st.alerts_total / hours if hours > 0 else None)
        false_rate.append(st.alerts_wrong / st.alerts_total if st.alerts_total else None)

    p = "page"
    nav = " ".join(
        f'<a href="#d{d.isoformat()}">{d.strftime("%a %d %b")}</a>' for d in reversed(days)
    )
    day_sections = []
    for d, st in zip(reversed(days), reversed(per_day), strict=True):
        chosen = d == day
        scope = "day" if chosen else f"day:{d.isoformat()}"
        cls = "day chosen" if chosen else "day"
        day_sections.append(
            f'<section class="{cls}" id="d{d.isoformat()}">'
            + _section(scope, f"Day: {d.strftime('%A %Y-%m-%d')}", st)
            + "</section>"
        )
    body = [
        "<h1>Hunch report</h1>",
        "<p>",
        f"Period: {html.escape(days[0].isoformat())} to ",
        _figure(p, "period_end", day.isoformat(), None),
        " (local time). Generated ",
        _figure(p, "generated_at", _local(now, tz).strftime("%Y-%m-%d %H:%M"), None),
        ". App version ",
        _figure(p, "app_version", _app_version(), None),
        ".</p><p>Model version(s): ",
        _figure(p, "model_versions", versions, None),
        ". Calibration(s): ",
        _figure(p, "calibration_ids", cal_ids, None),
        ".</p>",
        '<p class="note">Every figure comes from rows stored on this computer. '
        "CPU and memory count only Hunch's own process. Battery drain is the whole "
        "laptop's, not Hunch's cost. The false-alert rate counts only alerts marked "
        "wrong.</p>",
        f"<h2>Day</h2><nav>{nav}</nav>",
        f'<div class="days">{"".join(day_sections)}</div>',
        "<h2>Last 7 days</h2>",
        _chart("posture_share", "Good posture share", days, share, "%"),
        _chart("alerts_per_hour", "Alerts per monitored hour", days, per_hour, ""),
        _chart("false_alert_rate", "False-alert rate", days, false_rate, "%"),
        _section("week", f"Week: {days[0].isoformat()} to {day.isoformat()}", week),
    ]
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>Hunch report {day.isoformat()}</title><style>{CSS}</style></head>"
        f"<body>{''.join(body)}</body></html>\n"
    )


def write_report(
    db: Database,
    path: Path | str,
    *,
    day: date | None = None,
    now: float | None = None,
    tz: tzinfo | None = None,
) -> Path:
    """Write the report to ``path`` (only there; nothing is sent anywhere)."""
    target = Path(path)
    text = generate_report(db, day=day, now=now, tz=tz)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return target
