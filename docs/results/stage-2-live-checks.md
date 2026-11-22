# Stage 2 live checks

Run by the owner on 2026-11-22 on the development laptop (Windows 11 Home,
build 26300, USB webcam). Timings come from the app's own events and samples
tables unless marked "observed".

| Check | Result | Notes |
|---|---|---|
| L1 Camera sharing | pass | `hold` blocked the Windows Camera app and a Meet preview; `burst` let both work. Default changed to `burst`. See `camera-sharing.md`. |
| L2 Video call | fail, then fixed | Meet video worked, but the app did not pause: this Windows build no longer records camera use in the registry. Added call detection from window titles (`docs/specs/stage-2-call-detection.md`). |
| L2b Video call (call detection) | pass | Paused during a Meet call. Resumed about 15 s after closing the tab; about 1 minute after "end the meeting for everyone" with the tab left open (observed). |
| L3 Lock | fail, then fixed | The input-desktop check reported "locked" for only about 5 s, so sampling went on behind the lock screen. Now uses the Windows session state; rerun: paused for the whole 3.5 min lock, webcam light off, resumed within 1-2 s of unlocking. |
| L4 Sign out and in | fail, then fixed | Autostart worked but left a console window (uv's launcher starts `python.exe`, and Windows Terminal ignores hide requests). The app now restarts itself with no window; rerun: no console window. |
| L5 Low battery | pass | Threshold 100% unplugged: paused at 99%, resumed when plugged in. |
| L6 Install / uninstall | pass | The Startup apps entry (shown as "pythonw") disappeared after uninstall and came back after install. |
| L7 Tray actions | pass | Calibrate, Settings (applied without restart), I'm slouching now, Mark latest alert wrong. |
| L8 Nobody present | pass | Marked away when the owner left; after 5 min, checks every ~33 s; the return was seen on the next check. |

Found during the checks, not fixed in Stage 2:

- Burst mode samples about every 5 s instead of the configured 2 s, because
  the camera is opened for each sample (from the samples table; cost not
  measured yet).
- The tray shows "monitoring" while nobody is present; it has no "away" text.
- Task Manager lists the autostart entry as "pythonw", not "Posture Checker".

## Stage 2 fixes and Hunch name (2026-11-22)

| Check | Result | Notes |
|---|---|---|
| F1 Away tooltip | pass | "Hunch: away", then "Hunch: away, checking every 30 s" after 5 min away. |
| F2 + R2 Startup name | pass | Startup apps lists "hunch". A shortcut named "Posture Checker" alone still showed "pythonw", because Task Manager names an entry after the program it runs. |
| Sign out and in | pass | Hunch started with no console window. |
| R1 Alert sender | pass after sign-out | The first alert still showed "Posture Checker": Windows kept the old name for the same toast identity until the owner signed out and back in. Running an older checkout also writes the old name back. |
| F3 Burst timing | pass | Samples start every 2.0 s in both modes; see `burst-timing.md`. |
