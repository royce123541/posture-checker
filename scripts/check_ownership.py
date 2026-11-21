"""Check that each agent role only changed the files it owns.

Specs and acceptance tests are the contract the owner approves, so the code
that has to satisfy them must not change them in the same breath. For every
commit on the current branch that is not on ``main``:

* a commit touching ``docs/specs/`` must touch only that folder and have a
  subject starting with ``Spec:``;
* a commit touching ``tests/acceptance/`` must touch only that folder and have
  a subject starting with ``Acceptance tests:``.

This is a guard against accidents and shortcuts, not a security boundary: an
agent could still write a commit with the right subject. The reviewer agent
and CI history make that visible.

Usage: ``python scripts/check_ownership.py [--base REF]``. Exit code 1 lists
the offending commits.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Protected folder -> required commit subject prefix.
PROTECTED = {
    "docs/specs/": "Spec:",
    "tests/acceptance/": "Acceptance tests:",
}


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    files: tuple[str, ...]


def violations(commits: Iterable[Commit]) -> list[str]:
    """Return one message per rule broken; empty when everything is fine."""
    problems: list[str] = []
    for commit in commits:
        short = f"{commit.sha[:8]} {commit.subject!r}"
        for folder, prefix in PROTECTED.items():
            inside = [f for f in commit.files if f.startswith(folder)]
            if not inside:
                continue
            outside = [f for f in commit.files if not f.startswith(folder)]
            if outside:
                problems.append(
                    f"{short} changes {folder} together with other files: " + ", ".join(outside[:5])
                )
            if not commit.subject.startswith(prefix):
                problems.append(
                    f"{short} changes {folder} but its subject does not start with {prefix!r}"
                )
    return problems


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def default_base() -> str | None:
    """The merge base with main, or None when main is not known (e.g. a shallow clone)."""
    for ref in ("origin/main", "main"):
        try:
            return _git("merge-base", "HEAD", ref).strip()
        except subprocess.CalledProcessError:
            continue
    return None


def branch_commits(base: str) -> list[Commit]:
    commits = []
    for line in _git("log", "--no-merges", "--format=%H%x00%s", f"{base}..HEAD").splitlines():
        sha, subject = line.split("\x00", 1)
        files = _git("diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()
        commits.append(Commit(sha, subject, tuple(files)))
    return commits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="compare against this ref (default: merge base with main)")
    args = parser.parse_args(argv)

    base = args.base or default_base()
    if base is None:
        print("check_ownership: cannot find main; fetch it (full history) to run this check.")
        return 1
    commits = branch_commits(base)
    problems = violations(commits)
    if problems:
        print("Ownership check failed:")
        for problem in problems:
            print(f"  - {problem}")
        print("See 'File ownership' in README.md.")
        return 1
    print(f"Ownership check passed ({len(commits)} commit(s) since {base[:8]}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
