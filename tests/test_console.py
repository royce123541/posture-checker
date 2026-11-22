"""Leaving the console window the background app gets at sign-in."""

from __future__ import annotations

import os
import sys

from posture_checker.platform import console

TERMINAL_PID = 999_999  # stands in for a shell that is not us or our launcher


class FakeKernel32:
    def __init__(self, window: int, pids: list[int], fail: bool = False) -> None:
        self.window = window
        self.pids = pids
        self.fail = fail

    def GetConsoleWindow(self) -> int:
        return self.window

    def GetConsoleProcessList(self, buffer, size) -> int:
        if self.fail:
            return 0
        for i, pid in enumerate(self.pids[:size]):
            buffer[i] = pid
        return len(self.pids)


class FakeSpawn:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))


def run(kernel32: FakeKernel32) -> FakeSpawn:
    spawn = FakeSpawn()
    started = console.relaunch_without_console(["app"], kernel32, spawn)
    assert started == bool(spawn.calls)
    return spawn


def test_relaunches_windowless_when_only_we_use_the_console() -> None:
    [(cmd, kwargs)] = run(FakeKernel32(42, [os.getpid()])).calls
    assert cmd == [sys.executable, "-m", "posture_checker", "app"]
    assert kwargs["creationflags"] & console.CREATE_NO_WINDOW


def test_relaunched_copy_never_relaunches_again(monkeypatch) -> None:
    [(_, kwargs)] = run(FakeKernel32(42, [os.getpid()])).calls
    assert kwargs["env"][console.RELAUNCHED_ENV] == "1"
    monkeypatch.setenv(console.RELAUNCHED_ENV, "1")
    assert not run(FakeKernel32(42, [os.getpid()])).calls


def test_relaunches_when_only_our_launcher_shares_the_console() -> None:
    assert run(FakeKernel32(42, [os.getpid(), os.getppid()])).calls


def test_started_from_a_terminal_runs_in_place() -> None:
    assert not run(FakeKernel32(42, [os.getpid(), os.getppid(), TERMINAL_PID])).calls


def test_no_console_runs_in_place() -> None:
    assert not run(FakeKernel32(0, [])).calls


def test_failed_process_list_runs_in_place() -> None:
    assert not run(FakeKernel32(42, [os.getpid()], fail=True)).calls
