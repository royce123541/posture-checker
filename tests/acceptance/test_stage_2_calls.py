"""Stage 2 call-detection acceptance tests (docs/specs/stage-2-call-detection.md, C1-C8).

Interfaces the implementer must build (imported lazily inside the tests):

posture_checker.platform.windows_list
    WindowInfo(title: str, process: str)
    WindowLister protocol: windows() -> list[WindowInfo]
    WinWindowLister()  real ctypes version (camera-marked smoke test only)
posture_checker.policy.calls
    CallDetector(lister).apps_in_call() -> list[str]: sorted, de-duplicated
    subset of meet / zoom / teams. A raising lister gives [] and exactly one
    log record until the lister works again. Titles are never logged.
posture_checker.policy.pausing.PauseSupervisor
    new optional kwargs ``calls`` (probe with apps_in_call()) and
    ``pause_in_calls: bool = True``; ``pause_in_calls`` is also a plain
    settable attribute. Adds reason "in_call:<app>" with the 10 s camera grace.
    When pause_in_calls is False the probe is never called.
posture_checker.config.Config
    new field ``pause_in_calls: bool = True``.
posture_checker.settings_ui
    SettingsController.save accepts the key ``pause_in_calls``.
    ConfigWatcher.poll() copies Config.pause_in_calls onto
    ``monitor.supervisor.pause_in_calls``.
posture_checker.tray
    tray_status: "in_call:meet" -> "Paused: in a call (Google Meet)",
    "in_call:zoom" -> "(Zoom)", "in_call:teams" -> "(Microsoft Teams)"; grey icon.

Shared fakes (tests/acceptance/conftest.py): FakeWindowLister, ``lister`` and
``make_call_rig`` fixtures; Rig accepts ``calls=``.
"""

from __future__ import annotations

import logging
from importlib import import_module

import pytest

from acceptance.conftest import FakeBattery, FakeCameraUsage, FakeLockState
from posture_checker import monitor as mon
from posture_checker import paths
from posture_checker.config import Config, load_config, save_config

MARKER = "SECRET-MARKER-9f3c"


def calls_mod():
    return import_module("posture_checker.policy.calls")


def make_supervisor(clock, lister):
    pausing = import_module("posture_checker.policy.pausing")
    return pausing.PauseSupervisor(
        camera_usage=FakeCameraUsage(),
        lock_state=FakeLockState(),
        battery=FakeBattery(),
        clock=clock,
        calls=calls_mod().CallDetector(lister),
    )


# --- C1 -----------------------------------------------------------------------
def test_window_lister_protocol_and_fake_expose_window_info(lister):
    """Behaviour C1: WindowInfo has title and process; the fake yields WindowInfo."""
    wl = import_module("posture_checker.platform.windows_list")
    lister.add("Some title", "chrome.exe")
    (info,) = lister.windows()
    assert isinstance(info, wl.WindowInfo)
    assert (info.title, info.process) == ("Some title", "chrome.exe")
    assert hasattr(wl, "WindowLister")


@pytest.mark.camera
def test_real_windows_lister_returns_window_infos():
    """Behaviour C1: smoke test of the real Windows lister (hardware/Windows only)."""
    wl = import_module("posture_checker.platform.windows_list")
    result = wl.WinWindowLister().windows()
    assert isinstance(result, list)
    assert all(isinstance(w, wl.WindowInfo) for w in result)


# --- C2 -----------------------------------------------------------------------
CASES = [
    ([("Meet - abc-defg-hij", "chrome.exe")], ["meet"]),
    ([("Meet - abc-defg-hij", "msedge.exe")], ["meet"]),
    ([("Meet - abc-defg-hij", "CHROME.EXE")], ["meet"]),
    ([("Google Meet", "chrome.exe")], []),
    ([("Meet - abc-defg-hij", "notepad.exe")], []),
    ([("Meet - ab-defg-hij", "chrome.exe")], []),
    ([("Meet - ABC-defg-hij", "chrome.exe")], []),
    ([("Zoom Meeting", "Zoom.exe")], ["zoom"]),
    ([("Zoom Webinar", "Zoom.exe")], ["zoom"]),
    ([("Zoom Workplace", "Zoom.exe")], []),
    ([("Zoom", "Zoom.exe")], []),
    ([("Meeting with Ana | Microsoft Teams", "ms-teams.exe")], ["teams"]),
    ([("Call with Ana | Microsoft Teams", "Teams.exe")], ["teams"]),
    ([("Meeting in General | Microsoft Teams", "ms-teams.exe")], ["teams"]),
    ([("Chat | Microsoft Teams", "ms-teams.exe")], []),
    ([("Meet - abc-defg-hij", "chrome.exe"), ("Meet - xyz-wxyz-abc", "msedge.exe")], ["meet"]),
    ([("Zoom Meeting", "Zoom.exe"), ("Meet - abc-defg-hij", "chrome.exe")], ["meet", "zoom"]),
    ([], []),
]


@pytest.mark.parametrize(("windows", "expected"), CASES)
def test_call_detector_table(lister, windows, expected):
    """Behaviour C2: titles and processes in, sorted de-duplicated app names out."""
    for title, process in windows:
        lister.add(title, process)
    assert calls_mod().CallDetector(lister).apps_in_call() == expected


# --- C3 -----------------------------------------------------------------------
def test_failing_lister_returns_empty_and_logs_once(lister, caplog):
    """Behaviour C3: a raising lister gives [] and one log record over two calls."""
    lister.error = OSError("enum failed")
    detector = calls_mod().CallDetector(lister)
    with caplog.at_level(logging.DEBUG):
        assert detector.apps_in_call() == []
        assert detector.apps_in_call() == []
    assert len(caplog.records) == 1


def test_titles_never_reach_logs_or_database(make_call_rig, lister, db, caplog):
    """Behaviour C3: a title marker is in no log record and no database row."""
    lister.add(f"Zoom Meeting {MARKER}", "Zoom.exe")  # near miss, must not match
    lister.add(f"Notes {MARKER}", "notepad.exe")
    lister.add("Meet - abc-defg-hij", "chrome.exe")  # matches
    rig = make_call_rig()
    with caplog.at_level(logging.DEBUG):
        rig.run_for(15)
        lister.error = OSError("listing failed")
        rig.run_for(15)
        lister.error = None
        lister.clear()
        lister.add("Zoom Meeting", "Zoom.exe")
        lister.add(f"Other {MARKER}", "Zoom.exe")
        rig.run_for(15)
    for record in caplog.records:
        assert MARKER not in record.getMessage()
        assert MARKER not in str(record.exc_info)
    for table in ("events", "samples"):
        for row in db.query(f"SELECT * FROM {table}"):
            assert MARKER not in " ".join(str(v) for v in dict(row).values())
    details = [r["detail"] for r in db.query("SELECT detail FROM events WHERE kind = 'pause'")]
    assert "in_call:meet" in details and "in_call:zoom" in details


# --- C4 / C5 ------------------------------------------------------------------
def test_meet_window_pauses_and_resumes_after_grace(clock, lister):
    """Behaviour C4/C5: reason in_call:meet; still paused at +9 s, running at +10 s."""
    sup = make_supervisor(clock, lister)
    assert not sup.check().paused
    lister.add("Meet - abc-defg-hij", "chrome.exe")
    decision = sup.check()
    assert decision.paused and "in_call:meet" in decision.reasons
    lister.clear()
    clock.advance(9)
    assert "in_call:meet" in sup.check().reasons
    clock.advance(1)
    assert not sup.check().paused


def test_title_flicker_inside_grace_does_not_unpause(clock, lister):
    """Behaviour C4: a window briefly losing its title stays paused."""
    sup = make_supervisor(clock, lister)
    lister.add("Meet - abc-defg-hij", "chrome.exe")
    sup.check()
    lister.clear()
    clock.advance(5)
    assert sup.check().paused
    lister.add("Meet - abc-defg-hij", "chrome.exe")
    clock.advance(5)
    assert sup.check().paused
    lister.clear()
    clock.advance(9)
    assert sup.check().paused


def test_failing_calls_probe_counts_as_no_call(clock):
    """Behaviour C4: a probe that raises is treated as no call."""
    pausing = import_module("posture_checker.policy.pausing")

    class Broken:
        def apps_in_call(self):
            raise RuntimeError("nope")

    sup = pausing.PauseSupervisor(
        camera_usage=FakeCameraUsage(),
        lock_state=FakeLockState(),
        battery=FakeBattery(),
        clock=clock,
        calls=Broken(),
    )
    assert not sup.check().paused


def test_monitor_closes_camera_and_stays_closed_during_call(make_call_rig, lister):
    """Behaviour C4/C5: paused within 10 s of the window, camera closed, never reopened."""
    rig = make_call_rig(camera_mode="hold")
    rig.run_for(3)
    assert rig.camera.is_open
    lister.add("Meet - abc-defg-hij", "chrome.exe")
    states = [rig.tick(1).state for _ in range(10)]
    assert mon.PAUSED in states
    assert rig.camera.is_open is False
    opened = rig.camera.opened
    for _ in range(5):
        assert rig.tick(5).state == mon.PAUSED
    assert rig.camera.opened == opened
    lister.clear()
    states = [rig.tick(1).state for _ in range(9)]
    assert set(states) == {mon.PAUSED}
    states += [rig.tick(1).state for _ in range(6)]
    assert states[-1] == mon.PRESENT


# --- C6 -----------------------------------------------------------------------
def test_camera_and_call_reasons_coexist_with_own_event_rows(make_call_rig, lister, db):
    """Behaviour C6: both reasons listed; one pause and one resume row for each."""
    rig = make_call_rig()
    rig.run_for(3)
    rig.usage.apps = ["Zoom.exe"]
    lister.add("Zoom Meeting", "Zoom.exe")
    rig.run_for(3)
    reasons = rig.supervisor.check().reasons
    assert "camera_in_use:Zoom.exe" in reasons and "in_call:zoom" in reasons
    rig.usage.apps = []
    lister.clear()
    rig.run_for(20)
    assert not rig.supervisor.check().paused
    rows = db.query("SELECT kind, detail FROM events WHERE kind IN ('pause','resume')")
    got = sorted((r["kind"], r["detail"]) for r in rows)
    assert got == sorted(
        [
            ("pause", "camera_in_use:Zoom.exe"),
            ("resume", "camera_in_use:Zoom.exe"),
            ("pause", "in_call:zoom"),
            ("resume", "in_call:zoom"),
        ]
    )


# --- C7 -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("app", "name"),
    [("meet", "Google Meet"), ("zoom", "Zoom"), ("teams", "Microsoft Teams")],
)
def test_tray_tooltip_for_each_call_app(app, name):
    """Behaviour C7: exact tooltip text and a grey icon."""
    tray = import_module("posture_checker.tray")
    colour, tooltip = tray.tray_status(tray.TrayState(paused_reasons=(f"in_call:{app}",)))
    assert tooltip == f"Paused: in a call ({name})"
    assert colour == "grey"


# --- C8 -----------------------------------------------------------------------
def test_pause_in_calls_defaults_to_true():
    """Behaviour C8: the setting defaults to on."""
    assert Config().pause_in_calls is True


def test_pause_in_calls_false_never_calls_probe(make_call_rig, lister):
    """Behaviour C8: with the setting off the lister is never called and nothing pauses."""
    rig = make_call_rig(pause_in_calls=False)
    lister.add("Meet - abc-defg-hij", "chrome.exe")
    for _ in range(5):
        assert rig.tick(5).state != mon.PAUSED
        assert not any(r.startswith("in_call:") for r in rig.supervisor.check().reasons)
    assert lister.calls == 0


def test_settings_controller_saves_pause_in_calls(registry):
    """Behaviour C8: saving the setting writes it to the isolated config path."""
    ui = import_module("posture_checker.settings_ui")
    save_config(Config())
    controller = ui.SettingsController(paths.config_path(), registry)
    assert controller.save({"pause_in_calls": False}) is None
    assert load_config().pause_in_calls is False
    assert controller.save({"pause_in_calls": True}) is None
    assert load_config().pause_in_calls is True


def test_config_watcher_applies_pause_in_calls_within_5s(make_call_rig, lister, registry):
    """Behaviour C8: the running app picks the change up within 5 s."""
    ui = import_module("posture_checker.settings_ui")
    rig = make_call_rig()
    save_config(Config())
    watcher = ui.ConfigWatcher(paths.config_path(), rig.monitor, rig.clock)
    watcher.poll()
    ui.SettingsController(paths.config_path(), registry).save({"pause_in_calls": False})
    rig.clock.advance(5)
    watcher.poll()
    lister.add("Meet - abc-defg-hij", "chrome.exe")
    calls_before = lister.calls
    rig.run_for(15)
    assert lister.calls == calls_before
    assert not any(r.startswith("in_call:") for r in rig.supervisor.check().reasons)
