"""Turning ``{TOKEN}/Some Game/user*.dat`` into real files.

Two steps, deliberately separate:

``expand``
    Substitutes tokens. Pure string work, no filesystem access. A token that
    does not exist on this platform (``{SAVEDGAMES}`` on macOS) makes the whole
    pattern unavailable — that is a normal empty answer, not an error.
``resolve``
    Walks the filesystem and returns files that actually exist.

Matching is case-insensitive on both platforms. On Windows that matches the OS;
on macOS it is a deliberate choice, because APFS can be case-sensitive and a
game running under Wine may well have written ``User1.dat`` where the plugin
says ``user*.dat``.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from enum import StrEnum
from pathlib import Path

from savesmith.core.errors import PathResolutionError, UnknownPathTokenError
from savesmith.core.paths._system import KnownFolder, RegistryHive, SystemFacade
from savesmith.core.platform_ import Platform

_TOKEN_RE = re.compile(r"\{([A-Z_][A-Z0-9_]*)\}")
_MAGIC_RE = re.compile(r"[*?\[]")

# Beyond this, Windows needs the \\?\ prefix. 240 rather than 260 leaves room
# for the filename the caller is about to append.
_LONG_PATH_THRESHOLD = 240
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class PathToken(StrEnum):
    """Tokens a plugin may use in its ``detect.paths`` patterns."""

    APPDATA = "APPDATA"
    LOCALAPPDATA = "LOCALAPPDATA"
    LOCALLOW = "LOCALLOW"
    DOCUMENTS = "DOCUMENTS"
    SAVEDGAMES = "SAVEDGAMES"
    USERPROFILE = "USERPROFILE"
    HOME = "HOME"
    STEAM = "STEAM"
    CONTAINERS = "CONTAINERS"
    PREFS = "PREFS"
    SAVESMITH_DATA = "SAVESMITH_DATA"
    # Substituted by the Wine scanner, never resolved from the host.
    WINEUSER = "WINEUSER"


def extended_length_path(path: Path) -> str:
    r"""Add the ``\\?\`` prefix to long Windows paths.

    Without it, anything past 260 characters fails with a confusing "file not
    found" even though the file is right there. Detection is by shape — a drive
    letter and a real length — so this is a no-op for POSIX paths and for the
    temporary trees the tests build.
    """
    text = str(path)
    if len(text) <= _LONG_PATH_THRESHOLD:
        return text
    if not _WINDOWS_DRIVE_RE.match(text):
        return text
    if text.startswith("\\\\?\\"):
        return text
    return "\\\\?\\" + text.replace("/", "\\")


class PathResolver:
    """Resolves plugin path patterns against one machine.

    ``extra_tokens`` supplies values the host cannot know — the Wine scanner
    passes ``{"WINEUSER": "danil"}`` after inspecting a bottle.
    """

    def __init__(
        self,
        system: SystemFacade,
        extra_tokens: Mapping[str, str] | None = None,
    ) -> None:
        self._system = system
        self._extra = dict(extra_tokens or {})

    @property
    def platform(self) -> Platform:
        return self._system.platform

    # -- tokens ----------------------------------------------------------

    def token(self, name: str) -> Path | None:
        """Where a single token points, or ``None`` if not applicable here."""
        if name in self._extra:
            return Path(self._extra[name])
        try:
            parsed = PathToken(name)
        except ValueError:
            raise UnknownPathTokenError(
                name, f"{{{name}}}", known_tokens=tuple(t.value for t in PathToken)
            ) from None
        if self._system.platform is Platform.WINDOWS:
            return _windows_token(self._system, parsed)
        return _macos_token(self._system, parsed)

    def all_tokens(self) -> dict[str, Path | None]:
        """Every token and where it lands. Used by ``diagnostics``."""
        return {token.value: self._safe_token(token.value) for token in PathToken}

    def _safe_token(self, name: str) -> Path | None:
        try:
            return self.token(name)
        except PathResolutionError:
            return None

    # -- expansion -------------------------------------------------------

    def expand(self, pattern: str) -> Path | None:
        """Substitute tokens. No filesystem access.

        Returns ``None`` when the pattern relies on something this platform
        does not have.
        """
        normalised = pattern.replace("\\", "/")
        unavailable = False

        def substitute(match: re.Match[str]) -> str:
            nonlocal unavailable
            name = match.group(1)
            if name in self._extra:
                return self._extra[name]
            if name == PathToken.WINEUSER.value:
                raise PathResolutionError(
                    pattern,
                    "It refers to a Windows user inside a Wine bottle, so it can only "
                    "be used when scanning a bottle.",
                    detail="{WINEUSER} used outside a Wine prefix context",
                )
            value = self.token(name)
            if value is None:
                unavailable = True
                return ""
            return str(value).replace("\\", "/")

        text = _TOKEN_RE.sub(substitute, normalised)
        if unavailable:
            return None
        if not text:
            raise PathResolutionError(pattern, "It expands to an empty path.")
        return Path(text)

    # -- resolution ------------------------------------------------------

    def resolve(self, pattern: str) -> list[Path]:
        """Every existing file or folder matching the pattern.

        An empty list means "nothing here", which is ordinary. Only a broken
        pattern raises.
        """
        expanded = self.expand(pattern)
        if expanded is None:
            return []
        if "**" in str(expanded):
            raise PathResolutionError(
                pattern,
                "Recursive '**' patterns are not supported; list the folders explicitly.",
                detail="'**' in pattern",
            )

        parts = expanded.parts
        if not parts:
            return []
        current: list[Path] = [Path(parts[0])]
        for part in parts[1:]:
            current = list(self._step(current, part))
            if not current:
                return []
        return sorted({path for path in current if path.exists()})

    def _step(self, bases: Iterable[Path], part: str) -> Iterator[Path]:
        for base in bases:
            if _MAGIC_RE.search(part):
                yield from self._match_children(base, part)
            else:
                candidate = base / part
                if candidate.exists():
                    yield candidate
                else:
                    # Same-name-different-case: normal on a case-sensitive
                    # volume holding files written by Windows.
                    yield from self._match_children(base, part, exact_fold=True)

    def _match_children(self, base: Path, part: str, *, exact_fold: bool = False) -> Iterator[Path]:
        wanted = part.lower()
        try:
            with os.scandir(extended_length_path(base)) as entries:
                names = [entry.name for entry in entries]
        except (NotADirectoryError, FileNotFoundError):
            return
        except OSError:
            # Unreadable folder (permissions, an unmounted network drive).
            # Skipping beats aborting the whole scan.
            return
        for name in names:
            folded = name.lower()
            # fnmatchcase on pre-folded strings: fnmatch() would apply the
            # host's case rules and behave differently on Windows and macOS.
            matched = folded == wanted if exact_fold else fnmatch.fnmatchcase(folded, wanted)
            if matched:
                yield base / name


def _windows_token(system: SystemFacade, token: PathToken) -> Path | None:
    """Where each token lives on Windows.

    ``DOCUMENTS`` has no environment-variable fallback on purpose. With OneDrive
    enabled, ``%USERPROFILE%\\Documents`` exists, is empty, and is not where the
    game saves — falling back to it would turn a clear failure into a silent
    wrong answer.
    """
    match token:
        case PathToken.APPDATA:
            return system.known_folder(KnownFolder.ROAMING_APPDATA) or _env_path(system, "APPDATA")
        case PathToken.LOCALAPPDATA:
            return system.known_folder(KnownFolder.LOCAL_APPDATA) or _env_path(
                system, "LOCALAPPDATA"
            )
        case PathToken.LOCALLOW:
            low = system.known_folder(KnownFolder.LOCAL_APPDATA_LOW)
            if low is not None:
                return low
            local = system.known_folder(KnownFolder.LOCAL_APPDATA) or _env_path(
                system, "LOCALAPPDATA"
            )
            return local.parent / "LocalLow" if local is not None else None
        case PathToken.DOCUMENTS:
            return system.known_folder(KnownFolder.DOCUMENTS)
        case PathToken.SAVEDGAMES:
            return system.known_folder(KnownFolder.SAVED_GAMES)
        case PathToken.USERPROFILE | PathToken.HOME:
            return (
                system.known_folder(KnownFolder.PROFILE)
                or _env_path(system, "USERPROFILE")
                or system.home()
            )
        case PathToken.STEAM:
            return _windows_steam(system)
        case PathToken.SAVESMITH_DATA:
            local = _windows_token(system, PathToken.LOCALAPPDATA)
            return local / "SaveSmith" if local is not None else None
        case PathToken.CONTAINERS | PathToken.PREFS:
            return None  # macOS concepts
        case PathToken.WINEUSER:
            return None  # supplied by the Wine scanner
    return None


def _windows_steam(system: SystemFacade) -> Path | None:
    """Steam's install folder, asked for in the order Steam itself writes it."""
    from_hkcu = system.registry_read(RegistryHive.HKCU, r"Software\Valve\Steam", "SteamPath")
    if from_hkcu:
        return Path(from_hkcu.replace("\\", "/"))
    # 64-bit Windows puts the machine-wide key under Wow6432Node.
    from_hklm = system.registry_read(
        RegistryHive.HKLM, r"SOFTWARE\Wow6432Node\Valve\Steam", "InstallPath"
    )
    if from_hklm:
        return Path(from_hklm.replace("\\", "/"))
    program_files = _env_path(system, "ProgramFiles(x86)") or _env_path(system, "ProgramFiles")
    return program_files / "Steam" if program_files is not None else None


def _macos_token(system: SystemFacade, token: PathToken) -> Path | None:
    """Where each token lives on macOS.

    Application Support stands in for all three Windows appdata tokens: Unity
    games ported to the Mac put what Windows calls LocalLow there, so plugins
    can use one pattern list per game where the layout matches.
    """
    home = system.home()
    library = home / "Library"
    match token:
        case PathToken.APPDATA | PathToken.LOCALAPPDATA | PathToken.LOCALLOW:
            return library / "Application Support"
        case PathToken.DOCUMENTS:
            return home / "Documents"
        case PathToken.SAVEDGAMES:
            return None  # no macOS equivalent
        case PathToken.USERPROFILE | PathToken.HOME:
            return home
        case PathToken.STEAM:
            return library / "Application Support" / "Steam"
        case PathToken.CONTAINERS:
            return library / "Containers"
        case PathToken.PREFS:
            return library / "Preferences"
        case PathToken.SAVESMITH_DATA:
            return library / "Application Support" / "SaveSmith"
        case PathToken.WINEUSER:
            return None
    return None


def _env_path(system: SystemFacade, name: str) -> Path | None:
    value = system.env(name)
    return Path(value.replace("\\", "/")) if value else None
