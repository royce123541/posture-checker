from __future__ import annotations

import json

import pytest

from posture_checker import paths
from posture_checker.cli import build_parser, main
from posture_checker.config import Config, load_config, save_config


def test_defaults_when_no_file_exists() -> None:
    assert load_config() == Config()


def test_round_trip() -> None:
    save_config(Config(alert_window_s=90, sensitivity=1.5))
    loaded = load_config()
    assert loaded.alert_window_s == 90
    assert loaded.sensitivity == 1.5


def test_unknown_keys_are_ignored() -> None:
    paths.config_path().write_text(json.dumps({"sensitivity": 2.0, "removed_option": 1}))
    assert load_config().sensitivity == 2.0


@pytest.mark.parametrize(
    "bad",
    [
        {"alert_window_s": 1},
        {"bad_fraction": 0.2},
        {"sensitivity": 0},
        {"min_confidence": 1.5},
        {"sample_interval_s": 0},
    ],
)
def test_invalid_settings_are_rejected(bad) -> None:
    with pytest.raises(ValueError):
        Config(**bad).validate()


def test_data_folder_follows_the_override(tmp_path) -> None:
    assert paths.app_dir() == tmp_path / "home"
    assert paths.db_path().parent == paths.app_dir()
    assert paths.models_dir().is_dir()


def test_config_command_changes_and_prints_settings(capsys) -> None:
    assert main(["config", "--window", "45", "--sensitivity", "1.2"]) == 0
    assert load_config().alert_window_s == 45
    assert "sensitivity = 1.2" in capsys.readouterr().out


def test_cli_knows_its_commands() -> None:
    parser = build_parser()
    commands = (
        "fetch-model",
        "doctor",
        "calibrate",
        "run",
        "record",
        "fit-rules",
        "mark-wrong",
        "config",
    )
    for command in commands:
        assert parser.parse_args([command]).command == command


def test_blocked_toast_reasons_cover_every_windows_setting() -> None:
    from posture_checker.platform.windows import BLOCKED_REASONS

    # Windows NotificationSetting: 0 is enabled, 1-4 are the ways it can be off.
    assert set(BLOCKED_REASONS) == {1, 2, 3, 4}
    assert "Settings > System > Notifications" in BLOCKED_REASONS[2]


def test_mark_wrong_without_alerts_is_harmless(capsys) -> None:
    assert main(["mark-wrong"]) == 1
    assert "no alerts" in capsys.readouterr().out
