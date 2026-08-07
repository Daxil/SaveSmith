"""Unity PlayerPrefs — the settings that are not in the save file.

Plenty of Unity games keep real progress here rather than in a save: unlocked
levels, currency, whether the tutorial was finished. A save editor that only
looks at files misses them entirely, and the player is told their game is not
supported.

The two platforms could hardly be less alike:

**Windows** puts them in the registry under ``HKCU\\Software\\<Company>\\<Product>``,
and mangles every name: ``coins`` is stored as ``coins_h1234567890``. The suffix
is a hash of the name that Unity has never documented.

**macOS** puts them in a binary property list at
``~/Library/Preferences/unity.<Company>.<Product>.plist``, with the names intact.

**The hash is not reimplemented here.** Names are recovered by listing what is
actually stored and cutting at the last ``_h`` — which works, needs no
guesswork, and cannot drift when Unity changes something. The cost is that a
key that does not already exist cannot be created, which is exactly the rule
this project follows everywhere else: never invent a value the game has not
written itself.
"""

from __future__ import annotations

import plistlib
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from savesmith.core.errors import SaveSmithError
from savesmith.core.paths import PathResolver, RegistryHive, SystemFacade
from savesmith.core.platform_ import Platform

# Windows registry value types, named rather than imported: winreg does not
# exist off Windows and these are stable constants.
REG_SZ = 1
REG_BINARY = 3
REG_DWORD = 4
REG_QWORD = 11

_MANGLED = re.compile(r"^(?P<name>.*)_h\d+$")


class PlayerPrefsError(SaveSmithError):
    code = "playerprefs_unavailable"

    def __init__(self, reason: str, *, detail: str | None = None) -> None:
        super().__init__(f"This game's Unity settings could not be read: {reason}", detail=detail)


@dataclass(frozen=True)
class Entry:
    """One stored preference."""

    name: str
    value: Any
    raw_name: str
    """What it is actually called in storage — ``coins_h1234567890`` on Windows."""

    @property
    def kind(self) -> str:
        if isinstance(self.value, bool):
            return "bool"
        if isinstance(self.value, int):
            return "int"
        if isinstance(self.value, float):
            return "float"
        return "string"


class PlayerPrefs(Protocol):
    def read(self) -> dict[str, Entry]: ...
    def write(self, name: str, value: Any) -> None: ...
    def export(self) -> bytes:
        """A snapshot that can be backed up and restored by hand."""
        ...

    @property
    def location(self) -> str: ...


@dataclass
class RegistryPrefs:
    """PlayerPrefs in the Windows registry."""

    system: SystemFacade
    company: str
    product: str
    _entries: dict[str, Entry] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"Software\\{self.company}\\{self.product}"

    @property
    def location(self) -> str:
        return f"HKEY_CURRENT_USER\\{self.key}"

    def read(self) -> dict[str, Entry]:
        stored = self.system.registry_values(RegistryHive.HKCU, self.key)
        entries: dict[str, Entry] = {}
        for raw_name, (data, value_type) in stored.items():
            match = _MANGLED.match(raw_name)
            name = match.group("name") if match else raw_name
            entries[name] = Entry(
                name=name, value=_from_registry(data, value_type), raw_name=raw_name
            )
        self._entries = entries
        return entries

    def write(self, name: str, value: Any) -> None:
        entries = self._entries or self.read()
        existing = entries.get(name)
        if existing is None:
            # Creating one would mean computing Unity's undocumented hash, and
            # a key the game never wrote is a key it will not read.
            raise PlayerPrefsError(
                f"there is no setting called '{name}' to change",
                detail=f"{self.location}: known names are {sorted(entries)}",
            )
        data, value_type = _to_registry(value)
        self.system.registry_write(RegistryHive.HKCU, self.key, existing.raw_name, data, value_type)
        self._entries = {}

    def export(self) -> bytes:
        import json

        entries = self.read()
        payload = {
            "location": self.location,
            "values": {
                entry.raw_name: {"name": name, "value": _plain(entry.value), "kind": entry.kind}
                for name, entry in entries.items()
            },
        }
        return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass
class PlistPrefs:
    """PlayerPrefs in a macOS property list."""

    path: Path

    @property
    def location(self) -> str:
        return str(self.path)

    def read(self) -> dict[str, Entry]:
        try:
            with self.path.open("rb") as handle:
                loaded = plistlib.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, plistlib.InvalidFileException) as exc:
            raise PlayerPrefsError(
                "the settings file is damaged or unreadable", detail=f"{self.path}: {exc}"
            ) from exc
        if not isinstance(loaded, dict):
            return {}
        return {
            str(name): Entry(name=str(name), value=value, raw_name=str(name))
            for name, value in loaded.items()
        }

    def write(self, name: str, value: Any) -> None:
        entries = self.read()
        if name not in entries:
            raise PlayerPrefsError(
                f"there is no setting called '{name}' to change",
                detail=f"{self.path}: known names are {sorted(entries)}",
            )
        data = {key: entry.value for key, entry in entries.items()}
        data[name] = value
        try:
            with self.path.open("wb") as handle:
                plistlib.dump(data, handle, fmt=plistlib.FMT_BINARY)
        except OSError as exc:
            raise PlayerPrefsError(
                "the settings file could not be written", detail=f"{self.path}: {exc}"
            ) from exc

    def export(self) -> bytes:
        try:
            return self.path.read_bytes()
        except OSError:
            return b""


def open_prefs(system: SystemFacade, company: str, product: str) -> PlayerPrefs:
    """The PlayerPrefs store for one game on this machine."""
    if system.platform is Platform.WINDOWS:
        return RegistryPrefs(system=system, company=company, product=product)

    base = PathResolver(system).token("PREFS")
    if base is None:
        raise PlayerPrefsError("this platform has no Unity preferences folder")
    return PlistPrefs(path=base / f"unity.{company}.{product}.plist")


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------


def _from_registry(data: Any, value_type: int) -> Any:
    """Turn a stored registry value into something a person can read.

    Unity keeps strings as UTF-8 bytes with a trailing zero, and both integers
    and floats as a 32-bit word — the type is not recorded, so a value that is
    a plausible float is offered as one.
    """
    if value_type == REG_BINARY and isinstance(data, bytes | bytearray):
        return bytes(data).rstrip(b"\x00").decode("utf-8", errors="replace")
    if value_type in (REG_DWORD, REG_QWORD) and isinstance(data, int):
        return data
    if isinstance(data, bytes | bytearray):
        return bytes(data).rstrip(b"\x00").decode("utf-8", errors="replace")
    return data


def _to_registry(value: Any) -> tuple[Any, int]:
    if isinstance(value, bool):
        return int(value), REG_DWORD
    if isinstance(value, int):
        return value, REG_DWORD
    if isinstance(value, float):
        # Unity stores a float as the same four bytes an int would occupy.
        return struct.unpack("<I", struct.pack("<f", value))[0], REG_DWORD
    return str(value).encode("utf-8") + b"\x00", REG_BINARY


def _plain(value: Any) -> Any:
    return value if isinstance(value, int | float | str | bool) else str(value)
