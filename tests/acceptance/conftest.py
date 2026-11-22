"""Fakes for the Stage 2 acceptance tests.

Imports of Stage 2 modules are done lazily inside functions so that a missing
module fails the individual test, not the collection of the whole suite.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

from conftest import FakeCamera, FakeClock, FakePose, RecordingNotifier
from posture_checker.config import Config
from posture_checker.inference.rules import RulesModel
from posture_checker.synthetic import make_landmarks

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUMID_KEY = r"Software\Classes\AppUserModelId\PostureChecker.App"
WEBCAM_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"
)


class FakeCameraUsage:
    """Camera-in-use probe returning whatever apps the test scripts."""

    def __init__(self) -> None:
        self.apps: list[str] = []

    def apps_in_use(self) -> list[str]:
        return list(self.apps)


class FakeWindowLister:
    """Window lister returning whatever the test sets; counts calls.

    Set ``error`` to make ``windows()`` raise. ``add(title, process)`` builds
    WindowInfo lazily so a missing module fails the test, not collection.
    """

    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self.calls = 0
        self.error: Exception | None = None

    def add(self, title: str, process: str) -> None:
        self.items.append((title, process))

    def clear(self) -> None:
        self.items = []

    def windows(self) -> list[Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        info = import_module("posture_checker.platform.windows_list").WindowInfo
        return [info(title=t, process=p) for t, p in self.items]


class FakeLockState:
    def __init__(self) -> None:
        self.locked = False

    def is_locked(self) -> bool:
        return self.locked


class FakeBattery:
    """``percent=None`` means the machine has no battery."""

    def __init__(self) -> None:
        self.percent: int | None = None
        self.plugged = True
        self.saver = False

    def set(self, percent: int | None, plugged: bool, saver: bool = False) -> None:
        self.percent, self.plugged, self.saver = percent, plugged, saver

    def status(self) -> Any:
        status_cls = import_module("posture_checker.policy.pausing").BatteryStatus
        return status_cls(percent=self.percent, plugged=self.plugged, saver=self.saver)


class FakeRegistry:
    """In-memory registry: key path -> {value name: value}.

    Methods raise FileNotFoundError for a missing key, like winreg does.
    """

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, Any]] = {}

    def subkeys(self, path: str) -> list[str]:
        if path not in self.keys:
            raise FileNotFoundError(path)
        prefix = path + "\\"
        names = {k[len(prefix) :].split("\\")[0] for k in self.keys if k.startswith(prefix)}
        return sorted(names)

    def values(self, path: str) -> dict[str, Any]:
        if path not in self.keys:
            raise FileNotFoundError(path)
        return dict(self.keys[path])

    def set_value(self, path: str, name: str, value: Any) -> None:
        self.keys.setdefault(path, {})[name] = value

    def delete_value(self, path: str, name: str) -> None:
        if name not in self.keys.get(path, {}):
            raise FileNotFoundError(name)
        del self.keys[path][name]

    def delete_key(self, path: str) -> None:
        if path not in self.keys:
            raise FileNotFoundError(path)
        for k in [k for k in self.keys if k == path or k.startswith(path + "\\")]:
            del self.keys[k]

    # Test helper -----------------------------------------------------------
    def add_app(self, subkey: str, start: int, stop: int) -> None:
        r"""Add a ConsentStore entry, e.g. ``NonPackaged\C:#x#Zoom.exe``."""
        self.keys.setdefault(WEBCAM_KEY, {})
        parts = subkey.split("\\")
        for i in range(1, len(parts) + 1):
            self.keys.setdefault(WEBCAM_KEY + "\\" + "\\".join(parts[:i]), {})
        self.keys[WEBCAM_KEY + "\\" + subkey] = {
            "LastUsedTimeStart": start,
            "LastUsedTimeStop": stop,
        }

    def run_values(self) -> dict[str, Any]:
        return dict(self.keys.get(RUN_KEY, {}))


class RecordingMessageNotifier(RecordingNotifier):
    """RecordingNotifier plus the plain-message channel Stage 2 adds."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def message(self, text: str, on_action: Any = None) -> None:
        self.messages.append(text)


class FakeProcess:
    def __init__(self, exit_code: int, on_exit: Any = None) -> None:
        self.exit_code = exit_code
        self.on_exit = on_exit

    def wait(self) -> int:
        if self.on_exit is not None:
            self.on_exit()
        return self.exit_code


class FakeLauncher:
    """Records command lines; optionally runs ``inspect`` at launch time."""

    def __init__(self, exit_code: int = 0, on_exit: Any = None, inspect: Any = None) -> None:
        self.commands: list[list[str]] = []
        self.exit_code = exit_code
        self.on_exit = on_exit
        self.inspect = inspect

    def __call__(self, command: list[str]) -> FakeProcess:
        self.commands.append(list(command))
        if self.inspect is not None:
            self.inspect()
        return FakeProcess(self.exit_code, self.on_exit)


class FakeMutexBackend:
    """Named-mutex stand-in: ``try_create`` is True only for the first caller."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def try_create(self, name: str) -> bool:
        if name in self.names:
            return False
        self.names.add(name)
        return True


class Rig:
    """A Stage 2 monitor wired to fakes, with the pose controlled by the test."""

    def __init__(self, db, calibration, clock: FakeClock, *, calls: Any = None, **config) -> None:
        pausing = import_module("posture_checker.policy.pausing")
        mon = import_module("posture_checker.monitor")
        self.clock = clock
        self.db = db
        self.camera = FakeCamera()
        self.notifier = RecordingMessageNotifier()
        self.usage = FakeCameraUsage()
        self.lock = FakeLockState()
        self.battery = FakeBattery()
        self.landmarks: dict | None = {}
        if calibration is not None:
            db.insert_calibration(calibration)
        self.config = Config(**{"alert_window_s": 60, "cooldown_s": 300, **config})
        extra: dict[str, Any] = {}
        if calls is not None:
            extra = {"calls": calls, "pause_in_calls": self.config.pause_in_calls}
        self.supervisor = pausing.PauseSupervisor(
            camera_usage=self.usage,
            lock_state=self.lock,
            battery=self.battery,
            clock=clock,
            battery_pause_percent=self.config.battery_pause_percent,
            **extra,
        )
        self.monitor = mon.Monitor(
            config=self.config,
            db=db,
            source=self.camera,
            pose=FakePose(self._landmarks),
            model=RulesModel(),
            notifier=self.notifier,
            clock=clock,
            supervisor=self.supervisor,
        )

    def _landmarks(self):
        return None if self.landmarks is None else make_landmarks(**self.landmarks)

    def tick(self, seconds: float = 1.0):
        """Advance fake time, then take one sample."""
        self.clock.advance(seconds)
        return self.monitor.step()

    def run_for(self, seconds: float, step_s: float = 1.0) -> list:
        return [self.tick(step_s) for _ in range(int(seconds / step_s))]


@pytest.fixture
def make_rig(db, calibration, clock):
    def factory(**config) -> Rig:
        return Rig(db, calibration, clock, **config)

    return factory


@pytest.fixture
def lister() -> FakeWindowLister:
    return FakeWindowLister()


@pytest.fixture
def make_call_rig(db, calibration, clock, lister):
    """Rig whose supervisor has a ``calls`` probe (a CallDetector on ``lister``)."""

    def factory(**config) -> Rig:
        detector = import_module("posture_checker.policy.calls").CallDetector(lister)
        return Rig(db, calibration, clock, calls=detector, **config)

    return factory


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


class FakeShortcutWriter:
    """Stand-in for the WScript.Shell shortcut writer.

    ``create`` records its arguments and writes an empty file at ``path`` so
    "the shortcut exists" can be checked on a temporary Startup folder.
    """

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create(
        self, path: Any, target: Any, arguments: str, working_dir: Any, minimised: bool
    ) -> None:
        self.created.append(
            {
                "path": path,
                "target": target,
                "arguments": arguments,
                "working_dir": working_dir,
                "minimised": minimised,
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fake shortcut", encoding="utf-8")


@pytest.fixture
def shortcut_writer() -> FakeShortcutWriter:
    return FakeShortcutWriter()


@pytest.fixture
def startup_dir(tmp_path):
    folder = tmp_path / "Startup"
    folder.mkdir()
    return folder


class RecordingStop:
    """Stop-event stand-in for ``Monitor.run``: records each wait, stops after N."""

    def __init__(self, waits_allowed: int) -> None:
        self.waits: list[float] = []
        self.waits_allowed = waits_allowed

    def is_set(self) -> bool:
        return len(self.waits) >= self.waits_allowed

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(float(timeout or 0.0))
        return self.is_set()
