"""Stage 2 rename acceptance tests (spec: docs/specs/stage-2-rename.md, R1-R4).

Interfaces the implementer must build (imported lazily inside the tests):

posture_checker.tray.tray_status(state): tooltips start with "Hunch:" (monitoring,
    away, calibrate to start, camera unavailable); paused texts unchanged.
posture_checker.platform.windows.APP_DISPLAY_NAME == "Hunch" (toast display name).
posture_checker.install.register(...) writes DisplayName "Hunch" under the AUMID key
    (the AUMID itself stays "PostureChecker.App").
posture_checker.platform.autostart
    SHORTCUT_NAME = "Hunch.lnk"
    the old "Posture Checker.lnk" and the old Run value "PostureChecker" are deleted
    on both enable and disable.
    enable_autostart: writer.create(path, Path(sys.executable).with_name("hunch.exe"),
        "", REPO_ROOT, True), with sys.executable read at call time (tests
        monkeypatch it); raises AutostartError matching "hunch.exe not found" (and
        writes no shortcut) if that file is missing.
pyproject.toml: [project.gui-scripts] hunch = "posture_checker.app:main".
src/: no user-visible string literal "Posture Checker" (allow-list below).
"""

from __future__ import annotations

import ast
import sys
import tomllib
from importlib import import_module
from pathlib import Path

import pytest

from acceptance.conftest import AUMID_KEY, RUN_KEY

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "posture_checker"
OLD_SHORTCUT = "Posture Checker.lnk"
OLD_RUN_VALUE = "PostureChecker"

# Substrings of string literals that may keep the old name: the old shortcut file
# name, which autostart must still know in order to migrate (spec R3). Internal
# identifiers (APP_NAME "PostureChecker", AUMID "PostureChecker.App", the lock
# name, the Run value) contain no space, so they never match "Posture Checker".
SCAN_ALLOW_LIST = (OLD_SHORTCUT,)


def auto():
    return import_module("posture_checker.platform.autostart")


def tray():
    return import_module("posture_checker.tray")


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"away": True},
        {"away": True, "presence_check": True},
        {"calibrated": False},
        {"camera_available": False},
    ],
    ids=["monitoring", "away", "away-presence-check", "uncalibrated", "camera-unavailable"],
)
def test_tooltips_start_with_hunch(fields):
    """R1 / acceptance 1: non-paused tooltips start with "Hunch:"."""
    _colour, tooltip = tray().tray_status(tray().TrayState(**fields))
    assert tooltip.startswith("Hunch:")
    assert "Posture Checker" not in tooltip


def test_exact_tooltip_texts():
    """R1 / acceptance 1: the four texts named in the spec."""
    t = tray()
    assert t.tray_status(t.TrayState())[1] == "Hunch: monitoring"
    assert t.tray_status(t.TrayState(away=True))[1] == "Hunch: away"
    assert t.tray_status(t.TrayState(calibrated=False))[1] == "Hunch: calibrate to start"
    assert t.tray_status(t.TrayState(camera_available=False))[1] == "Hunch: camera unavailable"


def test_toast_display_name_registered_by_installer(registry, shortcut_writer, startup_dir):
    """R1 / acceptance 2: the installer registers display name "Hunch"; identity unchanged."""
    windows = import_module("posture_checker.platform.windows")
    install = import_module("posture_checker.install")
    assert windows.APP_DISPLAY_NAME == "Hunch"
    assert windows.AUMID == "PostureChecker.App"
    install.register(registry, writer=shortcut_writer, startup_dir=startup_dir, autostart=False)
    assert registry.values(AUMID_KEY)["DisplayName"] == "Hunch"


def test_enable_writes_hunch_shortcut_and_migrates_old_entries(
    hunch_exe, registry, shortcut_writer, startup_dir
):
    """R2, R3 / acceptance 3: Hunch.lnk -> hunch.exe, no arguments; old entries removed."""
    (startup_dir / OLD_SHORTCUT).write_text("")
    registry.set_value(RUN_KEY, OLD_RUN_VALUE, "old command")
    registry.set_value(RUN_KEY, "Other", "keep me")
    auto().enable_autostart(registry, writer=shortcut_writer, startup_dir=startup_dir)
    assert [p.name for p in startup_dir.iterdir()] == ["Hunch.lnk"]
    (call,) = shortcut_writer.created
    assert call["path"] == startup_dir / "Hunch.lnk"
    assert Path(call["target"]) == hunch_exe
    assert Path(call["target"]) == Path(sys.executable).with_name("hunch.exe")
    assert call["arguments"] == ""
    assert call["minimised"] is True
    assert registry.run_values() == {"Other": "keep me"}
    assert auto().is_autostart_enabled(startup_dir=startup_dir) is True


def test_disable_removes_new_and_old_entries(hunch_exe, registry, shortcut_writer, startup_dir):
    """R3 / acceptance 3: disable removes Hunch.lnk, the old shortcut and the old Run value."""
    auto().enable_autostart(registry, writer=shortcut_writer, startup_dir=startup_dir)
    (startup_dir / OLD_SHORTCUT).write_text("")
    registry.set_value(RUN_KEY, OLD_RUN_VALUE, "old command")
    registry.set_value(RUN_KEY, "Other", "keep me")
    auto().disable_autostart(registry, startup_dir=startup_dir)
    assert list(startup_dir.iterdir()) == []
    assert registry.run_values() == {"Other": "keep me"}
    assert auto().is_autostart_enabled(startup_dir=startup_dir) is False


def test_old_shortcut_alone_does_not_count_as_enabled(startup_dir):
    """R3: only Hunch.lnk means enabled; a leftover old shortcut does not."""
    (startup_dir / OLD_SHORTCUT).write_text("")
    assert auto().is_autostart_enabled(startup_dir=startup_dir) is False


def test_missing_hunch_exe_is_a_clear_error_and_writes_nothing(
    monkeypatch, tmp_path, registry, shortcut_writer, startup_dir
):
    """R2 / acceptance 4: no hunch.exe next to the interpreter -> error, no shortcut."""
    scripts = tmp_path / "bare-venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")
    (scripts / "pythonw.exe").write_text("")  # pythonw present must not be enough
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    with pytest.raises(auto().AutostartError, match=r"hunch\.exe not found"):
        auto().enable_autostart(registry, writer=shortcut_writer, startup_dir=startup_dir)
    assert shortcut_writer.created == []
    assert list(startup_dir.iterdir()) == []


def test_pyproject_declares_hunch_gui_entry_point():
    """R2 / acceptance 5: [project.gui-scripts] hunch = posture_checker.app:main."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["gui-scripts"]["hunch"] == "posture_checker.app:main"
    assert "posture-checker" in data["project"]["scripts"]  # R4: command unchanged


def _string_literals(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    for node in ast.walk(tree):
        is_text = isinstance(node, ast.Constant) and isinstance(node.value, str)
        if is_text and id(node) not in docstrings:
            yield node.lineno, node.value


def test_no_visible_posture_checker_string_in_src():
    """R1 / acceptance 6: no string literal in src/ says "Posture Checker" (allow-list above)."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, text in _string_literals(path):
            for allowed in SCAN_ALLOW_LIST:
                text = text.replace(allowed, "")
            if "Posture Checker" in text:
                offenders.append(f"{path.relative_to(REPO)}:{lineno}")
    assert offenders == []
