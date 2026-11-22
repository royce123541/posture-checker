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

REPO = Path(__file__).resolve().parents[1]
#: Repo scripts (e.g. the camera sharing probe) open the webcam too.
SCRIPT_SOURCES = sorted((REPO / "scripts").glob("*.py"))
CAMERA_SHARING_RESULT = REPO / "docs" / "results" / "camera-sharing.md"

#: File signatures of common image and video formats.
IMAGE_MAGIC = (
    b"\x89PNG",
    b"\xff\xd8\xff",  # JPEG
    b"GIF8",
    b"BM",  # BMP (only checked at the start of a file)
    b"RIFF",  # WebP / AVI
    b"II*\x00",  # TIFF
    b"MM\x00*",
)
#: A run of base64 this long could hide an encoded picture; nothing the app
#: writes as text (numbers, names, short messages) comes close.
LONG_BASE64 = re.compile(rb"[A-Za-z0-9+/=]{200,}")


def assert_no_image_data(data: bytes, what: str) -> None:
    """Fail if ``data`` looks like, or embeds, an image."""
    assert not data.startswith(IMAGE_MAGIC), f"{what} starts with an image signature"
    for magic in (b"\x89PNG", b"\xff\xd8\xff", b"GIF89a", b"GIF87a"):
        assert magic not in data, f"{what} contains an image signature {magic!r}"
    assert b"\x00" not in data, f"{what} contains binary data"
    text = data.decode("utf-8")  # raises if not text
    assert "data:image" not in text.lower(), f"{what} embeds a data: image URI"
    assert LONG_BASE64.search(data) is None, f"{what} contains a long base64 run"


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


# --- Stage 2: scripts, labels table, log file, camera-sharing result ----------


def test_scripts_exist() -> None:
    assert REPO / "scripts" / "camera_share_probe.py" in SCRIPT_SOURCES


@pytest.mark.parametrize("path", SCRIPT_SOURCES, ids=lambda p: p.name)
def test_no_script_writes_images_or_uses_the_network(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    match = IMAGE_WRITERS.search(text)
    assert match is None, f"{path.name} uses {match.group(0)!r}"
    match = NETWORK.search(text)
    assert match is None, f"{path.name} imports a network module: {match.group(0).strip()!r}"


def test_labels_table_holds_only_short_scalars(tmp_path, calibration) -> None:
    from posture_checker.actions import record_label

    db = Database(tmp_path / "posture.db")
    columns = {c["name"]: c["type"].upper() for c in db.query("PRAGMA table_info(labels)")}
    assert columns, "labels table is missing"
    assert set(columns.values()) <= {"INTEGER", "REAL", "TEXT"}
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
    monitor.step()
    clock.advance(1)
    for label in ("slouch", "forward_head", "too_close"):
        record_label(db, clock, label)
    rows = db.query("SELECT * FROM labels")
    assert len(rows) == 3
    for row in rows:
        for key in row.keys():  # noqa: SIM118 - sqlite3.Row needs keys()
            value = row[key]
            assert value is None or isinstance(value, int | float | str), (key, type(value))
            if isinstance(value, str):
                assert len(value) <= 64, f"labels.{key} is unexpectedly long"
    db.close()


def test_log_file_holds_no_image_data_and_folder_stays_clean(calibration) -> None:
    """A run with logging, an error and a label leaves only the db, log and config."""
    import logging
    from logging.handlers import RotatingFileHandler

    from acceptance.conftest import Rig
    from posture_checker import paths
    from posture_checker.app import App, setup_logging

    log_path = setup_logging()
    root = logging.getLogger()
    try:
        db = Database(paths.db_path())
        clock = FakeClock()
        rig = Rig(db, calibration, clock)
        app = App(rig.monitor, rig.supervisor, db, rig.notifier, clock)
        calls = {"n": 0}

        def provider():
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("pose failed")  # logged with a traceback
            return make_landmarks(head_forward=0.3)

        rig.monitor.pose = FakePose(provider)
        for _ in range(40):
            clock.advance(app.tick())
        app.label("slouch")
        rig.lock.locked = True
        clock.advance(app.tick())
        db.close()
        for handler in root.handlers:
            handler.flush()

        data = log_path.read_bytes()
        assert b"pose failed" in data  # the log really was written
        assert_no_image_data(data, log_path.name)
        leftovers = {p.name for p in paths.app_dir().iterdir()} - {
            "posture.db",
            "posture.db-wal",
            "posture.db-shm",
            "config.json",
            log_path.name,
            log_path.name + ".1",
            log_path.name + ".2",
        }
        assert leftovers == set()
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, RotatingFileHandler):
                handler.close()
                root.removeHandler(handler)


def test_camera_sharing_result_is_text_only() -> None:
    assert CAMERA_SHARING_RESULT.exists(), "docs/results/camera-sharing.md is missing"
    data = CAMERA_SHARING_RESULT.read_bytes()
    assert len(data) < 20_000
    assert_no_image_data(data, CAMERA_SHARING_RESULT.name)
    text = data.decode("utf-8")
    assert "![" not in text and "<img" not in text.lower(), "result links an image"
    assert not re.search(r"\.(png|jpe?g|gif|bmp|webp|mp4|avi)\b", text, re.IGNORECASE)
