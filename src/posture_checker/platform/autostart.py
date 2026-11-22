"""Start the app at sign-in through a shortcut in the user's Startup folder.

Task Manager's Startup apps names a shortcut after the program it runs, so the
shortcut runs ``hunch.exe``, uv's windowless launcher for the ``hunch`` GUI
entry point (spec Stage 2 rename, R2). Earlier versions used the shortcut
"Posture Checker.lnk" and before that the HKCU Run value ``PostureChecker``;
enabling and disabling both remove them so an upgrade never starts the app twice.
No admin rights are needed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from posture_checker.platform.registry import Registry

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: The Run value written before the Startup shortcut replaced it.
RUN_VALUE = "PostureChecker"
#: The Startup shortcut's file name (spec Stage 2 rename, R1).
SHORTCUT_NAME = "Hunch.lnk"
#: The shortcut used before the rename (spec Stage 2 fixes, F2), removed on upgrade.
OLD_SHORTCUT_NAME = "Posture Checker.lnk"
#: uv builds this from [project.gui-scripts] in pyproject.toml.
LAUNCHER_NAME = "hunch.exe"
#: Repository root: three folders above src/posture_checker/platform/. This
#: assumes a source checkout run from the repo's .venv, which is how
#: install.ps1 installs the app; a wheel install would need another folder.
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
            [str(powershell_executable()), "-NoProfile", "-NonInteractive", "-Command", _PS_SCRIPT],
            env=env,
            check=True,
            capture_output=True,
            timeout=WRITER_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


class AutostartError(OSError):
    """The Startup shortcut could not be created."""


def powershell_executable() -> Path:
    """Windows PowerShell by absolute path, so a ``powershell.exe`` on PATH is never used."""
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def describe_failure(error: BaseException) -> str:
    """A short reason for a failed shortcut write, with PowerShell's stderr if any."""
    stderr = getattr(error, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    detail = (stderr or "").strip() or str(error) or type(error).__name__
    return detail


def default_startup_dir() -> Path:
    """The per-user Startup folder (``shell:startup``)."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def pythonw_executable() -> Path:
    """The console-less interpreter next to the one running now."""
    return Path(sys.executable).with_name("pythonw.exe")


def launcher_executable() -> Path:
    """``hunch.exe`` next to the running interpreter (read at call time)."""
    return Path(sys.executable).with_name(LAUNCHER_NAME)


def _remove_old_entries(registry: Registry, startup_dir: Path) -> None:
    """Best effort: each old entry is removed on its own, so one failure never
    blocks the other or the new shortcut."""
    old = startup_dir / OLD_SHORTCUT_NAME
    try:
        old.unlink(missing_ok=True)
    except OSError as error:
        log.warning("could not remove the old shortcut %s: %r", old, error)
    try:
        registry.delete_value(RUN_KEY, RUN_VALUE)
    except FileNotFoundError:
        pass  # never written, or already removed
    except OSError as error:
        log.warning("could not remove the old Run value %s: %r", RUN_VALUE, error)


def enable_autostart(registry: Registry, *, writer: ShortcutWriter, startup_dir: Path) -> None:
    """Write (or overwrite) the one Startup shortcut and drop the old entries.

    Raises ``AutostartError`` if hunch.exe is missing (run ``uv sync``), so a
    shortcut that could never start the app is not written.
    """
    target = launcher_executable()
    if not target.exists():
        raise AutostartError(f"{LAUNCHER_NAME} not found at {target}; run uv sync")
    startup_dir.mkdir(parents=True, exist_ok=True)
    writer.create(
        startup_dir / SHORTCUT_NAME,
        target,
        "",  # the entry point needs no arguments
        REPO_ROOT,
        True,
    )
    _remove_old_entries(registry, startup_dir)


def disable_autostart(registry: Registry, *, startup_dir: Path) -> None:
    """Delete the shortcut and the old entries; no error if any is gone."""
    (startup_dir / SHORTCUT_NAME).unlink(missing_ok=True)
    _remove_old_entries(registry, startup_dir)


def is_autostart_enabled(*, startup_dir: Path) -> bool:
    return (startup_dir / SHORTCUT_NAME).exists()
