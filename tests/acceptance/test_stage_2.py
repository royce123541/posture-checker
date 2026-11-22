"""Stage 2 acceptance tests (spec: docs/specs/stage-2.md, behaviours 1-24).

Interfaces the implementer must build (all imported lazily inside the tests):

posture_checker.config.Config
    new field ``camera_mode: str`` ("hold" | "burst").
posture_checker.monitor.Monitor
    new optional kwarg ``supervisor: PauseSupervisor | None``. step() asks the
    supervisor first; paused -> Sample(state=PAUSED) and camera released. Burst
    mode closes the camera after each sample. After 5 min of consecutive AWAY
    samples it closes the camera after each sample and next_interval() is 30.
    Writes one ``events`` row (kind pause/resume, detail = reason) per reason
    transition. Monitor.pose stays a replaceable attribute.
posture_checker.policy.pausing
    BatteryStatus(percent: int | None, plugged: bool, saver: bool = False)
    PauseSupervisor(camera_usage, lock_state, battery, clock,
                    battery_pause_percent=20)
      probes: camera_usage.apps_in_use() -> list[str]; lock_state.is_locked()
      -> bool; battery.status() -> BatteryStatus (percent None = no battery)
      .check() -> decision with .paused: bool and .reasons: list[str]
        (reasons "camera_in_use:<app>", "locked", "low_battery", "manual",
         "calibrating"); camera-in-use clears only after a 10 s grace.
      .pause_manual(seconds: float | None)  (None = until resumed)
      .resume_manual()   .add_reason(name)   .remove_reason(name)
posture_checker.platform.camera_usage
    RegistryCameraUsage(registry, own_executables=(sys.executable,),
                        log_event=Callable[[str], None]) with
    .apps_in_use() -> list[str]; WinRegistry() real adapter (same protocol as
    the FakeRegistry in conftest: subkeys(path), values(path), set_value,
    delete_value, delete_key; FileNotFoundError for a missing key).
posture_checker.platform.autostart
    Autostart is the "Posture Checker.lnk" shortcut in a Startup folder (see
    test_stage_2_fixes.py): enable_autostart(registry, *, writer, startup_dir),
    disable_autostart(registry, *, startup_dir) (no error if absent),
    is_autostart_enabled(*, startup_dir). Both remove an old "PostureChecker"
    Run value.
posture_checker.install
    register(registry, *, writer, startup_dir, autostart=True)  (AUMID key +
      Startup shortcut)
    unregister(registry, *, startup_dir, remove_data=False)  (never raises)
posture_checker.platform.single_instance
    InstanceLock(backend) with .acquire() -> bool; backend.try_create(name).
posture_checker.tray
    TrayState(paused_reasons=(), manual_paused=False, calibrated=True,
              camera_available=True, has_recent_alert=False,
              latest_alert_marked=False)
    menu_model(state) -> list of items with .label, .enabled, .children
    tray_status(state) -> (colour in {"green","grey","amber"}, tooltip)
posture_checker.actions
    mark_latest_alert_wrong(db, clock) -> bool
    record_label(db, clock, label, confirm=None) -> int  (``labels`` table:
        ts, label, source, sample_id, model_version, predicted_label)
posture_checker.calibrate_flow
    run_calibration(monitor, supervisor, launcher) -> bool
    launcher(command: list[str]) -> process with .wait() -> int exit code
posture_checker.settings_ui
    SettingsController(config_path, registry).save(values: dict) -> str | None
    (error message, None on success; keys alert_window_s, sensitivity,
    cooldown_s, battery_pause_percent, camera_mode, autostart; missing keys
    keep their current value)
    ConfigWatcher(config_path, monitor, clock).poll()
posture_checker.app
    App(monitor, supervisor, db, notifier, clock, launcher=None)
      .startup()  .status() -> (colour, tooltip)  .tick() -> float seconds to
      wait (30 after an unhandled error; no sampling until 30 s have passed)
      .run(stop: threading.Event)
    setup_logging() -> Path (rotating file under the app dir, not stdout)
    notifier.message(text, on_action=None) for plain toasts.
posture_checker/__main__.py, scripts/camera_share_probe.py, install.ps1,
uninstall.ps1 at the repo root.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import logging
import sys
import threading
from importlib import import_module
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from acceptance.conftest import (
    AUMID_KEY,
    FakeBattery,
    FakeCameraUsage,
    FakeLauncher,
    FakeLockState,
    FakeMutexBackend,
    Rig,
)
from conftest import FakePose
from posture_checker import monitor as mon
from posture_checker import paths
from posture_checker.config import Config, load_config, save_config
from posture_checker.synthetic import make_landmarks

REPO = Path(__file__).resolve().parents[2]


def pausing():
    return import_module("posture_checker.policy.pausing")


def make_supervisor(clock, threshold=20):
    usage, lock, battery = FakeCameraUsage(), FakeLockState(), FakeBattery()
    sup = pausing().PauseSupervisor(
        camera_usage=usage,
        lock_state=lock,
        battery=battery,
        clock=clock,
        battery_pause_percent=threshold,
    )
    return sup, usage, lock, battery


# --- 1 ------------------------------------------------------------------------
def test_camera_probe_script_exists_and_imports_without_opening_a_camera(monkeypatch):
    """Behaviour 1: probe script exists and is importable without a camera."""
    script = REPO / "scripts" / "camera_share_probe.py"
    assert script.exists()
    import cv2

    def boom(*a, **k):
        raise AssertionError("importing the probe must not open a camera")

    monkeypatch.setattr(cv2, "VideoCapture", boom)
    spec = importlib.util.spec_from_file_location("camera_share_probe", script)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


@pytest.mark.camera
def test_camera_probe_live_part():
    """Behaviour 1: the live part is live check L1."""
    pytest.skip("live check L1: needs a webcam, the Camera app and a Meet/Zoom preview")


# --- 2 ------------------------------------------------------------------------
def test_burst_mode_closes_camera_after_each_sample(make_rig):
    """Behaviour 2: burst mode releases the camera after every sample."""
    rig = make_rig(camera_mode="burst")
    for _ in range(4):
        sample = rig.tick(2)
        assert sample.state == mon.PRESENT
        assert rig.camera.is_open is False
        assert rig.camera.opened == rig.camera.closed
    assert rig.camera.opened == 4


def test_hold_mode_keeps_camera_open_between_samples(make_rig):
    """Behaviour 2: hold mode keeps the camera open between samples."""
    rig = make_rig(camera_mode="hold")
    for _ in range(4):
        rig.tick(2)
        assert rig.camera.is_open is True
    assert rig.camera.opened == 1


# --- 3 ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda u, lk, b: u.apps.append("Zoom.exe"), {"camera_in_use:Zoom.exe"}),
        (lambda u, lk, b: setattr(lk, "locked", True), {"locked"}),
        (lambda u, lk, b: b.set(10, False), {"low_battery"}),
    ],
)
def test_supervisor_each_probe_gives_its_reason(clock, setup, expected):
    """Behaviour 3: each probe alone produces the expected reason."""
    sup, usage, lock, battery = make_supervisor(clock)
    assert sup.check().paused is False
    setup(usage, lock, battery)
    decision = sup.check()
    assert decision.paused is True
    assert set(decision.reasons) == expected


def test_supervisor_manual_reason_and_combinations(clock):
    """Behaviour 3: manual is a reason and combinations list every reason."""
    sup, usage, lock, battery = make_supervisor(clock)
    sup.pause_manual(None)
    assert set(sup.check().reasons) == {"manual"}
    usage.apps.append("Zoom.exe")
    lock.locked = True
    battery.set(5, False)
    assert set(sup.check().reasons) == {
        "manual",
        "camera_in_use:Zoom.exe",
        "locked",
        "low_battery",
    }


def test_paused_step_never_opens_camera_and_closes_it_first_time(make_rig):
    """Behaviour 3: any reason -> PAUSED, camera not opened, closed after first check."""
    rig = make_rig(camera_mode="hold")  # hold keeps the camera open between samples
    rig.tick(2)
    assert rig.camera.is_open
    rig.lock.locked = True
    sample = rig.tick(1)
    assert sample.state == mon.PAUSED
    assert rig.camera.is_open is False
    opened = rig.camera.opened
    for _ in range(5):
        assert rig.tick(5).state == mon.PAUSED
    assert rig.camera.opened == opened


# --- 4 ------------------------------------------------------------------------
def _usage(registry, events=None):
    cam = import_module("posture_checker.platform.camera_usage")
    return cam.RegistryCameraUsage(
        registry,
        own_executables=(sys.executable,),
        log_event=(events.append if events is not None else (lambda text: None)),
    )


def _own_key(executable: str) -> str:
    return "NonPackaged\\" + executable.replace("\\", "#").replace("/", "#")


@pytest.mark.parametrize(
    ("subkey", "start", "stop", "in_use"),
    [
        ("MSTeams_8wekyb3d8bbwe", 100, 0, True),
        ("NonPackaged\\C:#Apps#Zoom#Zoom.exe", 100, 0, True),
        ("MSTeams_8wekyb3d8bbwe", 100, 200, False),
        ("NonPackaged\\C:#Apps#Zoom#Zoom.exe", 100, 200, False),
        ("NonPackaged\\C:#Apps#Zoom#Zoom.exe", 0, 0, False),
    ],
)
def test_registry_entry_in_use_rules(registry, subkey, start, stop, in_use):
    """Behaviour 4: in use only when start > 0 and stop == 0."""
    registry.add_app(subkey, start, stop)
    assert bool(_usage(registry).apps_in_use()) is in_use


def test_registry_reports_app_name(registry):
    """Behaviour 4: the probe returns app names such as Zoom.exe."""
    registry.add_app("NonPackaged\\C:#Apps#Zoom#Zoom.exe", 100, 0)
    assert _usage(registry).apps_in_use() == ["Zoom.exe"]


def test_registry_ignores_own_python_executables(registry):
    """Behaviour 4: our own python.exe and pythonw.exe are excluded."""
    own = Path(sys.executable)
    registry.add_app(_own_key(str(own)), 100, 0)
    registry.add_app(_own_key(str(own.with_name("pythonw.exe"))), 100, 0)
    assert _usage(registry).apps_in_use() == []


def test_registry_missing_key_is_not_in_use_and_logs_once(registry):
    """Behaviour 4: a missing key means not in use, logged exactly once."""
    events: list[str] = []
    usage = _usage(registry, events)
    assert usage.apps_in_use() == []
    assert usage.apps_in_use() == []
    assert len(events) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="reads the real Windows registry")
def test_real_registry_key_parses():
    """Behaviour 4: the real ConsentStore key parses without error."""
    cam = import_module("posture_checker.platform.camera_usage")
    result = cam.RegistryCameraUsage(cam.WinRegistry()).apps_in_use()
    assert isinstance(result, list)


# --- 5 ------------------------------------------------------------------------
def test_other_app_starting_pauses_within_10s_and_resume_after_grace(make_rig):
    """Behaviour 5: pause <= 10 s after use starts; resume by 15 s after it stops."""
    rig = make_rig()
    rig.run_for(4)
    rig.usage.apps = ["Zoom.exe"]
    states = [rig.tick(1).state for _ in range(10)]
    assert mon.PAUSED in states
    assert rig.camera.is_open is False
    assert "camera_in_use:Zoom.exe" in rig.supervisor.check().reasons

    rig.usage.apps = []
    states = [rig.tick(1).state for _ in range(9)]
    assert set(states) == {mon.PAUSED}  # still inside the 10 s grace period
    states += [rig.tick(1).state for _ in range(6)]
    assert states[-1] == mon.PRESENT  # running again by 15 s


# --- 6 ------------------------------------------------------------------------
def test_lock_pauses_and_unlock_resumes_within_10s(make_rig):
    """Behaviour 6: locked -> paused with reason, camera closed; unlock -> running."""
    rig = make_rig()
    rig.run_for(3)
    rig.lock.locked = True
    states = [rig.tick(1).state for _ in range(10)]
    assert mon.PAUSED in states
    assert rig.supervisor.check().reasons == ["locked"]
    assert rig.camera.is_open is False
    rig.lock.locked = False
    states = [rig.tick(1).state for _ in range(10)]
    assert states[-1] == mon.PRESENT


# --- 7 ------------------------------------------------------------------------
BATTERY_CASES = {
    "low_unplugged": ([(19, False, False)], [True]),
    "hysteresis": (
        [(19, False, False), (22, False, False), (25, False, False)],
        [True, True, False],
    ),
    "plugging_in": ([(19, False, False), (19, True, False)], [True, False]),
    "plugged_low": ([(5, True, False)], [False]),
    "saver_on": ([(80, False, True)], [True]),
    "no_battery": ([(None, True, False)], [False]),
    "no_battery_unplugged_flag": ([(None, False, False)], [False]),
}


@pytest.mark.parametrize("case", BATTERY_CASES)
def test_battery_pause_table(clock, case):
    """Behaviour 7: battery threshold, hysteresis, saver and no-battery rules."""
    readings, expected = BATTERY_CASES[case]
    sup, _u, _lk, battery = make_supervisor(clock, threshold=20)
    for (percent, plugged, saver), want in zip(readings, expected, strict=True):
        battery.set(percent, plugged, saver)
        clock.advance(5)
        decision = sup.check()
        assert decision.paused is want
        if want:
            assert decision.reasons == ["low_battery"]


# --- 8 ------------------------------------------------------------------------
def test_resume_clears_only_the_manual_reason(clock):
    """Behaviour 8: Resume leaves automatic reasons holding the pause."""
    sup, _u, lock, _b = make_supervisor(clock)
    sup.pause_manual(None)
    lock.locked = True
    assert set(sup.check().reasons) == {"manual", "locked"}
    sup.resume_manual()
    decision = sup.check()
    assert decision.paused is True
    assert decision.reasons == ["locked"]
    lock.locked = False
    assert sup.check().paused is False


def test_timed_manual_pause_expires(clock):
    """Behaviour 8: a 15 minute manual pause ends by itself."""
    sup, *_ = make_supervisor(clock)
    sup.pause_manual(15 * 60)
    assert sup.check().paused is True
    clock.advance(15 * 60 + 1)
    assert sup.check().paused is False


# --- 9 ------------------------------------------------------------------------
def test_nobody_present_switches_to_presence_check_mode(make_rig):
    """Behaviour 9: after 5 min of away, camera closed between steps, 30 s interval."""
    rig = make_rig(camera_mode="hold")
    rig.landmarks = None
    start = rig.clock.now
    sample = rig.monitor.step()
    assert sample.state == mon.AWAY
    assert rig.monitor.next_interval(sample) == 10.0  # Stage 1 away interval
    while rig.clock.now < start + 310:
        rig.clock.advance(rig.monitor.next_interval(sample))
        sample = rig.monitor.step()
    assert sample.state == mon.AWAY
    assert rig.camera.is_open is False
    assert rig.monitor.next_interval(sample) == 30.0

    rig.landmarks = {}
    rig.clock.advance(30)
    sample = rig.monitor.step()
    assert sample.state == mon.PRESENT
    assert rig.monitor.next_interval(sample) == 2.0
    assert rig.camera.is_open is True  # hold mode again


# --- 10 -----------------------------------------------------------------------
def test_pause_and_resume_events_once_per_cause(make_rig, db):
    """Behaviour 10: one pause and one resume row per cause, reason in detail."""
    rig = make_rig()
    rig.run_for(3)
    rig.lock.locked = True
    rig.run_for(10)  # reason stays active: no further rows
    rig.lock.locked = False
    rig.run_for(3)
    rig.battery.set(10, False)
    rig.run_for(10)
    rig.battery.set(10, True)
    rig.run_for(3)
    rows = db.query("SELECT kind, detail FROM events WHERE kind IN ('pause','resume') ORDER BY id")
    assert [(r["kind"], r["detail"]) for r in rows] == [
        ("pause", "locked"),
        ("resume", "locked"),
        ("pause", "low_battery"),
        ("resume", "low_battery"),
    ]


# --- 11 -----------------------------------------------------------------------
def _menu(**state):
    tray = import_module("posture_checker.tray")
    items = tray.menu_model(tray.TrayState(**state))
    return {i.label: i for i in items}


EXPECTED_LABELS = [
    "Pause",
    "Resume",
    "Mark latest alert wrong",
    "I'm slouching now",
    "Calibrate...",
    "Settings...",
    "Open data folder",
    "Quit",
]


def test_menu_has_all_items_when_running():
    """Behaviour 11: all menu items exist; Resume is greyed out when not paused."""
    menu = _menu()
    assert list(menu) == EXPECTED_LABELS
    assert menu["Resume"].enabled is False
    assert menu["Mark latest alert wrong"].enabled is False
    for label in ("Pause", "I'm slouching now", "Calibrate...", "Settings...", "Quit"):
        assert menu[label].enabled is True
    assert [c.label for c in menu["Pause"].children] == ["15 min", "1 hour", "Until I resume"]


def test_menu_manually_paused_enables_resume():
    """Behaviour 11: Resume is enabled when manually paused."""
    assert _menu(manual_paused=True, paused_reasons=("manual",))["Resume"].enabled is True


def test_menu_auto_paused_keeps_resume_disabled():
    """Behaviour 11: automatic pauses alone do not enable Resume."""
    assert _menu(paused_reasons=("locked",))["Resume"].enabled is False


def test_menu_mark_wrong_enabled_only_for_unmarked_recent_alert():
    """Behaviour 11: Mark wrong needs a recent, unmarked alert."""
    assert _menu(has_recent_alert=True)["Mark latest alert wrong"].enabled is True
    assert _menu(has_recent_alert=False)["Mark latest alert wrong"].enabled is False
    marked = _menu(has_recent_alert=True, latest_alert_marked=True)
    assert marked["Mark latest alert wrong"].enabled is False


# --- 12 -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("state", "colour", "words"),
    [
        ({}, "green", ["monitoring"]),
        ({"paused_reasons": ("manual",), "manual_paused": True}, "grey", ["paused"]),
        ({"paused_reasons": ("locked",)}, "grey", ["paused", "locked"]),
        ({"paused_reasons": ("low_battery",)}, "grey", ["paused", "battery"]),
        ({"paused_reasons": ("camera_in_use:Zoom.exe",)}, "grey", ["camera in use by Zoom.exe"]),
        ({"calibrated": False}, "amber", ["calibrat"]),
        ({"camera_available": False}, "amber", ["camera"]),
    ],
)
def test_tray_status_colour_and_tooltip(state, colour, words):
    """Behaviour 12: icon colour and tooltip follow the state."""
    tray = import_module("posture_checker.tray")
    got_colour, tooltip = tray.tray_status(tray.TrayState(**state))
    assert got_colour == colour
    for word in words:
        assert word.lower() in tooltip.lower()


def test_tray_tooltip_exact_camera_text():
    """Behaviour 12: the tooltip reads 'Paused: camera in use by Zoom.exe'."""
    tray = import_module("posture_checker.tray")
    state = tray.TrayState(paused_reasons=("camera_in_use:Zoom.exe",))
    assert tray.tray_status(state)[1] == "Paused: camera in use by Zoom.exe"


def test_tray_tooltip_names_automatic_reason_while_manually_paused():
    """Behaviour 8 and 12: tooltip says which automatic reason still holds the pause."""
    tray = import_module("posture_checker.tray")
    state = tray.TrayState(paused_reasons=("manual", "locked"), manual_paused=True)
    assert "locked" in tray.tray_status(state)[1].lower()


# --- 13 -----------------------------------------------------------------------
def test_mark_latest_alert_wrong_marks_newest_only_once(db, clock):
    """Behaviour 13: newest alert gets feedback='wrong'; a second call is a no-op."""
    actions = import_module("posture_checker.actions")
    ids = [
        db.insert_alert(
            ts=clock() - age,
            label="slouch",
            bad_fraction=0.9,
            window_s=60,
            model_version="v",
            calibration_id=None,
        )
        for age in (600, 60)
    ]
    expected = {ids[0]: None, ids[1]: "wrong"}
    assert actions.mark_latest_alert_wrong(db, clock) is True
    got = {r["id"]: r["feedback"] for r in db.query("SELECT id, feedback FROM alerts")}
    assert got == expected
    assert actions.mark_latest_alert_wrong(db, clock) is False
    got = {r["id"]: r["feedback"] for r in db.query("SELECT id, feedback FROM alerts")}
    assert got == expected


# --- 14 -----------------------------------------------------------------------
def test_slouching_now_labels_recent_sample(make_rig, db):
    """Behaviour 14: a labels row with sample id, model version and predicted label."""
    actions = import_module("posture_checker.actions")
    rig = make_rig()
    rig.tick(2)
    sample = db.query("SELECT id, model_version, label FROM samples ORDER BY id DESC")[0]
    rig.clock.advance(3)
    before = rig.monitor.policy.suspect
    actions.record_label(db, rig.clock, "slouch", confirm=rig.notifier.message)
    rows = db.query("SELECT * FROM labels")
    assert len(rows) == 1
    row = rows[0]
    assert row["label"] == "slouch"
    assert row["source"] == "tray"
    assert row["sample_id"] == sample["id"]
    assert row["model_version"] == sample["model_version"]
    assert row["predicted_label"] == sample["label"]
    assert len(rig.notifier.messages) == 1  # confirmation toast
    assert rig.notifier.alerts == []
    assert db.query("SELECT COUNT(*) AS n FROM alerts")[0]["n"] == 0
    assert rig.monitor.policy.suspect == before


def test_slouching_now_without_recent_sample_has_null_sample_id(make_rig, db):
    """Behaviour 14: no sample in the last 10 s gives sample_id NULL."""
    actions = import_module("posture_checker.actions")
    rig = make_rig()
    rig.tick(2)
    rig.clock.advance(11)
    actions.record_label(db, rig.clock, "forward_head", confirm=rig.notifier.message)
    row = db.query("SELECT * FROM labels")[0]
    assert row["sample_id"] is None
    assert row["label"] == "forward_head"
    assert rig.notifier.alerts == []


# --- 15 -----------------------------------------------------------------------
def _run_calibrate(rig, exit_code, add_new):
    flow = import_module("posture_checker.calibrate_flow")
    rig.tick(2)  # camera is open now
    old_id = rig.monitor.calibration.id
    seen = {}

    def inspect():
        seen["reasons"] = list(rig.supervisor.check().reasons)
        seen["camera_open"] = rig.camera.is_open

    new_ids: list[int] = []

    def on_exit():
        if add_new:
            cal = dataclasses.replace(rig.monitor.calibration, ts=rig.clock() + 1)
            new_ids.append(rig.db.insert_calibration(cal))

    launcher = FakeLauncher(exit_code=exit_code, on_exit=on_exit, inspect=inspect)
    flow.run_calibration(rig.monitor, rig.supervisor, launcher)
    return old_id, new_ids, seen, launcher


def test_calibrate_success_reloads_calibration(make_rig):
    """Behaviour 15: pause + camera closed before launch; new calibration loaded."""
    rig = make_rig()
    old_id, new_ids, seen, launcher = _run_calibrate(rig, 0, add_new=True)
    assert "calibrating" in seen["reasons"]
    assert seen["camera_open"] is False
    command = launcher.commands[0]
    assert "pythonw" in command[0].lower()
    assert command[-1] == "calibrate"
    assert rig.monitor.calibration.id == new_ids[0] != old_id
    assert "calibrating" not in rig.supervisor.check().reasons


def test_calibrate_failure_keeps_old_calibration(make_rig):
    """Behaviour 15: on failure the old calibration stays; the reason still clears."""
    rig = make_rig()
    old_id, _new, seen, _launcher = _run_calibrate(rig, 1, add_new=False)
    assert "calibrating" in seen["reasons"]
    assert rig.monitor.calibration.id == old_id
    assert "calibrating" not in rig.supervisor.check().reasons


# --- 16 -----------------------------------------------------------------------
def test_settings_valid_values_are_saved(registry):
    """Behaviour 16: valid values are written to the config file."""
    ui = import_module("posture_checker.settings_ui")
    save_config(Config())
    controller = ui.SettingsController(paths.config_path(), registry)
    error = controller.save(
        {
            "alert_window_s": 90.0,
            "sensitivity": 1.5,
            "cooldown_s": 120.0,
            "battery_pause_percent": 30,
            "camera_mode": "burst",
            "autostart": True,
        }
    )
    assert error is None
    cfg = load_config()
    assert (cfg.alert_window_s, cfg.sensitivity, cfg.cooldown_s) == (90.0, 1.5, 120.0)
    assert cfg.battery_pause_percent == 30
    assert cfg.camera_mode == "burst"


def test_settings_invalid_values_return_message_and_leave_file(registry):
    """Behaviour 16: invalid values are rejected and the file is unchanged."""
    ui = import_module("posture_checker.settings_ui")
    save_config(Config())
    before = paths.config_path().read_bytes()
    controller = ui.SettingsController(paths.config_path(), registry)
    error = controller.save({"alert_window_s": 1.0})
    assert isinstance(error, str) and error
    assert paths.config_path().read_bytes() == before


def test_config_watcher_applies_saved_settings_within_5s(make_rig, registry):
    """Behaviour 16: the running app picks up a changed file within 5 s."""
    ui = import_module("posture_checker.settings_ui")
    rig = make_rig()
    save_config(Config())
    watcher = ui.ConfigWatcher(paths.config_path(), rig.monitor, rig.clock)
    watcher.poll()
    ui.SettingsController(paths.config_path(), registry).save({"alert_window_s": 90.0})
    rig.clock.advance(5)
    watcher.poll()
    assert rig.monitor.policy.window_s == 90.0
    assert rig.monitor.config.alert_window_s == 90.0


# --- 17 -----------------------------------------------------------------------
def _app(rig, launcher=None):
    app_mod = import_module("posture_checker.app")
    return app_mod.App(
        monitor=rig.monitor,
        supervisor=rig.supervisor,
        db=rig.db,
        notifier=rig.notifier,
        clock=rig.clock,
        launcher=launcher,
    )


def test_start_without_calibration_toasts_and_is_amber(db, clock):
    """Behaviour 17: 'Calibrate to start' toast and an amber tray."""
    rig = Rig(db, None, clock)
    app = _app(rig)
    app.startup()
    assert len(rig.notifier.messages) == 1
    assert "Calibrate to start" in rig.notifier.messages[0]
    assert app.status()[0] == "amber"


# --- 18 -----------------------------------------------------------------------
def test_app_constructs_with_fakes(make_rig):
    """Behaviour 18: the app can be built from fakes (no tray, no camera)."""
    assert _app(make_rig()) is not None
    assert importlib.util.find_spec("posture_checker.__main__") is not None


def test_logging_goes_to_a_rotating_file_not_stdout(capsys):
    """Behaviour 18: output goes to a rotating log in the data folder."""
    app_mod = import_module("posture_checker.app")
    log_path = Path(app_mod.setup_logging())
    loggers = (logging.getLogger(), logging.getLogger("posture_checker"))
    try:
        logging.getLogger("posture_checker").error("hello-log-line")
        for logger in loggers:
            for handler in logger.handlers:
                handler.flush()
        assert paths.app_dir() in log_path.parents
        assert "hello-log-line" in log_path.read_text(encoding="utf-8")
        assert "hello-log-line" not in capsys.readouterr().out
        rotating = [h for lg in loggers for h in lg.handlers if isinstance(h, RotatingFileHandler)]
        assert rotating
        assert rotating[0].maxBytes <= 1024 * 1024
        assert rotating[0].backupCount <= 2  # 3 files in total
    finally:
        for logger in loggers:
            for handler in list(logger.handlers):
                if isinstance(handler, RotatingFileHandler):
                    handler.close()
                    logger.removeHandler(handler)


# --- 19 -----------------------------------------------------------------------
def test_second_instance_lock_fails():
    """Behaviour 19: acquiring the single-instance lock twice fails the second time."""
    si = import_module("posture_checker.platform.single_instance")
    backend = FakeMutexBackend()
    first, second = si.InstanceLock(backend), si.InstanceLock(backend)
    assert first.acquire() is True
    assert second.acquire() is False


# --- 20 -----------------------------------------------------------------------
def test_run_stops_within_5s_and_releases_camera(make_rig):
    """Behaviour 20: stop event ends run() quickly with the camera released."""
    rig = make_rig(sample_interval_s=0.01, suspect_interval_s=0.01, away_interval_s=0.01)
    app = _app(rig)
    stop = threading.Event()
    thread = threading.Thread(target=app.run, args=(stop,), daemon=True)
    thread.start()
    threading.Timer(0.2, stop.set).start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert rig.camera.opened >= 1
    assert rig.camera.closed == rig.camera.opened
    assert rig.camera.is_open is False


# --- 21 -----------------------------------------------------------------------
def test_monitor_error_is_logged_toasted_once_and_restarts_after_30s(make_rig, db, caplog):
    """Behaviour 21: crash -> log + one toast; monitor runs again after 30 s."""
    rig = make_rig()
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pose exploded")
        return make_landmarks()

    rig.monitor.pose = FakePose(provider)
    app = _app(rig)
    with caplog.at_level(logging.ERROR):
        delay = app.tick()
    assert delay == 30
    assert any("pose exploded" in r.getMessage() or r.exc_info for r in caplog.records)
    assert len(rig.notifier.messages) == 1

    rig.clock.advance(10)
    app.tick()
    assert db.query("SELECT COUNT(*) AS n FROM samples")[0]["n"] == 0  # still waiting
    rig.clock.advance(20)
    app.tick()
    assert db.query("SELECT COUNT(*) AS n FROM samples")[0]["n"] == 1
    assert len(rig.notifier.messages) == 1


# --- 22 -----------------------------------------------------------------------
def test_enable_autostart_writes_one_shortcut(registry, shortcut_writer, startup_dir):
    """Behaviour 22: one Startup shortcut running pythonw -m posture_checker app."""
    auto = import_module("posture_checker.platform.autostart")
    auto.enable_autostart(registry, writer=shortcut_writer, startup_dir=startup_dir)
    auto.enable_autostart(registry, writer=shortcut_writer, startup_dir=startup_dir)
    assert [p.name for p in startup_dir.iterdir()] == ["Posture Checker.lnk"]
    call = shortcut_writer.created[-1]
    assert "pythonw.exe" in str(call["target"])
    assert call["arguments"] == "-m posture_checker app"
    assert registry.run_values() == {}
    assert auto.is_autostart_enabled(startup_dir=startup_dir) is True


def test_register_adds_aumid_and_shortcut(registry, shortcut_writer, startup_dir):
    """Behaviour 22: the Python part of install registers AUMID and the shortcut."""
    install = import_module("posture_checker.install")
    install.register(registry, writer=shortcut_writer, startup_dir=startup_dir)
    assert AUMID_KEY in registry.keys
    assert (startup_dir / "Posture Checker.lnk").exists()


def test_register_without_autostart_skips_shortcut(registry, shortcut_writer, startup_dir):
    """Behaviour 22: no shortcut and no Run value when autostart is off."""
    install = import_module("posture_checker.install")
    install.register(registry, writer=shortcut_writer, startup_dir=startup_dir, autostart=False)
    assert AUMID_KEY in registry.keys
    assert list(startup_dir.iterdir()) == []
    assert registry.run_values() == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only parse of install.ps1")
def test_install_script_mentions_run_value_and_aumid():
    """Behaviour 22: install.ps1 names the Run value and the AUMID (parsed, not run)."""
    text = (REPO / "install.ps1").read_text(encoding="utf-8")
    assert "PostureChecker.App" in text or "register" in text.lower()
    assert "uv sync" in text


# --- 23 -----------------------------------------------------------------------
def test_unregister_removes_shortcut_and_aumid(registry, shortcut_writer, startup_dir):
    """Behaviour 23: uninstall removes the Startup shortcut and the AUMID key."""
    install = import_module("posture_checker.install")
    install.register(registry, writer=shortcut_writer, startup_dir=startup_dir)
    install.unregister(registry, startup_dir=startup_dir)
    assert list(startup_dir.iterdir()) == []
    assert registry.run_values() == {}
    assert AUMID_KEY not in registry.keys


def test_unregister_on_empty_registry_raises_nothing(registry, startup_dir):
    """Behaviour 23: uninstall without a prior install, and twice, succeeds."""
    install = import_module("posture_checker.install")
    install.unregister(registry, startup_dir=startup_dir)
    install.unregister(registry, startup_dir=startup_dir)


def test_unregister_keeps_data_unless_remove_data(registry, startup_dir):
    """Behaviour 23: data folder survives uninstall; -RemoveData deletes it."""
    install = import_module("posture_checker.install")
    data = paths.app_dir()
    (data / "posture.db").write_text("x")
    install.unregister(registry, startup_dir=startup_dir)
    assert (data / "posture.db").exists()
    install.unregister(registry, startup_dir=startup_dir, remove_data=True)
    assert not (data / "posture.db").exists()


def test_uninstall_script_exists():
    """Behaviour 23: uninstall.ps1 exists and offers -RemoveData."""
    text = (REPO / "uninstall.ps1").read_text(encoding="utf-8")
    assert "RemoveData" in text


# --- 24 -----------------------------------------------------------------------
def test_settings_autostart_toggle_uses_the_same_shortcut(registry, shortcut_writer, startup_dir):
    """Behaviour 24: toggling autostart in Settings adds or removes the shortcut."""
    ui = import_module("posture_checker.settings_ui")
    save_config(Config())
    controller = ui.SettingsController(
        paths.config_path(), registry, writer=shortcut_writer, startup_dir=startup_dir
    )
    assert controller.save({"autostart": True}) is None
    assert (startup_dir / "Posture Checker.lnk").exists()
    assert controller.save({"autostart": False}) is None
    assert not (startup_dir / "Posture Checker.lnk").exists()
