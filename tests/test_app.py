"""The background app's controller, tray actions and install helpers, with fakes."""

from __future__ import annotations

import threading

from acceptance.conftest import FakeLauncher, FakeRegistry, FakeShortcutWriter, Rig
from posture_checker import install, paths, tray
from posture_checker import monitor as mon
from posture_checker.app import ALERT_STATE_CACHE_S, App, setup_logging
from posture_checker.calibrate_flow import gui_command
from posture_checker.inference import SLOUCH
from posture_checker.synthetic import make_landmarks


def make_app(db, calibration, clock, launcher=None, **config):
    rig = Rig(db, calibration, clock, **config)
    app = App(
        monitor=rig.monitor,
        supervisor=rig.supervisor,
        db=db,
        notifier=rig.notifier,
        clock=clock,
        launcher=launcher,
    )
    return rig, app


def test_startup_with_calibration_shows_nothing_and_is_green(db, calibration, clock) -> None:
    rig, app = make_app(db, calibration, clock)
    app.startup()
    assert rig.notifier.messages == []
    app.tick()
    assert app.status() == ("green", "Hunch: monitoring")


def test_camera_unavailable_turns_tray_amber(db, calibration, clock) -> None:
    rig, app = make_app(db, calibration, clock)
    rig.camera.available = False
    app.tick()
    assert app.status()[0] == "amber"


def test_tray_pause_and_resume_actions(db, calibration, clock) -> None:
    rig, app = make_app(db, calibration, clock)
    app.tick()
    app.tray_actions()[tray.PAUSE_15]()
    assert app.state().manual_paused is True
    clock.advance(1)
    assert app.tick() == mon.PAUSED_CHECK_INTERVAL_S
    assert rig.camera.is_open is False  # released by the worker's next check
    assert app.status() == ("grey", "Paused: by you")
    app.tray_actions()[tray.RESUME]()
    clock.advance(1)
    app.tick()
    assert app.status()[0] == "green"


def test_tray_label_action_records_and_confirms(db, calibration, clock) -> None:
    rig, app = make_app(db, calibration, clock)
    app.tick()
    app.tray_actions()[tray.LABEL_SLOUCH]()
    rows = db.query("SELECT label, sample_id FROM labels")
    assert [(r["label"], r["sample_id"] is not None) for r in rows] == [(SLOUCH, True)]
    assert len(rig.notifier.messages) == 1


def test_mark_wrong_state_follows_alerts(db, calibration, clock) -> None:
    _rig, app = make_app(db, calibration, clock)
    assert app.state().has_recent_alert is False
    db.insert_alert(
        ts=clock(),
        label=SLOUCH,
        bad_fraction=0.9,
        window_s=60,
        model_version="v",
        calibration_id=None,
    )
    clock.advance(ALERT_STATE_CACHE_S)  # inserted behind the app's back: wait for the cache
    assert (app.state().has_recent_alert, app.state().latest_alert_marked) == (True, False)
    app.tray_actions()[tray.MARK_WRONG]()
    assert app.state().latest_alert_marked is True
    clock.advance(31 * 60)
    assert app.state().has_recent_alert is False  # older than 30 min


def test_settings_action_launches_settings_window(db, calibration, clock) -> None:
    launcher = FakeLauncher()
    _rig, app = make_app(db, calibration, clock, launcher=launcher)
    app.tray_actions()[tray.SETTINGS]()
    assert launcher.commands == [gui_command("settings")]
    assert launcher.commands[0][-1] == "settings"


def test_calibrate_action_runs_in_background_once(db, calibration, clock) -> None:
    release = threading.Event()
    launched = threading.Event()

    class SlowProcess:
        def wait(self) -> int:
            launched.set()
            release.wait(5)
            return 1

    commands: list[list[str]] = []

    def launcher(command):
        commands.append(command)
        return SlowProcess()

    rig, app = make_app(db, calibration, clock, launcher=launcher)
    app.start_calibration()
    assert launched.wait(5)
    app.start_calibration()  # already calibrating: ignored
    release.set()
    for _ in range(100):
        if "calibrating" not in rig.supervisor.check().reasons:
            break
        threading.Event().wait(0.05)
    assert len(commands) == 1
    assert "calibrating" not in rig.supervisor.check().reasons


def test_error_toast_is_shown_only_once(db, calibration, clock) -> None:
    rig, app = make_app(db, calibration, clock)

    def broken():
        raise RuntimeError("still broken")

    rig.monitor.pose.provider = broken
    for _ in range(3):
        assert app.tick() == 30
        clock.advance(30)
    assert len(rig.notifier.messages) == 1
    assert rig.camera.is_open is False


def test_run_loop_acts_on_a_pause_without_waiting_for_the_next_sample(
    db, calibration, clock, monkeypatch
) -> None:
    monkeypatch.setattr("posture_checker.app.PAUSE_POLL_S", 0.01)
    rig, app = make_app(db, calibration, clock, sample_interval_s=60, camera_mode="hold")
    stop = threading.Event()
    thread = threading.Thread(target=app.run, args=(stop,), daemon=True)
    thread.start()
    for _ in range(200):
        if rig.camera.is_open:
            break
        threading.Event().wait(0.01)
    assert rig.camera.is_open
    rig.lock.locked = True
    for _ in range(200):
        if not rig.camera.is_open:
            break
        threading.Event().wait(0.01)
    stop.set()
    thread.join(5)
    assert rig.camera.is_open is False
    kinds = [(r["kind"], r["detail"]) for r in db.query("SELECT kind, detail FROM events")]
    assert ("pause", "locked") in kinds


def test_setup_logging_twice_keeps_one_file_handler() -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    try:
        setup_logging()
        setup_logging()
        assert len([h for h in root.handlers if isinstance(h, RotatingFileHandler)]) == 1
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, RotatingFileHandler):
                handler.close()
                root.removeHandler(handler)


def test_register_then_unregister_leaves_registry_and_startup_empty(tmp_path, hunch_exe) -> None:
    registry = FakeRegistry()
    startup = tmp_path / "Startup"
    install.register(registry, writer=FakeShortcutWriter(), startup_dir=startup)
    install.register(registry, writer=FakeShortcutWriter(), startup_dir=startup)
    assert [p.name for p in startup.iterdir()] == ["Hunch.lnk"]
    install.unregister(registry, startup_dir=startup)
    assert list(startup.iterdir()) == []
    assert registry.run_values() == {}
    assert install.AUMID_KEY not in registry.keys


def test_unregister_survives_registry_errors(tmp_path) -> None:
    class Broken(FakeRegistry):
        def delete_value(self, path, name):
            raise PermissionError(name)

        def delete_key(self, path):
            raise PermissionError(path)

    install.unregister(Broken(), startup_dir=tmp_path)


def test_remove_data_deletes_the_whole_folder(tmp_path) -> None:
    (paths.app_dir() / "config.json").write_text("{}")
    folder = paths.app_dir()
    install.unregister(FakeRegistry(), startup_dir=tmp_path, remove_data=True)
    assert not folder.exists()


# --- review findings ------------------------------------------------------------


def test_second_calibration_request_is_refused_synchronously(db, calibration, clock) -> None:
    """Two quick requests start one window, even before the worker has run."""
    release = threading.Event()
    commands: list[list[str]] = []

    class SlowProcess:
        def wait(self) -> int:
            release.wait(5)
            return 0

    def launcher(command):
        commands.append(command)
        return SlowProcess()

    _rig, app = make_app(db, calibration, clock, launcher=launcher)
    gate = threading.Event()
    real_run = app.supervisor.add_reason

    def slow_add_reason(name):  # hold the worker before it sets "calibrating"
        gate.wait(5)
        real_run(name)

    app.supervisor.add_reason = slow_add_reason
    assert app.start_calibration() is True
    assert app.start_calibration() is False  # the reason is not set yet, still refused
    assert "calibrating" in app.state().paused_reasons
    gate.set()
    release.set()
    for _ in range(100):
        if app.start_calibration():
            break
        threading.Event().wait(0.05)
    else:
        raise AssertionError("the slot was never released")
    release.set()
    for _ in range(100):
        if len(commands) == 2:
            break
        threading.Event().wait(0.05)
    assert len(commands) == 2  # the first run, then the retry after it finished


def test_tray_pause_does_not_wait_for_the_monitor(db, calibration, clock) -> None:
    rig, app = make_app(db, calibration, clock)
    held = threading.Event()
    done = threading.Event()

    def hold_monitor_lock():
        with rig.monitor._lock:  # e.g. a slow camera open on the worker
            held.set()
            done.wait(5)

    thread = threading.Thread(target=hold_monitor_lock, daemon=True)
    thread.start()
    assert held.wait(5)
    finished = threading.Event()

    def press_pause_and_resume():
        app.pause(None)
        app.resume()
        finished.set()

    threading.Thread(target=press_pause_and_resume, daemon=True).start()
    try:
        assert finished.wait(1), "tray action blocked on the monitor lock"
    finally:
        done.set()
        thread.join(5)


def test_error_toast_shown_again_after_a_recovery(db, calibration, clock) -> None:
    rig, app = make_app(db, calibration, clock)
    state = {"fail": True}

    def provider():
        if state["fail"]:
            raise RuntimeError("broken")
        return make_landmarks()

    rig.monitor.pose.provider = provider
    app.tick()  # error 1: toast
    clock.advance(30)
    app.tick()  # still broken: no second toast
    assert len(rig.notifier.messages) == 1
    state["fail"] = False
    clock.advance(30)
    app.tick()  # recovered
    state["fail"] = True
    clock.advance(2)
    app.tick()  # a new failure after recovery
    assert len(rig.notifier.messages) == 2


def test_alert_state_is_cached_and_refreshed_on_new_alert(db, calibration, clock) -> None:
    rig, app = make_app(db, calibration, clock, alert_window_s=10, cooldown_s=300)
    calls = {"n": 0}
    real = db.last_alert

    def counting():
        calls["n"] += 1
        return real()

    db.last_alert = counting
    for _ in range(5):
        app.state()
    assert calls["n"] == 1
    rig.landmarks = {"head_forward": 0.3}
    for _ in range(30):
        app.tick()
        clock.advance(1)
        if rig.notifier.alerts:
            break
    assert rig.notifier.alerts
    assert app.state().has_recent_alert is True  # refreshed at once, not after the cache


def test_second_instance_exits_before_touching_the_log() -> None:
    from acceptance.conftest import FakeMutexBackend, RecordingMessageNotifier
    from posture_checker.app import ALREADY_RUNNING, claim_single_instance
    from posture_checker.platform.single_instance import InstanceLock

    backend = FakeMutexBackend()
    started: list[str] = []
    first, second = RecordingMessageNotifier(), RecordingMessageNotifier()
    assert claim_single_instance(InstanceLock(backend), first, lambda: started.append("1"))
    assert not claim_single_instance(InstanceLock(backend), second, lambda: started.append("2"))
    assert started == ["1"]  # the second instance never set up logging
    assert second.messages == [ALREADY_RUNNING]
    assert first.messages == []


def test_tray_status_follows_pause_and_resume_without_a_monitor_step(
    db, calibration, clock
) -> None:
    rig, app = make_app(db, calibration, clock)
    rig.lock.locked = True
    app.tick()  # one real check: locked
    rig.lock.locked = False  # unlocked, but no check has run since
    app.tray_actions()[tray.PAUSE_15]()
    assert app.status() == ("grey", "Paused: by you, screen locked")
    app.tray_actions()[tray.RESUME]()
    assert app.status() == ("grey", "Paused: screen locked")  # only manual cleared
    app.tick()
    app.tray_actions()[tray.PAUSE_UNTIL_RESUMED]()
    assert app.status() == ("grey", "Paused: by you")
    app.tray_actions()[tray.RESUME]()
    assert app.status()[0] == "green"


def test_app_camera_probe_has_the_stale_check_on(db) -> None:
    """main() builds its probe here; it must pass a real FILETIME clock."""
    from posture_checker.app import build_camera_usage
    from posture_checker.platform.camera_usage import filetime_now

    hour = 3600 * 10_000_000
    registry = FakeRegistry()
    now = filetime_now()
    registry.add_app("NonPackaged\\C:#A#Crashed.exe", now - 13 * hour, 0)
    registry.add_app("NonPackaged\\C:#A#Zoom.exe", now - hour, 0)
    assert build_camera_usage(registry, db).apps_in_use() == ["Zoom.exe"]
    events = db.query("SELECT kind, detail FROM events")
    assert [r["kind"] for r in events] == ["camera_probe"]
    assert "Crashed.exe" in events[0]["detail"]


# --- Stage 2 fixes ----------------------------------------------------------------


def test_tick_subtracts_the_sample_time(db, calibration, clock) -> None:
    """F3: App.tick returns the interval minus the time the sample took."""
    from conftest import FakePose

    rig, app = make_app(db, calibration, clock, sample_interval_s=2.0)

    def slow_sample():
        clock.advance(1.5)
        return make_landmarks()

    rig.monitor.pose = FakePose(slow_sample)
    assert app.tick() == 0.5


def test_tick_never_returns_a_negative_wait(db, calibration, clock) -> None:
    from conftest import FakePose

    rig, app = make_app(db, calibration, clock, sample_interval_s=2.0)

    def very_slow_sample():
        clock.advance(5.0)
        return make_landmarks()

    rig.monitor.pose = FakePose(very_slow_sample)
    assert app.tick() == 0.0


def test_state_reports_away_then_presence_check(db, calibration, clock) -> None:
    """F1: App.state fills away and presence_check from the monitor."""
    rig, app = make_app(db, calibration, clock)
    rig.landmarks = None
    app.tick()
    assert app.state().away is True
    assert app.state().presence_check is False
    assert app.status() == ("green", "Hunch: away")
    clock.advance(mon.PRESENCE_CHECK_AFTER_S + 1)
    app.tick()
    assert app.state().presence_check is True
    rig.landmarks = {}
    clock.advance(1)
    app.tick()
    assert app.state().away is False
    assert app.status() == ("green", "Hunch: monitoring")


def test_app_main_relaunches_first_and_then_exits(monkeypatch) -> None:
    """Rename R2: hunch.exe calls app.main directly. The relaunch check runs before
    anything else, and the copy that relaunched never starts the app."""
    from posture_checker import app as app_mod
    from posture_checker.platform import console, single_instance, windows

    order: list[str] = []

    def relaunch(args):
        order.append(f"relaunch {args}")
        return True

    def must_not_run(*args, **kwargs):
        order.append("app started")
        raise AssertionError("the app must not start")

    monkeypatch.setattr(console, "relaunch_without_console", relaunch)
    monkeypatch.setattr(single_instance, "Win32MutexBackend", must_not_run)
    monkeypatch.setattr(windows, "ToastNotifier", must_not_run)
    monkeypatch.setattr(app_mod, "run_app", must_not_run)
    assert app_mod.main() == 0
    assert order == ["relaunch ['app']"]


def test_app_main_starts_the_app_when_no_relaunch_is_needed(monkeypatch) -> None:
    from posture_checker import app as app_mod
    from posture_checker.platform import console

    order: list[str] = []

    def no_relaunch(args):
        order.append("relaunch")
        return False

    def run_app():
        order.append("run_app")
        return 7

    monkeypatch.setattr(console, "relaunch_without_console", no_relaunch)
    monkeypatch.setattr(app_mod, "run_app", run_app)
    assert app_mod.main() == 7
    assert order == ["relaunch", "run_app"]
