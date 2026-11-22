"""Guards for the privacy rule: no image is ever saved or sent anywhere."""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

import posture_checker
from conftest import FakeCamera, FakeClock, FakePose, RecordingNotifier
from posture_checker.config import Config
from posture_checker.inference.rules import RulesModel
from posture_checker.monitor import Monitor
from posture_checker.storage.db import Database
from posture_checker.synthetic import make_landmarks

PACKAGE = Path(posture_checker.__file__).parent
SOURCES = sorted(PACKAGE.rglob("*.py"))

# Calls that write or encode pixels.
IMAGE_WRITERS = re.compile(
    r"\b(imwrite|imencode|VideoWriter|imsave|savefig|tofile|ImageGrab)\b|np\.save|\.save\("
)
# Modules that talk to the network.
NETWORK = re.compile(
    r"^\s*(import|from)\s+(socket|requests|httpx|aiohttp|urllib|http|ftplib|smtplib)\b",
    re.MULTILINE,
)
#: The one place allowed to use the network: the checksum-verified model download.
NETWORK_ALLOWED = {PACKAGE / "capture" / "pose.py"}


def test_package_has_sources() -> None:
    assert len(SOURCES) > 10


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_no_code_writes_or_encodes_images(path: Path) -> None:
    match = IMAGE_WRITERS.search(path.read_text(encoding="utf-8"))
    assert match is None, f"{path.name} uses {match.group(0)!r}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_only_the_model_download_touches_the_network(path: Path) -> None:
    if path in NETWORK_ALLOWED:
        return
    match = NETWORK.search(path.read_text(encoding="utf-8"))
    assert match is None, f"{path.name} imports a network module: {match.group(0).strip()!r}"


def test_database_has_no_binary_columns(tmp_path) -> None:
    db = Database(tmp_path / "posture.db")
    tables = [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert {"samples", "alerts", "calibrations", "events"} <= set(tables)
    for table in tables:
        for column in db.query(f"PRAGMA table_info({table})"):
            assert column["type"].upper() in {"INTEGER", "REAL", "TEXT", ""}, (
                f"{table}.{column['name']} is {column['type']}"
            )
    db.close()


def test_monitoring_works_with_the_network_cut_off(tmp_path, calibration, monkeypatch) -> None:
    def no_network(*args, **kwargs):
        raise AssertionError("the monitoring pipeline tried to open a socket")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    db = Database(tmp_path / "posture.db")
    db.insert_calibration(calibration)
    clock = FakeClock()
    notifier = RecordingNotifier()
    monitor = Monitor(
        config=Config(alert_window_s=10),
        db=db,
        source=FakeCamera(),
        pose=FakePose(lambda: make_landmarks(head_forward=0.3)),
        model=RulesModel(),
        notifier=notifier,
        clock=clock,
    )
    for _ in range(20):
        monitor.step()
        clock.advance(1.0)
    assert len(notifier.alerts) == 1
    db.close()


def test_data_folder_contains_only_the_database(tmp_path, calibration) -> None:
    """After a monitoring run, nothing but SQLite files exists on disk."""
    home = tmp_path / "data"
    home.mkdir()
    db = Database(home / "posture.db")
    db.insert_calibration(calibration)
    clock = FakeClock()
    monitor = Monitor(
        config=Config(),
        db=db,
        source=FakeCamera(),
        pose=FakePose(make_landmarks),
        model=RulesModel(),
        notifier=RecordingNotifier(),
        clock=clock,
    )
    for _ in range(10):
        monitor.step()
        clock.advance(2.0)
    db.close()
    leftovers = {p.name for p in home.iterdir()} - {
        "posture.db",
        "posture.db-wal",
        "posture.db-shm",
    }
    assert leftovers == set()
