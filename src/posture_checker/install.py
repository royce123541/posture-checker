"""The Python half of install.ps1 / uninstall.ps1.

Run as ``python -m posture_checker.install register [--no-autostart]`` or
``python -m posture_checker.install unregister [--remove-data]``. All changes
are per user (HKCU and the data folder); nothing needs admin rights.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from collections.abc import Sequence

from posture_checker import paths
from posture_checker.platform.autostart import disable_autostart, enable_autostart
from posture_checker.platform.registry import Registry
from posture_checker.platform.windows import APP_DISPLAY_NAME, AUMID

log = logging.getLogger(__name__)

AUMID_KEY = rf"Software\Classes\AppUserModelId\{AUMID}"


def register(registry: Registry, *, autostart: bool = True) -> None:
    """Register the toast identity and, unless turned off, start at login."""
    registry.set_value(AUMID_KEY, "DisplayName", APP_DISPLAY_NAME)
    if autostart:
        enable_autostart(registry)
    else:
        disable_autostart(registry)


def unregister(registry: Registry, *, remove_data: bool = False) -> None:
    """Undo ``register``. Never raises, so uninstall can always be re-run."""
    try:
        disable_autostart(registry)
    except OSError:
        log.exception("could not remove the Run value")
    try:
        registry.delete_key(AUMID_KEY)
    except FileNotFoundError:
        pass  # never registered, or already removed
    except OSError:
        log.exception("could not remove the AUMID key")
    if remove_data:
        try:
            shutil.rmtree(paths.app_dir())
        except OSError:
            log.exception("could not delete the data folder")


def main(argv: Sequence[str] | None = None) -> int:
    from posture_checker.platform.registry import WinRegistry

    parser = argparse.ArgumentParser(prog="python -m posture_checker.install")
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register")
    reg.add_argument("--no-autostart", action="store_true")
    unreg = sub.add_parser("unregister")
    unreg.add_argument("--remove-data", action="store_true")
    args = parser.parse_args(argv)

    registry = WinRegistry()
    if args.command == "register":
        register(registry, autostart=not args.no_autostart)
        print("Registered toast identity" + ("" if args.no_autostart else " and autostart"))
    else:
        unregister(registry, remove_data=args.remove_data)
        print("Removed autostart and toast identity" + (" and data" if args.remove_data else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
