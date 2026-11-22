"""Start the app at sign-in through a shortcut in the user's Startup folder.

Task Manager's Startup apps lists a Startup-folder shortcut by its file name,
so it shows "Posture Checker" (spec Stage 2 fixes, F2). Earlier versions used
the HKCU Run value ``PostureChecker``; enabling and disabling both remove it so
an upgrade never starts the app twice. No admin rights are needed.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from posture_checker.platform.registry import Registry

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: The Run value written before the Startup shortcut replaced it.
RUN_VALUE = "PostureChecker"
#: The file name Task Manager shows (spec F2).
SHORTCUT_NAME = "Posture Checker.lnk"
APP_ARGUMENTS = "-m posture_checker app"
#: Repository root: src/posture_checker/platform/autostart.py -> three parents up from src.
REPO_ROOT = Path(__file__).resolve().parents[3]
#: IShellLink "show minimised" window style (SW_SHOWMINNOACTIVE), per the WScript docs.
WINDOW_STYLE_MINIMISED = 7
#: IShellLink normal window style (SW_SHOWNORMAL).
WINDOW_STYLE_NORMAL = 1
#: Generous cap for one powershell.exe start plus a COM call; normally under 2 s.
WRITER_TIMEOUT_S = 30


class ShortcutWriter(Protocol):
    def create(
        self, path: Path, target: Path, arguments: str, working_dir: Path, minimised: bool
    ) -> None: ...


# Reads the values from environment variables so no path is ever pasted into
# the script text (no injection through quotes in a folder name).
_PS_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PC_LNK_PATH); "
    "$s.TargetPath = $env:PC_LNK_TARGET; "
    "$s.Arguments = $env:PC_LNK_ARGS; "
    "$s.WorkingDirectory = $env:PC_LNK_DIR; "
    "$s.WindowStyle = [int]$env:PC_LNK_STYLE; "
    "$s.Save()"
)


class WScriptShortcutWriter:
    """Writes a .lnk with the WScript.Shell COM object via powershell.exe."""

    def create(
        self, path: Path, target: Path, arguments: str, working_dir: Path, minimised: bool
    ) -> None:
        env = dict(os.environ)
        env.update(
            PC_LNK_PATH=str(path),
            PC_LNK_TARGET=str(target),
            PC_LNK_ARGS=arguments,
            PC_LNK_DIR=str(working_dir),
            PC_LNK_STYLE=str(WINDOW_STYLE_MINIMISED if minimised else WINDOW_STYLE_NORMAL),
        )
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_SCRIPT],
            env=env,
            check=True,
            capture_output=True,
            timeout=WRITER_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def default_startup_dir() -> Path:
    """The per-user Startup folder (``shell:startup``)."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def pythonw_executable() -> Path:
    """The console-less interpreter next to the one running now."""
    return Path(sys.executable).with_name("pythonw.exe")


def autostart_command() -> str:
    return f'"{pythonw_executable()}" {APP_ARGUMENTS}'


def _remove_run_value(registry: Registry) -> None:
    with contextlib.suppress(FileNotFoundError):
        registry.delete_value(RUN_KEY, RUN_VALUE)


def enable_autostart(registry: Registry, *, writer: ShortcutWriter, startup_dir: Path) -> None:
    """Write (or overwrite) the one Startup shortcut and drop the old Run value."""
    startup_dir.mkdir(parents=True, exist_ok=True)
    writer.create(
        startup_dir / SHORTCUT_NAME,
        pythonw_executable(),
        APP_ARGUMENTS,
        REPO_ROOT,
        True,
    )
    _remove_run_value(registry)


def disable_autostart(registry: Registry, *, startup_dir: Path) -> None:
    """Delete the shortcut and the old Run value; no error if either is gone."""
    (startup_dir / SHORTCUT_NAME).unlink(missing_ok=True)
    _remove_run_value(registry)


def is_autostart_enabled(*, startup_dir: Path) -> bool:
    return (startup_dir / SHORTCUT_NAME).exists()
