Status: draft

# Stage 2 fixes: away status, autostart name, burst timing

## 1. Goal

Fix three small problems the Stage 2 live checks found
(`docs/results/stage-2-live-checks.md`). No new features.

## 2. Decisions for the owner

1. **Tray while nobody is at the desk.** Recommended: keep the green icon and
   change the tooltip to "Posture Checker: away, checking every 30 s" once the
   slow presence checks start, and "Posture Checker: away" before that.
   Alternative: a separate blue icon colour for away.
2. **Name in Task Manager's Startup apps.** Recommended: start the app at
   sign-in from a shortcut named "Posture Checker" in the user's Startup folder
   (`shell:startup`) instead of the Run registry value. Task Manager then shows
   "Posture Checker". Alternative: a uv GUI entry point
   (`posture-checker-app.exe`), shown as "posture-checker-app".
3. **Burst sampling speed.** Recommended: count the time a sample took against
   the wait before the next one, so the configured interval is the time from
   one sample's start to the next. Sampling then runs every 2 s, or as fast as
   the camera can open if that takes longer. Alternative: leave it as is and
   only document it.

## 3. Behaviour

**F1 Away status.**
- The tray tooltip reads "Posture Checker: away" while the latest sample is
  "away" and monitoring is not paused.
- Once presence-check mode is on (more than 5 min away, `PRESENCE_CHECK_AFTER_S`),
  it reads "Posture Checker: away, checking every 30 s". The number comes from
  `PRESENCE_CHECK_INTERVAL_S`, not a second copy of it.
- Paused reasons still win over away. Uncalibrated and camera-unavailable
  still win over away.
- The icon stays green (decision 1).

**F2 Autostart shortcut.**
- Enabling autostart (installer or Settings) creates
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Posture Checker.lnk`.
  Its target is the same command as today
  (`.venv\Scripts\pythonw.exe -m posture_checker app`), its working folder is
  the repository, and it opens minimised.
- Disabling it deletes that shortcut. No error if it is already gone.
- "Is autostart on" means the shortcut exists.
- Both enable and disable also remove the old `PostureChecker` Run value if
  present, so an upgrade never starts the app twice.
- The shortcut is written by a small injectable writer (real one: the Windows
  Script Host `WScript.Shell` COM object, called through PowerShell, so no new
  dependency). Tests use a fake writer and a temporary Startup folder.
- `install.ps1` and `uninstall.ps1` use the same code path
  (`python -m posture_checker` commands), not their own copy.

**F3 Burst timing.**
- The wait before the next sample is the chosen interval minus the time the
  sample took, never below 0.
- This applies in every camera mode and to every interval
  (`sample_interval_s`, `suspect_interval_s`, away and paused intervals).
- A new script `scripts/bench_burst.py` measures, for 20 samples in each
  camera mode: camera open time, read time, pose time and the start-to-start
  interval, and writes the medians to `docs/results/burst-timing.md`. No
  frames are stored.

## 4. Acceptance tests

- F1: tooltip text for away, away in presence-check mode, paused while away,
  and uncalibrated while away.
- F2: enable creates the shortcut with the right target, arguments and working
  folder; disable deletes it; status follows the shortcut; enable and disable
  both remove a left-over Run value; disable with nothing there is harmless;
  Settings' autostart checkbox uses the same functions.
- F3: with a fake clock where a sample takes 1.5 s and the interval is 2 s,
  the wait is 0.5 s; when a sample takes 3 s, the wait is 0; the same holds
  for the away and suspect intervals.

## 5. Live checks

- **F1:** leave the desk for over 5 minutes; the tooltip changes to "away", then
  "away, checking every 30 s"; on return it says "monitoring".
- **F2:** run `.\install.ps1`; Startup apps lists "Posture Checker" (no
  "pythonw"); sign out and in: the app starts with no console window. Run
  `.\uninstall.ps1`: the entry is gone.
- **F3:** run `uv run python scripts/bench_burst.py`; read the burst
  start-to-start median in `docs/results/burst-timing.md`.

## 6. Out of scope

Making the camera open faster; a new icon colour (unless decision 1 changes).

## 7. Risks

- Some security tools flag scripts that create Startup shortcuts. The
  shortcut is created only when the user installs or ticks autostart.
- If opening the camera itself takes longer than 2 s, burst sampling stays
  slower than 2 s; F3 measures how much.
