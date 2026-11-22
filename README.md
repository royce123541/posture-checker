# Posture Checker

A background posture checker for a Windows laptop, built around an on-device
MLOps loop: user feedback becomes labeled data, models are versioned and
retrained on a schedule, and a new model is deployed only if it beats the
current one.

> **Status: Stage 2 of 6 (live checks pending).** Detection, calibration and
> alerts run as a background tray app that starts at login and pauses itself.
> Summaries, the trained model, the retraining loop and drift detection are the
> next stages. This
> README grows with each stage; results tables are filled in only with numbers
> that were actually measured.

## What works today

- Reads the webcam about once every 2 seconds and finds upper-body landmarks
  with MediaPipe Pose (lite model, CPU only).
- Compares each sample with your calibrated good posture and labels it `good`,
  `slouch`, `forward_head` or `too_close` using the `rules-v0` baseline model.
- Alerts with a Windows toast only when bad posture has lasted for a
  configurable window (default 60 s), with a cooldown between alerts.
- Scores every sample's confidence. Dark rooms, a turned head or a face at the
  edge of the frame produce `low_confidence` samples, which never trigger alerts.
- Stores measurements, alerts and "wrong alert" feedback in a local SQLite
  database, each row tagged with the model version and calibration that produced it.
- Runs in the background with a tray icon (green monitoring, grey paused, amber
  needs calibration or camera unavailable). It pauses and releases the webcam
  while another app uses it, while the screen is locked and on low battery,
  and samples only every 30 s after 5 minutes with nobody at the desk.
- Pauses during Google Meet, Zoom and Teams calls, recognised from the meeting
  window's title and process name (titles are never stored, only the app name).
  The "Pause during calls" checkbox in Settings turns this off.
- Tray menu: pause / resume, mark the latest alert wrong, "I'm slouching now"
  (stored as a label for later training), calibrate, settings, open data folder.

## Install (Stage 2)

From the repository folder, in PowerShell (no admin rights needed):

```powershell
.\install.ps1                # uv sync, pose model, autostart at login, start the app
.\install.ps1 -NoAutostart   # same, without starting at login
.\uninstall.ps1              # stop the app, remove autostart; keeps your data
.\uninstall.ps1 -RemoveData  # also delete %LOCALAPPDATA%\PostureChecker
```

The app logs to `%LOCALAPPDATA%\PostureChecker\posture-checker.log` (rotating,
at most 3 x 1 MB). `camera_mode` (`hold` or `burst`) is in Settings; the
default is `burst`, because the camera sharing probe
(`uv run python scripts/camera_share_probe.py`) showed that `hold` blocks other
apps from the webcam; see `docs/results/camera-sharing.md`.

## Terminal use (Stage 1)

Requires Windows 10/11 and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync                              # install dependencies into .venv
uv run posture-checker fetch-model   # download the pose model (checksum-verified)
uv run posture-checker doctor        # check camera, lighting and detection
uv run posture-checker calibrate     # sit in your good posture for 8 seconds
uv run posture-checker run           # start monitoring (Ctrl+C to stop)
```

Useful while testing: `uv run posture-checker run --window 20 --cooldown 30`
alerts after 20 seconds instead of 60.

Settings persist with `posture-checker config --window 90 --sensitivity 1.2`.

## Architecture

```
 webcam frame (memory only, never written)
      |
      v
 MediaPipe pose (lite, CPU) --> quality gate (lighting, visibility, head turn)
      |                              |
      v                              +--> "low confidence": logged, never alerts
 features relative to calibration
      |
      v
 posture model (version-tagged) --> alert policy (time window, cooldown)
      |                                   |
      v                                   v
 SQLite: samples, alerts, feedback,   toast: "Wrong alert" / "Pause 30 min"
 calibrations, events
```

| Module | Role |
|---|---|
| `capture/` | Webcam, pose estimation, per-sample quality score |
| `features.py` | Landmarks to posture measurements, relative to the calibration baseline |
| `calibration.py` | Records the "good posture" baseline (median over a few seconds) |
| `inference/` | Model interface and the `rules-v0` baseline |
| `policy/alerts.py` | Decides when sustained bad posture becomes an alert |
| `monitor.py` | The loop that ties it together; every dependency is injected |
| `storage/db.py` | SQLite schema and access |
| `platform/windows.py` | Toast notifications |
| `synthetic.py` | Synthetic landmarks for tests and CI |

## Design choices so far

- **Pretrained pose model plus a small posture model on top.** The pose model
  is frozen; the part that learns from this user is a small classifier on
  geometric features. It can be trained on minutes of data, retrained in
  seconds, and explained. `rules-v0` is the baseline it will have to beat.
- **Features are relative to calibration.** Rules and models see "face 18%
  larger than baseline", never raw pixels, so recalibrating adapts them to a new
  desk without retraining.
- **Low duty cycle.** One frame every 2 s (1 s while bad posture is suspected,
  10 s while nobody is there) instead of 30 fps, so the model is idle almost
  all of the time. Measured latency and resource use are reported in Stage 6.
- **CPU only.** Waking the laptop's discrete GPU would cost far more battery
  than a few milliseconds of CPU.
- **Confidence instead of guessing.** A sample the app does not trust is stored
  as `low_confidence` and excluded from alert decisions.
- **Dependency injection throughout.** Camera, pose model, posture model,
  notifier and clock are passed in, so the full pipeline runs in CI with fakes.

## Privacy

- Frames exist only in memory and are discarded right after pose estimation.
- The database stores numbers and short strings only: 13 upper-body landmark
  coordinates per sample, derived features, scores and timings.
- Nothing is uploaded. The only network access is the one-time, checksum-verified
  download of the pose model.
- `tests/test_privacy.py` enforces this: no image-writing calls in the package,
  no network imports outside the model download, no binary columns in the
  database, and the pipeline runs with sockets disabled.

## Known limitations

- A front-facing webcam cannot measure neck angle. Forward head and slouch are
  inferred from proxies (face size relative to shoulders, head height in the
  frame, head-to-shoulder distance), so results depend on a good calibration.
- `rules-v0` thresholds are geometric starting points, not tuned values.
- The webcam light stays on while monitoring.
- Call detection is a title heuristic: Meet counts only when the meeting is the
  active tab, Firefox is not covered, and app updates can change the titles.
  After a Meet call ends, the "meeting ended" page keeps the meeting title, so
  monitoring resumes when that tab is closed, or about a minute later when Meet
  leaves the page (observed in live check L2b).
- Windows only.

## Development

```powershell
uv run pytest          # tests (no webcam needed)
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy            # type-check
```

CI runs the same checks and builds the wheel on every push
(`.github/workflows/ci.yml`).

### File ownership

Each stage starts from a written spec (`docs/specs/`) and acceptance tests
written from it before any code (`tests/acceptance/`). So that code can't be
made to pass by changing its own targets, CI runs
`scripts/check_ownership.py`, which requires:

| Path | Commit subject must start with | May change other files in the same commit |
|---|---|---|
| `docs/specs/` | `Spec:` | no |
| `tests/acceptance/` | `Acceptance tests:` | no |
