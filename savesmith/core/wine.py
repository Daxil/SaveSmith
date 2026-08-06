"""Windows games running on a Mac: Whisky, CrossOver, plain Wine.

A meaningful share of Mac gamers run their Windows library through a
translation layer, and no save editor covers them. The saves are there — they
just live inside a bottle::

    ~/Library/Containers/com.isaacmarovitz.Whisky/Bottles/<uuid>/
        drive_c/users/crossover/AppData/LocalLow/Team Cherry/Hollow Knight/

Once a bottle is found, it is presented as a :class:`SystemFacade` like any
other machine, so the ordinary Windows token table resolves inside it. No
Wine-specific path logic anywhere else in the codebase.

Virtual machines (Parallels, VMware, UTM) are deliberately out of scope: the
guest's files live inside a disk image, and there is no live filesystem to read
from the host. Run SaveSmith inside the VM instead.
"""

from __future__ import annotations

import os
import plistlib
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from savesmith.core.errors import AmbiguousWineUserError, WinePrefixError
from savesmith.core.paths import KnownFolder, PathResolver, RegistryHive, SystemFacade, locate
from savesmith.core.platform_ import Platform

# Profiles Windows creates itself; none of them holds a player's saves.
_NON_USER_PROFILES = frozenset(
    {"public", "default", "default user", "all users", "defaultuser0", "desktop.ini"}
)

_WINDOWS_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.DOTALL)

# A scan walks unknown directory trees, so it needs a stop. A count is used
# rather than a clock so the behaviour is identical in tests and on a slow disk.
_DEFAULT_VISIT_BUDGET = 4000


class BottleKind(StrEnum):
    WHISKY = "whisky"
    CROSSOVER = "crossover"
    WINE = "wine"


@dataclass(frozen=True)
class WinePrefix:
    """One Wine bottle on disk."""

    path: Path
    kind: BottleKind
    name: str
    users: tuple[str, ...]
    """Every Windows profile inside the bottle that could belong to the player."""

    @property
    def drive_c(self) -> Path:
        return self.path / "drive_c"

    def preferred_user(self, system: SystemFacade) -> str:
        """The one profile to use, or raise rather than guess.

        Guessing wrong means editing a save the player never made, which is
        worse than one extra question.
        """
        if not self.users:
            raise WinePrefixError(str(self.path), "it has no Windows user profiles")
        if len(self.users) == 1:
            return self.users[0]
        host_user = system.username().lower()
        exact = [user for user in self.users if user.lower() == host_user]
        if len(exact) == 1:
            return exact[0]
        raise AmbiguousWineUserError(str(self.path), self.users)

    def system(self, username: str) -> BottleSystem:
        """Present this bottle as a Windows machine."""
        return BottleSystem(self, username)

    def resolver(self, username: str) -> PathResolver:
        """A resolver where ``{LOCALLOW}`` and friends point inside the bottle."""
        return PathResolver(self.system(username), extra_tokens={"WINEUSER": username})


def default_roots(system: SystemFacade) -> list[Path]:
    """Where the common tools keep their bottles.

    The platform comes from the facade rather than from the host, so the
    "no bottles on Windows" branch is testable from either OS.
    """
    if system.platform is not Platform.MACOS:
        return []  # Wine bottles are a macOS/Linux concept
    home = system.home()
    app_support = home / "Library" / "Application Support"
    return [
        home / "Library" / "Containers" / "com.isaacmarovitz.Whisky" / "Bottles",
        app_support / "Whisky" / "Bottles",
        app_support / "CrossOver" / "Bottles",
        home / ".wine",
        home / "Wine Prefixes",
    ]


def is_prefix(path: Path) -> bool:
    """A bottle is ``drive_c`` plus at least one registry hive.

    ``drive_c`` alone is not enough: half-deleted bottles and unrelated folders
    called drive_c both exist, and treating them as bottles produces confusing
    empty results later.
    """
    if not (path / "drive_c").is_dir():
        return False
    return any((path / hive).is_file() for hive in ("system.reg", "user.reg", "userdef.reg"))


def scan_prefixes(
    system: SystemFacade,
    extra_roots: Sequence[Path] = (),
    *,
    max_depth: int = 2,
    visit_budget: int = _DEFAULT_VISIT_BUDGET,
) -> list[WinePrefix]:
    """Find every bottle under the known locations plus any the user added.

    ``extra_roots`` is for the folder a user picks by hand; it is searched a
    little deeper because nobody organises their disk the way we expect.
    """
    budget = _Budget(visit_budget)
    found: dict[Path, WinePrefix] = {}
    for root in (*default_roots(system), *extra_roots):
        for prefix_path in _find_under(root, max_depth, budget):
            resolved = prefix_path.resolve()
            if resolved not in found:
                found[resolved] = _describe(prefix_path)
    return sorted(found.values(), key=lambda prefix: (prefix.name.lower(), str(prefix.path)))


class _Budget:
    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    def spend(self) -> bool:
        self.remaining -= 1
        return self.remaining > 0


def _find_under(root: Path, max_depth: int, budget: _Budget) -> Iterator[Path]:
    if not root.is_dir():
        return
    if is_prefix(root):
        yield root
        return  # bottles do not nest
    if max_depth <= 0 or not budget.spend():
        return
    try:
        with os.scandir(root) as entries:
            # Symlinks are not followed: CrossOver bottles link Documents back
            # to the real home folder, and following that walks the whole disk.
            children = sorted(
                entry.name for entry in entries if entry.is_dir(follow_symlinks=False)
            )
    except OSError:
        return
    for name in children:
        yield from _find_under(root / name, max_depth - 1, budget)


def _describe(path: Path) -> WinePrefix:
    return WinePrefix(
        path=path,
        kind=_kind_of(path),
        name=_name_of(path),
        users=tuple(_profiles_in(path)),
    )


_WHISKY_MARKERS = frozenset({"whisky", "com.isaacmarovitz.whisky"})
_CROSSOVER_MARKERS = frozenset({"crossover", "com.codeweavers.crossover"})


def _kind_of(path: Path) -> BottleKind:
    """Which tool made this bottle, judged by whole path components.

    Substring matching would be wrong: a bottle under "My Whisky Notes" — or a
    pytest temp directory named after this very test — is not a Whisky bottle.
    """
    components = {part.lower() for part in path.parts}
    if components & _WHISKY_MARKERS:
        return BottleKind.WHISKY
    if components & _CROSSOVER_MARKERS:
        return BottleKind.CROSSOVER
    return BottleKind.WINE


def _name_of(path: Path) -> str:
    """A name a person recognises.

    Whisky names bottles with a UUID and keeps the real name in a plist; using
    the folder name there would show the player a row of hex digits.
    """
    metadata = path / "Metadata.plist"
    if metadata.is_file():
        try:
            with metadata.open("rb") as handle:
                loaded = plistlib.load(handle)
            name = loaded.get("Name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except (OSError, plistlib.InvalidFileException, AttributeError):
            pass  # fall through to the folder name
    return path.name


def _profiles_in(prefix: Path) -> list[str]:
    users_dir = locate(prefix / "drive_c", "users")
    if users_dir is None:
        return []
    try:
        with os.scandir(users_dir) as entries:
            names = [
                entry.name
                for entry in entries
                # follow_symlinks stays on here: CrossOver's "crossover"
                # profile is often a link, and it is a real profile.
                if entry.is_dir() and entry.name.lower() not in _NON_USER_PROFILES
            ]
    except OSError:
        return []
    return sorted(names)


class BottleSystem:
    """A bottle, presented as if it were a Windows machine.

    This is what lets the ordinary Windows token table work unchanged: the
    resolver asks for ``LOCAL_APPDATA_LOW`` and gets a folder inside the bottle.
    """

    def __init__(self, prefix: WinePrefix, username: str) -> None:
        self._prefix = prefix
        self._username = username
        self._registry: dict[RegistryHive, dict[str, dict[str, str]]] = {}

    @property
    def platform(self) -> Platform:
        return Platform.WINDOWS  # from inside the bottle, this *is* Windows

    @property
    def prefix(self) -> WinePrefix:
        return self._prefix

    def env(self, name: str) -> str | None:
        # A bottle's environment belongs to the Wine process, not to us.
        return None

    def home(self) -> Path:
        return self._profile()

    def username(self) -> str:
        return self._username

    def _profile(self) -> Path:
        users = locate(self._prefix.drive_c, "users")
        base = users if users is not None else self._prefix.drive_c / "users"
        return locate(base, self._username) or base / self._username

    def known_folder(self, folder: KnownFolder) -> Path | None:
        """The Windows folder layout as Wine creates it.

        ``DOCUMENTS`` is a plain folder here — there is no OneDrive inside a
        bottle, so the redirect that complicates the real Windows case does not
        apply.
        """
        profile = self._profile()
        appdata = profile / "AppData"
        match folder:
            case KnownFolder.PROFILE:
                return profile
            case KnownFolder.ROAMING_APPDATA:
                return _existing(appdata / "Roaming")
            case KnownFolder.LOCAL_APPDATA:
                return _existing(appdata / "Local")
            case KnownFolder.LOCAL_APPDATA_LOW:
                return _existing(appdata / "LocalLow")
            case KnownFolder.DOCUMENTS:
                return _existing(profile / "Documents") or _existing(profile / "My Documents")
            case KnownFolder.SAVED_GAMES:
                return _existing(profile / "Saved Games")
            case KnownFolder.PUBLIC:
                return _existing(self._prefix.drive_c / "users" / "Public")
        return None

    def registry_read(self, hive: RegistryHive, key: str, value_name: str) -> str | None:
        """Read Wine's ``.reg`` text files instead of a real registry.

        Values that look like Windows paths come back translated to host paths.
        Returning ``C:\\Program Files (x86)\\Steam`` would be useless to every
        caller, since nothing on the Mac can open it.
        """
        raw = _registry_value(self._hive_data(hive), key, value_name)
        if raw is None:
            return None
        translated = self.translate(raw)
        return str(translated) if translated is not None else raw

    def _hive_data(self, hive: RegistryHive) -> dict[str, dict[str, str]]:
        if hive not in self._registry:
            filename = "user.reg" if hive is RegistryHive.HKCU else "system.reg"
            self._registry[hive] = _parse_reg(self._prefix.path / filename)
        return self._registry[hive]

    def translate(self, windows_path: str) -> Path | None:
        """Turn ``D:\\Games\\save.dat`` into a real path on this Mac.

        Drive letters are resolved through ``dosdevices``, which is how Wine
        itself maps them — a bottle can point ``d:`` anywhere.
        """
        match = _WINDOWS_PATH_RE.match(windows_path.strip())
        if match is None:
            return None
        letter, remainder = match.group(1).lower(), match.group(2)

        target = locate(self._prefix.path, "dosdevices", f"{letter}:")
        if target is None and letter == "c":
            target = self._prefix.drive_c
        if target is None or not target.exists():
            return None

        root = Path(os.path.realpath(target))
        parts = [part for part in remainder.replace("\\", "/").split("/") if part]
        if not parts:
            return root
        return locate(root, *parts) or root.joinpath(*parts)


def _existing(path: Path) -> Path | None:
    return path if path.is_dir() else None


def _parse_reg(path: Path) -> dict[str, dict[str, str]]:
    r"""Parse a Wine ``.reg`` file into ``{key: {value: data}}``.

    The format is Windows' own registry export, one section per key::

        [Software\\Valve\\Steam] 1700000000
        "SteamPath"="C:\\Program Files (x86)\\Steam"

    Only string and dword values are kept: nothing SaveSmith reads is binary,
    and skipping the rest keeps a corrupt hive from stopping a scan.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    hive: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        if stripped.startswith("["):
            end = stripped.find("]")
            if end == -1:
                continue
            key = _unescape(stripped[1:end])
            current = hive.setdefault(key.lower(), {})
            continue
        if current is None or "=" not in stripped:
            continue
        name, _, raw = stripped.partition("=")
        name = _unescape(name.strip().strip('"')) or "@"
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            current[name.lower()] = _unescape(raw[1:-1])
        elif raw.startswith("dword:"):
            try:
                current[name.lower()] = str(int(raw[6:], 16))
            except ValueError:
                continue
    return hive


def _unescape(text: str) -> str:
    return text.replace("\\\\", "\\").replace('\\"', '"')


def _registry_value(
    hive: dict[str, dict[str, str]], key: str, value_name: str
) -> str | None:
    entry = hive.get(key.replace("/", "\\").lower())
    if entry is None:
        return None
    return entry.get(value_name.lower())


def describe_prefixes(prefixes: Iterable[WinePrefix]) -> list[str]:
    """One human-readable line per bottle, for diagnostics and the GUI."""
    lines = []
    for prefix in prefixes:
        users = ", ".join(prefix.users) or "no profiles"
        lines.append(f"{prefix.name} [{prefix.kind.value}] {prefix.path} (users: {users})")
    return lines
