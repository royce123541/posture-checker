Status: approved

Approved by the owner on 2026-11-22 with the recommended answer to both decisions.

# Stage 2 addition: show the app as "Hunch"

## 1. Goal

The owner chose the name **Hunch** for everything a user sees (option 2 of
three offered on 2026-11-22). Internal names stay, so nothing breaks and no
data moves.

Live check F2 of `stage-2-fixes.md` showed that Task Manager names a Startup
shortcut after the program it runs (`pythonw.exe`), not after the shortcut. So
the shortcut must run a program that is itself called Hunch.

## 2. Decisions for the owner

1. **How Task Manager gets the name.** Recommended: add a GUI entry point
   `hunch = "posture_checker.app:main"` under `[project.gui-scripts]`; uv then
   builds `.venv\Scripts\hunch.exe`, a windowless launcher, and the Startup
   shortcut runs it. Task Manager shows "hunch" (lower case, from the file
   name), since the launcher has no version information. Alternative: build a
   launcher with a version resource that says "Hunch" (new build tool;
   rejected as too much upkeep).
2. **Old notifications in Action Center.** Recommended: keep the toast identity
   `PostureChecker.App` and only change its display name to "Hunch", so older
   notifications and Windows' notification setting for the app carry over.

## 3. Behaviour

**R1 Visible name.** "Hunch" replaces "Posture Checker" in:
- tray tooltips ("Hunch: monitoring", "Hunch: away", "Hunch: calibrate to
  start", "Hunch: camera unavailable"; paused texts are unchanged) and the tray
  icon's title;
- the toast display name and toast text;
- window titles (Settings, Calibrate);
- the Startup shortcut, now `Hunch.lnk`;
- installer and uninstaller messages;
- the README title and first paragraph ("Hunch, a posture checker ...").

**R2 Startup launcher.** The shortcut's target is `.venv\Scripts\hunch.exe`
with no arguments. If `hunch.exe` is missing, enabling autostart fails with a
clear message (same rule as the missing-pythonw check). If the uv launcher
still creates a console window, the relaunch-without-console logic from L4
must cover it.

**R3 Migration.** Enabling or disabling autostart also deletes an old
`Posture Checker.lnk` and the old `PostureChecker` Run value, so an upgrade
never starts the app twice.

**R4 Unchanged.** The `posture-checker` command and subcommands, the Python
package `posture_checker`, the data folder `%LOCALAPPDATA%\PostureChecker`,
environment variables, the toast identity, the single-instance lock name and
the GitHub repository.

## 4. Acceptance tests

- Tooltip texts for monitoring, away, uncalibrated and camera unavailable
  start with "Hunch:".
- The toast display name registered by the installer is "Hunch".
- Enable autostart writes `Hunch.lnk` whose target is `hunch.exe` next to the
  running interpreter, with no arguments; it removes an old
  `Posture Checker.lnk` and the old Run value; disable removes all three.
- Missing `hunch.exe` gives a clear error and writes no shortcut.
- `pyproject.toml` declares the `hunch` GUI entry point.
- No visible string in `src/` still says "Posture Checker" (a scan test with
  an explicit allow-list for internal identifiers).

## 5. Live checks

- `.\install.ps1`: Startup apps lists "hunch" (or "Hunch"); the tray tooltip
  says "Hunch: monitoring".
- Sign out and in: Hunch starts with no console window.
- A posture alert shows "Hunch" as the sender.

## 6. Out of scope

Renaming the command, package, data folder or repository.
