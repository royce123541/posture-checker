"""Startup-folder autostart helpers (Stage 2 fixes, F2), without touching Windows."""

from __future__ import annotations

from pathlib import Path

from acceptance.conftest import FakeRegistry, FakeShortcutWriter
from posture_checker.platform import autostart


def test_default_startup_dir_is_under_appdata(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    expected = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    assert autostart.default_startup_dir() == expected


def test_enable_creates_a_missing_startup_folder(tmp_path) -> None:
    startup = tmp_path / "does" / "not" / "exist"
    autostart.enable_autostart(FakeRegistry(), writer=FakeShortcutWriter(), startup_dir=startup)
    assert autostart.is_autostart_enabled(startup_dir=startup)


def test_repo_root_holds_the_project() -> None:
    assert (autostart.REPO_ROOT / "pyproject.toml").exists()


def test_powershell_script_takes_paths_from_the_environment_only() -> None:
    """No path or argument is pasted into the script text, so quotes cannot inject code."""
    script = autostart._PS_SCRIPT
    for name in ("PC_LNK_PATH", "PC_LNK_TARGET", "PC_LNK_ARGS", "PC_LNK_DIR", "PC_LNK_STYLE"):
        assert f"$env:{name}" in script
    assert "WScript.Shell" in script
    assert str(Path.home()) not in script


def test_real_writer_passes_values_as_environment(monkeypatch, tmp_path) -> None:
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    tricky = tmp_path / 'it\'s "odd"; $(x)' / "Posture Checker.lnk"
    autostart.WScriptShortcutWriter().create(tricky, Path("py.exe"), "-m x", tmp_path, True)
    assert seen["command"][-1] == autostart._PS_SCRIPT
    assert seen["env"]["PC_LNK_PATH"] == str(tricky)
    assert seen["env"]["PC_LNK_STYLE"] == str(autostart.WINDOW_STYLE_MINIMISED)
