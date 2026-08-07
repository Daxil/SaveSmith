"""Finding the files that ship with SaveSmith.

Two different worlds. Running from source, the plugins and the risk database
sit next to the package. Running from a packaged binary, PyInstaller has
unpacked them into a temporary folder and told us where through ``sys._MEIPASS``.

Everything that reads a shipped file goes through here, so the difference is
handled in one place instead of being rediscovered — usually by a user, after
release, when the packaged build cannot find its own plugins.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """Whether this is a packaged binary rather than a source checkout."""
    return bool(getattr(sys, "frozen", False))


def root() -> Path:
    """The folder shipped data lives in."""
    unpacked = getattr(sys, "_MEIPASS", None)
    if unpacked:
        return Path(str(unpacked))
    # savesmith/resources.py → savesmith/ → the checkout
    return Path(__file__).resolve().parent.parent


def bundled_path(*parts: str) -> Path:
    return root().joinpath(*parts)


def describe() -> str:
    return f"{'packaged binary' if is_frozen() else 'source checkout'} at {root()}"
