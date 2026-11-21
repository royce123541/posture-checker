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

_ARG_WRONG = "wrong"
_ARG_PAUSE = "pause"
_ARG_OPEN = "open"


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

    def message(self, text: str, on_action: Callable[[], None] | None = None) -> None:
        """A plain toast; with ``on_action``, clicking it (or "Open") calls it."""
        from windows_toasts import Toast, ToastActivatedEventArgs, ToastButton

        def activated(_event: ToastActivatedEventArgs) -> None:
            if on_action is not None:
                on_action()

        toast = Toast(
            text_fields=[APP_DISPLAY_NAME, text],
            group="posture-message",
            on_activated=activated if on_action is not None else None,
            actions=(ToastButton("Open", arguments=_ARG_OPEN),) if on_action is not None else (),
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

    def message(self, text: str, on_action: Callable[[], None] | None = None) -> None:
        print(f"\n*** {text}")
