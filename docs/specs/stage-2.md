Status: draft

# Stage 2: Background app

## 1. Goal

Turn the Stage 1 terminal program into a background app that starts at login
with no console window, lives in the tray, and gets out of the way on its own:
it releases the webcam when another app needs it, when the screen is locked,
when nobody is at the desk and when the battery is low, and resumes afterwards.
Calibration, settings and feedback (including "I'm slouching now") work
without a terminal, and one command installs or uninstalls everything.

## 2. Decisions for the owner

1. **How should the app hold the webcam: continuously or in short bursts?**
   Recommended: decide by the camera-sharing experiment (behaviour 1); if
   holding the camera blocks or breaks another app, use bursts (open, read one
   frame, close) for every sample. Trade-off: bursts free the camera between
   samples so Zoom/Meet/Teams can always take it, but every sample pays about a
   second of camera start-up and the webcam light blinks every 2 s instead of
   staying on.
2. **Low-battery threshold: pause below what charge when unplugged?**
   Recommended: 20 % (the existing `battery_pause_percent` default), also
   pausing whenever Windows Battery Saver is on. Trade-off: a higher threshold
   saves more battery but monitors less of an unplugged day.
3. **What should the tray show?** Recommended: status only. The icon colour
   shows the state (green monitoring, grey paused, amber needs calibration or
   camera unavailable) and the tooltip says why ("Paused: camera in use by
   Zoom.exe"). It does not show the live posture label. Trade-off: a live label
   is fun to watch but invites staring at the tray, and the toast already
   reports sustained bad posture.
4. **What does "I'm slouching now" record?** Recommended: one menu item that
   labels the current moment as `slouch` (a false-negative label), with a
   submenu for `forward_head` and `too_close`. Trade-off: the submenu gives
   Stage 5 cleaner labels at the cost of one extra click.
5. **Is autostart on by default after install?** Recommended: yes, with a
   checkbox in Settings to turn it off. Trade-off: starting at every login is
   the point of a background checker, but it means the webcam light comes on
   at login until the first auto-pause.
6. **Does uninstall delete your data (database, calibrations, settings)?**
   Recommended: no; keep it unless `uninstall.ps1 -RemoveData` is passed.
   Trade-off: keeping it lets a reinstall continue the Stage 5 dataset, but
   leaves a folder in `%LOCALAPPDATA%\PostureChecker` that you must delete
   yourself.

Decided here (not owner questions): "nobody present" handling (behaviour 9),
resume delays, single-instance rule, how the tray launches calibration and
settings (separate short-lived processes).

## 3. Behaviour

**Camera sharing (top risk, done first)**

1. A script `scripts/camera_share_probe.py` opens the webcam exactly as the
   app does (OpenCV DirectShow, 640x480), holds it, and guides the owner to
   start the Windows Camera app and a Meet or Zoom preview. It records, for
   each of "hold" and "burst" modes, whether the other app got a picture,
   whether our reads kept returning frames, and whether the other app appeared
   as in use in the registry probe (behaviour 4). It prints a short result
   table and writes it to `docs/results/camera-sharing.md` (text only, no
   frames). The chosen default `camera_mode` (decision 1) is set from this
   result and the result file is committed with the stage.
2. `Config` gains `camera_mode` with values `"hold"` and `"burst"`. In
   `"burst"` mode the monitor closes the camera after every sample, so between
   samples the frame source reports closed. In `"hold"` mode it keeps the
   Stage 1 behaviour (open between samples while monitoring).

**Pause supervisor**

3. A `PauseSupervisor` (in `policy/pausing.py`) combines injected probes
   (camera-in-use, screen-locked, battery, manual pause) into one decision:
   either "run" or "paused" with a list of active reasons. The monitor never
   opens the camera while any reason is active, and releases it on the first
   check after a reason appears. It is checked before every sample and at least
   every 5 s while paused.
4. **Another app uses the camera.** The camera-in-use probe reads
   `HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam`,
   both packaged subkeys and the `NonPackaged` subkeys, and reports in use when
   any entry has `LastUsedTimeStart > 0` and `LastUsedTimeStop == 0`, excluding
   entries whose path is this app's own Python executable. It returns the app
   names in use. If the key is missing or unreadable, the probe reports "not
   in use" and logs an event once (Stage 1's "camera busy" fallback still
   applies).
5. The app pauses with reason `camera_in_use:<app>` within 10 s of another app
   starting to use the camera, and resumes within 15 s after the last other
   app stops (one 10 s grace period, so a call that briefly toggles video does
   not make the app grab the camera mid-call).
6. **Screen locked.** Locking the screen (Win+L, sign-out screen, lid-close to
   lock) pauses with reason `locked` and releases the camera within 10 s.
   Unlocking resumes within 10 s.
7. **Low battery.** When on battery and charge is below
   `battery_pause_percent`, or when Windows Battery Saver is on, the app pauses
   with reason `low_battery`. Plugging in, or charge rising 5 points above the
   threshold, resumes (the 5-point hysteresis stops flapping at the edge).
   On a machine with no battery the probe never pauses.
8. **Manual pause** from the tray (15 min, 1 h, until resumed) or the toast
   (30 min) is a reason like the others. "Resume" clears only the manual
   reason; automatic reasons still hold the pause, and the tray tooltip says
   which.
9. **Nobody present.** Stage 1's away state (10 s sampling) stays. In addition,
   after 5 consecutive minutes of `away`, the monitor switches to
   presence-check mode: the camera is closed between samples (whatever
   `camera_mode` is) and samples every 30 s. The first non-away sample returns
   to normal sampling. Away is not a supervisor reason: it still needs the
   camera to notice your return.
10. Every transition into and out of a pause writes one `events` row
    (`pause` / `resume`, detail = reason), so Stage 3 can report paused time
    by cause. A reason that stays active writes no further rows.

**Tray and feedback**

11. The tray icon (pystray) offers: Pause (15 min / 1 hour / until I resume),
    Resume, Mark latest alert wrong, I'm slouching now (with the submenu of
    decision 4), Calibrate..., Settings..., Open data folder, Quit. Disabled
    items are greyed out: Resume when not manually paused, Mark wrong when
    there is no alert in the last 30 min or it is already marked.
12. Icon and tooltip follow decision 3 and update within 2 s of a state
    change.
13. "Mark latest alert wrong" sets `feedback='wrong'` on the latest alert,
    the same as the toast button and `posture-checker mark-wrong`.
14. "I'm slouching now" writes a row to a new `labels` table: `ts`, `label`,
    `source='tray'`, `sample_id` of the latest stored sample within the last
    10 s (NULL if none, e.g. while paused), `model_version` and the model's
    predicted label for that sample. It shows a short confirmation toast. It
    never triggers an alert and never changes the alert policy.
15. **Calibrate...** pauses monitoring with reason `calibrating`, releases the
    camera, and starts a separate `pythonw` process with a small Tkinter
    window (countdown, progress, result or error message). On success the
    running app reloads the calibration (`Monitor.reload_calibration`); on
    failure or close it keeps the old one. Either way the `calibrating` reason
    clears when that process exits.
16. **Settings...** opens a Tkinter window (separate process) for alert window,
    sensitivity, cooldown, battery threshold, camera mode and autostart. Saving
    validates with `Config.validate`; invalid values show an inline message and
    are not saved. The running app picks up saved settings within 5 s without a
    restart.
17. When the app starts with no calibration, it shows a toast "Calibrate to
    start" whose button opens the calibration window, and the tray is amber.

**Running in the background**

18. `posture-checker app` (and `pythonw -m posture_checker app`) runs the
    tray and the monitor thread with no console window. All output goes to a
    rotating log file in the data folder (at most 1 MB x 3 files).
19. Only one instance runs per user: a second start exits within 2 s with a
    "Posture Checker is already running" toast and leaves the first untouched.
20. Quit stops the monitor thread, releases the camera and exits within 5 s.
    Signing out or shutting down also releases the camera.
21. An unhandled error in the monitor thread is logged, shown once as a toast,
    and the monitor restarts after 30 s; the tray stays up.

**Install and uninstall**

22. `install.ps1` (run from the repo, no admin): `uv sync`, fetches the pose
    model, registers the toast AUMID, writes the HKCU Run value
    `PostureChecker` pointing at the venv's `pythonw.exe -m posture_checker
    app` (unless autostart is off), and starts the app. Running it twice
    leaves exactly one Run value and one running instance.
23. `uninstall.ps1` stops the running app, removes the HKCU Run value and the
    `Software\Classes\AppUserModelId\PostureChecker.App` key, and leaves the
    data folder unless `-RemoveData` is passed (decision 6). Running it twice,
    or without a prior install, succeeds without errors.
24. Toggling autostart in Settings adds or removes the same Run value.

## 4. Acceptance tests

All run without a webcam, using `FakeCamera`, `FakePose`, `FakeClock`,
`RecordingNotifier` from `tests/conftest.py`, plus new fakes listed here. New
fakes the test-writer may add: `FakeCameraUsage` (returns a scripted list of
apps), `FakeLockState`, `FakeBattery` (percent, plugged, saver), a
`FakeRegistry` mapping for the ConsentStore and Run keys, and a fake process
launcher that records the command line it was asked to start.

1. Camera probe: not automatable (needs hardware). Test asserts only that the
   script exists, is importable without opening a camera, and is marked
   `@pytest.mark.camera` for its live part. The result is live check L1.
2. Monitor in `burst` mode with `FakeCamera`: after each `step()` that read a
   frame, `camera.is_open` is False and `opened == closed`. In `hold` mode,
   `is_open` stays True between steps.
3. Supervisor unit tests: each probe alone produces the expected reason;
   combinations list all reasons; with any reason active, `step()` returns
   `PAUSED` and `FakeCamera.opened` does not increase; the camera is closed
   after the first paused step.
4. Registry parser given a `FakeRegistry`: an entry with stop == 0 and start >
   0 is "in use" (packaged and NonPackaged); stop > 0 is not; start == 0 and
   stop == 0 is not; an entry matching our own `sys.executable` is ignored;
   missing key returns not in use and logs exactly one event. A Windows-only
   test reads the real key and asserts it parses without error.
5. With `FakeClock`: in-use appears, supervisor reports paused at the next
   check (<= 10 s of fake time); in-use clears, monitor stays paused through
   the 10 s grace and runs by 15 s.
6. `FakeLockState` locked -> paused with reason `locked` and camera closed
   within 10 s fake time; unlocked -> running within 10 s.
7. `FakeBattery` table test: (unplugged, 19 %, threshold 20) pauses;
   (unplugged, 22 %) after pausing stays paused; (unplugged, 25 %) resumes;
   (plugged, 5 %) runs; saver on pauses; no battery never pauses.
8. Manual pause plus `locked`: Resume clears manual only, still paused with
   `locked`; after unlock, runs.
9. `FakePose` returning None for 5 min of fake time: after that, the camera is
   closed between steps and `next_interval` is 30 s; one sample with landmarks
   returns to 2 s and normal camera mode.
10. `events` table after a scripted sequence (lock, unlock, battery low,
    plug in) contains exactly one pause and one resume row per cause, with the
    reason in `detail`.
11. Tray menu is built from a pure function `menu_model(state) -> list of
    items` (label, enabled); tests assert items and enabled flags for: running,
    manually paused, auto-paused, no recent alert, alert already marked. pystray
    itself is not started in tests.
12. `tray_status(state)` returns (colour, tooltip); asserted for each state,
    including "Paused: camera in use by Zoom.exe". The 2 s refresh is checked by
    live check L3.
13. Tray "mark wrong" action on an in-memory DB with two alerts marks the
    newest one and returns True; a second call is a no-op.
14. "I'm slouching now" with a recent sample: one `labels` row with the
    sample's id, model version and predicted label; with no sample in 10 s:
    `sample_id` NULL; `RecordingNotifier` records no alert and the alert
    policy state is unchanged.
15. Calibrate action with the fake launcher: pause reason `calibrating` is set
    and camera closed before launch; launcher got a `pythonw ... calibrate`
    command; when the fake process exits with success and the DB has a new
    calibration, `monitor.calibration.id` is the new id; on failure it is the
    old id; reason cleared in both cases. The Tk window logic is tested via its
    non-UI controller (progress and error strings), not by opening a window.
16. Settings controller: valid values are saved to the isolated config path;
    invalid values return an error message and leave the file unchanged; the
    app's config watcher, driven by `FakeClock`, applies a changed file within
    5 s (e.g. new `alert_window_s` reaches `AlertPolicy`).
17. App start with an empty DB: notifier receives one "Calibrate to start"
    message and tray status is amber.
18. `app` entry point can be imported and constructed with fakes (no tray,
    no camera) and logs to a file under the isolated home, not stdout.
19. Single-instance lock: acquiring it twice in one test process returns
    False the second time (implementation uses a named mutex on Windows,
    injected).
20. `run()` with a stop event set from another thread returns within 5 s
    (fake intervals) and `FakeCamera.closed` equals `opened`.
21. A `FakePose` that raises once: error is logged, notifier records one error
    message, and the monitor runs again after 30 s fake time.
22. Autostart helper against `FakeRegistry`: enable writes one `PostureChecker`
    value whose command contains `pythonw.exe` and `-m posture_checker app`;
    enabling twice leaves one value. `install.ps1` is checked by a
    Windows-only test that parses it for the Run value name and AUMID (no
    execution) and by live check L6.
23. Uninstall helper against `FakeRegistry`: removes Run value and AUMID key;
    running on an empty registry raises nothing. `-RemoveData` path covered by
    a test that runs the Python part of uninstall against the isolated home.
24. Settings autostart toggle calls the same helper (asserted on `FakeRegistry`).

`tests/test_privacy.py` must still pass, and is extended so the `labels`
table, the log file and `docs/results/camera-sharing.md` contain no image data.

## 5. Live checks

- **L1 Camera sharing (about 5 min).** Run `uv run python
  scripts/camera_share_probe.py` and follow the prompts: open the Windows
  Camera app, then a Meet (Chrome) or Zoom preview, once in each mode. Read the
  result table and answer decision 1.
- **L2 Video call (about 3 min).** With the app running, start a Meet or Zoom
  call with video. The video works, the tray turns grey with "camera in use by
  ...", and within about 15 s of ending the call it turns green again.
- **L3 Lock (1 min).** Press Win+L; on unlock the tray shows it was paused
  (tooltip or Open data folder -> log). The webcam light goes off while
  locked. Check the tray colour changes within 2 s of resuming.
- **L4 Sign out and in (2 min).** Sign out and back in. The app starts on its
  own, with no console window, and the tray icon appears.
- **L5 Low battery (2 min).** Open Settings, set the threshold to 1 point
  above the current charge, unplug: the app pauses with "low battery". Plug in:
  it resumes. Restore the threshold.
- **L6 Install / uninstall (3 min).** Run `.\uninstall.ps1`, confirm the
  "Posture Checker" entry is gone from Task Manager's Startup tab and the app
  is not running; run `.\install.ps1`, confirm the entry is back and the tray
  icon appears.
- **L7 Tray actions (3 min).** Use Calibrate... (window appears, completes),
  Settings... (change the alert window, save, no restart needed), I'm slouching
  now (confirmation toast), and Mark latest alert wrong after a test alert.
- **L8 Nobody present (6 min).** Leave the desk for over 5 minutes; when you
  return, the next alert-relevant sample happens within about 30 s.

## 6. Out of scope

- Daily and weekly reports, resource metrics and the HTML report (Stage 3).
- Using the `labels` table or wrong-alert feedback for training (Stage 5).
- Any measured CPU, memory or battery numbers (Stage 6); no such numbers are
  claimed here.
- A packaged .exe, an installer with admin rights, Start menu shortcuts, or
  per-machine install.
- Detecting camera use by apps that bypass the Windows capability tracking
  (some old desktop apps and drivers), and microphone-only calls.
- Pausing on full-screen apps or games, or on a schedule.
- Toast buttons working after the app has quit (they need the process).

## 7. Risks and limitations

- **Camera sharing is unverified.** It is unknown whether this webcam with
  DirectShow lets a second app open it while we hold it. If it does not, and
  `hold` is chosen, a call may start with no video until our next pause check;
  that is why burst mode exists and why L1 comes first. Burst mode adds
  about a second of camera start-up per sample (Stage 1's comment; not
  measured on this machine) and a blinking webcam light, and may raise CPU and
  battery use; Stage 6 will measure it.
- **Race in burst mode.** If a call starts in the instant we hold the camera,
  the other app can still fail once; most apps retry, but this is not
  guaranteed.
- **The registry signal, as verified on this machine (read-only, 2026-11-21):**
  the ConsentStore webcam key exists with global value `Allow`. Packaged apps
  appear as subkeys (e.g. `MSTeams_8wekyb3d8bbwe`) and desktop apps under
  `NonPackaged` with `#` in place of `\` (e.g. Zoom.exe, chrome.exe,
  Discord.exe, and a `python.exe` from an earlier Python install, which is why
  our own executable must be excluded). No app was using the camera at the
  time, so every entry had a non-zero `LastUsedTimeStop`; the "stop == 0 means
  in use" rule itself was not observed live and is confirmed by L2. Meet in a
  browser shows up as the browser (`chrome.exe`), not as "Meet". The key is
  undocumented by Microsoft and could change in a Windows update; behaviour 4
  degrades to Stage 1's "camera busy" handling if it does.
- **Our own entry.** While we hold the camera, our Python process shows as in
  use; matching by executable path can fail if the venv path differs from
  `sys.executable` (for example `python.exe` vs `pythonw.exe`). The exclusion
  must cover both.
- **Lock detection by polling** can lag up to the check interval (5 s); a
  session-notification window would be instant but adds Win32 message-loop
  code. Either meets the 10 s target.
- **Battery** status comes from `psutil`; the WMI battery query on this
  machine returned no values when checked, so Battery Saver detection needs a
  separate Windows API call and is unverified until L5.
- **Toasts and buttons** only act while the app runs; after Quit, clicking an
  old toast does nothing.
- **New dependency:** `pystray` (and Pillow, which it needs to draw the icon).
  Tkinter ships with the uv-managed Python, but this is not yet checked on
  this machine's interpreter.
- **pythonw has no console,** so crashes before logging starts are invisible;
  the installer's final step starts the app and reports whether it is still
  running after 5 s.
- The webcam light is on whenever monitoring in `hold` mode, including at
  login.
