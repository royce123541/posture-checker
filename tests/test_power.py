"""Lock-state probe logic with fakes (no real session or desktop calls)."""

from __future__ import annotations

import logging

import pytest

from posture_checker.platform.power import (
    ERROR_ACCESS_DENIED,
    WTS_SESSIONSTATE_LOCK,
    WTS_SESSIONSTATE_UNLOCK,
    WindowsLockState,
)


def wts_fails() -> int:
    raise OSError(0, "fake failure", None, 1722)


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
    probe = WindowsLockState(FakeUser32(desktop=0), lambda: ERROR_ACCESS_DENIED, wts_fails)
    assert probe.is_locked() is True


def test_other_error_is_unlocked_and_logged_once(caplog) -> None:
    probe = WindowsLockState(FakeUser32(desktop=0), lambda: 87, wts_fails)
    with caplog.at_level(logging.WARNING):
        assert probe.is_locked() is False
        assert probe.is_locked() is False
    assert len([r for r in caplog.records if "OpenInputDesktop" in r.getMessage()]) == 1


def test_open_desktop_that_cannot_be_switched_to_is_locked() -> None:
    user32 = FakeUser32(desktop=42, can_switch=False)
    assert WindowsLockState(user32, lambda: 0, wts_fails).is_locked() is True
    assert user32.closed == 1


def test_normal_desktop_is_unlocked() -> None:
    user32 = FakeUser32(desktop=42, can_switch=True)
    assert WindowsLockState(user32, lambda: 0, wts_fails).is_locked() is False
    assert user32.closed == 1


@pytest.mark.parametrize(
    ("flags", "locked"), [(WTS_SESSIONSTATE_LOCK, True), (WTS_SESSIONSTATE_UNLOCK, False)]
)
def test_session_state_decides_without_touching_desktop(flags: int, locked: bool) -> None:
    # The fallback would say the opposite of the session state if it were used.
    user32 = FakeUser32(desktop=42, can_switch=locked)
    probe = WindowsLockState(user32, lambda: 0, lambda: flags)
    assert probe.is_locked() is locked
    assert user32.closed == 0


def test_session_failure_falls_back_and_logs_once(caplog) -> None:
    user32 = FakeUser32(desktop=0)
    probe = WindowsLockState(user32, lambda: ERROR_ACCESS_DENIED, wts_fails)
    with caplog.at_level(logging.WARNING):
        assert probe.is_locked() is True
        assert probe.is_locked() is True
    messages = [r.getMessage() for r in caplog.records if "session lock" in r.getMessage()]
    assert len(messages) == 1
    assert "1722" in messages[0]
