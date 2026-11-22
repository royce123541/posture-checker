"""Screen-lock and battery probes for the pause supervisor (Windows).

Both are polled (the supervisor checks at least every 5 s), which keeps
Win32 message-loop code out of the app; the spec's 10 s targets allow it.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from typing import Any

from posture_checker.policy.pausing import BatteryStatus

log = logging.getLogger(__name__)

#: Access right needed for SwitchDesktop; enough to probe the input desktop.
DESKTOP_SWITCHDESKTOP = 0x0100
#: SYSTEM_POWER_STATUS.SystemStatusFlag value meaning Battery Saver is on.
BATTERY_SAVER_ON = 1
#: Win32 error from OpenInputDesktop while the lock screen (secure desktop) is up.
ERROR_ACCESS_DENIED = 5


#: WTS_INFO_CLASS value WTSSessionInfoEx (wtsapi32.h).
WTS_SESSION_INFO_EX = 25
#: WTS_CURRENT_SERVER_HANDLE and WTS_CURRENT_SESSION (wtsapi32.h): this machine,
#: the session our process runs in.
WTS_CURRENT_SERVER_HANDLE = 0
WTS_CURRENT_SESSION = 0xFFFFFFFF
#: WTSINFOEXW.Level value whose union member is WTSINFOEX_LEVEL1_W.
WTS_INFOEX_LEVEL1 = 1
#: WTSINFOEX_LEVEL1_W.SessionFlags values. Windows 7 / Server 2008 R2 return
#: them inverted (a documented bug); those versions are not supported here.
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1
#: Character counts of the string fields in WTSINFOEX_LEVEL1_W (wtsapi32.h:
#: WINSTATIONNAME_LENGTH + 1, USERNAME_LENGTH + 1, DOMAIN_LENGTH + 1).
_WINSTATION_CHARS = 33
_USERNAME_CHARS = 21
_DOMAIN_CHARS = 18


def _wts_level1() -> type[ctypes.Structure]:
    from ctypes import wintypes

    class Level1(ctypes.Structure):
        _fields_ = (
            ("SessionId", wintypes.ULONG),
            ("SessionState", ctypes.c_int),
            ("SessionFlags", wintypes.LONG),
            ("WinStationName", wintypes.WCHAR * _WINSTATION_CHARS),
            ("UserName", wintypes.WCHAR * _USERNAME_CHARS),
            ("DomainName", wintypes.WCHAR * _DOMAIN_CHARS),
            ("LogonTime", ctypes.c_longlong),
            ("ConnectTime", ctypes.c_longlong),
            ("DisconnectTime", ctypes.c_longlong),
            ("LastInputTime", ctypes.c_longlong),
            ("CurrentTime", ctypes.c_longlong),
            ("IncomingBytes", wintypes.DWORD),
            ("OutgoingBytes", wintypes.DWORD),
            ("IncomingFrames", wintypes.DWORD),
            ("OutgoingFrames", wintypes.DWORD),
            ("IncomingCompressedBytes", wintypes.DWORD),
            ("OutgoingCompressedBytes", wintypes.DWORD),
        )

    return Level1


def wts_session_flags() -> int:  # pragma: no cover - needs Windows
    """SessionFlags of our session from WTSQuerySessionInformationW.

    Raises OSError if the call fails or returns an unexpected level.
    """
    from ctypes import wintypes

    level1 = _wts_level1()

    class InfoEx(ctypes.Structure):
        # The union in WTSINFOEXW has only the Level1 member.
        _fields_ = (("Level", wintypes.DWORD), ("Data", level1))

    win_dll: Any = getattr(ctypes, "WinDLL")  # noqa: B009 - absent off Windows
    wtsapi = win_dll("wtsapi32", use_last_error=True)
    query = wtsapi.WTSQuerySessionInformationW
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = wintypes.BOOL
    free = wtsapi.WTSFreeMemory
    free.argtypes = [ctypes.c_void_p]
    free.restype = None

    buffer = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not query(
        WTS_CURRENT_SERVER_HANDLE,
        WTS_CURRENT_SESSION,
        WTS_SESSION_INFO_EX,
        ctypes.byref(buffer),
        ctypes.byref(size),
    ):
        win_error: Any = getattr(ctypes, "WinError")  # noqa: B009 - absent off Windows
        raise win_error(ctypes.get_last_error())
    try:
        if not buffer.value or size.value < ctypes.sizeof(InfoEx):
            raise OSError(f"WTSSessionInfoEx returned {size.value} bytes")
        info = ctypes.cast(buffer, ctypes.POINTER(InfoEx)).contents
        if info.Level != WTS_INFOEX_LEVEL1:
            raise OSError(f"WTSSessionInfoEx returned level {info.Level}")
        return int(info.Data.SessionFlags)
    finally:
        free(buffer)


class WindowsLockState:
    """Locked when Windows reports our session as locked.

    Primary signal: the documented session state (WTSQuerySessionInformationW,
    WTSSessionInfoEx, SessionFlags). Live check L3 showed the older heuristic
    (the input desktop cannot be opened or switched to) reported "locked" for
    only about 5 s after Win+L on build 26300, so it is now only a fallback
    for when the session query fails.
    """

    def __init__(
        self,
        user32: Any = None,
        get_last_error: Callable[[], int] | None = None,
        session_flags: Callable[[], int] | None = None,
    ) -> None:
        """``user32``, ``get_last_error`` and ``session_flags`` are injectable for tests."""
        self._user32 = user32
        self._get_last_error = get_last_error
        self._session_flags = session_flags or wts_session_flags
        self._reported_error = False
        self._reported_wts_error = False

    def _api(self) -> tuple[Any, Callable[[], int]]:
        if self._user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.OpenInputDesktop.restype = ctypes.c_void_p
            self._user32 = user32
            self._get_last_error = ctypes.get_last_error
        assert self._get_last_error is not None
        return self._user32, self._get_last_error

    def is_locked(self) -> bool:
        if sys.platform != "win32" and self._user32 is None:
            return False
        try:
            flags = self._session_flags()
        except OSError as error:
            if not self._reported_wts_error:
                self._reported_wts_error = True
                log.warning(
                    "session lock query failed (%s, code %s); using the input-desktop check",
                    type(error).__name__,
                    getattr(error, "winerror", None) or error.errno,
                )
            return self._input_desktop_locked()
        self._reported_wts_error = False
        return flags == WTS_SESSIONSTATE_LOCK

    def _input_desktop_locked(self) -> bool:
        user32, get_last_error = self._api()
        desktop = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not desktop:
            error = get_last_error()
            if error == ERROR_ACCESS_DENIED:
                return True  # the secure desktop is showing: locked
            if not self._reported_error:
                self._reported_error = True
                log.warning(
                    "OpenInputDesktop failed with Win32 error %s; treating as unlocked", error
                )
            return False
        try:
            return not user32.SwitchDesktop(ctypes.c_void_p(desktop))
        finally:
            user32.CloseDesktop(ctypes.c_void_p(desktop))


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = (
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    )


def battery_saver_on() -> bool:
    """Windows Battery Saver state from GetSystemPowerStatus (False if unknown)."""
    if sys.platform != "win32":
        return False
    status = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return False
    return bool(status.SystemStatusFlag == BATTERY_SAVER_ON)


class PsutilBattery:
    """Charge and plug state from psutil, Battery Saver from Win32."""

    def status(self) -> BatteryStatus:
        import psutil

        try:
            battery = psutil.sensors_battery()
        except Exception:  # a broken driver must not stop monitoring
            log.exception("battery query failed; treating as no battery")
            battery = None
        if battery is None:
            return BatteryStatus(percent=None, plugged=True)
        return BatteryStatus(
            percent=int(battery.percent),
            plugged=bool(battery.power_plugged),
            saver=battery_saver_on(),
        )
