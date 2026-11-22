"""Live check F3: how long each part of a sample takes, per camera mode.

Usage (needs the webcam and the pose model):

    uv run python scripts/bench_burst.py

For each camera mode ("hold": open once and keep reading; "burst": open, read
one frame, close, for every sample) the script takes SAMPLES samples the way the
app does, waiting ``interval - time the sample took`` between them (spec Stage 2
fixes, F3). It records the camera open time, read time, pose time and the
start-to-start interval of each sample, prints the medians and writes them to
docs/results/burst-timing.md.

Only timings are kept: each frame is dropped as soon as the pose model has
seen it and nothing about the picture is written anywhere.
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULT_FILE = REPO / "docs" / "results" / "burst-timing.md"
MODES = ("hold", "burst")
#: Samples per camera mode, as the spec asks (F3).
SAMPLES = 20
#: Seconds from one sample's start to the next: the default sample_interval_s.
INTERVAL_S = 2.0


class Timing:
    """Per-sample timings (seconds) for one mode. A plain class so the script
    can be loaded with ``importlib`` without being registered in sys.modules."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.open_s: list[float] = []
        self.read_s: list[float] = []
        self.pose_s: list[float] = []
        self.start_to_start_s: list[float] = []
        self.failed_reads = 0


def median_ms(values: list[float]) -> str:
    return f"{statistics.median(values) * 1000:.0f}" if values else "n/a"


def measure(mode: str, samples: int = SAMPLES, interval_s: float = INTERVAL_S) -> Timing:
    from posture_checker.capture.camera import Camera
    from posture_checker.capture.pose import PoseEstimator
    from posture_checker.config import load_config

    config = load_config()
    camera = Camera(config.camera_index, config.frame_width, config.frame_height)
    pose = PoseEstimator()
    timing = Timing(mode)
    previous_start: float | None = None
    try:
        for _ in range(samples):
            start = time.perf_counter()
            if previous_start is not None:
                timing.start_to_start_s.append(start - previous_start)
            previous_start = start
            if not camera.is_open:
                camera.open()
                timing.open_s.append(time.perf_counter() - start)
            read_start = time.perf_counter()
            frame = camera.read()
            timing.read_s.append(time.perf_counter() - read_start)
            if frame is None:
                timing.failed_reads += 1
            else:
                pose_start = time.perf_counter()
                pose.estimate(frame)
                timing.pose_s.append(time.perf_counter() - pose_start)
            del frame  # never kept
            if mode == "burst":
                camera.close()
            took = time.perf_counter() - start
            time.sleep(max(0.0, interval_s - took))
    finally:
        camera.close()
        pose.close()
    return timing


def render(timings: list[Timing], interval_s: float = INTERVAL_S) -> str:
    today = dt.date.today().isoformat()
    lines = [
        "# Burst timing (Stage 2 fixes, F3)",
        "",
        f"Measured by `scripts/bench_burst.py` on {today}: {SAMPLES} samples per camera",
        f"mode, interval {interval_s:g} s, wait = interval minus the sample's time.",
        "Medians in milliseconds. In hold mode the camera opens once, so its open",
        "time is a single value.",
        "",
        "| Mode | Camera open | Read | Pose | Start-to-start | Failed reads |",
        "|---|---|---|---|---|---|",
    ]
    for t in timings:
        lines.append(
            f"| {t.mode} | {median_ms(t.open_s)} | {median_ms(t.read_s)} | "
            f"{median_ms(t.pose_s)} | {median_ms(t.start_to_start_s)} | {t.failed_reads} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--no-write", action="store_true", help="print only")
    args = parser.parse_args(argv)
    timings = []
    for mode in MODES:
        print(f"Measuring {mode} mode ({SAMPLES} samples, about {SAMPLES * INTERVAL_S:.0f} s)...")
        timings.append(measure(mode))
    text = render(timings)
    print(text)
    if not args.no_write:
        RESULT_FILE.write_text(text, encoding="utf-8")
        print(f"Written to {RESULT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
