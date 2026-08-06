"""The seam between SaveSmith and the machine it runs on.

Every question about the host — environment variables, the home directory, a
Windows known folder, a registry value — goes through a :class:`SystemFacade`.
Nothing else in the core calls ``os.environ`` or ``Path.home()`` directly.

The point is testability. :class:`FakeSystem` answers those questions from a
dictionary with paths rooted in a pytest ``tmp_path``, so the entire Windows
code path runs on a Mac against a real (temporary) filesystem. Without this
seam, "we will check it on Windows later" becomes the plan, and the spec
explicitly forbids that.

``FakeSystem`` ships in the package rather than in ``tests/`` on purpose:
plugin authors will need it to test their own path patterns.
"""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from savesmith.core.errors import UnsupportedPlatformError
from savesmith.core.paths import _windows
from savesmith.core.paths._windows import KnownFolder, RegistryHive
from savesmith.core.platform_ import Platform, current_platform

__all__ = [
    "FakeSystem",
    "KnownFolder",
    "RealSystem",
    "RegistryHive",
    "SystemFacade",
]


@runtime_checkable
class SystemFacade(Protocol):
    """What the path layer is allowed to know about the host."""

    @property
    def platform(self) -> Platform: ...

    def env(self, name: str) -> str | None:
        """An environment variable, or ``None`` if unset or empty."""
        ...

    def home(self) -> Path:
        """The current user's home directory."""
        ...

    def username(self) -> str:
        """The current user's login name.

        Used to pick the matching profile inside a Wine bottle.
        """
        ...

    def known_folder(self, folder: KnownFolder) -> Path | None:
        """A Windows known folder, or ``None`` if this machine has no such folder.

        Raises :class:`UnsupportedPlatformError` when called off Windows —
        that would be a bug in the resolver, and silence would hide it.
        """
        ...

    def registry_read(self, hive: RegistryHive, key: str, value_name: str) -> str | None:
        """A registry string value, or ``None`` if key or value is absent."""
        ...


def _require_windows(platform: Platform, what: str) -> None:
    if platform is not Platform.WINDOWS:
        raise UnsupportedPlatformError(
            platform.display_name,
            detail=f"{what} is Windows-only and was called on {platform.value}",
        )


class RealSystem:
    """Answers from the actual machine."""

    def __init__(self, platform: Platform | None = None) -> None:
        self._platform = platform if platform is not None else current_platform()

    @property
    def platform(self) -> Platform:
        return self._platform

    def env(self, name: str) -> str | None:
        value = os.environ.get(name)
        # An empty variable is not an answer; treat it as unset so callers do
        # not build paths like "/Team Cherry/..." rooted at nothing.
        return value or None

    def home(self) -> Path:
        return Path.home()

    def username(self) -> str:
        try:
            return getpass.getuser()
        except (KeyError, OSError):
            # No password entry and no USER/LOGNAME set: happens in stripped
            # containers. The home directory name is the next best guess.
            return Path.home().name

    def known_folder(self, folder: KnownFolder) -> Path | None:
        _require_windows(self._platform, "known_folder")
        return _windows.known_folder_path(folder)

    def registry_read(self, hive: RegistryHive, key: str, value_name: str) -> str | None:
        _require_windows(self._platform, "registry_read")
        return _windows.registry_read(hive, key, value_name)


@dataclass
class FakeSystem:
    """A machine described by a dictionary. For tests and for plugin authors.

    Point ``home_dir`` and the known folders at a ``tmp_path`` tree and the
    resolver will do real filesystem work against fake locations.
    """

    platform: Platform
    home_dir: Path
    env_vars: dict[str, str] = field(default_factory=dict)
    known_folders: dict[KnownFolder, Path] = field(default_factory=dict)
    registry: dict[tuple[RegistryHive, str, str], str] = field(default_factory=dict)
    user: str = "tester"

    def env(self, name: str) -> str | None:
        return self.env_vars.get(name) or None

    def home(self) -> Path:
        return self.home_dir

    def username(self) -> str:
        return self.user

    def known_folder(self, folder: KnownFolder) -> Path | None:
        _require_windows(self.platform, "known_folder")
        return self.known_folders.get(folder)

    def registry_read(self, hive: RegistryHive, key: str, value_name: str) -> str | None:
        _require_windows(self.platform, "registry_read")
        return self.registry.get((hive, key, value_name))
