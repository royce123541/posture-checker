Status: draft

# Stage 3 addition: count only recorded time, split desk and away

## 1. Goal

Live check L1 on 2026-11-22 showed "225.00 per hour (15 alerts in 4 min)" and
"17% good (221 of 1329 present samples)": the report counted the whole day's
alerts and samples but only the 4 minutes the new recorder had covered. The
owner also spends long stretches lying down out of view, which "Time
monitored" does not separate from time at the desk.

## 2. Decisions for the owner

1. **What counts as "covered".** Recommended: a moment is covered when it falls
   inside a metrics row's window, `(ts - interval_s, ts]`. Alerts, labels and
   samples outside every window are left out of all day and week figures.
2. **What counts as "at the desk".** Recommended: the share of a monitoring
   row's time whose samples were `present` or `low_confidence` (someone in
   view); the rest of that row's time is "away". Paused rows count as neither.

## 3. Behaviour

- **C1.** Every posture figure for a day or week (alerts total, by label,
  per hour, wrong, false-alert rate, "I'm slouching now", good share,
  latency, confidence) uses only covered alerts, labels and samples.
- **C2.** "Time monitored" is replaced by three figures: **at the desk**,
  **away** and **paused** (paused is the existing pause total). At the desk
  plus away equals the old monitored time.
- **C3.** At-desk time for a row is `interval_s` times
  `(n_present + n_low) / (n_present + n_low + n_away)`; a row with no samples
  of those states adds nothing.
- **C4.** Alerts per hour divide by at-desk hours. The raw-count rule
  (under 1 hour, or under 5 alerts) now uses at-desk time.
- **C5.** When some of the period's alerts or samples fall outside covered
  time, the page shows one line: "N alerts and M samples before recording
  started or during gaps are not counted."
- **C6.** Health figures (CPU, memory, battery) are unchanged.

## 4. Acceptance tests

- Alerts and samples before the first metrics row, and in a gap between rows,
  are excluded from every posture figure; the C5 line shows their counts.
- A row with 10 present and 10 away samples over 60 s gives 30 s at the desk
  and 30 s away.
- Alerts per hour uses at-desk time; with 2 h covered but 30 min at the desk,
  raw counts are shown.
- At-desk plus away equals the sum of monitoring rows' `interval_s`.
- No C5 line when nothing is excluded.

## 5. Live check

After a day of normal use including time lying down out of view: the report
shows at-desk and away times that roughly match the day, and alerts per hour
is a plausible number.
