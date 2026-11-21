"""The background app's controller, tray actions and install helpers, with fakes."""

from __future__ import annotations

import threading

from acceptance.conftest import FakeLauncher, FakeRegistry, Rig
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
    assert app.status() == ("green", "Posture Checker: monitoring")


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
    rig, app = make_app(db, calibration, clock, sample_interval_s=60)
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


def test_register_then_unregister_leaves_registry_empty() -> None:
    registry = FakeRegistry()
    install.register(registry)
    install.register(registry)
    assert list(registry.run_values()) == ["PostureChecker"]
    install.unregister(registry)
    assert registry.run_values() == {}
    assert install.AUMID_KEY not in registry.keys


def test_unregister_survives_registry_errors() -> None:
    class Broken(FakeRegistry):
        def delete_value(self, path, name):
            raise PermissionError(name)

        def delete_key(self, path):
            raise PermissionError(path)

    install.unregister(Broken())


def test_remove_data_deletes_the_whole_folder() -> None:
    (paths.app_dir() / "config.json").write_text("{}")
    folder = paths.app_dir()
    install.unregister(FakeRegistry(), remove_data=True)
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
