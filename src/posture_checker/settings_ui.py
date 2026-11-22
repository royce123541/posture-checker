"""The settings window and the running app's config watcher (behaviours 16, 24).

``SettingsController`` validates and stores settings and toggles autostart;
the Tkinter form only collects text and shows the controller's messages.
``ConfigWatcher`` runs inside the app and applies a changed file without a
restart.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from posture_checker.config import CAMERA_MODES, Config, load_config, save_config
from posture_checker.monitor import Monitor
from posture_checker.platform.autostart import (
    disable_autostart,
    enable_autostart,
    is_autostart_enabled,
)
from posture_checker.platform.registry import Registry

log = logging.getLogger(__name__)

#: How often the watcher re-reads the config file; well inside the spec's 5 s
#: pick-up target (behaviour 16).
WATCH_INTERVAL_S = 2.0

#: Settings shown in the window: key -> (label, type).
FIELDS: dict[str, tuple[str, type]] = {
    "alert_window_s": ("Alert after bad posture for (seconds)", float),
    "sensitivity": ("Sensitivity (1.0 default, higher is stricter)", float),
    "cooldown_s": ("Minimum time between alerts (seconds)", float),
    "battery_pause_percent": ("Pause below battery % when unplugged", int),
    "camera_mode": ("Camera mode (hold or burst)", str),
    "pause_in_calls": ("Pause during calls", bool),
    "autostart": ("Start at login", bool),
}


class SettingsController:
    def __init__(self, config_path: Path, registry: Registry) -> None:
        self.config_path = config_path
        self.registry = registry

    def current(self) -> dict[str, Any]:
        """Current values of every field, for filling in the form."""
        try:
            config = load_config(self.config_path)
        except (ValueError, OSError):
            config = Config()
        values: dict[str, Any] = {k: getattr(config, k) for k in FIELDS if k != "autostart"}
        values["autostart"] = is_autostart_enabled(self.registry)
        return values

    def parse_form(self, texts: Mapping[str, str | bool]) -> dict[str, Any] | str:
        """Convert the form's text to typed values, or return an error message."""
        values: dict[str, Any] = {}
        for key, raw in texts.items():
            if key not in FIELDS:
                return f"Unknown setting: {key}"
            label, kind = FIELDS[key]
            if kind is bool:
                values[key] = bool(raw)
                continue
            text = str(raw).strip()
            try:
                values[key] = kind(text)
            except ValueError:
                return f"{label}: '{text}' is not a valid {kind.__name__}"
        return values

    def save(self, values: Mapping[str, Any]) -> str | None:
        """Validate and store ``values``. Returns an error message, or None."""
        return self.store(values)

    def store(self, values: Mapping[str, Any]) -> str | None:
        unknown = [k for k in values if k not in FIELDS]
        if unknown:
            return f"Unknown setting: {', '.join(unknown)}"
        try:
            current = load_config(self.config_path)
        except (ValueError, OSError):
            current = Config()
        changes = {k: v for k, v in values.items() if k != "autostart"}
        try:
            config = dataclasses.replace(current, **changes)
            if config.camera_mode not in CAMERA_MODES:
                raise ValueError(f"camera mode must be one of {', '.join(CAMERA_MODES)}")
            config.validate()
        except (ValueError, TypeError) as error:
            return f"Not saved: {error}"
        save_config(config, self.config_path)
        if "autostart" in values:
            if values["autostart"]:
                enable_autostart(self.registry)
            else:
                disable_autostart(self.registry)
        return None


class ConfigWatcher:
    """Applies a changed config file to the running monitor."""

    def __init__(
        self,
        config_path: Path,
        monitor: Monitor,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config_path = config_path
        self.monitor = monitor
        self.clock = clock
        self._last_poll: float | None = None
        self._seen: bytes | None = None

    def poll(self) -> bool:
        """Re-read the file if due; True if new settings were applied."""
        now = self.clock()
        if self._last_poll is not None and now - self._last_poll < WATCH_INTERVAL_S:
            return False
        self._last_poll = now
        try:
            data = self.config_path.read_bytes()
        except FileNotFoundError:
            return False
        if data == self._seen:
            return False
        self._seen = data
        try:
            config = load_config(self.config_path)
        except (ValueError, OSError):
            log.exception("ignoring an invalid config file")
            return False
        self.monitor.apply_config(config)
        log.info("settings applied from %s", self.config_path)
        return True


def run_window(controller: SettingsController) -> int:  # pragma: no cover - needs a display
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Posture Checker: settings")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=16)
    frame.grid()
    current = controller.current()
    variables: dict[str, Any] = {}
    for row, (key, (label, kind)) in enumerate(FIELDS.items()):
        if kind is bool:
            var: Any = tk.BooleanVar(value=bool(current[key]))
            ttk.Checkbutton(frame, text=label, variable=var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=2
            )
        elif key == "camera_mode":
            var = tk.StringVar(value=str(current[key]))
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Combobox(frame, textvariable=var, values=CAMERA_MODES, state="readonly").grid(
                row=row, column=1, sticky="ew"
            )
        else:
            var = tk.StringVar(value=str(current[key]))
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=var, width=12).grid(row=row, column=1, sticky="ew")
        variables[key] = var

    message = tk.StringVar()
    ttk.Label(frame, textvariable=message, foreground="#b00020", wraplength=380).grid(
        row=len(FIELDS), column=0, columnspan=2, sticky="w", pady=6
    )

    def on_save() -> None:
        parsed = controller.parse_form({k: v.get() for k, v in variables.items()})
        error = parsed if isinstance(parsed, str) else controller.store(parsed)
        if error:
            message.set(error)
        else:
            root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=len(FIELDS) + 1, column=0, columnspan=2, sticky="e")
    ttk.Button(buttons, text="Cancel", command=root.destroy).grid(row=0, column=0, padx=4)
    ttk.Button(buttons, text="Save", command=on_save).grid(row=0, column=1)
    root.mainloop()
    return 0


def main() -> int:  # pragma: no cover - needs a display
    from posture_checker import paths
    from posture_checker.platform.registry import WinRegistry

    return run_window(SettingsController(paths.config_path(), WinRegistry()))
