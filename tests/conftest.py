from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import numpy as np
import pytest

from posture_checker import features as feat
from posture_checker import paths
from posture_checker.calibration import Calibration, build_calibration
from posture_checker.capture.pose import PoseResult
from posture_checker.storage.db import Database
from posture_checker.synthetic import make_landmarks


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch) -> None:
    """Never let a test touch the real app data folder."""
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "home"))


class _NoRealShortcutWriter:
    """Stand-in for WScriptShortcutWriter under test: writes an empty file only."""

    def create(self, path, target, arguments, working_dir, minimised) -> None:  # type: ignore[no-untyped-def]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


@pytest.fixture(autouse=True)
def isolated_startup(tmp_path, monkeypatch) -> None:
    """No test may read or write the real Startup folder or run powershell.exe."""
    from posture_checker import settings_ui
    from posture_checker.platform import autostart

    appdata = tmp_path / "AppData-Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))  # backstop for anything reading APPDATA
    startup = tmp_path / "TestStartup"
    for module in (autostart, settings_ui):
        monkeypatch.setattr(module, "default_startup_dir", lambda: startup)
        monkeypatch.setattr(module, "WScriptShortcutWriter", _NoRealShortcutWriter)


@pytest.fixture
def hunch_exe(tmp_path, monkeypatch):
    """A fake venv Scripts folder with python.exe and hunch.exe; sys.executable points there."""
    import sys

    scripts = tmp_path / "fake venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")
    exe = scripts / "hunch.exe"
    exe.write_text("")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    return exe


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def baseline() -> dict[str, float]:
    return feat.raw_features(make_landmarks())


@pytest.fixture
def calibration() -> Calibration:
    rng = np.random.default_rng(1)
    raws = [feat.raw_features(make_landmarks(noise=0.001, rng=rng)) for _ in range(20)]
    return build_calibration(raws, [0.95] * 20, ts=0.0)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeCamera:
    """A frame source that hands out blank frames and counts open/close calls."""

    def __init__(self, brightness: int = 120, available: bool = True) -> None:
        self.brightness = brightness
        self.available = available
        self.opened = 0
        self.closed = 0
        self.is_open = False

    def open(self) -> bool:
        if not self.available:
            return False
        self.opened += 1
        self.is_open = True
        return True

    def read(self) -> np.ndarray | None:
        if not self.is_open:
            return None
        # A gradient gives the frame realistic contrast.
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame += (
            np.linspace(self.brightness - 60, self.brightness + 60, 640, dtype=np.float32)
            .clip(0, 255)
            .astype(np.uint8)[None, :, None]
        )
        return frame

    def close(self) -> None:
        self.closed += 1
        self.is_open = False


class FakePose:
    """A pose backend that returns whatever landmarks the test scripted."""

    name = "fake-pose"

    def __init__(self, provider: Callable[[], np.ndarray | None]) -> None:
        self.provider = provider

    def estimate(self, frame_bgr: np.ndarray) -> PoseResult:
        return PoseResult(self.provider(), latency_ms=5.0)

    def close(self) -> None:
        pass


class RecordingNotifier:
    def __init__(self) -> None:
        self.alerts: list[tuple[int, str]] = []
        self.on_wrong: Callable[[], None] | None = None
        self.on_pause: Callable[[], None] | None = None

    def alert(self, alert_id, label, on_wrong, on_pause) -> None:
        self.alerts.append((alert_id, label))
        self.on_wrong = on_wrong
        self.on_pause = on_pause


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


#: Set to 1 to run tests marked ``camera`` (real webcam or Windows hardware).
#: Unset everywhere else, including CI, so those tests are skipped there.
RUN_CAMERA_ENV = "POSTURE_RUN_CAMERA_TESTS"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(RUN_CAMERA_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"needs real hardware; set {RUN_CAMERA_ENV}=1 to run")
    for item in items:
        if item.get_closest_marker("camera") is not None:
            item.add_marker(skip)
