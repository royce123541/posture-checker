"""Live check L1: can another app use the webcam while Posture Checker uses it?

Usage (needs the webcam, the Windows Camera app and a Meet or Zoom preview):

    uv run python scripts/camera_share_probe.py

For each of the two camera modes ("hold": open once and keep reading;
"burst": open, read one frame, close, every 2 s) and each other app, the script
keeps reading in the background while you start the other app, then asks
whether the other app showed a picture. It records whether our reads kept
returning frames and whether the registry probe saw the other app as using
the camera. It prints a table and writes it to docs/results/camera-sharing.md.

Only counts and yes/no answers are kept: frames are dropped as soon as they
are read and nothing about the picture is written anywhere.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULT_FILE = REPO / "docs" / "results" / "camera-sharing.md"
MODES = ("hold", "burst")
OTHER_APPS = ("Windows Camera app", "Meet (browser) or Zoom preview")
#: Seconds between our reads, matching the app's normal sampling interval.
SAMPLE_INTERVAL_S = 2.0
#: Seconds to keep reading after the other app is started, so several samples
#: fall in the period when both want the camera.
OBSERVE_S = 20.0


class Trial:
    """One mode x other-app run. A plain class (not a dataclass) so the script
    can be loaded with ``importlib`` without being registered in sys.modules."""

    def __init__(self, mode: str, other_app: str) -> None:
        self.mode = mode
        self.other_app = other_app
        self.reads_ok = 0
        self.reads_failed = 0
        self.registry_apps: set[str] = set()
        self.other_app_picture = "not asked"

    @property
    def our_reads(self) -> str:
        total = self.reads_ok + self.reads_failed
        return f"{self.reads_ok}/{total}"


def _sampler(mode: str, trial: Trial, stop: threading.Event) -> None:
    from posture_checker.capture.camera import Camera
    from posture_checker.config import load_config
    from posture_checker.platform.camera_usage import RegistryCameraUsage, WinRegistry

    config = load_config()
    camera = Camera(config.camera_index, config.frame_width, config.frame_height)
    usage = RegistryCameraUsage(WinRegistry())
    try:
        while not stop.is_set():
            if not camera.is_open:
                camera.open()
            frame = camera.read()
            ok = frame is not None
            del frame  # never kept
            if ok:
                trial.reads_ok += 1
            else:
                trial.reads_failed += 1
            trial.registry_apps.update(usage.apps_in_use())
            if mode == "burst":
                camera.close()
            stop.wait(SAMPLE_INTERVAL_S)
    finally:
        camera.close()


def _ask(question: str) -> str:
    while True:
        answer = input(f"{question} [y/n] ").strip().lower()
        if answer in ("y", "n"):
            return "yes" if answer == "y" else "no"


def run_trial(mode: str, other_app: str) -> Trial:
    trial = Trial(mode, other_app)
    input(f"\n[{mode}] Close the {other_app} if it is open, then press Enter to start reading.")
    stop = threading.Event()
    thread = threading.Thread(target=_sampler, args=(mode, trial, stop), daemon=True)
    thread.start()
    input(f"[{mode}] Now open the {other_app} and start its camera preview, then press Enter.")
    print(f"[{mode}] Observing for {OBSERVE_S:.0f} s...")
    time.sleep(OBSERVE_S)
    trial.other_app_picture = _ask(f"[{mode}] Did the {other_app} show a live picture?")
    stop.set()
    thread.join(timeout=10)
    input(f"[{mode}] Close the {other_app}, then press Enter.")
    return trial


def render(trials: list[Trial]) -> str:
    lines = [
        "# Camera sharing probe (live check L1)",
        "",
        f"Run on {dt.date.today().isoformat()} with `scripts/camera_share_probe.py`.",
        "Text only: counts and yes/no answers, no frames.",
        "",
        "| Mode | Other app | Other app got a picture | Our reads OK | Registry saw in use |",
        "|---|---|---|---|---|",
    ]
    for t in trials:
        seen = ", ".join(sorted(t.registry_apps)) or "nothing"
        lines.append(
            f"| {t.mode} | {t.other_app} | {t.other_app_picture} | {t.our_reads} | {seen} |"
        )
    lines += [
        "",
        "Decision 1: if any `hold` row says the other app got no picture, set the default",
        '`camera_mode` to `"burst"`.',
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-write", action="store_true", help="print only")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("This probe needs Windows (DirectShow and the registry).")
        return 1
    print("This takes about 5 minutes. Keep yourself in front of the camera.")
    trials = [run_trial(mode, app) for mode in MODES for app in OTHER_APPS]
    table = render(trials)
    print("\n" + table)
    if not args.no_write:
        RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESULT_FILE.write_text(table, encoding="utf-8")
        print(f"Written to {RESULT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
