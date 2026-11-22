"""The monitoring loop: frame -> pose -> features -> model -> alert policy.

Every collaborator (camera, pose model, posture model, notifier, clock) is
injected, so the whole pipeline runs in tests with fakes and no webcam.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from posture_checker import features as feat
from posture_checker.calibration import Calibration
from posture_checker.capture import quality as qual
from posture_checker.capture.camera import FrameSource
from posture_checker.capture.pose import PoseBackend
from posture_checker.config import Config
from posture_checker.inference import PostureModel, Prediction
from posture_checker.policy.alerts import Alert, AlertPolicy
from posture_checker.storage.db import Database

# Sample states
PRESENT = "present"
LOW_CONFIDENCE = "low_confidence"
AWAY = "away"
PAUSED = "paused"
UNCALIBRATED = "uncalibrated"
CAMERA_UNAVAILABLE = "camera_unavailable"

#: States that are written to the samples table.
STORED_STATES = (PRESENT, LOW_CONFIDENCE, AWAY)


class Notifier(Protocol):
    def alert(
        self,
        alert_id: int,
        label: str,
        on_wrong: Callable[[], None],
        on_pause: Callable[[], None],
    ) -> None: ...


@dataclass(frozen=True)
class Sample:
    ts: float
    state: str
    confidence: float = 0.0
    reasons: tuple[str, ...] = ()
    prediction: Prediction | None = None
    relative: dict[str, float] = field(default_factory=dict)
    latency_ms: float | None = None
    alert: Alert | None = None
    alert_id: int | None = None

    @property
    def label(self) -> str | None:
        return self.prediction.label if self.prediction else None


class Monitor:
    PAUSE_FROM_TOAST_S = 30 * 60

    def __init__(
        self,
        config: Config,
        db: Database,
        source: FrameSource,
        pose: PoseBackend,
        model: PostureModel,
        notifier: Notifier,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.db = db
        self.source = source
        self.pose = pose
        self.model = model
        self.notifier = notifier
        self.clock = clock
        self.policy = AlertPolicy(
            window_s=config.alert_window_s,
            bad_fraction=config.bad_fraction,
            cooldown_s=config.cooldown_s,
        )
        self.calibration: Calibration | None = db.latest_calibration()
        self._paused_until = 0.0
        self._camera_open = False

    # --- pause / resume -----------------------------------------------------
    def pause(self, seconds: float, reason: str = "manual") -> None:
        now = self.clock()
        self._paused_until = now + seconds
        self.db.log_event(now, "pause", f"{reason}:{int(seconds)}s")

    def resume(self) -> None:
        if self._paused_until > 0:
            self._paused_until = 0.0
            self.db.log_event(self.clock(), "resume", "manual")

    @property
    def paused(self) -> bool:
        return self.clock() < self._paused_until

    def reload_calibration(self) -> None:
        self.calibration = self.db.latest_calibration()
        self.policy.reset()

    # --- one sample ---------------------------------------------------------
    def _release_camera(self) -> None:
        if self._camera_open:
            self.source.close()
            self._camera_open = False

    def step(self) -> Sample:
        """Take and process one sample."""
        now = self.clock()

        if self.paused:
            self._release_camera()
            self.policy.reset()
            return Sample(ts=now, state=PAUSED)
        if self.calibration is None:
            self._release_camera()
            return Sample(ts=now, state=UNCALIBRATED)

        if not self._camera_open:
            self._camera_open = self.source.open()
        frame = self.source.read() if self._camera_open else None
        if frame is None:
            # Missing, busy (another app has it) or failed: let go and retry later.
            self._release_camera()
            self.policy.reset()
            return Sample(ts=now, state=CAMERA_UNAVAILABLE)

        height, width = frame.shape[:2]
        brightness, contrast = qual.frame_stats(frame)
        result = self.pose.estimate(frame)
        del frame  # the image is gone here; only numbers continue down the pipeline

        if result.landmarks is None:
            quality = qual.assess(None, brightness, contrast)
            # "no_person" alone means the desk is empty; with a lighting flag it
            # means the camera could not have seen anyone.
            state = AWAY if quality.reasons == ("no_person",) else LOW_CONFIDENCE
            self.policy.reset()
            self.db.insert_sample(
                ts=now,
                state=state,
                confidence=0.0,
                reasons=quality.reasons,
                brightness=brightness,
                pose_model=self.pose.name,
                calibration_id=self.calibration.id,
                latency_ms=result.latency_ms,
            )
            return Sample(
                ts=now, state=state, reasons=quality.reasons, latency_ms=result.latency_ms
            )

        raw = feat.raw_features(result.landmarks, aspect=width / height)
        relative = feat.relative_features(raw, self.calibration.baseline)
        quality = qual.assess(
            result.landmarks,
            brightness,
            contrast,
            yaw_asymmetry=raw["yaw_asymmetry"],
            shoulders_ok=relative["shoulders_ok"] >= 0.5,
        )
        prediction = self.model.predict(relative)
        confident = quality.confidence >= self.config.min_confidence
        state = PRESENT if confident else LOW_CONFIDENCE

        alert = self.policy.update(now, prediction.label, confident)
        alert_id = None
        if alert is not None:
            alert_id = self.db.insert_alert(
                ts=alert.ts,
                label=alert.label,
                bad_fraction=alert.bad_fraction,
                window_s=alert.window_s,
                model_version=prediction.model_version,
                calibration_id=self.calibration.id,
            )
            self._notify(alert_id, alert.label)

        self.db.insert_sample(
            ts=now,
            state=state,
            label=prediction.label,
            confidence=quality.confidence,
            reasons=quality.reasons,
            scores=prediction.scores,
            features=relative,
            landmarks=feat.upper_body(result.landmarks),
            brightness=brightness,
            model_version=prediction.model_version,
            pose_model=self.pose.name,
            calibration_id=self.calibration.id,
            latency_ms=result.latency_ms,
        )
        return Sample(
            ts=now,
            state=state,
            confidence=quality.confidence,
            reasons=quality.reasons,
            prediction=prediction,
            relative=relative,
            latency_ms=result.latency_ms,
            alert=alert,
            alert_id=alert_id,
        )

    def _notify(self, alert_id: int, label: str) -> None:
        def on_wrong() -> None:
            self.db.mark_alert_wrong(alert_id, self.clock())

        def on_pause() -> None:
            self.pause(self.PAUSE_FROM_TOAST_S, reason="toast")

        self.notifier.alert(alert_id, label, on_wrong, on_pause)

    # --- loop ---------------------------------------------------------------
    def next_interval(self, sample: Sample) -> float:
        """How long to wait before the next sample."""
        if sample.state in (AWAY, CAMERA_UNAVAILABLE, PAUSED, UNCALIBRATED):
            return self.config.away_interval_s
        if self.policy.suspect:
            return self.config.suspect_interval_s
        return self.config.sample_interval_s

    def run(
        self,
        stop: threading.Event,
        on_sample: Callable[[Sample], None] | None = None,
    ) -> None:
        """Sample until ``stop`` is set."""
        self.db.log_event(self.clock(), "start", self.model.version)
        try:
            while not stop.is_set():
                sample = self.step()
                if on_sample is not None:
                    on_sample(sample)
                stop.wait(self.next_interval(sample))
        finally:
            self._release_camera()
            self.db.log_event(self.clock(), "stop")
