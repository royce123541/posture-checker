"""The monitoring loop: frame -> pose -> features -> model -> alert policy.

Every collaborator (camera, pose model, posture model, notifier, clock, pause
supervisor) is injected, so the whole pipeline runs in tests with fakes and no
webcam.
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
from posture_checker.policy.pausing import MANUAL, PauseSupervisor
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

#: After this many seconds of consecutive "away" samples, switch to
#: presence-check mode (spec Stage 2, behaviour 9).
PRESENCE_CHECK_AFTER_S = 5 * 60
#: Sampling interval in presence-check mode (spec Stage 2, behaviour 9).
PRESENCE_CHECK_INTERVAL_S = 30.0
#: While paused, re-check the pause reasons at least this often (spec Stage 2,
#: behaviour 3).
PAUSED_CHECK_INTERVAL_S = 5.0


class Recorder(Protocol):
    """Takes every sample for the per-minute metrics (Stage 3, behaviour 3)."""

    def observe(self, sample: Sample) -> None: ...

    def poll(self) -> None: ...


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
        supervisor: PauseSupervisor | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.source = source
        self.pose = pose
        self.model = model
        self.notifier = notifier
        self.clock = clock
        self.supervisor = supervisor
        self.recorder = recorder
        self.policy = AlertPolicy(
            window_s=config.alert_window_s,
            bad_fraction=config.bad_fraction,
            cooldown_s=config.cooldown_s,
        )
        self.calibration: Calibration | None = db.latest_calibration()
        self._paused_until = 0.0
        self._camera_open = False
        #: Pause reasons active at the last check, so each change is logged once.
        self._active_reasons: set[str] = set()
        #: Start of the current run of consecutive "away" samples.
        self._away_since: float | None = None
        #: State of the latest sample (the tray shows "camera unavailable").
        self.last_state: str | None = None
        #: Serialises sampling with calls from other threads (tray actions).
        self._lock = threading.RLock()

    # --- pause / resume -----------------------------------------------------
    def pause(self, seconds: float, reason: str = "manual") -> None:
        if self.supervisor is not None:
            # The supervisor owns pausing; check_pause() logs the transition.
            self.supervisor.pause_manual(seconds)
            return
        now = self.clock()
        self._paused_until = now + seconds
        self.db.log_event(now, "pause", f"{reason}:{int(seconds)}s")

    def resume(self) -> None:
        if self.supervisor is not None:
            self.supervisor.resume_manual()
            return
        if self._paused_until > 0:
            self._paused_until = 0.0
            self.db.log_event(self.clock(), "resume", "manual")

    @property
    def paused(self) -> bool:
        return self.clock() < self._paused_until

    def check_pause(self) -> list[str]:
        """Return the active pause reasons, logging one event per change.

        With a supervisor, every reason that appears writes one ``pause`` row
        and every reason that clears writes one ``resume`` row (behaviour 10).
        The camera is released as soon as any reason is active.
        """
        with self._lock:
            if self.supervisor is None:
                return [MANUAL] if self.paused else []
            reasons = self.supervisor.check().reasons
            now = self.clock()
            for reason in reasons:
                if reason not in self._active_reasons:
                    self.db.log_event(now, "pause", reason)
            for reason in sorted(self._active_reasons - set(reasons)):
                self.db.log_event(now, "resume", reason)
            self._active_reasons = set(reasons)
            if reasons:
                self.release_camera()
                self.policy.reset()
            return reasons

    def reload_calibration(self) -> None:
        with self._lock:
            self.calibration = self.db.latest_calibration()
            self.policy.reset()

    def apply_config(self, config: Config) -> None:
        """Use new settings without a restart (behaviour 16)."""
        with self._lock:
            self.config = config
            self.policy.window_s = config.alert_window_s
            self.policy.bad_fraction = config.bad_fraction
            self.policy.cooldown_s = config.cooldown_s
            if hasattr(self.model, "sensitivity"):
                self.model.sensitivity = config.sensitivity
            if self.supervisor is not None:
                self.supervisor.battery_pause_percent = config.battery_pause_percent
                self.supervisor.pause_in_calls = config.pause_in_calls

    @property
    def presence_check(self) -> bool:
        """True after 5 minutes of nobody at the desk (behaviour 9)."""
        return (
            self._away_since is not None
            and self.clock() - self._away_since >= PRESENCE_CHECK_AFTER_S
        )

    # --- one sample ---------------------------------------------------------
    def release_camera(self) -> None:
        """Close the camera if it is open. Safe to call from any thread."""
        with self._lock:
            if self._camera_open:
                self.source.close()
                self._camera_open = False

    def step(self) -> Sample:
        """Take and process one sample."""
        with self._lock:
            sample = self._step()
            self.last_state = sample.state
            # Burst mode, and presence-check mode whatever the camera mode, let
            # go of the camera between samples.
            if self.config.camera_mode == "burst" or self.presence_check:
                self.release_camera()
            if self.recorder is not None:
                # Every sample, paused ones too, so paused minutes get a row.
                self.recorder.observe(sample)
                self.recorder.poll()
            return sample

    def _track_away(self, state: str, now: float) -> None:
        if state == AWAY:
            if self._away_since is None:
                self._away_since = now
        else:
            self._away_since = None

    def _step(self) -> Sample:
        now = self.clock()

        if self.check_pause():
            self.release_camera()
            self.policy.reset()
            return Sample(ts=now, state=PAUSED)
        if self.calibration is None:
            self.release_camera()
            return Sample(ts=now, state=UNCALIBRATED)

        if not self._camera_open:
            self._camera_open = self.source.open()
        frame = self.source.read() if self._camera_open else None
        if frame is None:
            # Missing, busy (another app has it) or failed: let go and retry later.
            self.release_camera()
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
            self._track_away(state, now)
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
        self._track_away(state, now)

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
        if sample.state == PAUSED:
            return min(self.config.away_interval_s, PAUSED_CHECK_INTERVAL_S)
        if sample.state == AWAY and self.presence_check:
            return PRESENCE_CHECK_INTERVAL_S
        if sample.state in (AWAY, CAMERA_UNAVAILABLE, UNCALIBRATED):
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
                started = self.clock()
                sample = self.step()
                if on_sample is not None:
                    on_sample(sample)
                # Fixes F3: count the sample's own time against the interval, so the
                # interval is start-to-start; never a negative wait.
                took = self.clock() - started
                stop.wait(max(0.0, self.next_interval(sample) - took))
        finally:
            self.release_camera()
            self.db.log_event(self.clock(), "stop")
