"""Stage 2 additions to the monitor beyond the acceptance tests."""

from __future__ import annotations

from acceptance.conftest import Rig
from posture_checker import monitor as mon
from posture_checker.config import Config


def test_paused_interval_is_at_most_five_seconds(db, calibration, clock) -> None:
    rig = Rig(db, calibration, clock)
    rig.lock.locked = True
    sample = rig.tick(1)
    assert sample.state == mon.PAUSED
    assert rig.monitor.next_interval(sample) == mon.PAUSED_CHECK_INTERVAL_S


def test_toast_pause_goes_through_supervisor(db, calibration, clock) -> None:
    rig = Rig(db, calibration, clock)
    rig.monitor.pause(rig.monitor.PAUSE_FROM_TOAST_S, reason="toast")
    assert rig.tick(1).state == mon.PAUSED
    assert rig.supervisor.manual_active
    rig.monitor.resume()
    assert rig.tick(1).state == mon.PRESENT
    rows = db.query("SELECT kind, detail FROM events ORDER BY id")
    assert [(r["kind"], r["detail"]) for r in rows] == [("pause", "manual"), ("resume", "manual")]


def test_away_streak_is_broken_by_a_present_sample(db, calibration, clock) -> None:
    rig = Rig(db, calibration, clock)
    rig.landmarks = None
    rig.run_for(200, step_s=10)
    rig.landmarks = {}
    rig.tick(2)
    rig.landmarks = None
    rig.run_for(200, step_s=10)
    assert rig.monitor.presence_check is False  # only 200 s since the last presence
    rig.run_for(110, step_s=10)
    assert rig.monitor.presence_check is True


def test_apply_config_updates_policy_model_and_battery(db, calibration, clock) -> None:
    rig = Rig(db, calibration, clock)
    new = Config(alert_window_s=30, cooldown_s=10, sensitivity=2.0, battery_pause_percent=40)
    rig.monitor.apply_config(new)
    assert rig.monitor.config is new
    assert (rig.monitor.policy.window_s, rig.monitor.policy.cooldown_s) == (30, 10)
    assert rig.monitor.model.sensitivity == 2.0
    assert rig.supervisor.battery_pause_percent == 40


def test_switching_to_burst_releases_camera_at_next_sample(db, calibration, clock) -> None:
    rig = Rig(db, calibration, clock)
    rig.tick(2)
    assert rig.camera.is_open
    rig.monitor.apply_config(Config(camera_mode="burst"))
    rig.tick(2)
    assert rig.camera.is_open is False


def test_no_supervisor_keeps_stage_1_pause_events(db, calibration, clock) -> None:
    from conftest import FakeCamera, FakePose, RecordingNotifier
    from posture_checker.inference.rules import RulesModel
    from posture_checker.synthetic import make_landmarks

    db.insert_calibration(calibration)
    monitor = mon.Monitor(
        config=Config(),
        db=db,
        source=FakeCamera(),
        pose=FakePose(make_landmarks),
        model=RulesModel(),
        notifier=RecordingNotifier(),
        clock=clock,
    )
    assert monitor.check_pause() == []
    monitor.pause(60)
    assert monitor.check_pause() == ["manual"]
