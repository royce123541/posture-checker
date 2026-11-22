"""Where the app keeps its local state.

Everything the app writes lives in one per-user directory, outside the repo:
``%LOCALAPPDATA%\\PostureChecker`` on Windows. Set ``POSTURE_CHECKER_HOME`` to
redirect it (tests and benchmarks do this).
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "PostureChecker"
HOME_ENV = "POSTURE_CHECKER_HOME"


def app_dir() -> Path:
    """Return the app's data directory, creating it if needed."""
    override = os.environ.get(HOME_ENV)
    if override:
        base = Path(override)
    else:
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) / APP_NAME if local else Path.home() / f".{APP_NAME.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return app_dir() / "posture.db"


def config_path() -> Path:
    return app_dir() / "config.json"


def models_dir() -> Path:
    path = app_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path
