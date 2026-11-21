from __future__ import annotations

from acceptance.conftest import FakeRegistry
from posture_checker.platform.camera_usage import WEBCAM_KEY, RegistryCameraUsage


def usage(registry, own=(r"C:\venv\Scripts\python.exe",), events=None):
    return RegistryCameraUsage(
        registry, own_executables=own, log_event=(events.append if events is not None else None)
    )


def test_packaged_app_name_drops_publisher_id() -> None:
    reg = FakeRegistry()
    reg.add_app("MSTeams_8wekyb3d8bbwe", 100, 0)
    assert usage(reg).apps_in_use() == ["MSTeams"]


def test_several_apps_sorted_and_deduplicated() -> None:
    reg = FakeRegistry()
    reg.add_app("NonPackaged\\C:#A#Zoom.exe", 1, 0)
    reg.add_app("NonPackaged\\D:#B#Zoom.exe", 1, 0)
    reg.add_app("NonPackaged\\C:#P#chrome.exe", 1, 0)
    assert usage(reg).apps_in_use() == ["Zoom.exe", "chrome.exe"]


def test_own_base_interpreter_is_excluded() -> None:
    # A uv venv's python.exe starts the base interpreter, which the registry records.
    reg = FakeRegistry()
    reg.add_app("NonPackaged\\C:#Python312#pythonw.exe", 1, 0)
    reg.add_app("NonPackaged\\C:#Other#python.exe", 1, 0)
    own = (r"C:\venv\Scripts\python.exe", r"C:\Python312\python.exe")
    assert usage(reg, own=own).apps_in_use() == ["python.exe"]


def test_exclusion_ignores_case() -> None:
    reg = FakeRegistry()
    reg.add_app("NonPackaged\\c:#VENV#scripts#PYTHON.EXE", 1, 0)
    assert usage(reg).apps_in_use() == []


def test_entry_without_timestamps_is_not_in_use() -> None:
    reg = FakeRegistry()
    reg.keys[WEBCAM_KEY] = {"Value": "Allow"}
    reg.keys[WEBCAM_KEY + "\\SomeApp_8wekyb3d8bbwe"] = {"Value": "Allow"}
    assert usage(reg).apps_in_use() == []


def test_unreadable_key_logs_once() -> None:
    class Denied(FakeRegistry):
        def subkeys(self, path):
            raise PermissionError(path)

    events: list[str] = []
    probe = usage(Denied(), events=events)
    for _ in range(3):
        assert probe.apps_in_use() == []
    assert len(events) == 1
