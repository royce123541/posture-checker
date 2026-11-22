"""Settings controller and config watcher (behaviours 16 and 24), without Tk."""

from __future__ import annotations

import pytest

from acceptance.conftest import FakeRegistry, FakeShortcutWriter
from conftest import FakeCamera, FakePose, RecordingNotifier
from posture_checker import paths
from posture_checker.config import Config, load_config, save_config
from posture_checker.inference.rules import RulesModel
from posture_checker.monitor import Monitor
from posture_checker.settings_ui import WATCH_INTERVAL_S, ConfigWatcher, SettingsController
from posture_checker.synthetic import make_landmarks


def make_controller(tmp_path) -> SettingsController:
    return SettingsController(
        paths.config_path(),
        FakeRegistry(),
        writer=FakeShortcutWriter(),
        startup_dir=tmp_path / "Startup",
    )


@pytest.fixture
def controller(tmp_path) -> SettingsController:
    save_config(Config())
    return make_controller(tmp_path)


def test_current_includes_autostart(controller) -> None:
    values = controller.current()
    assert values["alert_window_s"] == 60.0
    assert values["camera_mode"] == "burst"
    assert values["autostart"] is False
    controller.store({"autostart": True})
    assert controller.current()["autostart"] is True


def test_parse_form_converts_text(controller) -> None:
    parsed = controller.parse_form(
        {"alert_window_s": " 90 ", "battery_pause_percent": "25", "autostart": True}
    )
    assert parsed == {"alert_window_s": 90.0, "battery_pause_percent": 25, "autostart": True}


@pytest.mark.parametrize(
    ("texts", "fragment"),
    [
        ({"alert_window_s": "soon"}, "not a valid float"),
        ({"battery_pause_percent": "20.5"}, "not a valid int"),
        ({"colour": "red"}, "Unknown setting"),
    ],
)
def test_parse_form_errors(controller, texts, fragment) -> None:
    result = controller.parse_form(texts)
    assert isinstance(result, str) and fragment in result


@pytest.mark.parametrize(
    "values",
    [
        {"camera_mode": "sometimes"},
        {"battery_pause_percent": 150},
        {"sensitivity": 10.0},
        {"cooldown_s": -1.0},
        {"alert_window_s": "abc"},
        {"unknown": 1},
    ],
)
def test_invalid_values_leave_file_and_autostart_untouched(controller, values) -> None:
    before = paths.config_path().read_bytes()
    error = controller.save({**values, "autostart": True})
    assert error
    assert paths.config_path().read_bytes() == before
    assert controller.registry.run_values() == {}


def test_missing_file_starts_from_defaults(tmp_path) -> None:
    controller = make_controller(tmp_path)
    assert controller.save({"cooldown_s": 30.0}) is None
    assert load_config().cooldown_s == 30.0


def make_monitor(db, clock) -> Monitor:
    return Monitor(
        config=Config(),
        db=db,
        source=FakeCamera(),
        pose=FakePose(make_landmarks),
        model=RulesModel(),
        notifier=RecordingNotifier(),
        clock=clock,
    )


def test_watcher_applies_every_live_setting(db, clock, controller) -> None:
    monitor = make_monitor(db, clock)
    watcher = ConfigWatcher(paths.config_path(), monitor, clock)
    watcher.poll()
    controller.save({"sensitivity": 2.0, "cooldown_s": 10.0, "camera_mode": "burst"})
    clock.advance(WATCH_INTERVAL_S)
    assert watcher.poll() is True
    assert monitor.model.sensitivity == 2.0
    assert monitor.policy.cooldown_s == 10.0
    assert monitor.config.camera_mode == "burst"


def test_watcher_rate_limits_and_skips_unchanged_file(db, clock, controller) -> None:
    watcher = ConfigWatcher(paths.config_path(), make_monitor(db, clock), clock)
    assert watcher.poll() is True  # first read applies the file
    controller.save({"alert_window_s": 30.0})
    assert watcher.poll() is False  # too soon
    clock.advance(WATCH_INTERVAL_S)
    assert watcher.poll() is True
    clock.advance(WATCH_INTERVAL_S)
    assert watcher.poll() is False  # unchanged


def test_watcher_keeps_settings_when_file_is_broken(db, clock, controller) -> None:
    monitor = make_monitor(db, clock)
    watcher = ConfigWatcher(paths.config_path(), monitor, clock)
    watcher.poll()
    paths.config_path().write_text("{not json", encoding="utf-8")
    clock.advance(WATCH_INTERVAL_S)
    assert watcher.poll() is False
    assert monitor.config.alert_window_s == 60.0
