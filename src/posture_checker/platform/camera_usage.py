"""Detect other apps using the webcam from the Windows privacy registry.

Windows records webcam use per app under the CapabilityAccessManager
ConsentStore: packaged (Store) apps as direct subkeys, desktop apps under
``NonPackaged`` with ``#`` in place of ``\\`` in their path. An app is using the
camera right now when ``LastUsedTimeStart > 0`` and ``LastUsedTimeStop == 0``.

The key is undocumented and could change in a Windows update. If it cannot be
read, the probe reports "not in use" (logging that once) and the app falls
back to Stage 1's "camera busy" handling.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import PureWindowsPath

from posture_checker.platform.registry import Registry, WinRegistry

__all__ = ["WEBCAM_KEY", "RegistryCameraUsage", "WinRegistry"]

log = logging.getLogger(__name__)

WEBCAM_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"
)
NON_PACKAGED = "NonPackaged"
#: Packaged app keys end in "_" plus a 13-character publisher id
#: (e.g. ``MSTeams_8wekyb3d8bbwe``); it is dropped from the display name.
_PUBLISHER_SUFFIX = re.compile(r"_[0-9a-z]{13}$")


def _default_own_executables() -> tuple[str, ...]:
    # A uv venv's python.exe is a launcher that starts the base interpreter, so
    # the registry may record either path; both are ours.
    return (sys.executable, getattr(sys, "_base_executable", sys.executable))


def _norm(path: str) -> str:
    return os.path.normcase(str(PureWindowsPath(path.replace("/", "\\"))))


def _own_paths(executables: Iterable[str]) -> set[str]:
    """Our executables plus their python.exe / pythonw.exe siblings."""
    own: set[str] = set()
    for exe in executables:
        path = PureWindowsPath(exe.replace("/", "\\"))
        for name in (path.name, "python.exe", "pythonw.exe"):
            own.add(_norm(str(path.with_name(name))))
    return own


#: An entry that has said "in use" for longer than this is treated as stale:
#: an app that crashed, or a machine that hibernated mid-call, never writes
#: LastUsedTimeStop, which would otherwise keep us paused forever. 12 h is far
#: longer than any real call (review finding, Stage 2).
STALE_AFTER_S = 12 * 60 * 60
#: Registry timestamps are FILETIMEs: 100 ns ticks since 1601-01-01 UTC.
FILETIME_TICKS_PER_S = 10_000_000
#: Seconds from 1601-01-01 to the Unix epoch 1970-01-01.
FILETIME_EPOCH_OFFSET_S = 11_644_473_600


def filetime_now() -> int:
    """The current time as a Windows FILETIME."""
    return int((time.time() + FILETIME_EPOCH_OFFSET_S) * FILETIME_TICKS_PER_S)


def _in_use(values: dict[str, object]) -> bool:
    start = values.get("LastUsedTimeStart", 0)
    stop = values.get("LastUsedTimeStop", 0)
    return isinstance(start, int) and isinstance(stop, int) and start > 0 and stop == 0


class RegistryCameraUsage:
    """Camera-in-use probe for the pause supervisor."""

    def __init__(
        self,
        registry: Registry,
        own_executables: Iterable[str] | None = None,
        log_event: Callable[[str], None] | None = None,
        now_filetime: Callable[[], int] | None = None,
    ) -> None:
        """``now_filetime`` turns on the stale-entry check (the app passes
        ``filetime_now``); without it every open entry counts as in use."""
        self.registry = registry
        self._now_filetime = now_filetime
        self._reported_stale: set[str] = set()
        executables = (
            _default_own_executables() if own_executables is None else tuple(own_executables)
        )
        self._own = _own_paths(executables)
        self._log_event = log_event or log.warning
        self._reported_failure = False

    def apps_in_use(self) -> list[str]:
        """Names of other apps using the camera now, e.g. ``["Zoom.exe"]``."""
        try:
            top = self.registry.subkeys(WEBCAM_KEY)
        except OSError as error:
            if not self._reported_failure:
                self._reported_failure = True
                self._log_event(f"camera-use registry key unreadable: {error!r}")
            return []
        apps: set[str] = set()
        for sub in top:
            if sub == NON_PACKAGED:
                apps.update(self._non_packaged())
            elif self._entry_in_use(WEBCAM_KEY + "\\" + sub, sub):
                apps.add(_PUBLISHER_SUFFIX.sub("", sub))
        return sorted(apps)

    def _non_packaged(self) -> list[str]:
        base = WEBCAM_KEY + "\\" + NON_PACKAGED
        try:
            entries = self.registry.subkeys(base)
        except OSError:
            return []
        apps = []
        for entry in entries:
            path = entry.replace("#", "\\")
            if _norm(path) in self._own:
                continue
            if self._entry_in_use(base + "\\" + entry, entry):
                apps.append(PureWindowsPath(path).name)
        return apps

    def _entry_in_use(self, key: str, name: str) -> bool:
        try:
            values = self.registry.values(key)
        except OSError:
            return False
        if not _in_use(values):
            self._reported_stale.discard(name)
            return False
        if self._now_filetime is not None:
            start = values["LastUsedTimeStart"]
            assert isinstance(start, int)
            age_s = (self._now_filetime() - start) / FILETIME_TICKS_PER_S
            if age_s > STALE_AFTER_S:
                if name not in self._reported_stale:
                    self._reported_stale.add(name)
                    self._log_event(
                        f"ignoring stale camera-use entry {name}: "
                        f"in use for {age_s / 3600:.0f} h with no stop time"
                    )
                return False
        return True
