"""List visible top-level windows with their title and process name.

Used only by the call detector (stage 2 call detection, behaviour C1). Titles
can hold personal text, so they stay in memory and are never logged or stored.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Protocol

__all__ = ["WinWindowLister", "WindowInfo", "WindowLister"]

#: OpenProcess access right that is enough for QueryFullProcessImageNameW and
#: is granted even for most elevated processes (Win32 documentation).
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: Buffer length in characters for a process image path; 32767 is the longest
#: extended-length path Windows allows.
MAX_PATH_CHARS = 32767


@dataclass(frozen=True)
class WindowInfo:
    title: str
    #: Executable file name only, for example ``chrome.exe``.
    process: str


class WindowLister(Protocol):
    def windows(self) -> list[WindowInfo]: ...


class WinWindowLister:  # pragma: no cover - needs Windows; camera-marked smoke test
    """Real lister through ``ctypes`` (EnumWindows and friends)."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("window listing is only available on Windows")
        from ctypes import wintypes

        self._wintypes = wintypes
        windll: Any = getattr(ctypes, "windll")  # noqa: B009 - absent off Windows
        self._user32 = windll.user32
        self._kernel32 = windll.kernel32
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _process_name(self, hwnd: Any) -> str:
        wintypes = self._wintypes
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(MAX_PATH_CHARS)
            size = wintypes.DWORD(MAX_PATH_CHARS)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return PureWindowsPath(buf.value).name
        finally:
            self._kernel32.CloseHandle(handle)

    def windows(self) -> list[WindowInfo]:
        found: list[WindowInfo] = []

        def visit(hwnd: Any, _lparam: Any) -> bool:
            # Minimised windows are still "visible" here (decision 2).
            if not self._user32.IsWindowVisible(hwnd):
                return True
            length = self._user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value:
                found.append(WindowInfo(buf.value, self._process_name(hwnd)))
            return True

        if not self._user32.EnumWindows(self._enum_proc(visit), 0):
            raise ctypes.WinError()
        return found
