"""The calibration window's controller (behaviour 15), tested without Tk."""

from __future__ import annotations

from conftest import FakeCamera, FakeClock, FakePose
from posture_checker.calibrate_ui import (
    CAMERA_ERROR,
    EXIT_CANCELLED,
    EXIT_FAILED,
    EXIT_OK,
    CalibrationController,
)
from posture_checker.synthetic import make_landmarks


def controller(db, provider=make_landmarks, camera=None, on_sleep=None):
    clock = FakeClock()
    messages: list[str] = []

    def sleep(seconds: float) -> None:
        messages.append(ctl.message)
        if on_sleep is not None:
            on_sleep(ctl)
        clock.advance(seconds)

    ctl = CalibrationController(
        camera or FakeCamera(),
        FakePose(provider),
        db,
        clock=clock,
        sleep=sleep,
        countdown=3,
        duration_s=5.0,
    )
    return ctl, messages


def test_success_saves_calibration_and_reports_it(db) -> None:
    camera = FakeCamera()
    ctl, messages = controller(db, camera=camera)
    assert ctl.run() == EXIT_OK
    assert ctl.done and ctl.exit_code == EXIT_OK
    assert messages[:3] == [
        f"Sit in your good posture and look at the screen. Starting in {n}..." for n in (3, 2, 1)
    ]
    assert any(m.startswith("Hold still... ") and "usable samples" in m for m in messages)
    saved = db.latest_calibration()
    assert saved is not None
    assert ctl.message.startswith(f"Calibration #{saved.id} saved from {saved.n_samples} samples")
    assert ctl.fraction == 1.0
    assert camera.opened == camera.closed == 1


def test_nobody_in_view_fails_with_reason_and_keeps_db_empty(db) -> None:
    ctl, _ = controller(db, provider=lambda: None)
    assert ctl.run() == EXIT_FAILED
    assert ctl.message.startswith("Calibration failed: only 0 usable samples")
    assert db.latest_calibration() is None


def test_camera_unavailable_message(db) -> None:
    ctl, _ = controller(db, camera=FakeCamera(available=False))
    assert ctl.run() == EXIT_CANCELLED
    assert ctl.message == CAMERA_ERROR


def test_cancel_during_capture_stops_and_closes_camera(db) -> None:
    camera = FakeCamera()

    def cancel_when_capturing(ctl):
        if ctl.message.startswith("Hold still"):
            ctl.cancel()

    ctl, _ = controller(db, camera=camera, on_sleep=cancel_when_capturing)
    assert ctl.run() == EXIT_CANCELLED
    assert ctl.message == "Calibration cancelled."
    assert camera.is_open is False
    assert db.latest_calibration() is None


def test_cancel_during_countdown_never_opens_camera(db) -> None:
    camera = FakeCamera()
    ctl, _ = controller(db, camera=camera, on_sleep=lambda c: c.cancel())
    assert ctl.run() == EXIT_CANCELLED
    assert camera.opened == 0


def test_missing_shoulders_adds_a_hint(db) -> None:
    ctl, _ = controller(db, provider=lambda: make_landmarks(shoulder_visibility=0.1))
    assert ctl.run() == EXIT_OK
    assert "shoulders were not clearly visible" in ctl.message
