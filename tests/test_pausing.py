from __future__ import annotations

from acceptance.conftest import FakeBattery, FakeCameraUsage, FakeLockState
from posture_checker.policy.pausing import (
    CALIBRATING,
    CAMERA_GRACE_S,
    PauseSupervisor,
)


def make(clock, threshold=20):
    usage, lock, battery = FakeCameraUsage(), FakeLockState(), FakeBattery()
    sup = PauseSupervisor(usage, lock, battery, clock=clock, battery_pause_percent=threshold)
    return sup, usage, lock, battery


def test_each_camera_app_has_its_own_grace_period(clock) -> None:
    sup, usage, *_ = make(clock)
    usage.apps = ["Zoom.exe", "chrome.exe"]
    sup.check()
    usage.apps = ["chrome.exe"]  # Zoom stops; Chrome keeps going
    clock.advance(CAMERA_GRACE_S)
    assert sup.check().reasons == ["camera_in_use:chrome.exe"]
    usage.apps = []
    clock.advance(CAMERA_GRACE_S - 1)
    assert sup.check().paused
    clock.advance(1)
    assert not sup.check().paused


def test_app_returning_during_grace_restarts_it(clock) -> None:
    sup, usage, *_ = make(clock)
    usage.apps = ["Zoom.exe"]
    sup.check()
    usage.apps = []
    clock.advance(8)
    usage.apps = ["Zoom.exe"]  # video toggled back on
    sup.check()
    usage.apps = []
    clock.advance(8)
    assert sup.check().paused  # 16 s after first stop, but only 8 s after the last use


def test_add_and_remove_reason(clock) -> None:
    sup, *_ = make(clock)
    sup.add_reason(CALIBRATING)
    assert sup.check().reasons == [CALIBRATING]
    sup.remove_reason(CALIBRATING)
    sup.remove_reason(CALIBRATING)  # removing twice is harmless
    assert not sup.check().paused


def test_reasons_property_reports_last_check_without_probing(clock) -> None:
    sup, _u, lock, _b = make(clock)
    assert sup.reasons == []
    lock.locked = True
    assert sup.reasons == []  # not probed yet
    sup.check()
    assert sup.reasons == ["locked"]


def test_manual_active_reflects_timed_pause(clock) -> None:
    sup, *_ = make(clock)
    assert sup.manual_active is False
    sup.pause_manual(60)
    assert sup.manual_active is True
    clock.advance(61)
    assert sup.manual_active is False


def test_saver_without_battery_never_pauses(clock) -> None:
    sup, _u, _lk, battery = make(clock)
    battery.set(None, False, saver=True)
    assert not sup.check().paused


def test_saver_off_again_uses_threshold_with_hysteresis(clock) -> None:
    sup, _u, _lk, battery = make(clock, threshold=20)
    battery.set(80, False, saver=True)
    assert sup.check().paused
    battery.set(80, False, saver=False)
    assert not sup.check().paused  # well above threshold + hysteresis


def test_threshold_change_takes_effect(clock) -> None:
    sup, _u, _lk, battery = make(clock, threshold=20)
    battery.set(40, False)
    assert not sup.check().paused
    sup.battery_pause_percent = 50
    assert sup.check().reasons == ["low_battery"]
