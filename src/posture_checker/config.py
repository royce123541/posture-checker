"""User-adjustable settings, stored as JSON next to the database."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from posture_checker import paths

CAMERA_MODES = ("hold", "burst")


@dataclass
class Config:
    # --- Sampling -----------------------------------------------------------
    #: Seconds between samples while posture looks fine.
    sample_interval_s: float = 2.0
    #: Seconds between samples while bad posture is suspected (faster, so the
    #: alert decision rests on more evidence).
    suspect_interval_s: float = 1.0
    #: Seconds between samples while nobody is in front of the camera.
    away_interval_s: float = 10.0

    # --- Alerting -----------------------------------------------------------
    #: Bad posture must persist this long before an alert fires.
    alert_window_s: float = 60.0
    #: Share of confident samples in the window that must be bad.
    bad_fraction: float = 0.8
    #: Minimum quiet time between two alerts.
    cooldown_s: float = 300.0
    #: 1.0 is the default. Higher fires on smaller deviations, lower is more
    #: forgiving. Rule thresholds are divided by this value.
    sensitivity: float = 1.0
    #: Samples below this confidence never count towards an alert.
    min_confidence: float = 0.6

    # --- Camera -------------------------------------------------------------
    camera_index: int = 0
    frame_width: int = 640
    frame_height: int = 480
    #: "hold" keeps the camera open between samples (Stage 1 behaviour);
    #: "burst" opens it, reads one frame and closes it for every sample, so other
    #: apps can always take it. "hold" stays the default until live check L1
    #: (scripts/camera_share_probe.py) shows whether holding blocks other apps.
    camera_mode: str = "hold"

    # --- Power --------------------------------------------------------------
    #: Pause monitoring below this battery percentage when unplugged.
    battery_pause_percent: int = 20

    def validate(self) -> None:
        if self.sample_interval_s <= 0 or self.suspect_interval_s <= 0:
            raise ValueError("sampling intervals must be positive")
        if self.alert_window_s < 5:
            raise ValueError("alert_window_s must be at least 5 seconds")
        if not 0.5 <= self.bad_fraction <= 1.0:
            raise ValueError("bad_fraction must be between 0.5 and 1.0")
        if not 0.25 <= self.sensitivity <= 4.0:
            raise ValueError("sensitivity must be between 0.25 and 4.0")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if self.cooldown_s < 0:
            raise ValueError("cooldown_s must not be negative")
        if not 0 <= self.battery_pause_percent <= 100:
            raise ValueError("battery_pause_percent must be between 0 and 100")
        if self.camera_mode not in CAMERA_MODES:
            raise ValueError(f"camera_mode must be one of {', '.join(CAMERA_MODES)}")


def load_config(path: Path | None = None) -> Config:
    """Load settings, ignoring unknown keys so old files survive upgrades."""
    path = path or paths.config_path()
    if not path.exists():
        return Config()
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(Config)}
    config = Config(**{k: v for k, v in data.items() if k in known})
    config.validate()
    return config


def save_config(config: Config, path: Path | None = None) -> None:
    config.validate()
    path = path or paths.config_path()
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
