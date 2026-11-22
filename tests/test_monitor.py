"""End-to-end pipeline tests with a fake camera, fake pose model and fake clock."""

from __future__ import annotations

import json

import pytest

from conftest import FakeCamera, FakeClock, FakePose, RecordingNotifier
from posture_checker import monitor as mon
from posture_checker.config import Config
from posture_checker.inference import FORWARD_HEAD, GOOD, SLOUCH
from posture_checker.inference.rules import RulesModel
from posture_checker.synthetic import make_landmarks


class Rig:
    """A monitor wired to fakes, with the pose controlled by the test."""

    def __init__(self, db, calibration, clock: FakeClock, **config) -> None:
        self.pose_kwargs: dict | None = {}
        self.clock = clock
        self.camera = FakeCamera()
        self.notifier = RecordingNotifier()
        self.db = db
        if calibration is not None:
            db.insert_calibration(calibration)
        self.monitor = mon.Monitor(
            config=Config(**{"alert_window_s": 60, "cooldown_s": 300, **config}),
            db=db,
            source=self.camera,
            pose=FakePose(self._landmarks),
            model=RulesModel(),
            notifier=self.notifier,
            clock=clock,
        )

    def _landmarks(self):
        return None if self.pose_kwargs is None else make_landmarks(**self.pose_kwargs)

    def run(self, seconds: float, **pose) -> list[mon.Sample]:
        """Hold one posture for ``seconds``, sampling as the monitor asks to."""
        self.pose_kwargs = pose
        return self._loop(seconds)

    def run_away(self, seconds: float) -> list[mon.Sample]:
        self.pose_kwargs = None
        return self._loop(seconds)

    def _loop(self, seconds: float) -> list[mon.Sample]:
        samples = []
        end = self.clock.now + seconds
        while self.clock.now < end:
            sample = self.monitor.step()
            samples.append(sample)
            self.clock.advance(self.monitor.next_interval(sample))
        return samples


@pytest.fixture
def rig(db, calibration, clock) -> Rig:
    return Rig(db, calibration, clock)


def test_good_posture_is_logged_without_alerts(rig) -> None:
    samples = rig.run(180)
    assert all(s.state == mon.PRESENT and s.label == GOOD for s in samples)
    assert rig.notifier.alerts == []
    assert len(rig.db.query("SELECT * FROM samples")) == len(samples)


def test_sustained_slouch_alerts_and_records_lineage(rig) -> None:
    rig.run(30)
    rig.run(90, head_drop=0.8, shoulder_drop=0.3)
    assert [label for _, label in rig.notifier.alerts] == [SLOUCH]

    alert = rig.db.last_alert()
    assert alert["label"] == SLOUCH
    assert alert["model_version"] == "rules-v0"
    assert alert["calibration_id"] == rig.monitor.calibration.id

    row = rig.db.query("SELECT * FROM samples WHERE label = 'slouch' LIMIT 1")[0]
    assert row["model_version"] == "rules-v0"
    assert row["pose_model"] == "fake-pose"
    assert json.loads(row["features_json"])["face_drop"] > 0.45
    assert len(json.loads(row["landmarks_json"])) == 13


def test_brief_lean_in_does_not_alert(rig) -> None:
    rig.run(120)
    rig.run(15, head_forward=0.2)
    rig.run(120)
    assert rig.notifier.alerts == []


def test_bad_lighting_reports_low_confidence_instead_of_alerting(rig) -> None:
    rig.camera.brightness = 22  # nearly dark room
    samples = rig.run(300, head_forward=0.3)
    assert all(s.state == mon.LOW_CONFIDENCE for s in samples)
    assert all("too_dark" in s.reasons for s in samples)
    assert rig.notifier.alerts == []
    stored = rig.db.query("SELECT DISTINCT state FROM samples")
    assert [r["state"] for r in stored] == [mon.LOW_CONFIDENCE]


def test_samples_faster_while_bad_posture_is_suspected(rig) -> None:
    rig.run(20)
    assert rig.monitor.next_interval(rig.monitor.step()) == rig.monitor.config.sample_interval_s
    rig.run(10, head_forward=0.2)
    assert rig.monitor.next_interval(rig.monitor.step()) == rig.monitor.config.suspect_interval_s


def test_leaving_the_desk_resets_the_window_and_backs_off(rig) -> None:
    rig.run(50, head_forward=0.2)  # almost a full window of bad posture
    away = rig.run_away(40)
    assert all(s.state == mon.AWAY for s in away)
    assert rig.monitor.next_interval(away[-1]) == rig.monitor.config.away_interval_s
    rig.run(40, head_forward=0.2)  # back, but a new window has to fill first
    assert rig.notifier.alerts == []
    rig.run(40, head_forward=0.2)
    assert [label for _, label in rig.notifier.alerts] == [FORWARD_HEAD]


def test_wrong_alert_button_stores_feedback(rig) -> None:
    rig.run(120, head_forward=0.2)
    assert rig.notifier.on_wrong is not None
    rig.notifier.on_wrong()
    alert = rig.db.last_alert()
    assert alert["feedback"] == "wrong"
    assert alert["feedback_ts"] == rig.clock.now


def test_pause_button_releases_the_camera_then_resumes(rig) -> None:
    rig.run(120, head_forward=0.2)
    assert rig.camera.is_open
    rig.notifier.on_pause()
    paused = rig.run(60, head_forward=0.2)
    assert all(s.state == mon.PAUSED for s in paused)
    assert not rig.camera.is_open  # the webcam light is off while paused
    stored_before = len(rig.db.query("SELECT * FROM samples"))

    rig.clock.advance(mon.Monitor.PAUSE_FROM_TOAST_S)
    resumed = rig.run(10)
    assert resumed[0].state == mon.PRESENT
    assert rig.camera.is_open
    assert len(rig.db.query("SELECT * FROM samples")) > stored_before
    assert [r["kind"] for r in rig.db.query("SELECT kind FROM events")] == ["pause"]


def test_manual_resume_ends_a_pause_early(rig) -> None:
    rig.monitor.pause(3600)
    assert rig.monitor.step().state == mon.PAUSED
    rig.monitor.resume()
    assert rig.monitor.step().state == mon.PRESENT


def test_busy_camera_is_reported_and_retried(rig) -> None:
    rig.camera.available = False
    samples = rig.run(30)
    assert all(s.state == mon.CAMERA_UNAVAILABLE for s in samples)
    assert rig.db.query("SELECT * FROM samples") == []
    rig.camera.available = True  # the other app let go of the camera
    assert rig.run(4)[0].state == mon.PRESENT


def test_without_calibration_the_camera_stays_off(db, clock) -> None:
    rig = Rig(db, None, clock)
    samples = rig.run(30)
    assert all(s.state == mon.UNCALIBRATED for s in samples)
    assert rig.camera.opened == 0


def test_recalibration_is_picked_up(rig, calibration) -> None:
    first = rig.monitor.calibration.id
    rig.db.insert_calibration(calibration)
    rig.monitor.reload_calibration()
    assert rig.monitor.calibration.id != first
    rig.run(4)
    row = rig.db.query("SELECT calibration_id FROM samples ORDER BY id DESC LIMIT 1")[0]
    assert row["calibration_id"] == rig.monitor.calibration.id


def test_run_loop_stops_and_releases_the_camera(rig) -> None:
    import threading

    stop = threading.Event()
    seen: list[mon.Sample] = []

    def on_sample(sample: mon.Sample) -> None:
        seen.append(sample)
        if len(seen) == 3:
            stop.set()

    rig.monitor.config.sample_interval_s = 0.01
    rig.monitor.run(stop, on_sample=on_sample)
    assert len(seen) == 3
    assert not rig.camera.is_open
    assert [r["kind"] for r in rig.db.query("SELECT kind FROM events")] == ["start", "stop"]


def test_dark_empty_frame_is_low_confidence_not_away(rig) -> None:
    rig.camera.brightness = 12
    samples = rig.run_away(30)
    assert all(s.state == mon.LOW_CONFIDENCE for s in samples)
    assert all(s.reasons == ("no_person", "too_dark") for s in samples)
    rig.camera.brightness = 120
    assert rig.run_away(10)[0].state == mon.AWAY
