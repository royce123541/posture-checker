"""A small registry interface so registry logic can be tested with a fake.

Only ``HKEY_CURRENT_USER`` is used: the app never needs admin rights. Every
method raises ``FileNotFoundError`` for a missing key or value, as ``winreg``
does, so callers treat the fake and the real registry the same way.
"""

from __future__ import annotations

from typing import Any, Protocol


class Registry(Protocol):
    def subkeys(self, path: str) -> list[str]: ...

    def values(self, path: str) -> dict[str, Any]: ...

    def set_value(self, path: str, name: str, value: Any) -> None: ...

    def delete_value(self, path: str, name: str) -> None: ...

    def delete_key(self, path: str) -> None: ...


class WinRegistry:
    """``HKEY_CURRENT_USER`` through ``winreg``."""

    def __init__(self) -> None:
        import winreg  # Windows only; imported here so the module loads anywhere

        self._winreg = winreg
        self._root = winreg.HKEY_CURRENT_USER

    def subkeys(self, path: str) -> list[str]:
        winreg = self._winreg
        names: list[str] = []
        with winreg.OpenKey(self._root, path) as key:
            count = winreg.QueryInfoKey(key)[0]
            for i in range(count):
                names.append(winreg.EnumKey(key, i))
        return names

    def values(self, path: str) -> dict[str, Any]:
        winreg = self._winreg
        result: dict[str, Any] = {}
        with winreg.OpenKey(self._root, path) as key:
            count = winreg.QueryInfoKey(key)[1]
            for i in range(count):
                name, value, _kind = winreg.EnumValue(key, i)
                result[name] = value
        return result

    def set_value(self, path: str, name: str, value: Any) -> None:
        winreg = self._winreg
        kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
        with winreg.CreateKey(self._root, path) as key:
            winreg.SetValueEx(key, name, 0, kind, value)

    def delete_value(self, path: str, name: str) -> None:
        winreg = self._winreg
        with winreg.OpenKey(self._root, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)

    def delete_key(self, path: str) -> None:
        """Delete a key and everything under it."""
        for child in self.subkeys(path):
            self.delete_key(path + "\\" + child)
        self._winreg.DeleteKey(self._root, path)
