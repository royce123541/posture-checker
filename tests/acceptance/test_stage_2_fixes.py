"""Stage 2 fixes acceptance tests (spec: docs/specs/stage-2-fixes.md, F1-F3).

Interfaces the implementer must build (imported lazily inside the tests):

posture_checker.tray.TrayState
    two new bool fields, default False: ``away`` (latest sample is away) and
    ``presence_check`` (presence-check mode is on).
posture_checker.tray.tray_status(state)
    precedence: paused reasons > uncalibrated > camera unavailable > away.
    away -> (GREEN, "Posture Checker: away"); away and presence_check ->
    (GREEN, "Posture Checker: away, checking every 30 s") where 30 is
    posture_checker.monitor.PRESENCE_CHECK_INTERVAL_S formatted as an int.
posture_checker.app: App.state() fills the two new fields (not tested here).
posture_checker.platform.autostart
    SHORTCUT_NAME = "Posture Checker.lnk"
    enable_autostart(registry, *, writer, startup_dir: Path) -> None
    disable_autostart(registry, *, startup_dir: Path) -> None  (no error if gone)
    is_autostart_enabled(*, startup_dir: Path) -> bool  (shortcut file exists)
    writer.create(path: Path, target: Path, arguments: str, working_dir: Path,
                  minimised: bool) -> None
      target = pythonw_executable(); arguments = "-m posture_checker app";
      working_dir = repository root; path = startup_dir / SHORTCUT_NAME.
    Both enable and disable delete the old Run value "PostureChecker".
    A real writer class (WScript.Shell through PowerShell) is not tested.
posture_checker.settings_ui.SettingsController(config_path, registry, *,
    writer, startup_dir)   -- autostart reads/writes through the functions above.
posture_checker.monitor.Monitor.run(stop, ...)
    waits ``max(0, interval - time the sample took)`` via ``stop.wait(t)``
    (interval as next_interval gives it). Monitor.next_interval is unchanged.

Note: the Stage 2 tests in test_stage_2.py that call the old autostart
signatures (behaviours 22 and 24) become obsolete; the owner must update them.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest

from acceptance.conftest import RUN_KEY, RecordingStop
from conftest import FakePose
from posture_checker import monitor as mon
from posture_checker import paths
from posture_checker.config import Config, save_config
from posture_checker.synthetic import make_landmarks

REPO = Path(__file__).resolve().parents[2]
RUN_NAME = "PostureChecker"


def tray():
    return import_module("posture_checker.tray")


# --- F1 -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"away": True}, "Posture Checker: away"),
        (
            {"away": True, "presence_check": True},
            f"Posture Checker: away, checking every {int(mon.PRESENCE_CHECK_INTERVAL_S)} s",
        ),
    ],
)
def test_away_tooltip_stays_green(state, expected):
    """F1: away shows an away tooltip, with the interval in presence-check mode."""
    colour, tooltip = tray().tray_status(tray().TrayState(**state))
    assert colour == tray().GREEN
    assert tooltip == expected


def test_presence_check_interval_comes_from_the_constant(monkeypatch):
    """F1: the number in the tooltip is PRESENCE_CHECK_INTERVAL_S, not a copy."""
    monkeypatch.setattr(mon, "PRESENCE_CHECK_INTERVAL_S", 45.0)
    _, tooltip = tray().tray_status(tray().TrayState(away=True, presence_check=True))
    assert tooltip == "Posture Checker: away, checking every 45 s"


def test_paused_wins_over_away():
    """F1: a pause reason beats away."""
    colour, tooltip = tray().tray_status(
        tray().TrayState(away=True, presence_check=True, paused_reasons=("locked",))
    )
    assert colour == tray().GREY
    assert tooltip.startswith("Paused")


def test_uncalibrated_wins_over_away():
    """F1: uncalibrated beats away."""
    colour, tooltip = tray().tray_status(tray().TrayState(away=True, calibrated=False))
    assert colour == tray().AMBER
    assert tooltip == "Posture Checker: calibrate to start"


def test_camera_unavailable_wins_over_away():
    """F1: camera unavailable beats away."""
    colour, tooltip = tray().tray_status(tray().TrayState(away=True, camera_available=False))
    assert colour == tray().AMBER
    assert tooltip == "Posture Checker: camera unavailable"


def test_not_away_still_says_monitoring():
    """F1: the default state is unchanged."""
    assert tray().tray_status(tray().TrayState()) == (tray().GREEN, "Posture Checker: monitoring")


# --- F2 -----------------------------------------------------------------------
def auto():
    return import_module("posture_checker.platform.autostart")


def test_enable_creates_shortcut_with_target_arguments_and_folder(
    registry, shortcut_writer, startup_dir
):
    """F2: enabling writes 'Posture Checker.lnk' with the right command, folder, minimised."""
    auto().enable_autostart(registry, writer=shortcut_writer, startup_dir=startup_dir)
    shortcut = startup_dir / "Posture Checker.lnk"
    assert shortcut.exists()
    (call,) = shortcut_writer.created
    assert call["path"] == shortcut
    assert Path(call["target"]) == auto().pythonw_executable()
    assert call["arguments"] == "-m posture_checker app"
    assert Path(call["working_dir"]).resolve() == REPO
    assert call["minimised"] is True


def test_disable_deletes_shortcut(registry, shortcut_writer, startup_dir):
    """F2: disabling deletes the shortcut."""
    auto().enable_autostart(registry, writer=shortcut_writer, startup_dir=startup_dir)
    auto().disable_autostart(registry, startup_dir=startup_dir)
    assert not (startup_dir / "Posture Checker.lnk").exists()


def test_status_follows_the_shortcut(registry, shortcut_writer, startup_dir):
    """F2: 'is autostart on' means the shortcut exists."""
    assert auto().is_autostart_enabled(startup_dir=startup_dir) is False
    auto().enable_autostart(registry, writer=shortcut_writer, startup_dir=startup_dir)
    assert auto().is_autostart_enabled(startup_dir=startup_dir) is True
    (startup_dir / "Posture Checker.lnk").unlink()
    assert auto().is_autostart_enabled(startup_dir=startup_dir) is False


def test_a_run_value_alone_does_not_count_as_enabled(registry, startup_dir):
    """F2: only the shortcut counts, not the old Run value."""
    registry.set_value(RUN_KEY, RUN_NAME, "old command")
    assert auto().is_autostart_enabled(startup_dir=startup_dir) is False


def test_enable_removes_left_over_run_value(registry, shortcut_writer, startup_dir):
    """F2: enabling removes the old Run value so the app never starts twice."""
    registry.set_value(RUN_KEY, RUN_NAME, "old command")
    registry.set_value(RUN_KEY, "Other", "keep me")
    auto().enable_autostart(registry, writer=shortcut_writer, startup_dir=startup_dir)
    assert registry.run_values() == {"Other": "keep me"}


def test_disable_removes_left_over_run_value(registry, startup_dir):
    """F2: disabling removes the old Run value too."""
    registry.set_value(RUN_KEY, RUN_NAME, "old command")
    auto().disable_autostart(registry, startup_dir=startup_dir)
    assert RUN_NAME not in registry.run_values()


def test_disable_with_nothing_there_is_harmless(registry, startup_dir):
    """F2: no shortcut and no Run value: no error."""
    auto().disable_autostart(registry, startup_dir=startup_dir)
    auto().disable_autostart(registry, startup_dir=startup_dir)
    assert auto().is_autostart_enabled(startup_dir=startup_dir) is False


def test_settings_checkbox_uses_the_same_functions(registry, shortcut_writer, startup_dir):
    """F2: Settings' autostart checkbox creates and deletes the shortcut."""
    ui = import_module("posture_checker.settings_ui")
    save_config(Config())
    controller = ui.SettingsController(
        paths.config_path(), registry, writer=shortcut_writer, startup_dir=startup_dir
    )
    assert controller.current()["autostart"] is False
    assert controller.save({"autostart": True}) is None
    assert (startup_dir / "Posture Checker.lnk").exists()
    assert controller.current()["autostart"] is True
    assert registry.run_values() == {}
    assert controller.save({"autostart": False}) is None
    assert not (startup_dir / "Posture Checker.lnk").exists()
    assert controller.current()["autostart"] is False


# --- F3 -----------------------------------------------------------------------
def waits_for(make_rig, *, took_s, landmarks, warmup=0, **config):
    """Run the monitor loop; return the waits after the warm-up samples."""
    rig = make_rig(**config)

    def provider():
        rig.clock.advance(took_s)  # the sample takes this long
        return None if landmarks is None else make_landmarks(**landmarks)

    rig.monitor.pose = FakePose(provider)
    stop = RecordingStop(waits_allowed=warmup + 1)
    rig.monitor.run(stop)
    return stop.waits[warmup:]


@pytest.mark.parametrize("camera_mode", ["hold", "burst"])
@pytest.mark.parametrize(
    ("took_s", "expected"),
    [(1.5, 0.5), (3.0, 0.0), (2.0, 0.0), (0.0, 2.0)],
)
def test_wait_is_interval_minus_sample_time(make_rig, camera_mode, took_s, expected):
    """F3: a 2 s interval minus the sample time, never below 0, in every camera mode."""
    (wait,) = waits_for(
        make_rig,
        took_s=took_s,
        landmarks={},
        camera_mode=camera_mode,
        sample_interval_s=2.0,
    )
    assert wait == pytest.approx(expected)


@pytest.mark.parametrize(("took_s", "expected"), [(4.0, 6.0), (12.0, 0.0)])
def test_away_wait_counts_sample_time(make_rig, took_s, expected):
    """F3: the same rule for the away interval."""
    (wait,) = waits_for(
        make_rig, took_s=took_s, landmarks=None, camera_mode="burst", away_interval_s=10.0
    )
    assert wait == pytest.approx(expected)


@pytest.mark.parametrize(("took_s", "expected"), [(1.5, 2.5), (5.0, 0.0)])
def test_suspect_wait_counts_sample_time(make_rig, took_s, expected):
    """F3: the same rule for the suspect interval (after a streak of bad samples)."""
    waits = waits_for(
        make_rig,
        took_s=took_s,
        landmarks={"head_forward": 0.6, "head_drop": 0.2},
        warmup=30,
        camera_mode="burst",
        sample_interval_s=10.0,
        suspect_interval_s=4.0,
        cooldown_s=100000,
    )
    assert waits[-1] == pytest.approx(expected)
