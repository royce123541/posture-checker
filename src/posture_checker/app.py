"""The background app: tray icon plus the monitor on a worker thread.

``App`` holds everything except the tray and the real devices, so tests build
it from fakes. ``main`` wires up the real camera, pose model, Windows probes,
toasts and tray, and is what ``pythonw -m posture_checker app`` runs.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Protocol

from posture_checker import actions, paths
from posture_checker import tray as tray_mod
from posture_checker.calibrate_flow import Launcher, gui_command, launch, run_calibration
from posture_checker.monitor import CAMERA_UNAVAILABLE, PAUSED, Monitor, Sample
from posture_checker.policy.pausing import CALIBRATING, PauseSupervisor
from posture_checker.settings_ui import ConfigWatcher
from posture_checker.storage.db import Database
from posture_checker.tray import TrayState

log = logging.getLogger(__name__)

LOG_FILE = "posture-checker.log"
#: Rotating log limits from spec behaviour 18: at most 1 MB x 3 files.
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 2
#: After an unhandled monitor error, wait this long before sampling again
#: (spec behaviour 21).
RESTART_DELAY_S = 30.0
#: While waiting for the next sample, re-check the pause reasons this often so
#: a pause or resume is acted on promptly (spec behaviour 3: at least every 5 s).
PAUSE_POLL_S = 5.0
#: Pause lengths offered in the tray (spec behaviour 8).
PAUSE_15_MIN_S = 15 * 60
PAUSE_1_HOUR_S = 60 * 60
#: The tray re-reads the latest-alert state at most this often (it refreshes
#: every 2 s); a new alert or a "mark wrong" refreshes it at once.
ALERT_STATE_CACHE_S = 10.0

CALIBRATE_PROMPT = "Calibrate to start: sit in your good posture for a few seconds."
ERROR_TOAST = "Posture Checker hit an error and will retry in 30 seconds. Details are in the log."
ALREADY_RUNNING = "Posture Checker is already running."


class AppNotifier(Protocol):
    def alert(
        self,
        alert_id: int,
        label: str,
        on_wrong: Callable[[], None],
        on_pause: Callable[[], None],
    ) -> None: ...

    def message(self, text: str, on_action: Callable[[], None] | None = None) -> None: ...


def setup_logging() -> Path:
    """Send all logging to a rotating file in the data folder; return its path."""
    path = paths.app_dir() / LOG_FILE
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, RotatingFileHandler):
            root.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)
    return path


class App:
    def __init__(
        self,
        monitor: Monitor,
        supervisor: PauseSupervisor,
        db: Database,
        notifier: AppNotifier,
        clock: Callable[[], float] = time.time,
        launcher: Launcher | None = None,
        watcher: ConfigWatcher | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self.monitor = monitor
        self.supervisor = supervisor
        self.db = db
        self.notifier = notifier
        self.clock = clock
        self.launcher: Launcher = launcher or launch
        self.watcher = watcher
        self.on_quit = on_quit
        self._retry_at = 0.0
        self._error_shown = False
        self._last_sample: Sample | None = None
        #: Held while a calibration window is running (one at a time).
        self._calibration_slot = threading.Lock()
        #: Cached (has recent alert, marked) and when it was read.
        self._alert_state: tuple[bool, bool] | None = None
        self._alert_state_at = 0.0
        self._alert_state_lock = threading.Lock()

    # --- state for the tray -------------------------------------------------
    def _alert_feedback_state(self) -> tuple[bool, bool]:
        """Alert state for the menu, re-read at most every ALERT_STATE_CACHE_S."""
        with self._alert_state_lock:
            now = self.clock()
            if self._alert_state is None or now - self._alert_state_at >= ALERT_STATE_CACHE_S:
                self._alert_state = actions.alert_feedback_state(self.db, self.clock)
                self._alert_state_at = now
            return self._alert_state

    def _forget_alert_state(self) -> None:
        with self._alert_state_lock:
            self._alert_state = None

    def state(self) -> TrayState:
        has_recent, marked = self._alert_feedback_state()
        reasons = tuple(self.supervisor.reasons)
        if self._calibration_slot.locked() and CALIBRATING not in reasons:
            reasons += (CALIBRATING,)  # claimed, not yet seen by a supervisor check
        return TrayState(
            paused_reasons=reasons,
            manual_paused=self.supervisor.manual_active,
            calibrated=self.monitor.calibration is not None,
            camera_available=self.monitor.last_state != CAMERA_UNAVAILABLE,
            has_recent_alert=has_recent,
            latest_alert_marked=marked,
        )

    def status(self) -> tuple[str, str]:
        return tray_mod.tray_status(self.state())

    # --- lifecycle ----------------------------------------------------------
    def startup(self) -> None:
        if self.monitor.calibration is None:
            self.notifier.message(CALIBRATE_PROMPT, on_action=self.request_calibration)

    def tick(self) -> float:
        """Take one sample if due; return the seconds to wait before the next."""
        now = self.clock()
        if now < self._retry_at:
            return self._retry_at - now
        try:
            if self.watcher is not None:
                self.watcher.poll()
            sample = self.monitor.step()
        except Exception:
            log.exception("monitor error; restarting in %.0f s", RESTART_DELAY_S)
            try:
                self.monitor.release_camera()
            except Exception:
                log.exception("could not release the camera after an error")
            if not self._error_shown:
                self._error_shown = True
                self.notifier.message(ERROR_TOAST)
            self._retry_at = now + RESTART_DELAY_S
            return RESTART_DELAY_S
        self._last_sample = sample
        # A new failure after a recovery deserves its own toast.
        self._error_shown = False
        if sample.alert_id is not None:
            self._forget_alert_state()  # "Mark latest alert wrong" becomes available
        return self.monitor.next_interval(sample)

    def _pause_changed(self) -> bool:
        """True if the paused/running decision differs from the last sample's."""
        was_paused = self._last_sample is not None and self._last_sample.state == PAUSED
        try:
            return bool(self.monitor.check_pause()) != was_paused
        except Exception:
            log.exception("pause check failed")
            return False

    def run(self, stop: threading.Event) -> None:
        """Sample until ``stop`` is set, then release the camera."""
        self.db.log_event(self.clock(), "start", self.monitor.model.version)
        try:
            while not stop.is_set():
                delay = self.tick()
                deadline = time.monotonic() + delay
                while not stop.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if stop.wait(min(remaining, PAUSE_POLL_S)):
                        break
                    if time.monotonic() < deadline and self._pause_changed():
                        break  # act on a new pause, or resume, right away
        finally:
            self.monitor.release_camera()
            self.db.log_event(self.clock(), "stop")

    # --- tray actions -------------------------------------------------------
    # These run on the tray thread. They only update the supervisor and never
    # take the monitor's lock, so a slow camera open cannot freeze the tray;
    # the worker's pause poll (every PAUSE_POLL_S) releases the camera.
    def pause(self, seconds: float | None) -> None:
        self.supervisor.pause_manual(seconds)

    def resume(self) -> None:
        self.supervisor.resume_manual()

    def mark_wrong(self) -> None:
        actions.mark_latest_alert_wrong(self.db, self.clock)
        self._forget_alert_state()

    def label(self, name: str) -> None:
        actions.record_label(self.db, self.clock, name, confirm=self.notifier.message)

    def request_calibration(self) -> None:
        """Tray and toast callback for ``start_calibration``."""
        self.start_calibration()

    def start_calibration(self) -> bool:
        """Calibrate in a separate process without blocking the caller.

        The calibration slot is claimed here, synchronously, so a second request
        (a double click, or the toast plus the tray) is refused instead of
        starting a second window whose exit would resume monitoring early.
        """
        if not self._calibration_slot.acquire(blocking=False):
            log.info("calibration already running; request ignored")
            return False

        def work() -> None:
            try:
                run_calibration(self.monitor, self.supervisor, self.launcher)
            except Exception:
                log.exception("calibration failed to start")
            finally:
                self._calibration_slot.release()

        try:
            threading.Thread(target=work, name="calibrate", daemon=True).start()
        except Exception:
            self._calibration_slot.release()
            raise
        return True

    def open_settings(self) -> None:
        self.launcher(gui_command("settings"))

    def open_data_folder(self) -> None:
        os.startfile(paths.app_dir())

    def quit(self) -> None:
        if self.on_quit is not None:
            self.on_quit()

    def tray_actions(self) -> dict[str, Callable[[], None]]:
        return {
            tray_mod.PAUSE_15: lambda: self.pause(PAUSE_15_MIN_S),
            tray_mod.PAUSE_60: lambda: self.pause(PAUSE_1_HOUR_S),
            tray_mod.PAUSE_UNTIL_RESUMED: lambda: self.pause(None),
            tray_mod.RESUME: self.resume,
            tray_mod.MARK_WRONG: self.mark_wrong,
            tray_mod.LABEL_SLOUCH: lambda: self.label("slouch"),
            tray_mod.LABEL_FORWARD_HEAD: lambda: self.label("forward_head"),
            tray_mod.LABEL_TOO_CLOSE: lambda: self.label("too_close"),
            tray_mod.CALIBRATE: self.request_calibration,
            tray_mod.SETTINGS: self.open_settings,
            tray_mod.OPEN_DATA: self.open_data_folder,
            tray_mod.QUIT: self.quit,
        }


#: Seconds Quit waits for the monitor thread (spec behaviour 20: exit within 5 s).
QUIT_JOIN_S = 4.0


def main() -> int:  # pragma: no cover - needs Windows, a webcam and a desktop
    """Run the tray app until Quit."""
    setup_logging()
    sys.excepthook = lambda *exc: log.critical("unhandled error", exc_info=exc)
    threading.excepthook = lambda args: log.critical(
        "unhandled error in thread %s",
        args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),  # type: ignore[arg-type]
    )

    from posture_checker.platform.single_instance import InstanceLock, Win32MutexBackend
    from posture_checker.platform.windows import ToastNotifier

    notifier = ToastNotifier()
    if not InstanceLock(Win32MutexBackend()).acquire():
        log.info("another instance is running; exiting")
        notifier.message(ALREADY_RUNNING)
        return 0

    from posture_checker.capture.camera import Camera
    from posture_checker.capture.pose import PoseEstimator
    from posture_checker.config import load_config
    from posture_checker.inference.rules import RulesModel
    from posture_checker.platform.camera_usage import RegistryCameraUsage, filetime_now
    from posture_checker.platform.power import PsutilBattery, WindowsLockState
    from posture_checker.platform.registry import WinRegistry

    config = load_config()
    db = Database()

    def log_probe_event(text: str) -> None:
        log.warning(text)
        db.log_event(time.time(), "camera_probe", text)

    supervisor = PauseSupervisor(
        camera_usage=RegistryCameraUsage(
            WinRegistry(), log_event=log_probe_event, now_filetime=filetime_now
        ),
        lock_state=WindowsLockState(),
        battery=PsutilBattery(),
        battery_pause_percent=config.battery_pause_percent,
    )
    monitor = Monitor(
        config=config,
        db=db,
        source=Camera(config.camera_index, config.frame_width, config.frame_height),
        pose=PoseEstimator(),
        model=RulesModel(config.sensitivity),
        notifier=notifier,
        supervisor=supervisor,
    )
    stop = threading.Event()
    icon: tray_mod.TrayIcon | None = None

    def on_quit() -> None:
        stop.set()
        if icon is not None:
            icon.stop()

    app = App(
        monitor=monitor,
        supervisor=supervisor,
        db=db,
        notifier=notifier,
        watcher=ConfigWatcher(paths.config_path(), monitor),
        on_quit=on_quit,
    )
    icon = tray_mod.TrayIcon(app.state, app.tray_actions())
    worker = threading.Thread(target=app.run, args=(stop,), name="monitor", daemon=True)
    log.info("Posture Checker starting (model %s)", monitor.model.version)
    app.startup()
    worker.start()
    try:
        icon.run()  # blocks until Quit
    finally:
        stop.set()
        worker.join(timeout=QUIT_JOIN_S)
        monitor.release_camera()
        db.close()
        log.info("Posture Checker stopped")
    return 0
