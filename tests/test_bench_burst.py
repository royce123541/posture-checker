"""scripts/bench_burst.py: the report renders without a camera."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_burst.py"


def load():
    spec = importlib.util.spec_from_file_location("bench_burst", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render_shows_medians_in_ms() -> None:
    bench = load()
    t = bench.Timing("burst")
    t.open_s = [1.0, 1.2, 1.4]
    t.read_s = [0.01, 0.02, 0.03]
    t.pose_s = [0.03]
    t.start_to_start_s = [2.0, 2.4]
    text = bench.render([t, bench.Timing("hold")])
    assert "| burst | 1200 | 20 | 30 | 2200 | 0 |" in text
    assert "| hold | n/a | n/a | n/a | n/a | 0 |" in text


def test_twenty_samples_per_mode() -> None:
    bench = load()
    assert bench.SAMPLES == 20
    assert bench.MODES == ("hold", "burst")


def test_results_file_says_not_measured_until_run() -> None:
    text = (SCRIPT.parents[1] / "docs" / "results" / "burst-timing.md").read_text("utf-8")
    assert "measured" in text.lower()
