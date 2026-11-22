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
def hunch_exe(tmp_path, monkeypatch):
    """A fake interpreter folder holding python.exe and hunch.exe; sys.executable points there.

    Autostart looks for ``hunch.exe`` next to the running interpreter, so tests
    that enable autostart use this fixture. Returns the hunch.exe path.
    """
    import sys

    scripts = tmp_path / "fake-venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")
    exe = scripts / "hunch.exe"
    exe.write_text("")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    return exe


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


# --- Stage 3: summaries and monitoring ---------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: slow test (runs in CI)")


class FakeSystemProbe:
    """Scripted CPU, RSS and battery readings for the metrics recorder.

    Set ``cpu_error`` / ``rss_error`` / ``battery_error`` to make that reading raise.
    """

    def __init__(self, cpu: float | None = 12.5, rss_mb: float = 180.0) -> None:
        self.cpu = cpu
        self.rss = rss_mb
        self.battery_status: Any = None  # BatteryStatus; None = no battery
        self.cpu_error: Exception | None = None
        self.rss_error: Exception | None = None
        self.battery_error: Exception | None = None

    def set_battery(self, percent: int | None, plugged: bool) -> None:
        status_cls = import_module("posture_checker.policy.pausing").BatteryStatus
        self.battery_status = status_cls(percent=percent, plugged=plugged, saver=False)

    def cpu_percent(self) -> float | None:
        if self.cpu_error is not None:
            raise self.cpu_error
        return self.cpu

    def rss_mb(self) -> float:
        if self.rss_error is not None:
            raise self.rss_error
        return self.rss

    def battery(self) -> Any:
        if self.battery_error is not None:
            raise self.battery_error
        if self.battery_status is None:
            self.set_battery(None, True)
        return self.battery_status


class ScriptedPose:
    """Pose backend that plays a script of ``(kind, latency_ms)``, then a default.

    kind is "present" (confident good posture), "low" (face barely visible, so
    low confidence) or "away" (no landmarks).
    """

    name = "scripted-pose"

    def __init__(self, script: list[tuple[str, float]] | None = None) -> None:
        self.script = list(script or [])
        self.default: tuple[str, float] = ("present", 5.0)

    def estimate(self, frame_bgr: Any) -> Any:
        from posture_checker.capture.pose import PoseResult

        kind, latency = self.script.pop(0) if self.script else self.default
        if kind == "away":
            return PoseResult(None, latency_ms=latency)
        if kind == "low":
            return PoseResult(make_landmarks(face_visibility=0.3), latency_ms=latency)
        return PoseResult(make_landmarks(), latency_ms=latency)

    def close(self) -> None:
        pass


class MetricsRig:
    """A Stage 2 Rig whose Monitor also feeds a MetricsRecorder (all fakes)."""

    def __init__(self, rig: Rig, recorder: Any, probe: FakeSystemProbe, pose: ScriptedPose) -> None:
        self.rig = rig
        self.recorder = recorder
        self.probe = probe
        self.pose = pose
        self.db = rig.db
        self.clock = rig.clock

    def drive(self, minutes: int) -> None:
        """30 samples a minute at odd seconds (never on a minute boundary).

        Ends with one extra sample just after the last boundary, which is what
        makes the recorder write the last full row.
        """
        self.clock.advance(1.0)
        for _ in range(30 * minutes):
            self.rig.monitor.step()
            self.clock.advance(2.0)
        self.rig.monitor.step()

    def rows(self) -> list[Any]:
        return self.db.query("SELECT * FROM metrics ORDER BY ts, rowid")


def make_recorder(
    db: Any, config: Config, probe: Any, clock: Any, rig: Rig | None = None, **kw: Any
) -> Any:
    metrics = import_module("posture_checker.metrics")
    extra: dict[str, Any] = {}
    if rig is not None:
        extra = {
            "paused_reasons": lambda: tuple(rig.supervisor.reasons),
            "model_version": lambda: rig.monitor.model.version,
            "calibration_id": lambda: (
                rig.monitor.calibration.id if rig.monitor.calibration else None
            ),
        }
    return metrics.MetricsRecorder(
        db=db, config=config, probe=probe, clock=clock, **{**extra, **kw}
    )


@pytest.fixture
def make_metrics_rig(db, calibration, clock):
    def factory(script: list[tuple[str, float]] | None = None, timer: Any = None, **config):
        mon = import_module("posture_checker.monitor")
        rig = Rig(db, calibration, clock, **config)
        probe = FakeSystemProbe()
        pose = ScriptedPose(script)
        kw: dict[str, Any] = {} if timer is None else {"timer": timer}
        recorder = make_recorder(db, rig.config, probe, clock, rig, **kw)
        rig.monitor = mon.Monitor(
            config=rig.config,
            db=db,
            source=rig.camera,
            pose=pose,
            model=RulesModel(),
            notifier=rig.notifier,
            clock=clock,
            supervisor=rig.supervisor,
            recorder=recorder,
        )
        return MetricsRig(rig, recorder, probe, pose)

    return factory


# --- report seeding -----------------------------------------------------------


def local_ts(tz: Any, day: Any, hour: float = 0.0, offset_days: int = 0) -> float:
    """Epoch seconds for ``hour`` (may be fractional) of ``day + offset_days`` in ``tz``."""
    from datetime import datetime, time, timedelta

    start = datetime.combine(day + timedelta(days=offset_days), time(0), tzinfo=tz)
    return start.timestamp() + hour * 3600.0


METRIC_COLUMNS = (
    "ts, interval_s, cpu_percent, rss_mb, n_present, n_low_confidence, n_away,"
    " latency_p50_ms, latency_p95_ms, mean_confidence, battery_percent, on_ac,"
    " camera_mode, paused_reasons, model_version, calibration_id, metrics_cost_ms"
)


def insert_metrics(
    db: Any,
    ts: float,
    interval_s: float = 3600.0,
    *,
    cpu: float | None = 5.0,
    rss: float | None = 100.0,
    confidence: float | None = 0.8,
    battery: int | None = None,
    on_ac: int | None = None,
    reasons: str = "",
    model_version: str | None = "rules-v9-test",
    calibration_id: int | None = None,
    n_present: int | None = None,
    n_low: int = 0,
    n_away: int = 0,
) -> None:
    """Insert one metrics row with raw SQL on the database file (spec B2 column names)."""
    import sqlite3

    if n_present is None:  # by default a monitoring row is entirely at the desk
        n_present = 20 if not reasons else 0
    conn = sqlite3.connect(db.path)
    try:
        conn.execute(
            f"INSERT INTO metrics({METRIC_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, interval_s, cpu, rss, n_present, n_low, n_away, None, None, confidence,
             battery, on_ac, "burst", reasons, model_version, calibration_id, 1.0),
        )  # fmt: skip
        conn.commit()
    finally:
        conn.close()


def seed_report_db(db: Any, calibration: Any, tz: Any, day: Any) -> dict[str, Any]:
    """Known values for behaviours 10-12 (week ending ``day``, times in ``tz``).

    Chosen day: 4 h monitored, 30 min paused (in_call:meet), 10 present samples
    (7 good, 3 slouch) plus 2 low-confidence "slouch" and 2 away samples that
    must not count, 3 alerts (slouch, slouch, forward_head; 1 marked wrong),
    2 labels (slouch, forward_head).
    Day-3: 2 h monitored, 10 good present samples, 1 too_close alert.
    Days -8 and +1 hold an alert and 1 h monitored each that the week must not count.
    """
    import dataclasses

    cal_id = db.insert_calibration(dataclasses.replace(calibration, ts=local_ts(tz, day, 8.0)))
    mv = "rules-v9-test"

    def sample(ts: float, state: str, label: str | None, conf: float = 0.9) -> None:
        db.insert_sample(
            ts=ts, state=state, confidence=conf, label=label, model_version=mv,
            calibration_id=cal_id, latency_ms=5.0,
        )  # fmt: skip

    for hour in (9, 10, 11, 12):
        insert_metrics(db, local_ts(tz, day, hour), 3600.0, calibration_id=cal_id)
    insert_metrics(
        db, local_ts(tz, day, 13.5), 1800.0, reasons="in_call:meet", calibration_id=cal_id
    )
    for i in range(10):
        sample(local_ts(tz, day, 9.0 + i / 100), "present", "good" if i < 7 else "slouch")
    for i in range(2):
        sample(local_ts(tz, day, 9.5 + i / 100), "low_confidence", "slouch", 0.2)
        sample(local_ts(tz, day, 9.6 + i / 100), "away", None, 0.0)
    ids = [
        db.insert_alert(
            ts=local_ts(tz, day, 10.0 + i / 10), label=label, bad_fraction=0.9, window_s=60.0,
            model_version=mv, calibration_id=cal_id,
        )
        for i, label in enumerate(("slouch", "slouch", "forward_head"))
    ]  # fmt: skip
    db.mark_alert_wrong(ids[1], local_ts(tz, day, 10.5))
    for i, label in enumerate(("slouch", "forward_head")):
        db.insert_label(
            ts=local_ts(tz, day, 11.0 + i / 10), label=label, source="tray", sample_id=None,
            model_version=mv, predicted_label="good",
        )  # fmt: skip

    for hour in (9, 10):
        insert_metrics(db, local_ts(tz, day, hour, -3), 3600.0, calibration_id=cal_id)
    for i in range(10):
        sample(local_ts(tz, day, 9.0 + i / 100, -3), "present", "good")
    db.insert_alert(
        ts=local_ts(tz, day, 10.0, -3), label="too_close", bad_fraction=0.9, window_s=60.0,
        model_version=mv, calibration_id=cal_id,
    )  # fmt: skip

    for offset in (-8, 1):
        insert_metrics(db, local_ts(tz, day, 10.0, offset), 3600.0, calibration_id=cal_id)
        db.insert_alert(
            ts=local_ts(tz, day, 10.0, offset), label="slouch", bad_fraction=0.9, window_s=60.0,
            model_version=mv, calibration_id=cal_id,
        )  # fmt: skip
    return {"calibration_id": cal_id, "model_version": mv}


@pytest.fixture
def file_db(tmp_path):
    """A file-backed database (helpers that seed with raw SQL need a path)."""
    from posture_checker.storage.db import Database

    database = Database(tmp_path / "stage3.db")
    yield database
    database.close()
