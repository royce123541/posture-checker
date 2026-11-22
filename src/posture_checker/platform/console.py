"""Hide the console window the background app was given at sign-in (Windows).

In a uv environment ``.venv\\Scripts\\pythonw.exe`` is a launcher that starts
the real interpreter as ``python.exe``, which gets a fresh console window. Live
check L4 showed that window staying open after sign-in. The app hides it, but
only when no other process shares the console: run from a terminal, the
terminal is listed too and is left alone.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any

#: ShowWindow command that hides the window (winuser.h).
SW_HIDE = 0
#: Room for a few process ids; we only need to know whether there is more than one.
PROCESS_LIST_SIZE = 4


def hide_own_console(kernel32: Any = None, user32: Any = None) -> bool:
    """Hide this process's console window if it is the only process using it.

    Returns True when a window was hidden. ``kernel32``/``user32`` are injectable
    for tests.
    """
    if kernel32 is None or user32 is None:
        if sys.platform != "win32":
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32.GetConsoleWindow.argtypes = []
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        kernel32.GetConsoleProcessList.argtypes = [
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
        ]
        kernel32.GetConsoleProcessList.restype = ctypes.c_uint32
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_int
    window = kernel32.GetConsoleWindow()
    if not window:
        return False  # no console at all (true pythonw): nothing to hide
    pids = (ctypes.c_uint32 * PROCESS_LIST_SIZE)()
    count = kernel32.GetConsoleProcessList(pids, PROCESS_LIST_SIZE)
    if count != 1:
        return False  # shared with a terminal (or the call failed): leave it
    user32.ShowWindow(window, SW_HIDE)
    return True
