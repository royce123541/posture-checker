"""The background app's controller, tray actions and install helpers, with fakes."""

from __future__ import annotations

import threading

from acceptance.conftest import FakeLauncher, FakeRegistry, Rig
from posture_checker import install, paths, tray
from posture_checker import monitor as mon
from posture_checker.app import App, setup_logging
from posture_checker.calibrate_flow import gui_command
from posture_checker.inference import SLOUCH


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
    assert rig.camera.is_open is False  # released at once, not at the next sample
    assert app.state().manual_paused is True
    clock.advance(1)
    assert app.tick() == mon.PAUSED_CHECK_INTERVAL_S
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
