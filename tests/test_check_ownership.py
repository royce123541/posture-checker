from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_ownership.py"
_spec = importlib.util.spec_from_file_location("check_ownership", SCRIPT)
assert _spec is not None and _spec.loader is not None
check_ownership = importlib.util.module_from_spec(_spec)
sys.modules["check_ownership"] = check_ownership
_spec.loader.exec_module(check_ownership)
Commit = check_ownership.Commit
violations = check_ownership.violations


def test_ordinary_code_commits_pass() -> None:
    assert violations([Commit("a" * 40, "Add tray icon", ("src/x.py", "tests/test_x.py"))]) == []


def test_spec_and_acceptance_commits_with_right_prefix_pass() -> None:
    commits = [
        Commit("a" * 40, "Spec: stage 2", ("docs/specs/stage-2.md",)),
        Commit("b" * 40, "Acceptance tests: stage 2", ("tests/acceptance/test_stage_2.py",)),
    ]
    assert violations(commits) == []


@pytest.mark.parametrize(
    ("subject", "files", "expected"),
    [
        ("Fix pause", ("tests/acceptance/test_stage_2.py",), "subject"),
        ("Acceptance tests: x", ("tests/acceptance/t.py", "src/a.py"), "together"),
        ("Tweak spec", ("docs/specs/stage-2.md",), "subject"),
        ("Spec: x", ("docs/specs/stage-2.md", "README.md"), "together"),
    ],
)
def test_protected_folders_are_enforced(subject, files, expected) -> None:
    problems = violations([Commit("c" * 40, subject, files)])
    assert problems
    assert any(expected in p for p in problems)


def test_unprotected_docs_are_free() -> None:
    assert violations([Commit("d" * 40, "Explain stage 1", ("docs/explainers/stage-1.md",))]) == []


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_script_runs_against_a_real_repository(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "switch", "-q", "-c", "stage-x")
    (repo / "tests" / "acceptance").mkdir(parents=True)
    (repo / "tests" / "acceptance" / "t.py").write_text("x")
    (repo / "src.py").write_text("y")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Make tests pass")

    monkeypatch.chdir(repo)
    assert check_ownership.main([]) == 1
    _git(repo, "reset", "-q", "--hard", "main")
    assert check_ownership.main([]) == 0
