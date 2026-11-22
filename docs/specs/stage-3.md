Status: draft

# Stage 3: Summaries and monitoring

## 1. Goal

Record a small row of health numbers every minute while Hunch runs (CPU,
memory, pose latency, confidence, sample counts, battery, camera mode), and
turn those rows plus the existing `samples`, `alerts`, `labels` and `events`
tables into a local static HTML report with daily and weekly views, opened
from the tray. The report is how the owner sees posture trends, false-alert
rate and what the app costs the laptop.

## 2. Decisions for the owner

1. **Report format?** Recommended: one self-contained HTML file with inline
   CSS and inline SVG charts. Trade-off: no interactivity beyond
   switching day/week, but no server, no JavaScript libraries, nothing to load
   from the network. (Alternative: a Tkinter window; harder to test and share.)
2. **Metrics interval?** Recommended: every 60 s. Trade-off: one row a minute
   (about 1,440 a day) is cheap and matches the plan; 10 s gives finer CPU
   spikes at 6x the rows.
3. **Retention for metrics?** Recommended: keep 90 days, delete older rows
   once a day, hard cap 200,000 rows (oldest deleted first). Trade-off: 90 days
   covers several weekly comparisons at roughly 20-30 MB; longer needs more disk.
   `samples`, `alerts`, `labels` and `events` are not pruned in this stage.
4. **Use psutil?** Recommended: yes. It is already a dependency
   (`pyproject.toml`, used for battery in Stage 2), so this adds nothing new;
   it gives process CPU % and memory on Windows without ctypes code.
5. **When is the report regenerated?** Recommended: on demand, when the
   owner clicks "Open report", plus `posture-checker report` on the command
   line. Trade-off: always fresh when opened and zero background cost; an
   automatic nightly file would be stale by the time it is read anyway.
6. **Default view?** Recommended: today plus the last 7 days on one page,
   with a day picker for the last 7 days. Trade-off: one file covers both
   views; older weeks need the CLI `--week YYYY-MM-DD` option.

## 3. Behaviour

### Metrics recorder

1. While the app runs, a recorder writes one row to a new `metrics` table
   every 60 s (interval in `Config.metrics_interval_s`, default 60), measured
   with the injected clock. Rows are written while paused too (CPU while paused
   is part of the cost).
2. Each row has: `ts` (minute end), `interval_s`, `cpu_percent` (this process,
   psutil, normalised to all cores, 0-100), `rss_mb`, `n_present`,
   `n_low_confidence`, `n_away` (samples in the minute by state),
   `latency_p50_ms`, `latency_p95_ms`, `mean_confidence` (present samples only;
   NULL when there were none), `battery_percent`, `on_ac` (1/0/NULL when no
   battery), `camera_mode`, `paused_reasons` (comma-separated, empty when
   running), `model_version`, `calibration_id`.
3. Sample counts, latency and confidence come from the samples the monitor
   produced in that minute (kept in memory by the recorder, not re-read from
   the database), so they match the `samples` table rows for the same minute.
4. A failure reading CPU, memory or battery stores NULL for that field and
   logs once; it never stops monitoring or the recorder.
5. The recorder's own work per minute (gather + one insert) takes under
   50 ms of wall time, measured with a real monotonic timer and stored
   as `metrics_cost_ms` in the row.
6. Once a day (and at start-up) rows older than 90 days are deleted, then, if
   more than 200,000 rows remain, the oldest are deleted down to 200,000.
7. Stopping the app writes a final partial row with its real `interval_s`.

### Report

8. "Open report" in the tray (above "Open data folder") and
   `posture-checker report [--out PATH] [--week YYYY-MM-DD]` write
   `report.html` in the data folder and the tray item opens it in the default
   browser via the local file path.
9. The HTML contains no `http://`, `https://` or `//` URL in any `src`,
   `href`, `@import` or `url(...)`; no `<script src>`; no images other than
   inline SVG. It renders fully with the network off.
10. For a chosen day and for the 7 days ending that day, the report shows:
    time monitored (sum of `interval_s` with no pause reason), good vs bad
    posture share (present samples labelled `good` vs any other label),
    alerts per monitored hour, alerts by label, false-alert rate (alerts with
    feedback `wrong` divided by alerts), "I'm slouching now" count by label
    (from `labels`), and pause time by reason (from `paused_reasons`).
11. Weekly view includes one bar per day for posture share, alerts per hour
    and false-alert rate, drawn as inline SVG.
12. System health section: CPU % mean and p95, memory mean and max, pose
    latency p50/p95 (from `samples.latency_ms`), mean confidence, battery drain
    in % per hour while on battery, shown separately for monitoring and paused
    minutes. Drain is computed only from consecutive on-battery rows.
13. Every figure is computed from stored rows. Where there is no data the
    report prints "not measured yet" (or "no data"), never 0 or a guess. Rates
    with a denominator under 1 hour or under 5 alerts are shown with the raw
    counts beside them ("1 of 2 alerts").
14. The page shows the model version(s) and calibration id(s) and date used
    in the period, the app version, and the time it was generated.
15. Generation for 7 days of data (about 10,000 metrics rows and 300,000
    samples) finishes within 5 s on the development laptop.
16. All times are shown in local time; days run from local midnight.

### Privacy

17. The `metrics` table and the report contain numbers and short labels only:
    no landmarks, features, frames or image data. The report is written only
    to the data folder (or `--out`) and never sent anywhere.

## 4. Acceptance tests

All use a temporary SQLite database, `FakeClock`, and a fake system probe
(`FakeSystemProbe` returning scripted CPU, RSS and battery values) injected
into the recorder; no webcam, no psutil calls.

- B1: advance the fake clock 180 s with scripted samples; assert 3 rows with
  `interval_s` 60. Repeat while paused; rows still written with the reason.
- B2: assert the column set and that each value equals the scripted probe
  value / sample statistics.
- B3: feed 30 samples (mixed states) through the monitor with a fake pose
  model; assert row counts and p50/p95/mean equal those computed from the
  `samples` rows in that minute.
- B4: probe raises for CPU and battery; assert NULLs, one log line, monitor
  still produces samples.
- B5: assert `metrics_cost_ms` is present and under 50 for a row with 30
  samples (real timer, fast fakes).
- B6: insert rows 100 and 10 days old; run prune; only the 10-day row
  remains. With the cap patched to 100, insert 150; assert 100 newest remain.
- B7: stop the app after 25 fake seconds; assert final row `interval_s` 25.
- B8: tray menu model contains "Open report"; invoking it calls an injected
  opener with the report path, and the file exists. CLI `report --out` writes
  the file.
- B9: generate from a seeded database; parse the HTML and assert no external
  URL patterns, no `<script src`, no `<img`, no `<link`.
- B10-B12: seeded database with known values (e.g. 4 h monitored, 3 alerts,
  1 wrong, 2 labels, 30 min paused for `call`); assert the exact figures and
  that the SVG has 7 bars in the weekly chart. Battery: rows 80% to 70% over
  2 on-battery hours gives 5.0 %/h; mixed AC rows are excluded.
- B13: empty database; report generates and every figure reads "not measured
  yet" or "no data"; small denominators show raw counts.
- B14: assert model version, calibration id and generation time strings.
- B15: synthetic 7-day database; generation under 5 s (marked `slow`, run in
  CI).
- B16: a timestamp at 23:30 and 00:30 local fall on different days
  (timezone fixed in the test).
- B17: `tests/test_privacy.py` still passes; new test asserts the `metrics`
  schema has no JSON/blob columns and the report contains no landmark or
  feature field names. No network module imported by the report code.

## 5. Live checks

- L1 (2 min): run Hunch 3 minutes, then `posture-checker report`; check
  3 rows in `metrics` and values look plausible against Task Manager CPU and
  memory for "hunch" (within a few percent / tens of MB).
- L2 (1 min): tray "Open report"; the page opens in the browser with
  Wi-Fi off and shows today's figures and charts.
- L3 (a working day): use Hunch normally, mark one alert wrong and use
  "I'm slouching now" once; next day check those counts and pause time
  (including a call or lock) match what happened.
- L4 (1 hour unplugged): run on battery; check the report shows a drain
  figure, and that it is about the same as Windows battery usage suggests.

## 6. Out of scope

Pruning `samples`/`events`; comparing with a no-Hunch battery baseline;
notifications or nudges based on the report; exporting or emailing reports;
charts more than 7 days in one view; fixing the "pythonw" name; any change to
detection, thresholds or the model. README result tables stay "not measured
yet" until an ml-evaluator script measures them; the report is not a result.

## 7. Risks and limitations

- psutil process CPU % needs two readings; the first row after start has no
  CPU value (stored NULL). It counts only Hunch's process, not camera driver
  or Windows camera service work, so real cost is understated.
- Battery drain includes everything else the laptop does; it is not Hunch's
  cost. The report labels it "laptop drain while Hunch ran".
- Battery % is reported in whole percent, so drain over short periods is
  coarse; under 1 hour on battery shows raw values only.
- False-alert rate only counts alerts the owner bothered to mark; unmarked
  wrong alerts make it look better than it is. Missed slouches are only seen
  through "I'm slouching now".
- The `samples` table is not pruned and grows about 40,000 rows a day; weekly
  queries rely on `idx_samples_ts`. Generation time may grow over months.
- If the app crashes, the last partial minute is lost (behaviour 7 only runs
  on a clean stop).
- `metrics_cost_ms` measures the recorder, not total app overhead.
