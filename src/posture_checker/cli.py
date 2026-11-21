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
    from posture_checker.inference.rules import RulesModel
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

    notifier = ConsoleNotifier() if args.no_toast or sys.platform != "win32" else ToastNotifier()
    monitor = Monitor(
        config=config,
        db=db,
        source=Camera(config.camera_index, config.frame_width, config.frame_height),
        pose=PoseEstimator(),
        model=RulesModel(config.sensitivity),
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
