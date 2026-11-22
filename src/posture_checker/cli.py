"""Command-line entry point.

Stage 1 runs in the foreground from a terminal. The tray app that replaces the
terminal for everyday use arrives in Stage 2 and reuses the same pieces.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Sequence
from datetime import datetime

from posture_checker import paths
from posture_checker.config import Config, load_config, save_config


def _open_camera(config: Config):  # type: ignore[no-untyped-def]
    from posture_checker.capture.camera import Camera

    camera = Camera(config.camera_index, config.frame_width, config.frame_height)
    if not camera.open():
        print(
            "Could not open the webcam. It may be in use by another app, "
            "or blocked in Settings > Privacy & security > Camera.",
            file=sys.stderr,
        )
        return None
    return camera


def cmd_fetch_model(_: argparse.Namespace) -> int:
    from posture_checker.capture.pose import POSE_LITE, ensure_model

    path = ensure_model(POSE_LITE)
    print(f"Pose model ready and checksum verified: {path}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the camera, lighting and detection without storing anything."""
    from posture_checker import features as feat
    from posture_checker.capture import quality as qual
    from posture_checker.capture.pose import PoseEstimator

    config = load_config()
    print(f"Data folder: {paths.app_dir()}")
    camera = _open_camera(config)
    if camera is None:
        return 1
    pose = PoseEstimator()
    latencies: list[float] = []
    brightnesses: list[float] = []
    confidences: list[float] = []
    found = 0
    shoulders = 0
    try:
        for i in range(args.samples):
            frame = camera.read()
            if frame is None:
                print(f"  {i + 1:2d}: no frame from camera")
                continue
            height, width = frame.shape[:2]
            brightness, contrast = qual.frame_stats(frame)
            brightnesses.append(brightness)
            result = pose.estimate(frame)
            del frame
            latencies.append(result.latency_ms)
            if result.landmarks is None:
                print(f"  {i + 1:2d}: nobody detected   brightness={brightness:5.1f}")
            else:
                found += 1
                raw = feat.raw_features(result.landmarks, aspect=width / height)
                has_shoulders = raw["shoulders_ok"] >= 0.5
                shoulders += has_shoulders
                quality = qual.assess(
                    result.landmarks, brightness, contrast, raw["yaw_asymmetry"], has_shoulders
                )
                confidences.append(quality.confidence)
                print(
                    f"  {i + 1:2d}: confidence={quality.confidence:.2f} "
                    f"brightness={brightness:5.1f} contrast={contrast:4.1f} "
                    f"face_scale={raw['face_scale']:.3f} "
                    f"shoulders={'yes' if has_shoulders else 'no '} "
                    f"latency={result.latency_ms:5.1f}ms "
                    f"{','.join(quality.reasons) or 'ok'}"
                )
            time.sleep(0.4)
    finally:
        camera.close()
        pose.close()

    print(f"\nPerson detected in {found}/{args.samples} frames, shoulders in {shoulders}.")
    if brightnesses and max(brightnesses) < qual.DARK_FULL:
        print(
            f"The picture is very dark (brightness {max(brightnesses):.0f} of 255). "
            "Check that the lens is not covered and that the room is lit."
        )
    if confidences:
        print(f"Mean confidence: {sum(confidences) / len(confidences):.2f}")
    if len(latencies) > 1:
        # The first inference includes one-off model warm-up.
        warm = sorted(latencies[1:])
        median = warm[len(warm) // 2]
        print(f"Pose latency (after warm-up): median {median:.1f} ms, max {warm[-1]:.1f} ms")
    return 0 if found else 2


def cmd_app(_: argparse.Namespace) -> int:
    from posture_checker.app import main as app_main

    return app_main()


def cmd_settings(_: argparse.Namespace) -> int:
    from posture_checker.settings_ui import main as settings_main

    return settings_main()


def cmd_calibrate(args: argparse.Namespace) -> int:
    if args.gui:
        from posture_checker.calibrate_ui import main as calibrate_window

        return calibrate_window(seconds=args.seconds, countdown=args.countdown)

    from posture_checker.calibration import CalibrationError, capture_calibration
    from posture_checker.capture.pose import PoseEstimator
    from posture_checker.storage.db import Database

    config = load_config()
    camera = _open_camera(config)
    if camera is None:
        return 1
    pose = PoseEstimator()
    print("Sit the way you want to sit: back against the chair, screen at arm's length,")
    print("looking at the screen. Hold still.")
    for n in range(args.countdown, 0, -1):
        print(f"  starting in {n}...", end="\r", flush=True)
        time.sleep(1)

    def progress(fraction: float, usable: int) -> None:
        print(
            f"  capturing {fraction * 100:3.0f}%  usable samples: {usable}   ", end="\r", flush=True
        )

    try:
        calibration = capture_calibration(
            camera, pose, duration_s=args.seconds, on_progress=progress
        )
    except CalibrationError as error:
        print(f"\nCalibration failed: {error}", file=sys.stderr)
        return 2
    finally:
        camera.close()
        pose.close()

    db = Database()
    calibration_id = db.insert_calibration(calibration)
    db.log_event(calibration.ts, "calibration", str(calibration_id))
    db.close()
    print(
        f"\nCalibration #{calibration_id} saved from {calibration.n_samples} samples "
        f"(mean confidence {calibration.mean_confidence:.2f})."
    )
    if not calibration.has_shoulders:
        print(
            "Note: your shoulders were not clearly visible, so only face-based measurements "
            "are used. Tilting the screen back a little usually brings them into view."
        )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from posture_checker.capture.camera import Camera
    from posture_checker.capture.pose import PoseEstimator
    from posture_checker.inference.rules import load_active
    from posture_checker.monitor import Monitor, Sample
    from posture_checker.platform.windows import ConsoleNotifier, ToastNotifier
    from posture_checker.storage.db import Database

    config = load_config()
    if args.window is not None:
        config.alert_window_s = args.window
    if args.sensitivity is not None:
        config.sensitivity = args.sensitivity
    if args.cooldown is not None:
        config.cooldown_s = args.cooldown
    config.validate()

    db = Database()
    if db.latest_calibration() is None:
        print("No calibration yet. Run `posture-checker calibrate` first.", file=sys.stderr)
        return 1

    notifier: ConsoleNotifier | ToastNotifier
    if args.no_toast or sys.platform != "win32":
        notifier = ConsoleNotifier()
    else:
        notifier = ToastNotifier()
        blocked = notifier.blocked_reason()
        if blocked:
            print(
                f"Warning: Windows will not show alert pop-ups because {blocked}. "
                "Alerts are printed here instead; turn notifications on to get pop-ups.",
                file=sys.stderr,
            )
            notifier = ConsoleNotifier()
    monitor = Monitor(
        config=config,
        db=db,
        source=Camera(config.camera_index, config.frame_width, config.frame_height),
        pose=PoseEstimator(),
        model=load_active(config.sensitivity),
        notifier=notifier,
    )

    def show(sample: Sample) -> None:
        stamp = datetime.fromtimestamp(sample.ts).strftime("%H:%M:%S")
        line = f"{stamp}  {sample.state:<18}"
        if sample.prediction is not None:
            rel = sample.relative
            line += (
                f" {sample.prediction.label:<13} conf={sample.confidence:.2f}"
                f" size={rel['face_scale_ratio']:.2f} drop={rel['face_drop']:+.2f}"
                f" head/shoulder={rel['head_vs_shoulder_ratio']:.2f}"
                f" {sample.latency_ms:.0f}ms"
            )
        if sample.reasons:
            line += f"  [{','.join(sample.reasons)}]"
        print(line)
        if sample.alert is not None:
            print(
                f"          -> alert #{sample.alert_id}: {sample.alert.label} "
                f"({sample.alert.bad_fraction:.0%} of the last {sample.alert.window_s:.0f}s)"
            )

    print(
        f"Monitoring with {monitor.model.version}: alert after {config.alert_window_s:.0f}s of bad "
        f"posture, sensitivity {config.sensitivity}. Press Ctrl+C to stop."
    )
    stop = threading.Event()
    try:
        monitor.run(stop, on_sample=show)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        db.close()
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Guided session: hold each prompted posture while labelled samples are stored."""
    from posture_checker.capture.pose import PoseEstimator
    from posture_checker.recording import DEFAULT_STEPS, Step, run_recording
    from posture_checker.storage.db import Database

    db = Database()
    calibration = db.latest_calibration()
    if calibration is None:
        print("No calibration yet. Run `posture-checker calibrate` first.", file=sys.stderr)
        db.close()
        return 1
    steps = DEFAULT_STEPS
    if args.seconds is not None:
        steps = tuple(Step(s.label, s.instruction, args.seconds) for s in steps)
    total = sum(s.seconds for s in steps)
    print(f"Guided recording: {len(steps)} postures, about {total / 60:.0f} minutes.")
    print("Only measurements are stored, never images. Hold each posture until the next prompt.")

    config = load_config()
    camera = _open_camera(config)
    if camera is None:
        db.close()
        return 1
    pose = PoseEstimator()

    def on_step(index: int, step: Step) -> None:
        print(f"\n[{index + 1}/{len(steps)}] {step.instruction}")
        for n in range(3, 0, -1):
            print(f"  starting in {n}...", end="\r", flush=True)
            time.sleep(1)

    def on_progress(fraction: float) -> None:
        print(f"  recording {fraction * 100:3.0f}%   ", end="\r", flush=True)

    try:
        summary = run_recording(
            camera, pose, db, calibration, steps=steps, on_step=on_step, on_progress=on_progress
        )
    except KeyboardInterrupt:
        print("\nStopped early; samples recorded so far are kept.")
        return 1
    finally:
        camera.close()
        pose.close()
        db.close()
    print(f"\nSession {summary.session} saved:")
    for label, count in summary.counts.items():
        print(f"  {label:<13} {count} samples")
    print(f"  skipped (nobody found or low confidence): {summary.skipped_low_confidence}")
    print("Next: `posture-checker fit-rules` to fit thresholds to this recording.")
    return 0


def cmd_fit_rules(args: argparse.Namespace) -> int:
    """Fit rule thresholds to recordings, compare with the active rules, deploy if better."""
    from posture_checker.inference.rules import RulesModel, load_active, save_fitted
    from posture_checker.mlops import fit_rules as fr
    from posture_checker.storage.db import Database

    db = Database()
    sessions = db.recorded_sessions()
    if not sessions:
        print("No recordings yet. Run `posture-checker record` first.", file=sys.stderr)
        db.close()
        return 1
    if args.session:
        missing = [s for s in args.session if s not in sessions]
        if missing:
            print(f"Unknown recording session(s): {', '.join(missing)}", file=sys.stderr)
            print(f"Recorded sessions: {', '.join(sessions)}", file=sys.stderr)
            db.close()
            return 1
        chosen = list(dict.fromkeys(args.session))
    else:
        chosen = sessions if args.all else sessions[-1:]
    samples = db.recorded_samples(chosen)
    db.close()

    train, test = fr.time_blocked_split(samples)
    current = load_active(1.0)  # thresholds are fitted at sensitivity 1
    fitted, fits = fr.fit_thresholds(train, current.thresholds, keep=args.keep or ())
    candidate = RulesModel(1.0, thresholds=fitted, version=fr.next_version(current.version))
    before = fr.evaluate(current, test)
    after = fr.evaluate(candidate, test)

    print(f"Sessions: {', '.join(chosen)}  ({len(train)} fitting, {len(test)} held-out samples)\n")
    print(f"{'threshold':<30}{'old':>8}{'new':>8}  balanced accuracy (fit)")
    for fit in fits:
        acc = "-" if fit.train_balanced_accuracy is None else f"{fit.train_balanced_accuracy:.2f}"
        print(f"{fit.name:<30}{fit.old:>8.3f}{fit.new:>8.3f}  {acc}  {fit.note}")

    def show(name: str, ev: fr.Evaluation) -> None:
        print(
            f"\n{name}: accuracy {ev.accuracy:.0%}, good labelled bad {ev.false_bad_rate:.0%}, "
            f"bad posture detected {ev.bad_detection_rate:.0%}  (n={ev.n})"
        )
        print("  true \\ predicted  " + "".join(f"{p:>14}" for p in ev.confusion))
        for true_label, row in ev.confusion.items():
            if sum(row.values()):
                print(f"  {true_label:<18}" + "".join(f"{v:>14}" for v in row.values()))

    show(f"Active ({current.version}) on held-out data", before)
    show(f"Candidate ({candidate.version}) on held-out data", after)

    if not fr.passes_gate(after, before):
        print(f"\nCandidate is not better on held-out data; keeping {current.version}")
        return 2
    if not args.deploy:
        print(f"\nCandidate passes the gate. Run with --deploy to make {candidate.version} active.")
        return 0
    path = save_fitted(
        fitted,
        candidate.version,
        {
            "sessions": chosen,
            "previous_version": current.version,
            "n_train": len(train),
            "n_test": len(test),
            "held_out_before": fr.evaluation_dict(before),
            "held_out_after": fr.evaluation_dict(after),
            "fitted_at": time.time(),
        },
    )
    print(f"\nDeployed {candidate.version} -> {path}")
    return 0


def cmd_mark_wrong(_: argparse.Namespace) -> int:
    from posture_checker.storage.db import Database

    db = Database()
    alert = db.last_alert()
    if alert is None:
        print("There are no alerts yet.")
        return 1
    db.mark_alert_wrong(alert["id"], time.time())
    db.close()
    print(f"Alert #{alert['id']} ({alert['label']}) marked as wrong.")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config = load_config()
    changed = False
    for name in ("alert_window_s", "sensitivity", "cooldown_s", "bad_fraction"):
        value = getattr(args, name)
        if value is not None:
            setattr(config, name, value)
            changed = True
    if changed:
        save_config(config)
    for name, value in vars(config).items():
        print(f"{name} = {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="posture-checker", description=__doc__)
    parser.add_argument(
        "--gui",
        action="store_true",
        help="use a window instead of the terminal (how the tray starts calibrate)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch-model", help="download and verify the pose model").set_defaults(
        func=cmd_fetch_model
    )

    doctor = sub.add_parser("doctor", help="check camera, lighting and detection")
    doctor.add_argument("--samples", type=int, default=10)
    doctor.set_defaults(func=cmd_doctor)

    calibrate = sub.add_parser("calibrate", help="record your good posture")
    calibrate.add_argument("--seconds", type=float, default=8.0)
    calibrate.add_argument("--countdown", type=int, default=3)
    calibrate.set_defaults(func=cmd_calibrate)

    run = sub.add_parser("run", help="monitor posture in the foreground")
    run.add_argument("--window", type=float, help="seconds of bad posture before an alert")
    run.add_argument("--sensitivity", type=float, help="1.0 default; higher is stricter")
    run.add_argument("--cooldown", type=float, help="seconds between alerts")
    run.add_argument("--no-toast", action="store_true", help="print alerts instead of toasts")
    run.set_defaults(func=cmd_run)

    sub.add_parser("app", help="run in the background with a tray icon").set_defaults(func=cmd_app)
    sub.add_parser("settings", help="open the settings window").set_defaults(func=cmd_settings)

    record = sub.add_parser("record", help="guided labelled recording of postures")
    record.add_argument("--seconds", type=float, help="seconds per posture (default 25)")
    record.set_defaults(func=cmd_record)

    from posture_checker.mlops.fit_rules import TARGETS as FIT_TARGETS

    fit = sub.add_parser("fit-rules", help="fit rule thresholds to your recordings")
    which = fit.add_mutually_exclusive_group()
    which.add_argument(
        "--all", action="store_true", help="use every recording, not just the latest"
    )
    which.add_argument(
        "--session",
        action="append",
        metavar="ID",
        help="use this recording session (repeat to combine several)",
    )
    fit.add_argument(
        "--keep",
        action="append",
        metavar="THRESHOLD",
        choices=list(FIT_TARGETS),
        help="leave this threshold at its current value (repeatable)",
    )
    fit.add_argument("--deploy", action="store_true", help="make the fitted rules active if better")
    fit.set_defaults(func=cmd_fit_rules)

    sub.add_parser("mark-wrong", help="mark the latest alert as wrong").set_defaults(
        func=cmd_mark_wrong
    )

    config = sub.add_parser("config", help="show or change settings")
    config.add_argument("--window", dest="alert_window_s", type=float)
    config.add_argument("--sensitivity", type=float)
    config.add_argument("--cooldown", dest="cooldown_s", type=float)
    config.add_argument("--bad-fraction", dest="bad_fraction", type=float)
    config.set_defaults(func=cmd_config)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
