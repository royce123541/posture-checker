"""The calibration window (spec Stage 2, behaviour 15).

``CalibrationController`` holds all the logic and the text the window shows
(countdown, progress, result or error); it is tested with fakes. The Tkinter
part only displays the controller's state and runs it on a worker thread.

Exit codes of the window process: 0 saved, 1 cancelled or camera unavailable,
2 calibration rejected (moved too much, too dark, ...).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from posture_checker.calibration import CalibrationError, capture_calibration
from posture_checker.capture.camera import FrameSource
from posture_checker.capture.pose import PoseBackend
from posture_checker.storage.db import Database

EXIT_OK, EXIT_CANCELLED, EXIT_FAILED = 0, 1, 2
#: Seconds of countdown before capture starts (same default as the CLI).
COUNTDOWN_S = 3
#: Seconds of good posture to capture (same default as the CLI).
CAPTURE_S = 8.0
#: Window refresh period in milliseconds (smooth enough for a progress bar).
UI_POLL_MS = 100
#: After a successful calibration the window closes itself after this long.
CLOSE_AFTER_SUCCESS_MS = 3000
#: How long the closing window waits for a cancelled capture to stop: a little
#: over one capture step (0.25 s) plus a pose inference.
WORKER_JOIN_S = 2.0

CAMERA_ERROR = (
    "Could not open the webcam. It may be in use by another app, "
    "or blocked in Settings > Privacy & security > Camera."
)


class _Cancelled(Exception):
    pass


class CalibrationController:
    def __init__(
        self,
        source: FrameSource,
        pose: PoseBackend,
        db: Database,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        countdown: int = COUNTDOWN_S,
        duration_s: float = CAPTURE_S,
    ) -> None:
        self.source = source
        self.pose = pose
        self.db = db
        self.clock = clock
        self.sleep = sleep
        self.countdown = countdown
        self.duration_s = duration_s
        self.message = (
            "Sit the way you want to sit: back against the chair, screen at arm's length."
        )
        self.fraction = 0.0
        self.exit_code: int | None = None
        self._cancel = threading.Event()

    @property
    def done(self) -> bool:
        return self.exit_code is not None

    def cancel(self) -> None:
        self._cancel.set()

    def _finish(self, code: int, message: str) -> int:
        self.message = message
        self.exit_code = code
        return code

    def _progress(self, fraction: float, usable: int) -> None:
        if self._cancel.is_set():
            raise _Cancelled
        self.fraction = fraction
        self.message = f"Hold still... {fraction * 100:.0f}% ({usable} usable samples)"

    def run(self) -> int:
        for n in range(self.countdown, 0, -1):
            if self._cancel.is_set():
                return self._finish(EXIT_CANCELLED, "Calibration cancelled.")
            self.message = f"Sit in your good posture and look at the screen. Starting in {n}..."
            self.sleep(1.0)
        if not self.source.open():
            return self._finish(EXIT_CANCELLED, CAMERA_ERROR)
        try:
            calibration = capture_calibration(
                self.source,
                self.pose,
                duration_s=self.duration_s,
                clock=self.clock,
                sleep=self.sleep,
                on_progress=self._progress,
            )
        except _Cancelled:
            return self._finish(EXIT_CANCELLED, "Calibration cancelled.")
        except CalibrationError as error:
            return self._finish(EXIT_FAILED, f"Calibration failed: {error}")
        finally:
            self.source.close()

        calibration_id = self.db.insert_calibration(calibration)
        self.db.log_event(calibration.ts, "calibration", str(calibration_id))
        self.fraction = 1.0
        text = (
            f"Calibration #{calibration_id} saved from {calibration.n_samples} samples. "
            "Monitoring resumes now."
        )
        if not calibration.has_shoulders:
            text += (
                " Your shoulders were not clearly visible, so only face-based measurements "
                "are used; tilting the screen back a little usually helps."
            )
        return self._finish(EXIT_OK, text)


def run_window(controller: CalibrationController) -> int:  # pragma: no cover - needs a display
    """Show the calibration window until it is closed; return the exit code."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Posture Checker: calibrate")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frame = ttk.Frame(root, padding=16)
    frame.grid()
    text = tk.StringVar(value=controller.message)
    ttk.Label(frame, textvariable=text, wraplength=360, width=60).grid(row=0, column=0, pady=4)
    bar = ttk.Progressbar(frame, length=360, maximum=1.0)
    bar.grid(row=1, column=0, pady=8)
    button = ttk.Button(frame, text="Cancel")
    button.grid(row=2, column=0)

    def close() -> None:
        controller.cancel()
        root.destroy()

    button.configure(command=close)
    root.protocol("WM_DELETE_WINDOW", close)
    worker = threading.Thread(target=controller.run, name="calibration", daemon=True)
    worker.start()

    def poll() -> None:
        text.set(controller.message)
        bar["value"] = controller.fraction
        if controller.done:
            button.configure(text="Close")
            if controller.exit_code == EXIT_OK:
                root.after(CLOSE_AFTER_SUCCESS_MS, root.destroy)
            return
        root.after(UI_POLL_MS, poll)

    poll()
    root.mainloop()
    # Let a cancelled capture stop (it checks at every sample) before the
    # caller closes the camera and pose model under it.
    worker.join(timeout=WORKER_JOIN_S)
    return controller.exit_code if controller.exit_code is not None else EXIT_CANCELLED


def main(seconds: float = CAPTURE_S, countdown: int = COUNTDOWN_S) -> int:  # pragma: no cover
    from posture_checker.capture.camera import Camera
    from posture_checker.capture.pose import PoseEstimator
    from posture_checker.config import load_config

    config = load_config()
    db = Database()
    pose = PoseEstimator()
    camera = Camera(config.camera_index, config.frame_width, config.frame_height)
    controller = CalibrationController(camera, pose, db, countdown=countdown, duration_s=seconds)
    try:
        return run_window(controller)
    finally:
        camera.close()
        pose.close()
        db.close()
