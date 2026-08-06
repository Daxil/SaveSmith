"""Reading Steam's own bookkeeping: libraries, installed games, user folders.

Steam is the fastest way to answer "which games does this person own and where
did they land on disk", and it answers identically on Windows and macOS. That
matters twice over: it feeds the risk classifier in milestone 3, which needs
the AppID to look a game up in ``risk_db.json``.

Nothing here writes. Nothing here judges whether a game is safe to edit — that
is milestone 3's job. This module only reports what is installed.

One rule throughout: a damaged or unreachable file downgrades to a recorded
problem, never to an exception that aborts the scan. A user with one library on
an unplugged external drive must still see the games on the drives that are
plugged in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from savesmith.core import vdf
from savesmith.core.errors import SaveSmithError, SteamDataError, SteamNotFoundError, VdfParseError
from savesmith.core.paths import PathResolver, SystemFacade, extended_length_path, locate

# Where libraryfolders.vdf has lived across client versions, best first.
_LIBRARY_INDEX_LOCATIONS = (
    ("steamapps", "libraryfolders.vdf"),
    ("config", "libraryfolders.vdf"),
    ("SteamApps", "libraryfolders.vdf"),
)

# Anything that marks a folder as an actual Steam install rather than a
# leftover empty directory.
_STEAM_MARKERS = ("steamapps", "SteamApps", "config", "userdata")


@dataclass(frozen=True)
class SteamLibrary:
    """One folder Steam installs games into."""

    path: Path
    label: str = ""
    available: bool = True
    """False when the folder is listed but not reachable — an external drive
    that is currently unplugged, or a network share that is offline."""

    @property
    def steamapps(self) -> Path | None:
        return locate(self.path, "steamapps")


@dataclass(frozen=True)
class InstalledGame:
    appid: int
    name: str
    install_dir: Path
    """Where the game's files are. May not exist if an install was interrupted."""
    library: SteamLibrary
    manifest_path: Path
    size_on_disk: int | None = None
    last_updated: int | None = None

    @property
    def is_installed(self) -> bool:
        return self.install_dir.is_dir()


@dataclass(frozen=True)
class SteamUser:
    """A local Steam account folder under ``userdata``."""

    account_id: str
    path: Path

    def cloud_cache(self, appid: int) -> Path | None:
        """``remotecache.vdf`` for one game, if Steam Cloud has touched it.

        Its presence is the signal milestone 3 uses to decide whether to show
        the Steam Cloud wizard before allowing an edit.
        """
        return locate(self.path, str(appid), "remotecache.vdf")


@dataclass
class SteamScan:
    """Everything one pass over a Steam install found.

    ``problems`` carries what went wrong without stopping the scan. The GUI
    shows them as warnings next to an otherwise usable game list.
    """

    root: Path
    libraries: list[SteamLibrary] = field(default_factory=list)
    games: list[InstalledGame] = field(default_factory=list)
    users: list[SteamUser] = field(default_factory=list)
    problems: list[SaveSmithError] = field(default_factory=list)

    def game_by_appid(self, appid: int) -> InstalledGame | None:
        return next((game for game in self.games if game.appid == appid), None)


class SteamInstall:
    """A Steam installation on this machine."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def discover(cls, system: SystemFacade) -> SteamInstall:
        """Find Steam, or raise :class:`SteamNotFoundError`.

        Not finding Steam is not fatal to the app — games can be added by hand —
        so callers are expected to catch this and carry on.
        """
        resolver = PathResolver(system)
        candidate = resolver.token("STEAM")
        searched = (str(candidate),) if candidate else ()
        if candidate is None or not candidate.is_dir():
            raise SteamNotFoundError(searched=searched)
        if not any(locate(candidate, marker) for marker in _STEAM_MARKERS):
            raise SteamNotFoundError(
                searched=searched,
                detail=f"{candidate} exists but has none of {_STEAM_MARKERS}",
            )
        return cls(candidate)

    # -- scanning --------------------------------------------------------

    def scan(self) -> SteamScan:
        """One full pass: libraries, installed games, local accounts."""
        result = SteamScan(root=self.root)
        result.libraries = self._libraries(result.problems)
        for library in result.libraries:
            if library.available:
                result.games += self._games_in(library, result.problems)
        result.games.sort(key=lambda game: game.name.lower())
        result.users = self._users()
        return result

    def _library_index(self) -> Path | None:
        for segments in _LIBRARY_INDEX_LOCATIONS:
            found = locate(self.root, *segments)
            if found is not None and found.is_file():
                return found
        return None

    def _libraries(self, problems: list[SaveSmithError]) -> list[SteamLibrary]:
        """Read libraryfolders.vdf, in whichever of its three shapes it is in.

        The Steam root is always a library, even in the oldest files that do
        not list it.
        """
        libraries: list[SteamLibrary] = [self._as_library(self.root, label="Steam")]

        index = self._library_index()
        if index is None:
            # Normal on a fresh install with a single library.
            return libraries

        try:
            parsed = vdf.load_file(index)
        except VdfParseError as exc:
            problems.append(
                SteamDataError(str(index), "the library list is damaged", detail=exc.detail)
            )
            return libraries

        block = vdf.get_dict(parsed, "libraryfolders")
        if not block:
            # Some builds omit the wrapping key entirely.
            block = parsed

        for key, value in block.items():
            if not key.isdigit():
                continue  # TimeNextStatsReport, ContentStatsID and friends
            if isinstance(value, str):
                raw_path, label = value, ""  # generation 1: "1" "D:\\SteamLibrary"
            else:
                raw_path = vdf.get_str(value, "path") or ""
                label = vdf.get_str(value, "label") or ""
            if not raw_path:
                problems.append(
                    SteamDataError(str(index), f"library entry {key} has no path")
                )
                continue
            libraries.append(self._as_library(Path(raw_path.replace("\\", "/")), label=label))

        return _deduplicate(libraries)

    def _as_library(self, path: Path, *, label: str = "") -> SteamLibrary:
        # is_dir() on an unplugged drive is False rather than an exception,
        # which is exactly the "listed but unavailable" case.
        return SteamLibrary(path=path, label=label, available=path.is_dir())

    def _games_in(
        self, library: SteamLibrary, problems: list[SaveSmithError]
    ) -> list[InstalledGame]:
        steamapps = library.steamapps
        if steamapps is None:
            return []
        games: list[InstalledGame] = []
        for manifest in _appmanifests(steamapps):
            game = self._read_manifest(manifest, library, problems)
            if game is not None:
                games.append(game)
        return games

    def _read_manifest(
        self, manifest: Path, library: SteamLibrary, problems: list[SaveSmithError]
    ) -> InstalledGame | None:
        try:
            parsed = vdf.load_file(manifest)
        except VdfParseError as exc:
            problems.append(
                SteamDataError(
                    str(manifest),
                    f"the entry for one game is damaged, so it is missing from the list "
                    f"({manifest.name})",
                    detail=exc.detail,
                )
            )
            return None

        appid = vdf.get_int(parsed, "AppState", "appid") or _appid_from_filename(manifest)
        if appid is None:
            problems.append(SteamDataError(str(manifest), "a game entry has no AppID"))
            return None

        install_dir_name = vdf.get_str(parsed, "AppState", "installdir") or ""
        common = locate(library.path, "steamapps", "common")
        install_dir = (
            (locate(common, install_dir_name) or common / install_dir_name)
            if common is not None and install_dir_name
            else library.path / "steamapps" / "common" / install_dir_name
        )

        return InstalledGame(
            appid=appid,
            name=vdf.get_str(parsed, "AppState", "name") or f"App {appid}",
            install_dir=install_dir,
            library=library,
            manifest_path=manifest,
            size_on_disk=vdf.get_int(parsed, "AppState", "SizeOnDisk"),
            last_updated=vdf.get_int(parsed, "AppState", "LastUpdated"),
        )

    def _users(self) -> list[SteamUser]:
        userdata = locate(self.root, "userdata")
        if userdata is None:
            return []
        users: list[SteamUser] = []
        try:
            with os.scandir(extended_length_path(userdata)) as entries:
                for entry in entries:
                    # "0" and "anonymous" appear next to real account folders.
                    if entry.is_dir() and entry.name.isdigit() and entry.name != "0":
                        users.append(SteamUser(account_id=entry.name, path=userdata / entry.name))
        except OSError:
            return []
        return sorted(users, key=lambda user: user.account_id)


def _appmanifests(steamapps: Path) -> list[Path]:
    try:
        with os.scandir(extended_length_path(steamapps)) as entries:
            names = [
                entry.name
                for entry in entries
                if entry.is_file()
                and entry.name.lower().startswith("appmanifest_")
                and entry.name.lower().endswith(".acf")
            ]
    except OSError:
        return []
    return [steamapps / name for name in sorted(names)]


def _appid_from_filename(manifest: Path) -> int | None:
    digits = manifest.stem.split("_", 1)[-1]
    return int(digits) if digits.isdigit() else None


def _deduplicate(libraries: list[SteamLibrary]) -> list[SteamLibrary]:
    """Steam lists its own root in newer files, so it arrives twice."""
    seen: dict[str, SteamLibrary] = {}
    for library in libraries:
        key = str(library.path).rstrip("/\\").lower()
        existing = seen.get(key)
        # Prefer the entry that carries a label from the index file.
        if existing is None or (not existing.label and library.label):
            seen[key] = library
    return list(seen.values())
