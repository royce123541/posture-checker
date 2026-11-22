# Stage 2 explainer: the background app

## 1. What it does
Stage 1 was a terminal program. Stage 2 turns it into a small app that starts when you log in and sits in the Windows notification area (the "tray", by the clock). A coloured tray icon shows whether it is watching, paused or needs calibration. It pauses itself when the webcam is needed elsewhere, the screen is locked or the battery is low, and you can pause, calibrate, change settings or flag a wrong alert from the tray menu. `install.ps1` and `uninstall.ps1` set it up and remove it for one user.

## 2. How it works
1. `install.ps1` (via `install.py`) registers autostart: one value in the per-user "Run" registry key.
2. `app.py` starts one monitor thread plus the tray (`tray.py`, built on pystray). A named mutex (a Windows lock shared by processes) allows only one copy.
3. Each loop the monitor asks `policy/pausing.py` for a decision. The supervisor reads three probes in `platform/`: `camera_usage.py` (Windows' camera-consent registry records which apps use the webcam), `power.py` (screen lock, battery, Battery Saver).
4. If any reason is active, the monitor releases the camera and never opens it. It logs a `pause` or `resume` event per reason.
5. The tray refreshes every 2 s from the supervisor's reasons. Calibrate and Settings open as separate Tk windows (`calibrate_flow.py`, `settings_ui.py`); saved settings apply within about 2 s without restart.
6. Errors are logged to a rotating file; the app shows one notification and restarts the monitor after 30 s.

## 3. Key decisions
- **Reasons combined, not one flag.** The supervisor keeps a list (manual, calibrating, locked, low battery, camera-in-use per app). Alternative: one "paused" boolean. A boolean breaks as soon as two causes overlap: pressing Resume while a video call is running would wrongly restart the camera. Here Resume clears only "manual". Cost: more code and more tests.
- **Camera-in-use has a 10 s grace per app,** so a call that briefly turns video off does not get its camera grabbed. Cost: resume is a little late.
- **Battery pauses below a threshold with 5-point hysteresis** (the pause ends only 5 points higher) to stop flapping on and off.
- **Burst camera by default.** "Hold" keeps the camera open between samples; "burst" opens it per sample. The camera-sharing check (L1, `scripts/camera_share_probe.py`, results in `docs/results/camera-sharing.md`) showed that while hold has the webcam, neither the Windows Camera app nor a Meet preview gets a picture; in burst both do. So the default is burst.
- **Probes failing count as "no reason", logged once,** rather than crashing the monitor. Cost: a broken probe silently stops pausing.
- **Stale registry entries (older than 12 h) are ignored,** because a crashed app can leave "still in use" forever.
- **Tk windows as separate processes** rather than inside the tray process, so a window problem cannot freeze the tray.

How it was built: I approved the written spec first. Acceptance tests were written next, before any code. Then the implementation was written to pass them (319 tests pass, 1 live test skipped). Two review rounds followed; the main findings fixed were stale registry entries pausing forever, two quick Calibrate clicks opening two windows, and tray Pause blocking on the monitor or showing the wrong colour for 5 s. Each fix has a test that fails without it.

## 4. Likely interview questions
**Q: How does it know another app is using the camera?**
A: Windows records camera access per app in a registry key. If an app has a start time and no stop time, it is using the camera. I ignore my own Python processes, and I ignore entries older than 12 hours as stale. I have not yet confirmed the rule against a real video call.

**Q: Why a list of pause reasons instead of a boolean?**
A: Causes overlap. If I am paused manually and a call starts, then Resume should end only my manual pause. A list lets each cause clear independently, and the tray can say why it is paused.

**Q: Should the app hold the camera open or open it for each sample?**
A: Opening per sample. I measured it with a probe script on my laptop: while the app held the webcam, the Windows Camera app and a Meet preview got no picture; with per-sample opening both worked. Its extra cost per sample is not measured yet.

**Q: How did you build this with several AI agents, and how did you keep quality?**
A: I approved a written spec first. Tests came before code. A separate agent implemented, and two review rounds found real bugs, such as a duplicate calibration window and stale registry entries. Each got a regression test. A file-ownership check stops one agent editing another's files.

**Q: What happens when something crashes?**
A: Each probe is wrapped so a failure is logged and treated as "no reason". If the monitor itself fails, the app shows one notification and restarts it after 30 s. Quit stops the thread and releases the camera.

**Q: How do you avoid privacy problems?**
A: Tests scan the scripts for image writing and network use, and check that a run leaves only the database, log and config files, with no image data in the log.

## 5. Limitations to own
- The registry "in use" rule and the screen-lock error code are unverified until live checks (L2, L3); lock was only tested in the unlocked state.
- Battery Saver and low-battery paths were not tested on a real battery (L5).
- The tray icon, its refresh, the Tk windows, toasts, the mutex and both install scripts were never run on a real desktop; scripts were only parsed.
- Camera-in-use detection: during the L1 probe Windows recorded no camera use in the registry for any app, so the "pause during calls" signal did not fire. Burst mode keeps calls working regardless; whether the pause works is checked in L2.
- Deviation from the spec: "I'm slouching now" is a submenu (Slouching / Head forward / Too close), so it takes hover plus click, because pystray cannot make a submenu item clickable.
- There is no explicit camera release at sign-out or shutdown; it is released only because the process ends.
- Windows-only. No CPU, memory or battery numbers are claimed (Stage 6).
