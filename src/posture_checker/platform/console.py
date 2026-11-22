"""Get the background app out of the console window it is given at sign-in.

In a uv environment ``.venv\\Scripts\\pythonw.exe`` is a launcher that starts
the real interpreter as ``python.exe``, which gets a fresh console window. On
Windows 11 that window is hosted by Windows Terminal, which ignores requests to
hide it (live check L4). So when the app finds it is the only process on its
console, it starts itself again with no console at all and exits. The launcher
passes the venv on to its child (``__PYVENV_LAUNCHER__``), so the new process
uses the same environment. Started from a terminal, the terminal shares the
console and nothing is relaunched.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any

#: Room for a few process ids; we only need to know whether there is more than one.
PROCESS_LIST_SIZE = 4
#: CreateProcess flags (winbase.h): a console with no window (which the uv
#: launcher's own child inherits), in its own process group so Ctrl+C events
#: never reach it. Not DETACHED_PROCESS: with no console at all, the launcher's
#: child opens a new visible one (seen while testing L4).
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
#: Set on the relaunched process so it never relaunches again.
RELAUNCHED_ENV = "POSTURE_CHECKER_RELAUNCHED"


def owns_console_alone(kernel32: Any = None) -> bool:
    """True when this process has a console window that no other process shares."""
    if kernel32 is None:
        if sys.platform != "win32":
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleWindow.argtypes = []
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        kernel32.GetConsoleProcessList.argtypes = [
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
        ]
        kernel32.GetConsoleProcessList.restype = ctypes.c_uint32
    if not kernel32.GetConsoleWindow():
        return False  # no console at all: already windowless
    pids = (ctypes.c_uint32 * PROCESS_LIST_SIZE)()
    count = kernel32.GetConsoleProcessList(pids, PROCESS_LIST_SIZE)
    if count == 0 or count > PROCESS_LIST_SIZE:
        return False  # the call failed, or many processes share it
    # Only us and the uv launcher that started us (live check L4 showed the
    # launcher itself holds the console); a terminal would be another process.
    return set(pids[:count]) <= {os.getpid(), os.getppid()}


def relaunch_without_console(
    args: Sequence[str],
    kernel32: Any = None,
    spawn: Callable[..., Any] = subprocess.Popen,
) -> bool:
    """Start ``python -m posture_checker <args>`` with no console if we own one alone.

    Returns True when a new process was started; the caller should then exit.
    """
    if os.environ.get(RELAUNCHED_ENV) or not owns_console_alone(kernel32):
        return False
    # sys.executable is the venv's launcher, which starts its own child again;
    # the marker stops that child from relaunching in turn (an endless loop
    # seen while testing L4).
    spawn(
        [sys.executable, "-m", "posture_checker", *args],
        env={**os.environ, RELAUNCHED_ENV: "1"},
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return True
