"""List visible top-level windows with their title and process name.

Used only by the call detector (stage 2 call detection, behaviour C1). Titles
can hold personal text, so they stay in memory and are never logged or stored.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Protocol

__all__ = ["WinWindowLister", "WindowInfo", "WindowLister"]

#: OpenProcess access right that is enough for QueryFullProcessImageNameW and
#: is granted even for most elevated processes (Win32 documentation).
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: Buffer length in characters for a process image path. 1024 covers any
#: realistic install path; a longer one just fails the lookup (no match).
PROCESS_NAME_CHARS = 1024


@dataclass(frozen=True)
class WindowInfo:
    title: str
    #: Executable file name only, for example ``chrome.exe``.
    process: str


class WindowLister(Protocol):
    def windows(self) -> list[WindowInfo]: ...


class WinWindowLister:  # pragma: no cover - needs Windows; camera-marked smoke test
    """Real lister through ``ctypes`` (EnumWindows and friends).

    ``title_filter``, if given, is checked first: windows whose title it rejects
    are skipped without the (more costly) process-name lookup.
    """

    def __init__(self, title_filter: Callable[[str], bool] | None = None) -> None:
        if sys.platform != "win32":
            raise OSError("window listing is only available on Windows")
        from ctypes import wintypes

        self._title_filter = title_filter
        win_dll: Any = getattr(ctypes, "WinDLL")  # noqa: B009 - absent off Windows
        user32 = win_dll("user32", use_last_error=True)
        kernel32 = win_dll("kernel32", use_last_error=True)
        winfunctype: Any = getattr(ctypes, "WINFUNCTYPE")  # noqa: B009 - absent off Windows
        self._enum_proc = winfunctype(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        user32.EnumWindows.argtypes = [self._enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._wintypes = wintypes
        self._user32 = user32
        self._kernel32 = kernel32

    def _process_name(self, hwnd: Any) -> str:
        wintypes = self._wintypes
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(PROCESS_NAME_CHARS)
            size = wintypes.DWORD(PROCESS_NAME_CHARS)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return PureWindowsPath(buf.value).name
        finally:
            self._kernel32.CloseHandle(handle)

    def windows(self) -> list[WindowInfo]:
        found: list[WindowInfo] = []
        failure: list[str] = []

        def visit(hwnd: Any, _lparam: Any) -> bool:
            # An exception escaping a ctypes callback is printed to stderr and
            # swallowed; catch it, keep only its type (never a title) and stop.
            try:
                # Minimised windows are still "visible" here (decision 2).
                if not self._user32.IsWindowVisible(hwnd):
                    return True
                length = self._user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                self._user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if not title:
                    return True
                if self._title_filter is not None and not self._title_filter(title):
                    return True
                found.append(WindowInfo(title, self._process_name(hwnd)))
                return True
            except Exception as error:
                failure.append(type(error).__name__)
                return False

        ok = self._user32.EnumWindows(self._enum_proc(visit), 0)
        if failure:
            raise OSError(f"window enumeration callback failed: {failure[0]}")
        if not ok:
            # EnumWindows also returns 0 when the callback stops it; handled above.
            win_error: Any = getattr(ctypes, "WinError")  # noqa: B009 - absent off Windows
            raise win_error(ctypes.get_last_error())
        return found
