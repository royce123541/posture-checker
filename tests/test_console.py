"""Hiding the console window the background app gets at sign-in."""

from __future__ import annotations

from posture_checker.platform.console import SW_HIDE, hide_own_console


class FakeKernel32:
    def __init__(self, window: int, processes: int) -> None:
        self.window = window
        self.processes = processes

    def GetConsoleWindow(self) -> int:
        return self.window

    def GetConsoleProcessList(self, pids, size) -> int:
        return self.processes


class FakeUser32:
    def __init__(self) -> None:
        self.shown: list[tuple[int, int]] = []

    def ShowWindow(self, window, command) -> int:
        self.shown.append((window, command))
        return 1


def test_hides_a_console_only_this_process_uses() -> None:
    user32 = FakeUser32()
    assert hide_own_console(FakeKernel32(window=42, processes=1), user32)
    assert user32.shown == [(42, SW_HIDE)]


def test_leaves_a_terminal_alone() -> None:
    user32 = FakeUser32()
    assert not hide_own_console(FakeKernel32(window=42, processes=2), user32)
    assert user32.shown == []


def test_nothing_to_do_without_a_console() -> None:
    user32 = FakeUser32()
    assert not hide_own_console(FakeKernel32(window=0, processes=0), user32)
    assert user32.shown == []
