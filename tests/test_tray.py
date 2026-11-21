from __future__ import annotations

from posture_checker import tray
from posture_checker.tray import TrayState, menu_model, tray_status


def by_label(state: TrayState) -> dict[str, tray.MenuItem]:
    return {item.label: item for item in menu_model(state)}


def test_every_leaf_item_has_an_action() -> None:
    def leaves(items):
        for item in items:
            if item.children:
                yield from leaves(item.children)
            else:
                yield item

    actions = [item.action for item in leaves(menu_model(TrayState()))]
    assert all(actions)
    assert len(actions) == len(set(actions))


def test_slouching_submenu_offers_three_labels() -> None:
    children = by_label(TrayState())["I'm slouching now"].children
    assert [c.action for c in children] == [
        tray.LABEL_SLOUCH,
        tray.LABEL_FORWARD_HEAD,
        tray.LABEL_TOO_CLOSE,
    ]


def test_calibrate_disabled_while_calibrating() -> None:
    menu = by_label(TrayState(paused_reasons=("calibrating",)))
    assert menu["Calibrate..."].enabled is False


def test_tooltip_lists_every_reason() -> None:
    state = TrayState(paused_reasons=("camera_in_use:Zoom.exe", "low_battery"))
    assert tray_status(state) == ("grey", "Paused: camera in use by Zoom.exe, low battery")


def test_tooltip_is_truncated_to_windows_limit() -> None:
    reasons = tuple(f"camera_in_use:{'x' * 40}{i}.exe" for i in range(5))
    _colour, tooltip = tray_status(TrayState(paused_reasons=reasons))
    assert len(tooltip) == tray.TOOLTIP_MAX


def test_pause_wins_over_missing_calibration() -> None:
    state = TrayState(paused_reasons=("calibrating",), calibrated=False)
    assert tray_status(state) == ("grey", "Paused: calibrating")


def test_icon_image_is_drawn_in_memory() -> None:
    image = tray.icon_image("amber")
    assert image.size == (64, 64)
    assert image.getpixel((32, 32))[:3] == tray.COLOURS["amber"]


def test_refresh_interval_meets_spec_target() -> None:
    assert tray.REFRESH_S == 2.0


class FakeIcon:
    """Stands in for pystray.Icon; counts updates."""

    def __init__(self) -> None:
        self.icon = None
        self.title = ""
        self.menu = None
        self.menu_updates = 0

    def update_menu(self) -> None:
        self.menu_updates += 1


def test_concurrent_refreshes_are_serialised() -> None:
    import threading

    inside = {"now": 0, "max": 0}
    gate = threading.Lock()

    def get_state() -> TrayState:
        with gate:
            inside["now"] += 1
            inside["max"] = max(inside["max"], inside["now"])
        threading.Event().wait(0.02)
        with gate:
            inside["now"] -= 1
        return TrayState()

    icon = tray.TrayIcon(get_state, {})
    icon._icon = FakeIcon()
    threads = [threading.Thread(target=icon.refresh) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert inside["max"] == 1
    assert icon._icon.menu_updates == 1  # built once; unchanged menus are not rebuilt
