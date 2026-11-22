"""Guided recording: collect labelled posture measurements.

The app prompts the user through a fixed sequence of postures ("sit normally",
"slouch", ...) and stores each sample's measurements with the prompted label.
Only numbers are stored, never images. The labelled samples are used to fit
the rule thresholds and, from Stage 4, to train and evaluate models.

Each prompt starts with a short settling period whose samples are discarded,
because the user is still moving into the posture.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from posture_checker import features as feat
from posture_checker.calibration import Calibration
from posture_checker.capture import quality as qual
from posture_checker.capture.camera import FrameSource
from posture_checker.capture.pose import PoseBackend
from posture_checker.inference import FORWARD_HEAD, GOOD, SLOUCH, TOO_CLOSE
from posture_checker.storage.db import Database


@dataclass(frozen=True)
class Step:
    label: str
    instruction: str
    seconds: float


#: The default session. "Sit normally" comes first and last so that the good
#: class is spread over time, like the other classes are in real use.
DEFAULT_STEPS: tuple[Step, ...] = (
    Step(GOOD, "Sit the way you normally sit when your posture is good.", 25),
    Step(SLOUCH, "Slouch: let your back round and your head sink.", 25),
    Step(FORWARD_HEAD, "Push your head forward towards the screen, back still upright.", 25),
    Step(TOO_CLOSE, "Lean your whole body in close to the screen.", 25),
    Step(GOOD, "Sit normally with good posture again.", 25),
)

#: Seconds at the start of each step whose samples are discarded while the
#: user moves into the posture.
SETTLE_S = 4.0
#: Samples below this confidence are not stored as labelled data.
MIN_CONFIDENCE = 0.6


@dataclass(frozen=True)
class RecordingSummary:
    session: str
    counts: dict[str, int]
    skipped_low_confidence: int


def run_recording(
    source: FrameSource,
    pose: PoseBackend,
    db: Database,
    calibration: Calibration,
    steps: Sequence[Step] = DEFAULT_STEPS,
    interval_s: float = 0.5,
    settle_s: float = SETTLE_S,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    on_step: Callable[[int, Step], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> RecordingSummary:
    """Run a guided session. The camera must already be open."""
    session = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    counts: dict[str, int] = {}
    skipped = 0
    for index, step in enumerate(steps):
        if on_step is not None:
            on_step(index, step)
        start = clock()
        while (elapsed := clock() - start) < step.seconds:
            if on_progress is not None:
                on_progress(min(elapsed / step.seconds, 1.0))
            settling = elapsed < settle_s
            frame = source.read()
            if frame is not None and not settling:
                height, width = frame.shape[:2]
                brightness, contrast = qual.frame_stats(frame)
                result = pose.estimate(frame)
                del frame  # only numbers continue from here
                if result.landmarks is None:
                    skipped += 1
                else:
                    raw = feat.raw_features(result.landmarks, aspect=width / height)
                    relative = feat.relative_features(raw, calibration.baseline)
                    quality = qual.assess(
                        result.landmarks,
                        brightness,
                        contrast,
                        yaw_asymmetry=raw["yaw_asymmetry"],
                        shoulders_ok=relative["shoulders_ok"] >= 0.5,
                    )
                    if quality.confidence < MIN_CONFIDENCE:
                        skipped += 1
                    else:
                        db.insert_recorded_sample(
                            session=session,
                            ts=clock(),
                            step=index,
                            label=step.label,
                            confidence=quality.confidence,
                            raw=raw,
                            features=relative,
                            landmarks=feat.upper_body(result.landmarks),
                            calibration_id=calibration.id,
                        )
                        counts[step.label] = counts.get(step.label, 0) + 1
            sleep(interval_s)
    db.log_event(clock(), "recording", session)
    return RecordingSummary(session=session, counts=counts, skipped_low_confidence=skipped)
