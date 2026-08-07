"""Whatever the user pointed at, turned into a game to look into.

People do not think in terms of "the install folder". They think of the thing
they double-click: ``eldenring.exe`` on Windows, ``Elden Ring.app`` on a Mac.
Asking for anything else is asking them to already know the answer, and being
told "no saves found" because the folder was one level too deep is the failure
that makes a save editor look broken.

So every command takes any of these and works out the rest:

* the executable — ``.../ELDEN RING/Game/eldenring.exe``
* a Mac application — ``Elden Ring.app``
* a Wineskin or CrossOver wrapper — an ``.app`` with a whole bottle inside
* the install folder, as before
* a save file itself

The executable is worth more than the folder it sits in. ``Game/`` and
``Binaries/Win64/`` are not the game's name; ``eldenring`` is, and the name is
what every later guess about where the saves live is built on.
"""

from __future__ import annotations

import os
import plistlib
from dataclasses import dataclass
from pathlib import Path

from savesmith.core.errors import SaveSmithError
from savesmith.core.wine import WRAPPER_SUFFIX, is_prefix

# Folders that are packaging, not the game. Climbing out of them finds the
# install root, which is where engine markers and the Steam appid live.
_PACKAGING = frozenset(
    {"binaries", "win64", "win32", "windowsnoeditor", "windows", "bin", "bin64",
     "x64", "x86", "game", "macos", "contents", "resources"}
)

_MAX_CLIMB = 4

# Companions that ship beside a game and are never the game.
_NOT_THE_GAME = (
    "unins", "vcredist", "directx", "dxsetup", "setup", "launcher", "crashpad",
    "crashreport", "eac", "easyanticheat", "battleye", "notification_helper",
    "ue4prereqsetup", "ueprereqsetup", "oalinst", "dotnet",
)


@dataclass(frozen=True)
class Target:
    """A place to look for a game, and what was worked out on the way."""

    folder: Path
    """The install folder to examine."""

    name: str | None = None
    """The game's own name, taken from the executable or the bundle."""

    bottle: Path | None = None
    """The Wine prefix this game lives in, when it is inside one."""

    note: str | None = None
    """What was worked out, in words, for the interface to show."""

    save_file: Path | None = None
    """Set when the user pointed straight at a save rather than at a game."""


def resolve(path: Path) -> Target:
    """Work out what the user meant by this path.

    Never guesses about content: the decision is made from the shape of the
    path and from what is on disk beside it, so it can be explained in one
    sentence when it turns out wrong.
    """
    path = path.expanduser()
    if not path.exists():
        raise SaveSmithError(
            f"There is nothing at this path: {path}",
            detail="the path does not exist",
        )

    if path.is_dir():
        if path.suffix.lower() == ".app":
            return _from_application(path)
        return Target(folder=path, bottle=_bottle_of(path))

    if _is_executable(path):
        return _from_executable(path)

    # A file that is not a program is the save itself. Saying so beats
    # searching for saves in a folder the user did not ask about.
    return Target(
        folder=path.parent,
        bottle=_bottle_of(path),
        save_file=path,
        note=f"{path.name} is taken as the save file itself",
    )


# ---------------------------------------------------------------------------
# The three shapes
# ---------------------------------------------------------------------------


def _from_executable(executable: Path) -> Target:
    """An ``.exe`` (or a Mac binary): climb to the install root, keep the name.

    The name is dropped when the file is one of the companions every installer
    leaves behind. ``unins000.exe`` still says which folder the game is in, and
    it says nothing at all about what the game is called.
    """
    root = _install_root(executable.parent)
    note = None
    if root != executable.parent:
        note = f"the game is installed in {root}"
    return Target(
        folder=root,
        name=None if _is_a_companion(executable.stem) else executable.stem,
        bottle=_bottle_of(executable),
        note=note,
    )


def _from_application(bundle: Path) -> Target:
    """A Mac ``.app`` — either a real game or a Windows game in a wrapper."""
    inside = bundle.joinpath(*WRAPPER_SUFFIX)
    if is_prefix(inside):
        # The wrapper's own file name, not what its Info.plist says. A Wineskin
        # bundle inherits the plist of the wrapper template, so "elden ring.app"
        # calls itself "elden_ring" — and the game list, which names bottles the
        # same way Finder does, would then disagree with this screen about what
        # the user just clicked on.
        return Target(
            folder=inside,
            name=bundle.stem,
            bottle=inside,
            note=f"{bundle.name} is a Windows bottle; looking inside it",
        )

    # A native Mac game. Its saves are named after the bundle, not after the
    # binary buried in Contents/MacOS.
    return Target(
        folder=bundle,
        name=_bundle_name(bundle) or bundle.stem,
        note=f"{bundle.name} is a Mac application",
    )


def _install_root(folder: Path) -> Path:
    """Climb out of ``Game/`` and ``Binaries/Win64/`` to the install folder."""
    current = folder
    for _ in range(_MAX_CLIMB):
        if current.name.lower() not in _PACKAGING:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


# ---------------------------------------------------------------------------
# Small facts about files
# ---------------------------------------------------------------------------


def _is_executable(path: Path) -> bool:
    """Whether this file is a program rather than a save.

    A Windows executable is known by its suffix even on a Mac, because a
    bottle's ``.exe`` is the usual case here. A Mac binary has no suffix at
    all, so the executable bit is the only thing left to go on.
    """
    suffix = path.suffix.lower()
    if suffix in (".exe", ".bat", ".cmd", ".com"):
        return True
    if suffix:
        return False
    return os.access(path, os.X_OK)


def _is_a_companion(stem: str) -> bool:
    lowered = stem.lower()
    return any(word in lowered for word in _NOT_THE_GAME)


def _bundle_name(bundle: Path) -> str | None:
    """What a Mac application calls itself, from its own Info.plist."""
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    for key in ("CFBundleName", "CFBundleDisplayName", "CFBundleExecutable"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _bottle_of(path: Path) -> Path | None:
    from savesmith.core.wine import containing_prefix

    return containing_prefix(path if path.is_dir() else path.parent)
