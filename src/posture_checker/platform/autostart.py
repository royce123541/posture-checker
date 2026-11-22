"""Start the app at login through the per-user Run key (no admin needed)."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from posture_checker.platform.registry import Registry

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PostureChecker"


def pythonw_executable() -> Path:
    """The console-less interpreter next to the one running now."""
    return Path(sys.executable).with_name("pythonw.exe")


def autostart_command() -> str:
    return f'"{pythonw_executable()}" -m posture_checker app'


def enable_autostart(registry: Registry) -> None:
    """Write (or overwrite) the one ``PostureChecker`` Run value."""
    registry.set_value(RUN_KEY, RUN_VALUE, autostart_command())


def disable_autostart(registry: Registry) -> None:
    """Remove the Run value; no error if it is already gone."""
    with contextlib.suppress(FileNotFoundError):
        registry.delete_value(RUN_KEY, RUN_VALUE)


def is_autostart_enabled(registry: Registry) -> bool:
    try:
        return RUN_VALUE in registry.values(RUN_KEY)
    except FileNotFoundError:
        return False
