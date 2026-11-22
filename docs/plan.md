# Posture checker with an on-device MLOps loop

## Context

The approved build plan. Stage specs in `docs/specs/` refine it; where they differ, the approved spec wins.

Portfolio project for an MLOps internship: a background posture checker for this laptop whose main point is the ML lifecycle around it (feedback to labels, versioned data and models, gated retraining, drift detection, monitoring, CI).

Detected on this machine: Windows 11, ASUS TUF F15, i5-11400H (6 cores / 12 threads), 8 GB RAM, RTX 3050 plus Intel UHD, USB UVC webcam, battery present. Python 3.10, uv, git and an authenticated GitHub CLI are installed.

Your answers:
- Repo lives in `Downloads\posture-checker`.
- Public GitHub repo. Commits use your existing git identity.
- You will do one guided labeling session of about 10 minutes.
- Alerts are Windows toasts with buttons, plus a tray icon.

## Key decisions

| Area | Choice | Why, and what it costs |
|---|---|---|
| Runtime | Python 3.11 managed by uv, Windows only | Fastest route to a working ML system; uv gives a locked, one-command environment. Cost: not a single .exe, about 400 MB of dependencies. OS-specific code sits behind one `platform/` module so a port is possible later. |
| Pose estimation | MediaPipe Pose Landmarker (lite), CPU only, frozen | Pretrained, gives shoulders, ears, eyes, nose with visibility scores. Never touches the RTX 3050, so the discrete GPU stays asleep. |
| Posture model | Small classifier on geometric features measured relative to your calibration (good / slouch / forward head / too close) | This is the model that gets versioned and retrained. Trainable from minutes of data, retrains in seconds, explainable. A rule-based `rules-v0` ships first and is the baseline every trained model must beat. |
| Serving | scikit-learn model exported to ONNX, run with ONNX Runtime | The background process never imports scikit-learn, pandas or MLflow, which keeps memory down. |
| Sampling | One frame about every 2 s at 640x480, slower when you are away or on battery, faster while bad posture is suspected | The biggest resource saving is not running at 30 fps. |
| Storage | SQLite in `%LOCALAPPDATA%\PostureChecker` | One file, no server. Stores landmark features, scores and metrics only. |
| Experiments and registry | MLflow with a local SQLite backend, `champion` / `challenger` aliases | Industry-standard names, runs only at training time. |
| Data versioning | Immutable Parquet snapshots named by content hash, hash logged on every MLflow run | Data is produced on-device by a running app, so DVC's git-coupled workflow fits an unattended job badly. I will say so in the README. |
| Drift | Hand-written PSI and KS tests per feature against the post-calibration reference | About 50 lines and testable, instead of a heavy library inside the background process. |
| UI | Tray icon (pystray), native toasts (windows-toasts), small Tkinter windows for calibration and settings, a generated HTML report for summaries and monitoring | No always-on web server, so idle cost is zero. |
| Autostart | `HKCU\...\Run` entry launching `pythonw.exe` | No admin rights, no console window, visible in Task Manager's Startup tab. |
| Retrain schedule | The app itself starts the trainer as a separate low-priority process when a week has passed or enough new labels exist, and only on AC power | A fixed Task Scheduler time is often missed on a laptop. |
| CI | GitHub Actions on `windows-latest`: ruff, mypy, pytest, pipeline smoke test on synthetic data, wheel build | Camera-free: tests replay synthetic landmark sequences. |

## Architecture

```
 webcam frame (memory only, never written)
      |
      v
 MediaPipe pose (lite, CPU) --> quality gate (visibility, brightness, face angle)
      |                              |
      v                              +--> "low confidence": logged, never alerts
 features relative to calibration
      |
      v
 posture model (ONNX, version-tagged) --> alert policy (time window, sensitivity)
      |                                        |
      v                                        v
 SQLite: samples, alerts, feedback,       toast: "Wrong alert" / "Pause 30 min"
 calibrations, resource metrics,          tray: pause, resume, calibrate,
 drift checks, model version per row            "I'm slouching now", report
      |
      +--> HTML report: daily / weekly summary, model and system health
      +--> drift monitor --> "setup changed, recalibrate?" toast
      +--> retrain job: snapshot data -> train -> evaluate -> gate vs champion
                         -> promote (atomic swap, hot reload) or keep; MLflow logs all

 Pause supervisor: other app using camera, screen locked, nobody present,
 low battery, manual pause  -->  camera released, resumes automatically
```

One background process (tray plus a monitor thread). Calibration, settings, report generation and training run as short-lived separate processes.

## Repository layout

```
src/posture_checker/
  app.py, cli.py, config.py
  capture/      camera.py, pose.py, quality.py
  features.py, calibration.py
  inference/    rules.py, onnx_model.py, model_store.py
  policy/       alerts.py (state machine), pausing.py
  platform/     windows.py (lock, camera-in-use, battery, autostart, toasts)
  storage/      db.py, schema.sql
  ui/           tray.py, calibrate.py, settings.py, report.py
  monitoring/   metrics.py, drift.py
  mlops/        dataset.py, train.py, evaluate.py, gate.py, promote.py, scheduler.py
tests/  scripts/ (collect_session, bench_latency, bench_resources)
docs/   install.ps1  uninstall.ps1  .github/workflows/ci.yml  README.md
```

## Build stages

Each stage ends with tests passing, a commit, and a short report to you including what does not work yet.

**Stage 0: Scaffold.** Move the session to `Downloads\posture-checker`, `git init`, uv project, lint and test config, CI workflow, empty-but-green pipeline. I ask once before creating the public repo and pushing.

**Stage 1: Detection and alerts (first working version).** Camera capture, pose, quality gate, features, calibration, `rules-v0`, alert state machine with adjustable window and sensitivity, toast, SQLite logging with model version on every row. Run from a terminal for now.

**Stage 2: Background app.** Tray with pause, resume, mark wrong, "I'm slouching now", calibrate, settings. Auto-pause supervisor. Autostart. `install.ps1` and `uninstall.ps1`.

**Stage 3: Summaries and monitoring.** Daily and weekly report; per-minute CPU, memory, latency, confidence and battery state; false-alert rate over time.

**Stage 4: Data, first trained model, optimization.** Your guided session, which runs the lite and full pose models on the same in-memory frames so their accuracy can be compared later without storing any image. Snapshots, MLflow, time-blocked train/test split, ONNX export. Before/after table: full vs lite pose model, 30 fps vs duty-cycled sampling, scikit-learn vs ONNX serving, with latency and accuracy for each.

**Stage 5: Continuous loop.** Feedback to labels (wrong alert = false positive, "I'm slouching now" = false negative, corrected-after-alert = weak positive), scheduled retrain, promotion gate (must not lose on the frozen test set, must reduce replayed false alerts on recent feedback, must stay inside the latency budget), rollback, drift detection and recalibration prompt.

**Stage 6: Measure and document.** CPU, memory and battery measurements, README with diagram, reasoning and results, demo-video shot list, interview talking points.

## Verification

- Unit tests: feature geometry, alert state machine with a fake clock, pause logic with mocked providers, drift statistics on synthetic shifts, promotion gate, snapshot determinism.
- Integration test: synthetic landmark sequences replayed through the full pipeline with no camera.
- Privacy tests: no image-writing calls in the package, no blob columns in the schema, pipeline passes with sockets disabled.
- Live checks with you: Stage 1 alert fires after sustained slouch and not after a brief lean; Stage 2 sign out and back in, lock screen, start a Zoom or Meet call, unplug to low-battery threshold (simulated threshold); Stage 5 a forced challenger that is worse gets rejected and one that is better gets promoted, with both visible in MLflow.
- Resource numbers: `bench_resources.py` over 30 minutes per mode; battery by alternating app-on and app-off blocks on battery and comparing discharge rate.
- CI green on every pushed stage.

## Limitations I already know about

- A front-facing webcam cannot measure neck angle directly. Forward head and slouch are inferred from proxies (neck compression, face size, torso drop) relative to your calibration, so calibration quality matters.
- Accuracy numbers will describe one person, one desk, about 10 minutes of labeled data. They are not general claims.
- Feedback is biased: you only label what the app alerts on. The "I'm slouching now" tray action partly fixes this.
- Camera sharing is the riskiest requirement. Windows normally gives a webcam to one app at a time, and I have not yet tested how this webcam behaves when Zoom or Chrome asks for it while we hold it. Stage 2 starts with that experiment; the fallback is opening the camera only in short bursts.
- The webcam light is on while monitoring.
- Drift detection can confuse "you slouch more this week" with "your desk changed". I will use setup-sensitive signals (best posture of the day, shoulder position in frame, brightness) and report how well it separates them.
- Battery measurement on a laptop is noisy; I will report the spread, not just a mean.
- Targets to measure, not promises: under 3% average total CPU, under 300 MB memory, under 30 ms per frame.

## What I need from you during the build

- Stage 1 and 2: a few minutes in front of the camera, one sign-out/sign-in, one test video call.
- Stage 4: the 10-minute guided session.
- Stage 6: about 40 minutes on battery for the power test.
- Approval before the first push to the public repo.
