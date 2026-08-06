"""Which operating system are we on.

This module and the ``savesmith.core.paths`` package are the *only* places in
the core allowed to look at ``sys.platform``. Everything else takes a
``Platform`` value as an argument, which is what makes Windows behaviour
testable from a Mac. ``tests/test_no_platform_branching.py`` enforces this.

The trailing underscore in the module name keeps it from shadowing the stdlib
``platform`` module.
"""

from __future__ import annotations

import sys
from enum import StrEnum

from savesmith.core.errors import UnsupportedPlatformError


class Platform(StrEnum):
    """An operating system family, from SaveSmith's point of view."""

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    OTHER = "other"

    @property
    def is_supported(self) -> bool:
        """Whether SaveSmith knows how to find saves here.

        Linux is recognised but unsupported: milestone 1 covers Windows and
        macOS only. Recognising it lets us fail with a sentence instead of an
        obscure KeyError.
        """
        return self in (Platform.WINDOWS, Platform.MACOS)

    @property
    def display_name(self) -> str:
        return _DISPLAY_NAMES[self]


_DISPLAY_NAMES: dict[Platform, str] = {
    Platform.WINDOWS: "Windows",
    Platform.MACOS: "macOS",
    Platform.LINUX: "Linux",
    Platform.OTHER: "this operating system",
}


def platform_from_sys(sys_platform: str) -> Platform:
    """Map a ``sys.platform`` string onto a :class:`Platform`.

    Takes the string rather than reading it so tests can cover every branch
    without monkeypatching the interpreter.
    """
    if sys_platform.startswith("win"):
        return Platform.WINDOWS
    if sys_platform == "darwin":
        return Platform.MACOS
    if sys_platform.startswith("linux"):
        return Platform.LINUX
    return Platform.OTHER


def current_platform() -> Platform:
    """The platform this process is running on. Never raises."""
    return platform_from_sys(sys.platform)


def require_supported(platform: Platform | None = None) -> Platform:
    """Return the platform, or raise if SaveSmith does not support it.

    Call this at the entry points that need real filesystem access, not deep
    inside helpers — one clear failure beats a dozen scattered ones.
    """
    resolved = platform if platform is not None else current_platform()
    if not resolved.is_supported:
        raise UnsupportedPlatformError(
            resolved.display_name,
            detail=f"sys.platform={sys.platform!r} mapped to {resolved.value!r}",
        )
    return resolved
