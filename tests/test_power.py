"""Lock-state probe logic with a fake user32 (no real desktop calls)."""

from __future__ import annotations

import logging

from posture_checker.platform.power import ERROR_ACCESS_DENIED, WindowsLockState


class FakeUser32:
    def __init__(self, desktop: int = 0, can_switch: bool = True) -> None:
        self.desktop = desktop
        self.can_switch = can_switch
        self.closed = 0

    def OpenInputDesktop(self, *_args):
        return self.desktop

    def SwitchDesktop(self, _desktop):
        return self.can_switch

    def CloseDesktop(self, _desktop):
        self.closed += 1


def test_access_denied_means_locked() -> None:
    probe = WindowsLockState(FakeUser32(desktop=0), lambda: ERROR_ACCESS_DENIED)
    assert probe.is_locked() is True


def test_other_error_is_unlocked_and_logged_once(caplog) -> None:
    probe = WindowsLockState(FakeUser32(desktop=0), lambda: 87)
    with caplog.at_level(logging.WARNING):
        assert probe.is_locked() is False
        assert probe.is_locked() is False
    assert len([r for r in caplog.records if "OpenInputDesktop" in r.getMessage()]) == 1


def test_open_desktop_that_cannot_be_switched_to_is_locked() -> None:
    user32 = FakeUser32(desktop=42, can_switch=False)
    assert WindowsLockState(user32, lambda: 0).is_locked() is True
    assert user32.closed == 1


def test_normal_desktop_is_unlocked() -> None:
    user32 = FakeUser32(desktop=42, can_switch=True)
    assert WindowsLockState(user32, lambda: 0).is_locked() is False
    assert user32.closed == 1
