"""Windows integration: native toast notifications with action buttons."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable

from posture_checker.inference import FORWARD_HEAD, SLOUCH, TOO_CLOSE

APP_DISPLAY_NAME = "Posture Checker"
#: Application User Model ID: how Windows attributes our toasts to an app.
AUMID = "PostureChecker.App"
_AUMID_KEY = rf"Software\Classes\AppUserModelId\{AUMID}"

MESSAGES = {
    SLOUCH: ("You're slouching", "Sit back and lift your chest."),
    FORWARD_HEAD: ("Your head is drifting forward", "Bring your ears back over your shoulders."),
    TOO_CLOSE: ("You're too close to the screen", "Move back to arm's length."),
}

#: Windows NotificationSetting values (other than 0, "enabled") and what to tell the user.
BLOCKED_REASONS = {
    1: "notifications from Posture Checker are turned off in Settings > System > Notifications",
    2: "notifications are turned off for your Windows account (Settings > System > Notifications)",
    3: "notifications are disabled by a group policy on this PC",
    4: "Windows rejected the app's notification registration",
}

_ARG_WRONG = "wrong"
_ARG_PAUSE = "pause"


def register_aumid() -> None:
    """Register the app's toast identity for the current user (no admin needed).

    Windows only shows toasts with working buttons for a registered AUMID.
    """
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _AUMID_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_DISPLAY_NAME)


def unregister_aumid() -> None:
    import winreg

    with contextlib.suppress(FileNotFoundError):
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _AUMID_KEY)


class ToastNotifier:
    """Shows a posture alert with "Wrong alert" and "Pause 30 min" buttons.

    Button callbacks only work while this process is alive, which is fine for a
    background app; the tray menu offers the same actions as a fallback.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("ToastNotifier needs Windows")
        from windows_toasts import InteractableWindowsToaster

        register_aumid()
        self._toaster = InteractableWindowsToaster(APP_DISPLAY_NAME, notifierAUMID=AUMID)

    def blocked_reason(self) -> str | None:
        """Why Windows would silently drop our toasts, or None if it will show them.

        Windows gives no error when notifications are off; the toast just never
        appears. Asking up front lets the app say so instead.
        """
        try:
            setting = int(self._toaster.toastNotifier.setting)
        except OSError:
            return None  # cannot tell; assume toasts work
        return BLOCKED_REASONS.get(setting)

    def alert(
        self,
        alert_id: int,
        label: str,
        on_wrong: Callable[[], None],
        on_pause: Callable[[], None],
    ) -> None:
        from windows_toasts import Toast, ToastActivatedEventArgs, ToastButton

        title, advice = MESSAGES.get(label, ("Check your posture", "Sit upright."))

        def activated(event: ToastActivatedEventArgs) -> None:
            if event.arguments == _ARG_WRONG:
                on_wrong()
            elif event.arguments == _ARG_PAUSE:
                on_pause()

        toast = Toast(
            text_fields=[title, advice],
            # One group: a new alert replaces the old one instead of stacking up.
            group="posture-alert",
            on_activated=activated,
            actions=(
                ToastButton("Wrong alert", arguments=_ARG_WRONG),
                ToastButton("Pause 30 min", arguments=_ARG_PAUSE),
            ),
        )
        self._toaster.show_toast(toast)


class ConsoleNotifier:
    """Fallback that prints alerts; used with ``run --no-toast`` and off Windows."""

    def alert(
        self,
        alert_id: int,
        label: str,
        on_wrong: Callable[[], None],
        on_pause: Callable[[], None],
    ) -> None:
        title, advice = MESSAGES.get(label, ("Check your posture", "Sit upright."))
        print(f"\n*** ALERT #{alert_id}: {title}. {advice}")
