"""One running app per user, enforced with a named Win32 mutex.

Windows releases the mutex when the owning process exits (even on a crash), so
a stale lock can never block the next start.
"""

from __future__ import annotations

from typing import Any, Protocol

#: "Local\\" scopes the mutex to the current login session, i.e. per user.
MUTEX_NAME = "Local\\PostureChecker.App.SingleInstance"
#: Win32 error code returned when the named mutex already exists.
ERROR_ALREADY_EXISTS = 183


class MutexBackend(Protocol):
    def try_create(self, name: str) -> bool: ...


class Win32MutexBackend:
    """Creates the named mutex and keeps its handle for the process lifetime."""

    def __init__(self) -> None:
        self._handles: list[Any] = []

    def try_create(self, name: str) -> bool:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return False
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        self._handles.append(handle)  # held until the process exits
        return True


class InstanceLock:
    def __init__(self, backend: MutexBackend, name: str = MUTEX_NAME) -> None:
        self.backend = backend
        self.name = name

    def acquire(self) -> bool:
        """True if this is the only instance; False if one is already running."""
        return self.backend.try_create(self.name)
