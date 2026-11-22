"""Startup-folder autostart helpers (Stage 2 fixes, F2)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from acceptance.conftest import FakeRegistry, FakeShortcutWriter
from posture_checker import paths, settings_ui
from posture_checker.config import Config, load_config, save_config
from posture_checker.platform import autostart

# Captured at import, before the autouse fixture in tests/conftest.py swaps them out.
REAL_WRITER = autostart.WScriptShortcutWriter
REAL_DEFAULT_STARTUP_DIR = autostart.default_startup_dir


def test_tests_never_see_the_real_startup_folder(tmp_path) -> None:
    """The autouse fixture redirects every default away from the real folder."""
    real_appdata = Path.home() / "AppData" / "Roaming"
    for module in (autostart, settings_ui):
        folder = module.default_startup_dir()
        assert folder.is_relative_to(tmp_path)
        assert module.WScriptShortcutWriter is not REAL_WRITER
    assert not Path(os.environ["APPDATA"]).is_relative_to(real_appdata)
    controller = settings_ui.SettingsController(paths.config_path(), FakeRegistry())
    assert controller.startup_dir.is_relative_to(tmp_path)
    assert not isinstance(controller.writer, REAL_WRITER)


def test_default_startup_dir_is_under_appdata(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    expected = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    assert REAL_DEFAULT_STARTUP_DIR() == expected


def test_powershell_is_resolved_from_system_root(monkeypatch) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"C:\Win")
    expected = Path(r"C:\Win") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    assert autostart.powershell_executable() == expected


def test_enable_creates_a_missing_startup_folder(tmp_path, hunch_exe) -> None:
    startup = tmp_path / "does" / "not" / "exist"
    autostart.enable_autostart(FakeRegistry(), writer=FakeShortcutWriter(), startup_dir=startup)
    assert autostart.is_autostart_enabled(startup_dir=startup)


def test_enable_refuses_when_hunch_exe_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(autostart, "launcher_executable", lambda: tmp_path / "hunch.exe")
    writer = FakeShortcutWriter()
    with pytest.raises(autostart.AutostartError, match=r"hunch\.exe not found"):
        autostart.enable_autostart(FakeRegistry(), writer=writer, startup_dir=tmp_path)
    assert writer.created == []


def test_repo_root_holds_the_project() -> None:
    assert (autostart.REPO_ROOT / "pyproject.toml").exists()


def test_powershell_script_takes_paths_from_the_environment_only() -> None:
    """No path or argument is pasted into the script text, so quotes cannot inject code."""
    script = autostart._PS_SCRIPT
    for name in ("PC_LNK_PATH", "PC_LNK_TARGET", "PC_LNK_ARGS", "PC_LNK_DIR", "PC_LNK_STYLE"):
        assert f"$env:{name}" in script
    assert "WScript.Shell" in script


def test_real_writer_passes_values_as_environment(monkeypatch, tmp_path) -> None:
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    tricky = tmp_path / 'it\'s "odd"; $(x)' / "Hunch.lnk"
    REAL_WRITER().create(tricky, Path("py.exe"), "-m x", tmp_path, True)
    assert seen["command"][0] == str(autostart.powershell_executable())
    assert seen["command"][-1] == autostart._PS_SCRIPT
    assert seen["env"]["PC_LNK_PATH"] == str(tricky)
    assert seen["env"]["PC_LNK_STYLE"] == str(autostart.WINDOW_STYLE_MINIMISED)


class FailingWriter:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def create(self, *args) -> None:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("powershell.exe"),
        subprocess.CalledProcessError(1, "powershell", stderr=b"Access denied"),
        subprocess.TimeoutExpired("powershell", 30),
    ],
)
def test_settings_reports_writer_failure_and_saves_nothing(tmp_path, hunch_exe, error) -> None:
    save_config(Config())
    startup = tmp_path / "Startup"
    controller = settings_ui.SettingsController(
        paths.config_path(), FakeRegistry(), writer=FailingWriter(error), startup_dir=startup
    )
    message = controller.save({"autostart": True, "cooldown_s": 42.0})
    assert message is not None
    assert message.startswith("Not saved: could not change the startup shortcut (")
    if isinstance(error, subprocess.CalledProcessError):
        assert "Access denied" in message
    assert load_config().cooldown_s == Config().cooldown_s
    assert not autostart.is_autostart_enabled(startup_dir=startup)


def test_install_main_reports_writer_failure(monkeypatch, capsys, hunch_exe) -> None:
    from posture_checker import install
    from posture_checker.platform import registry

    monkeypatch.setattr(registry, "WinRegistry", FakeRegistry)
    error = subprocess.CalledProcessError(1, "powershell", stderr=b"blocked by policy")
    monkeypatch.setattr(autostart, "WScriptShortcutWriter", lambda: FailingWriter(error))
    assert install.main(["register"]) == 1
    assert "blocked by policy" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="needs WScript.Shell")
def test_real_writer_round_trip(tmp_path) -> None:
    """Writes a real .lnk (folder with a space) and reads it back through WScript.Shell."""
    folder = tmp_path / "Start Up folder"
    folder.mkdir()
    link = folder / autostart.SHORTCUT_NAME
    target = tmp_path / "fake launcher" / "hunch.exe"  # a dummy; never run
    target.parent.mkdir()
    target.write_bytes(b"")
    REAL_WRITER().create(link, target, "", autostart.REPO_ROOT, True)
    assert link.exists()
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PC_LNK_PATH); "
        "$s.TargetPath; $s.Arguments; $s.WorkingDirectory; $s.WindowStyle"
    )
    out = subprocess.run(
        [
            str(autostart.powershell_executable()),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        env={**os.environ, "PC_LNK_PATH": str(link)},
        check=True,
        capture_output=True,
        text=True,
        timeout=autostart.WRITER_TIMEOUT_S,
    ).stdout.splitlines()
    assert Path(out[0]).resolve() == target.resolve()
    assert out[1] == ""
    assert Path(out[2]).resolve() == autostart.REPO_ROOT
    assert out[3] == str(autostart.WINDOW_STYLE_MINIMISED)


class FailingRunValueRegistry(FakeRegistry):
    def delete_value(self, path, name):
        raise PermissionError(name)


def test_locked_old_shortcut_does_not_block_run_value_or_enable(
    tmp_path, hunch_exe, monkeypatch, caplog
) -> None:
    startup = tmp_path / "Startup"
    startup.mkdir()
    old = startup / autostart.OLD_SHORTCUT_NAME
    old.write_text("")
    registry = FakeRegistry()
    registry.set_value(autostart.RUN_KEY, autostart.RUN_VALUE, "old")
    real_unlink = Path.unlink

    def locked_unlink(self, missing_ok=False):
        if self.name == autostart.OLD_SHORTCUT_NAME:
            raise PermissionError("in use")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    autostart.enable_autostart(registry, writer=FakeShortcutWriter(), startup_dir=startup)
    assert autostart.is_autostart_enabled(startup_dir=startup)
    assert autostart.RUN_VALUE not in registry.run_values()
    assert old.exists()
    assert "old shortcut" in caplog.text


def test_run_value_failure_does_not_block_old_shortcut_removal(tmp_path, caplog) -> None:
    startup = tmp_path / "Startup"
    startup.mkdir()
    (startup / autostart.OLD_SHORTCUT_NAME).write_text("")
    autostart.disable_autostart(FailingRunValueRegistry(), startup_dir=startup)
    assert list(startup.iterdir()) == []
    assert "old Run value" in caplog.text
