"""Run calibration from the tray (spec Stage 2, behaviour 15).

The running app pauses with reason ``calibrating``, lets go of the camera and
starts a separate console-less process that shows the calibration window and
owns the camera meanwhile. When that process exits successfully the app loads
the new calibration; otherwise it keeps the old one. Either way the
``calibrating`` reason clears.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from typing import Protocol

from posture_checker.monitor import Monitor
from posture_checker.platform.autostart import pythonw_executable
from posture_checker.policy.pausing import CALIBRATING, PauseSupervisor

log = logging.getLogger(__name__)


class Process(Protocol):
    def wait(self) -> int: ...


Launcher = Callable[[list[str]], Process]


def gui_command(subcommand: str) -> list[str]:
    """Command line for one of the app's windows, run without a console."""
    return [str(pythonw_executable()), "-m", "posture_checker", "--gui", subcommand]


def launch(command: Sequence[str]) -> Process:
    """Default launcher: start the process detached from any console."""
    return subprocess.Popen(list(command))


def run_calibration(
    monitor: Monitor,
    supervisor: PauseSupervisor,
    launcher: Launcher = launch,
) -> bool:
    """Calibrate in a separate process; True if a new calibration was loaded."""
    supervisor.add_reason(CALIBRATING)
    try:
        monitor.check_pause()  # logs the pause and releases the camera now
        monitor.release_camera()
        process = launcher(gui_command("calibrate"))
        exit_code = process.wait()
        if exit_code == 0:
            monitor.reload_calibration()
            return True
        log.info("calibration window exited with code %s; keeping the old calibration", exit_code)
        return False
    finally:
        supervisor.remove_reason(CALIBRATING)
