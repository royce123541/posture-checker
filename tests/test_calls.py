from __future__ import annotations

import logging

import pytest

from posture_checker.config import Config
from posture_checker.platform.windows_list import WindowInfo
from posture_checker.policy.calls import CallDetector, match_window
from posture_checker.policy.pausing import PauseSupervisor
from posture_checker.tray import describe_reason


class Lister:
    def __init__(self) -> None:
        self.items: list[WindowInfo] = []
        self.error: Exception | None = None

    def windows(self) -> list[WindowInfo]:
        if self.error is not None:
            raise self.error
        return list(self.items)


@pytest.mark.parametrize(
    ("title", "process", "app"),
    [
        ("Meet - abc-defg-hij - Google Chrome", "chrome.exe", "meet"),
        ("Meet - abc-defg-hijk", "chrome.exe", None),
        ("Meet - abc-defg-hij", "firefox.exe", None),
        ("Zoom Meeting", "ZOOM.EXE", "zoom"),
        ("Zoom Meeting 2", "Zoom.exe", None),
        ("Zoom Meeting", "chrome.exe", None),
        ("Call with Ana", "ms-teams.exe", "teams"),
        ("Meeting with Ana | Microsoft Teams", "chrome.exe", None),
        ("Calendar | Microsoft Teams", "Teams.exe", None),
    ],
)
def test_match_window_edges(title: str, process: str, app: str | None) -> None:
    assert match_window(WindowInfo(title, process)) == app


def test_failure_logs_again_after_recovery(caplog: pytest.LogCaptureFixture) -> None:
    lister = Lister()
    detector = CallDetector(lister)
    lister.error = OSError("boom")
    with caplog.at_level(logging.DEBUG):
        detector.apps_in_call()
        lister.error = None
        detector.apps_in_call()
        lister.error = OSError("boom")
        detector.apps_in_call()
    assert len(caplog.records) == 2
    assert all("boom" not in r.getMessage() for r in caplog.records)


class Usage:
    def apps_in_use(self) -> list[str]:
        return []


class Unlocked:
    def is_locked(self) -> bool:
        return False


class Mains:
    def status(self):  # type: ignore[no-untyped-def]
        from posture_checker.policy.pausing import BatteryStatus

        return BatteryStatus(None, True)


def test_turning_setting_off_drops_reason_at_once() -> None:
    now = [0.0]
    lister = Lister()
    lister.items = [WindowInfo("Zoom Meeting", "Zoom.exe")]
    sup = PauseSupervisor(Usage(), Unlocked(), Mains(), clock=lambda: now[0])
    sup.calls = CallDetector(lister)
    assert sup.check().reasons == ["in_call:zoom"]
    sup.pause_in_calls = False
    assert sup.check().reasons == []
    lister.items = []
    sup.pause_in_calls = True
    assert sup.check().reasons == []


def test_unknown_call_app_described_by_name() -> None:
    assert describe_reason("in_call:webex") == "in a call (webex)"


def test_config_rejects_non_bool_setting() -> None:
    with pytest.raises(ValueError):
        Config(pause_in_calls="no").validate()  # type: ignore[arg-type]
