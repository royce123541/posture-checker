"""The system-tray icon.

The menu and the icon's colour and tooltip are computed by pure functions of a
``TrayState`` (``menu_model`` and ``tray_status``), so they are unit-tested
without starting pystray. ``TrayIcon`` is the thin layer that draws them.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from posture_checker import monitor
from posture_checker.policy.pausing import (
    CALIBRATING,
    CAMERA_IN_USE_PREFIX,
    IN_CALL_PREFIX,
    LOCKED,
    LOW_BATTERY,
    MANUAL,
)

log = logging.getLogger(__name__)

GREEN, GREY, AMBER = "green", "grey", "amber"
#: Icon fill colours (RGB) for each status.
COLOURS = {GREEN: (46, 160, 67), GREY: (140, 140, 140), AMBER: (230, 160, 20)}
#: Windows truncates notification-area tooltips to 127 characters.
TOOLTIP_MAX = 127
#: How often the icon re-reads the app state: the spec's target (behaviour 12,
#: update within 2 s of a change), and no more often, to keep polling cheap.
REFRESH_S = 2.0

# Menu action ids, mapped to callbacks by TrayIcon.
PAUSE_15 = "pause_15"
PAUSE_60 = "pause_60"
PAUSE_UNTIL_RESUMED = "pause_until_resumed"
RESUME = "resume"
MARK_WRONG = "mark_wrong"
LABEL_SLOUCH = "label_slouch"
LABEL_FORWARD_HEAD = "label_forward_head"
LABEL_TOO_CLOSE = "label_too_close"
CALIBRATE = "calibrate"
SETTINGS = "settings"
OPEN_DATA = "open_data"
QUIT = "quit"


@dataclass(frozen=True)
class TrayState:
    paused_reasons: tuple[str, ...] = ()
    manual_paused: bool = False
    calibrated: bool = True
    camera_available: bool = True
    has_recent_alert: bool = False
    latest_alert_marked: bool = False
    #: The latest sample found nobody at the desk (Stage 2 fixes, F1).
    away: bool = False
    #: Presence-check mode is on (away for longer than PRESENCE_CHECK_AFTER_S).
    presence_check: bool = False


@dataclass(frozen=True)
class MenuItem:
    label: str
    action: str | None = None
    enabled: bool = True
    children: tuple[MenuItem, ...] = ()


def menu_model(state: TrayState) -> list[MenuItem]:
    """The tray menu for ``state`` (spec behaviour 11)."""
    calibrating = CALIBRATING in state.paused_reasons
    return [
        MenuItem(
            "Pause",
            children=(
                MenuItem("15 min", PAUSE_15),
                MenuItem("1 hour", PAUSE_60),
                MenuItem("Until I resume", PAUSE_UNTIL_RESUMED),
            ),
        ),
        MenuItem("Resume", RESUME, enabled=state.manual_paused),
        MenuItem(
            "Mark latest alert wrong",
            MARK_WRONG,
            enabled=state.has_recent_alert and not state.latest_alert_marked,
        ),
        # pystray items with a submenu cannot also be clicked, so "slouching"
        # is the first submenu entry rather than the parent item itself.
        MenuItem(
            "I'm slouching now",
            children=(
                MenuItem("Slouching", LABEL_SLOUCH),
                MenuItem("Head forward", LABEL_FORWARD_HEAD),
                MenuItem("Too close to the screen", LABEL_TOO_CLOSE),
            ),
        ),
        MenuItem("Calibrate...", CALIBRATE, enabled=not calibrating),
        MenuItem("Settings...", SETTINGS),
        MenuItem("Open data folder", OPEN_DATA),
        MenuItem("Quit", QUIT),
    ]


#: Display names for the in_call:<app> reasons (spec C7).
CALL_APP_NAMES = {"meet": "Google Meet", "zoom": "Zoom", "teams": "Microsoft Teams"}


def describe_reason(reason: str) -> str:
    if reason.startswith(IN_CALL_PREFIX):
        app = reason[len(IN_CALL_PREFIX) :]
        return f"in a call ({CALL_APP_NAMES.get(app, app)})"
    if reason.startswith(CAMERA_IN_USE_PREFIX):
        return "camera in use by " + reason[len(CAMERA_IN_USE_PREFIX) :]
    return {
        MANUAL: "by you",
        LOCKED: "screen locked",
        LOW_BATTERY: "low battery",
        CALIBRATING: "calibrating",
    }.get(reason, reason)


def tray_status(state: TrayState) -> tuple[str, str]:
    """Icon colour and tooltip for ``state`` (spec behaviour 12, decision 3; fixes F1)."""
    if state.paused_reasons:
        text = "Paused: " + ", ".join(describe_reason(r) for r in state.paused_reasons)
        return GREY, text[:TOOLTIP_MAX]
    if not state.calibrated:
        return AMBER, "Posture Checker: calibrate to start"
    if not state.camera_available:
        return AMBER, "Posture Checker: camera unavailable"
    if state.away:
        if state.presence_check:
            # Read at call time so the tooltip never holds a second copy of the number.
            seconds = int(monitor.PRESENCE_CHECK_INTERVAL_S)
            return GREEN, f"Posture Checker: away, checking every {seconds} s"
        return GREEN, "Posture Checker: away"
    return GREEN, "Posture Checker: monitoring"


def icon_image(colour: str) -> Any:
    """A filled circle in the status colour (drawn, never loaded or saved)."""
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((4, 4, size - 4, size - 4), fill=COLOURS[colour])
    return image


class TrayIcon:
    """pystray icon whose menu, colour and tooltip follow ``get_state``.

    ``run`` blocks the calling thread (pystray wants the main thread on
    Windows) until ``stop`` is called.
    """

    def __init__(
        self,
        get_state: Callable[[], TrayState],
        actions: Mapping[str, Callable[[], None]],
    ) -> None:
        self.get_state = get_state
        self.actions = actions
        self._icon: Any = None
        self._stopped = threading.Event()
        self._shown: tuple[str, str] | None = None
        self._shown_menu: list[MenuItem] | None = None
        self._refresh_lock = threading.Lock()

    def _pystray_items(self, items: list[MenuItem]) -> list[Any]:
        import pystray

        result = []
        for item in items:
            if item.children:
                sub = pystray.Menu(*self._pystray_items(list(item.children)))
                result.append(pystray.MenuItem(item.label, sub, enabled=item.enabled))
            else:
                result.append(
                    pystray.MenuItem(item.label, self._callback(item.action), enabled=item.enabled)
                )
        return result

    def _callback(self, action: str | None) -> Callable[[Any, Any], None]:
        def run(_icon: Any, _item: Any) -> None:
            handler = self.actions.get(action or "")
            if handler is None:
                return
            try:
                handler()
            except Exception:
                log.exception("tray action %s failed", action)
            self.refresh()

        return run

    def _menu(self) -> Any:

        return self._pystray_menu(menu_model(self.get_state()))

    def _pystray_menu(self, model: list[MenuItem]) -> Any:
        import pystray

        return pystray.Menu(*self._pystray_items(model))

    def refresh(self) -> None:
        """Redraw the icon, tooltip and menu if the state changed.

        Called by the refresh loop and after every menu action (pystray's
        thread), so it runs under a lock.
        """
        with self._refresh_lock:
            if self._icon is None:
                return
            state = self.get_state()
            status = tray_status(state)
            if status != self._shown:
                self._shown = status
                self._icon.icon = icon_image(status[0])
                self._icon.title = status[1]
            model = menu_model(state)
            if model != self._shown_menu:
                # Rebuild only on change, so an open menu is not replaced every refresh.
                self._shown_menu = model
                self._icon.menu = self._pystray_menu(model)
                self._icon.update_menu()

    def _refresh_loop(self) -> None:
        while not self._stopped.wait(REFRESH_S):
            try:
                self.refresh()
            except Exception:
                log.exception("tray refresh failed")

    def run(self) -> None:
        import pystray

        colour, tooltip = tray_status(self.get_state())
        self._shown = (colour, tooltip)
        self._icon = pystray.Icon("PostureChecker", icon_image(colour), tooltip, self._menu())

        def setup(icon: Any) -> None:
            icon.visible = True
            threading.Thread(target=self._refresh_loop, name="tray-refresh", daemon=True).start()

        self._icon.run(setup=setup)

    def stop(self) -> None:
        self._stopped.set()
        if self._icon is not None:
            self._icon.stop()
