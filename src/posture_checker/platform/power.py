"""Screen-lock and battery probes for the pause supervisor (Windows).

Both are polled (the supervisor checks at least every 5 s), which keeps
Win32 message-loop code out of the app; the spec's 10 s targets allow it.
"""

from __future__ import annotations

import ctypes
import logging
import sys

from posture_checker.policy.pausing import BatteryStatus

log = logging.getLogger(__name__)

#: Access right needed for SwitchDesktop; enough to probe the input desktop.
DESKTOP_SWITCHDESKTOP = 0x0100
#: SYSTEM_POWER_STATUS.SystemStatusFlag value meaning Battery Saver is on.
BATTERY_SAVER_ON = 1


class WindowsLockState:
    """Locked when the input desktop is the secure (lock / sign-in) desktop.

    While the workstation is locked, our process cannot open or switch to the
    input desktop. This heuristic is widely used but not documented as a lock
    signal; live check L3 confirms it on this machine.
    """

    def is_locked(self) -> bool:
        if sys.platform != "win32":
            return False
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        desktop = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not desktop:
            return True
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
