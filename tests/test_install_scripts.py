"""install.ps1 / uninstall.ps1 find processes by this checkout's venv paths."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "posture-checker-processes.ps1"


def test_helper_matches_by_executable_path_and_includes_children() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert "ExecutablePath" in text
    assert "ParentProcessId" in text  # base interpreter started by the venv launcher
    for exe in ("pythonw.exe", "python.exe", "posture-checker.exe"):
        assert exe in text


def test_both_scripts_use_the_helper_and_not_a_command_line_substring() -> None:
    for name in ("install.ps1", "uninstall.ps1"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert "posture-checker-processes.ps1" in text, name
        assert "Get-PostureCheckerProcess -RepoRoot $PSScriptRoot" in text, name
        assert "-like" not in text, f"{name} still matches a command-line substring"


def test_install_checks_only_the_app_and_uninstall_stops_everything() -> None:
    install = (REPO / "install.ps1").read_text(encoding="utf-8")
    uninstall = (REPO / "uninstall.ps1").read_text(encoding="utf-8")
    assert install.count("-AppOnly") == 2
    assert "-AppOnly" not in uninstall
