"""Every game on this machine that SaveSmith knows how to reach.

The alternative is asking the user for a path, and a path is the one thing they
reliably do not have. They know they own the game; they do not know that Steam
put it in ``steamapps/common`` inside a bottle inside an ``.app`` in their home
folder. So the program goes and looks.

Four places, because those are the four that can be enumerated honestly rather
than guessed at:

* **Steam on this machine** — its own manifests say what is installed and where.
* **Steam inside a Wine bottle** — the same manifests, read through the bottle.
  A Mac user running Windows games has their whole library here and nowhere the
  host can see.
* **Wine bottles themselves** — a wrapper called ``Elden Ring.app`` is a game
  even when there is no Steam inside it to ask.
* **Mac applications** — but only the ones that show an engine's fingerprint.
  Listing every ``.app`` in /Applications would bury four games under sixty
  utilities, and "is this a game?" is not a question a file browser can answer.

Nothing here reads a save or decides anything about risk. It produces a list to
choose from; :func:`savesmith.core.discover.look_at` takes it from there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from savesmith.core.errors import SaveSmithError
from savesmith.core.paths import PathResolver, SystemFacade, locate
from savesmith.core.plugin import Plugin
from savesmith.core.repository import bundled
from savesmith.core.steam import SteamInstall
from savesmith.core.wine import (
    WRAPPER_SUFFIX,
    WinePrefix,
    application_folders,
    scan_prefixes,
)

# Where Steam installs itself inside a Windows filesystem.
_STEAM_IN_BOTTLE = (
    ("Program Files (x86)", "Steam"),
    ("Program Files", "Steam"),
)

# What an engine leaves inside a Mac application bundle.
_MAC_ENGINE_MARKERS = (
    "UnityPlayer.dylib",
    "UnrealEngine",
    "Data",  # Unity's Contents/Resources/Data
    "godot",
)

_MAX_APPS_PER_FOLDER = 300

# Steam installs its own plumbing into steamapps alongside the games. It has
# appids and manifests like everything else, and no saves whatsoever.
_NOT_GAMES = (
    "steamworks common redistributables",
    "steam controller configs",
    "steam linux runtime",
    "proton",
    "steamvr",
    "steam runtime",
)


def _is_plumbing(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _NOT_GAMES)


@dataclass(frozen=True)
class Game:
    """One game the user could pick, and where to point SaveSmith at it."""

    name: str
    path: Path
    """What to hand to ``look_at`` — an install folder or an application."""

    source: str
    """Where this was found, in words, for the interface to group by."""

    steam_appid: int | None = None
    bottle: str | None = None
    """The bottle's name, when the game lives inside one."""

    installed: bool = True

    @property
    def key(self) -> str:
        """Identity for de-duplication, since one game can be found twice."""
        return str(self.path).rstrip("/").lower()


@dataclass
class Library:
    games: list[Game] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """What could not be read, without stopping the rest of the scan."""

    def add(self, game: Game) -> None:
        if not any(existing.key == game.key for existing in self.games):
            self.games.append(game)

    def sorted(self) -> list[Game]:
        return sorted(self.games, key=lambda game: (game.source, game.name.lower()))


def scan(system: SystemFacade) -> Library:
    """Everything findable, most trustworthy source first.

    Never raises. A machine with no Steam, no bottles and no games is a valid
    answer — an empty list with an explanation beats an error the user cannot
    act on.
    """
    library = Library()
    _steam_here(system, library)
    bottles = _bottles(system, library)
    for prefix in bottles:
        _steam_in_bottle(prefix, library)
    _mac_applications(system, library, bottles)
    _games_known_by_their_saves(system, library, bottles)
    return library


# ---------------------------------------------------------------------------
# The sources
# ---------------------------------------------------------------------------


def _steam_here(system: SystemFacade, library: Library) -> None:
    try:
        found = SteamInstall.discover(system).scan()
    except SaveSmithError:
        return  # no Steam on this machine, which is not a problem to report
    for game in found.games:
        if _is_plumbing(game.name):
            continue
        library.add(
            Game(
                name=game.name,
                path=game.install_dir,
                source="Steam",
                steam_appid=game.appid,
                installed=game.is_installed,
            )
        )


def _bottles(system: SystemFacade, library: Library) -> list[WinePrefix]:
    try:
        return list(scan_prefixes(system))
    except SaveSmithError as problem:
        library.problems.append(problem.user_message)
        return []


def _steam_in_bottle(prefix: WinePrefix, library: Library) -> None:
    """The Windows Steam library living inside a bottle.

    Read through the bottle's own filesystem, so the install paths that come
    back are real paths on this disk and can be handed straight to the rest of
    the program.
    """
    for segments in _STEAM_IN_BOTTLE:
        root = locate(prefix.drive_c, *segments)
        if root is None:
            continue
        try:
            found = SteamInstall(root).scan()
        except SaveSmithError as problem:
            library.problems.append(problem.user_message)
            return
        for game in found.games:
            if _is_plumbing(game.name):
                continue
            library.add(
                Game(
                    name=game.name,
                    path=game.install_dir,
                    source=f"Steam в бутылке {prefix.name}",
                    steam_appid=game.appid,
                    bottle=prefix.name,
                    installed=game.is_installed,
                )
            )
        return


def _mac_applications(
    system: SystemFacade, library: Library, bottles: list[WinePrefix]
) -> None:
    """Mac applications: wrappers by name, native games by engine fingerprint."""
    wrapped = {prefix.path for prefix in bottles}
    for folder in application_folders(system):
        for bundle in _bundles_in(folder):
            inside = bundle.joinpath(*WRAPPER_SUFFIX)
            if inside in wrapped or inside.is_dir():
                # A wrapper is one game with a Windows filesystem inside it.
                library.add(
                    Game(
                        name=bundle.stem,
                        path=bundle,
                        source="Программы (Windows-обёртка)",
                        bottle=bundle.stem,
                    )
                )
            elif _looks_like_a_mac_game(bundle):
                library.add(Game(name=bundle.stem, path=bundle, source="Программы"))


def _games_known_by_their_saves(
    system: SystemFacade, library: Library, bottles: list[WinePrefix]
) -> None:
    """Games found by the saves they left, not by being installed.

    A game can be uninstalled, or installed in one bottle while its saves sit
    in another, and the save is still the thing the player came for. Worse: the
    same game in two bottles gives two different saves that both look right,
    and a list that shows only one of them silently hands over the wrong
    playthrough — which is exactly what happened, twice, with a level 575
    character shown in place of a level 713 one.

    Each plugin already says where its game keeps saves. Those patterns are
    resolved inside every bottle and on the host, so a game turns up once per
    place it actually has a save.
    """
    for plugin in bundled().load().plugins:
        _saves_of(plugin, system, library, bottles)


def _saves_of(
    plugin: Plugin, system: SystemFacade, library: Library, bottles: list[WinePrefix]
) -> None:
    # A host pattern is absolute — {APPDATA}/… — while a bottle pattern is
    # written relative to the bottle's own root, because that is the only thing
    # a plugin author can know about somebody else's disk.
    machines: list[tuple[str | None, PathResolver, tuple[str, ...]]] = [
        (None, PathResolver(system), plugin.detect.patterns_for(system.platform))
    ]
    for prefix in bottles:
        try:
            user = prefix.preferred_user(system)
        except SaveSmithError:
            continue  # several profiles and no way to tell whose save it is
        rooted = tuple(
            f"{prefix.path.as_posix()}/{pattern}" for pattern in plugin.detect.wine_patterns
        )
        machines.append((prefix.name, prefix.resolver(user), rooted))

    for bottle, resolver, patterns in machines:
        folders: list[Path] = []
        for pattern in patterns:
            try:
                found = resolver.resolve(pattern)
            except SaveSmithError:
                continue
            for path in found:
                # One entry per place, not per file. A game with sixty-two
                # rolling backups is still one game somebody wants to open,
                # and the folder is what the rest of the program takes.
                folder = path if path.is_dir() else path.parent
                if folder not in folders:
                    folders.append(folder)

        # Already listed for this bottle by its installation; the save folder
        # would be the same game a second time under a different heading.
        if any(game.name == plugin.game and game.bottle == bottle for game in library.games):
            continue

        for folder in folders:
            library.add(
                Game(
                    name=plugin.game,
                    path=folder,
                    source=f"Сохранения в бутылке {bottle}" if bottle else "Сохранения",
                    steam_appid=plugin.steam_appid,
                    bottle=bottle,
                )
            )


def _bundles_in(folder: Path) -> list[Path]:
    try:
        with os.scandir(folder) as entries:
            bundles = [
                Path(entry.path)
                for entry in entries
                if entry.name.endswith(".app") and entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return []
    return sorted(bundles)[:_MAX_APPS_PER_FOLDER]


def _looks_like_a_mac_game(bundle: Path) -> bool:
    """Whether a Mac application was built by a game engine.

    Deliberately narrow. A game this misses can still be pointed at by hand; a
    text editor this lets through makes the whole list untrustworthy.
    """
    resources = bundle / "Contents" / "Resources"
    frameworks = bundle / "Contents" / "Frameworks"
    for base in (resources, frameworks, bundle / "Contents"):
        for marker in _MAC_ENGINE_MARKERS:
            if locate(base, marker) is not None:
                return True
    return False
