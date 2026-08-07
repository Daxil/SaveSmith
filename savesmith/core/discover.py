"""Point at a game folder, get its save files.

The user should not have to know where a game hides its saves — that is the
whole problem SaveSmith exists to solve. They pick the folder the game is
installed in, and everything else follows from what is in it.

Games do not store saves next to themselves, but they do announce which engine
built them, and each engine puts saves in a place that follows from the game's
name::

    <install>/Engine/ + <install>/TheInvincible/   → Unreal
        saves at {LOCALAPPDATA}/TheInvincible/Saved/SaveGames

    <install>/MyGame_Data/app.info                 → Unity
        saves at {LOCALLOW}/<company>/<product>

    <install>/www/save/                            → RPG Maker
        saves right there

So: recognise the engine, work out where it would have put things, look there,
and run whatever is found through the decoder ladder. The same pass notices
anti-cheat components, because the install folder is exactly where they live
and the risk tier depends on it.

For Unity games the search does not stop at files. Plenty of them keep real
progress in PlayerPrefs — the registry on Windows, a property list on macOS —
and a scan that only walks the filesystem reports "no saves found" for a game
whose coins are sitting in plain sight somewhere else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from savesmith.core import detect
from savesmith.core.detect import Report
from savesmith.core.errors import SaveSmithError
from savesmith.core.paths import PathResolver, SystemFacade, locate
from savesmith.core.playerprefs import Entry, open_prefs

# Files that are never a save, however promising the folder.
_IGNORED_SUFFIXES = frozenset(
    {
        ".exe", ".dll", ".so", ".dylib", ".sys", ".pak", ".uasset", ".umap", ".bk2",
        ".bnk", ".wem", ".ogg", ".wav", ".mp3", ".mp4", ".bik", ".png", ".jpg",
        ".dds", ".ttf", ".otf", ".pdb", ".manifest", ".txt", ".log", ".ini", ".cfg",
        ".xml", ".html", ".css", ".js", ".pck", ".assets", ".resource", ".bundle",
    }
)

# Names that look like saves even without a familiar extension.
_SAVE_HINTS = ("save", "slot", "profile", "player", "user", "progress", "career")

_ANTICHEAT_MARKERS = (
    "easyanticheat",
    "battleye",
    "_eac",
    "anticheat",
    "denuvo",
    "nprotect",
    "gameguard",
)

# A save is small. Anything larger is game data that merely sits in the way.
_MAX_SAVE_BYTES = 64 * 1024 * 1024
_MAX_FILES_PER_LOCATION = 400
_MAX_DEPTH = 4


class Engine(StrEnum):
    UNREAL = "unreal"
    UNITY = "unity"
    RPGMAKER = "rpgmaker"
    GODOT = "godot"
    GAMEMAKER = "gamemaker"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GameFolder:
    """What an installed game says about itself."""

    path: Path
    title: str
    engine: Engine
    evidence: tuple[str, ...] = ()
    project: str | None = None
    """Unreal project or Unity product name — what the save folder is named after."""
    company: str | None = None
    steam_appid: int | None = None
    anticheat: tuple[str, ...] = ()

    @property
    def has_anticheat(self) -> bool:
        return bool(self.anticheat)

    def summary(self) -> str:
        parts = [self.title, f"engine: {self.engine.value}"]
        if self.steam_appid:
            parts.append(f"Steam {self.steam_appid}")
        if self.anticheat:
            parts.append("anti-cheat: " + ", ".join(self.anticheat))
        return " | ".join(parts)


@dataclass(frozen=True)
class FoundSave:
    path: Path
    location: str
    """Where this came from, in words, for the interface."""
    report: Report

    @property
    def recognised(self) -> bool:
        return self.report.solved

    @property
    def format(self) -> str:
        best = self.report.best
        return best.description if best else "unknown"

    @property
    def size(self) -> int:
        return self.report.look.size


@dataclass(frozen=True)
class FoundPrefs:
    """A Unity game's PlayerPrefs, wherever this platform keeps them."""

    location: str
    entries: tuple[Entry, ...]

    @property
    def numbers(self) -> tuple[Entry, ...]:
        """The ones worth showing first — currency and counters look like this."""
        return tuple(
            entry
            for entry in self.entries
            if isinstance(entry.value, int | float) and not isinstance(entry.value, bool)
        )


@dataclass
class Discovery:
    game: GameFolder
    searched: list[Path] = field(default_factory=list)
    saves: list[FoundSave] = field(default_factory=list)
    prefs: FoundPrefs | None = None

    @property
    def recognised(self) -> list[FoundSave]:
        return [save for save in self.saves if save.recognised]

    @property
    def found_anything(self) -> bool:
        return bool(self.saves) or bool(self.prefs and self.prefs.entries)

    def explain(self) -> list[str]:
        lines = [self.game.summary(), ""]
        lines += [f"Looked in: {path}" for path in self.searched]
        if self.prefs is not None:
            lines.append(f"Looked in: {self.prefs.location} (Unity settings)")
        if not self.found_anything:
            lines.append("No save files found in any of those places.")
            return lines
        if self.saves:
            lines.append("")
            for save in self.saves[:20]:
                mark = "✓" if save.recognised else "?"
                lines.append(f"  {mark} {save.path.name} — {save.format} ({save.size} bytes)")
        if self.prefs is not None and self.prefs.entries:
            lines += ["", f"Unity settings ({len(self.prefs.entries)} of them):"]
            shown = self.prefs.numbers or self.prefs.entries
            for entry in shown[:20]:
                lines.append(f"  ✓ {entry.name} = {entry.value} ({entry.kind})")
            if len(shown) > 20:
                lines.append(f"  … and {len(shown) - 20} more")
            lines.append("  Change one with: savesmith prefs --game-folder <folder> --set NAME VAL")
        return lines


def examine(folder: Path) -> GameFolder:
    """Work out what this installed game is, from its own files."""
    title = folder.name
    entries = _entries(folder)
    lowered = {name.lower(): name for name in entries}

    engine, project, company, evidence = _engine_of(folder, entries, lowered)
    return GameFolder(
        path=folder,
        title=title,
        engine=engine,
        evidence=evidence,
        project=project,
        company=company,
        steam_appid=_steam_appid(folder),
        anticheat=_anticheat(folder),
    )


def _entries(folder: Path) -> list[str]:
    try:
        with os.scandir(folder) as scan:
            return [entry.name for entry in scan]
    except OSError:
        return []


def _engine_of(
    folder: Path, entries: list[str], lowered: dict[str, str]
) -> tuple[Engine, str | None, str | None, tuple[str, ...]]:
    # Unreal: an Engine folder beside the project folder of the same name as
    # the executable. The project name is what the save folder is called.
    if "engine" in lowered:
        for name in entries:
            candidate = folder / name
            if name.lower() == "engine" or not candidate.is_dir():
                continue
            if locate(candidate, "Binaries") or locate(candidate, "Content"):
                return Engine.UNREAL, name, None, ("Engine/ beside " + name,)
        return Engine.UNREAL, _executable_stem(entries), None, ("Engine/ folder",)

    # Unity: the <Game>_Data folder, which also names the publisher.
    data_folder = next((name for name in entries if name.lower().endswith("_data")), None)
    if data_folder or "unityplayer.dll" in lowered:
        company, product = _unity_identity(folder, data_folder)
        return Engine.UNITY, product, company, (data_folder or "UnityPlayer.dll",)

    # RPG Maker MV and MZ ship a browser runtime and a www folder.
    if "www" in lowered or "nw.exe" in lowered:
        return Engine.RPGMAKER, None, None, ("www/ or nw.exe",)

    if any(name.lower().endswith(".pck") for name in entries):
        return Engine.GODOT, _executable_stem(entries), None, (".pck archive",)

    if any(name.lower().endswith(".win") for name in entries):
        return Engine.GAMEMAKER, _executable_stem(entries), None, ("data.win",)

    return Engine.UNKNOWN, _executable_stem(entries), None, ()


def _executable_stem(entries: list[str]) -> str | None:
    """The game's own executable, ignoring the usual companions."""
    skipped = ("unins", "vcredist", "directx", "setup", "launcher", "crashpad", "eac")
    for name in sorted(entries):
        if not name.lower().endswith(".exe"):
            continue
        if any(word in name.lower() for word in skipped):
            continue
        return name[:-4]
    return None


def _unity_identity(folder: Path, data_folder: str | None) -> tuple[str | None, str | None]:
    """Unity writes the publisher and product into <Game>_Data/app.info."""
    if data_folder is None:
        return None, None
    info = folder / data_folder / "app.info"
    try:
        lines = info.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, data_folder[: -len("_Data")] or None
    company = lines[0].strip() if lines else None
    product = lines[1].strip() if len(lines) > 1 else None
    return company or None, product or None


def _steam_appid(folder: Path) -> int | None:
    marker = locate(folder, "steam_appid.txt")
    if marker is None:
        return None
    try:
        return int(marker.read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return None


def _anticheat(folder: Path, max_depth: int = 2) -> tuple[str, ...]:
    """Anti-cheat components in the install folder.

    Their presence does not stop anything by itself — it sets the risk tier the
    user is shown before they change a thing.
    """
    found: set[str] = set()

    def walk(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            with os.scandir(path) as scan:
                children = list(scan)
        except OSError:
            return
        for entry in children:
            lowered = entry.name.lower()
            for marker in _ANTICHEAT_MARKERS:
                if marker in lowered:
                    found.add(entry.name)
            if entry.is_dir(follow_symlinks=False):
                walk(Path(entry.path), depth + 1)

    walk(folder, 0)
    return tuple(sorted(found))


def save_locations(game: GameFolder, system: SystemFacade) -> list[Path]:
    """Everywhere this game might keep saves, most likely first."""
    resolver = PathResolver(system)
    name = game.project or game.title
    places: list[Path | None] = []

    def token(key: str, *parts: str) -> Path | None:
        base = resolver.token(key)
        return base.joinpath(*parts) if base is not None else None

    match game.engine:
        case Engine.UNREAL:
            places += [
                token("LOCALAPPDATA", name, "Saved", "SaveGames"),
                game.path / name / "Saved" / "SaveGames",
            ]
        case Engine.UNITY:
            if game.company and game.project:
                places += [
                    token("LOCALLOW", game.company, game.project),
                    token("APPDATA", game.company, game.project),
                    token("APPDATA", f"unity.{game.company}.{game.project}"),
                ]
        case Engine.RPGMAKER:
            places += [game.path / "www" / "save", game.path / "save"]
        case Engine.GODOT:
            places += [token("APPDATA", "Godot", "app_userdata", name)]
        case Engine.GAMEMAKER:
            places += [token("LOCALAPPDATA", name), token("APPDATA", name)]
        case Engine.UNKNOWN:
            pass

    # Places any game might use, whatever built it.
    places += [
        token("SAVEDGAMES", name),
        token("DOCUMENTS", "My Games", name),
        token("APPDATA", name),
        token("LOCALAPPDATA", name),
        game.path,
    ]

    seen: list[Path] = []
    for place in places:
        if place is not None and place.is_dir() and place not in seen:
            seen.append(place)
    return seen


def find_saves(
    game: GameFolder,
    system: SystemFacade,
    *,
    max_files: int = _MAX_FILES_PER_LOCATION,
) -> Discovery:
    """Look in every plausible place and identify what is there."""
    result = Discovery(game=game)
    result.searched = save_locations(game, system)

    seen: set[Path] = set()
    for place in result.searched:
        own_folder = place == game.path
        label = "the game's own folder" if own_folder else str(place)
        # The install folder is full of everything except saves, so only names
        # that say "save" are considered there.
        for path in _candidate_files(place, max_files, strict=own_folder):
            resolved = path.resolve()
            if resolved in seen:
                continue  # reached through a parent location as well
            seen.add(resolved)
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            result.saves.append(
                FoundSave(path=path, location=label, report=detect.identify(raw, max_depth=3))
            )

    # Recognised first, then by name so repeated runs read the same.
    result.saves.sort(key=lambda save: (not save.recognised, str(save.path)))
    result.prefs = find_prefs(game, system)
    return result


def find_prefs(game: GameFolder, system: SystemFacade) -> FoundPrefs | None:
    """A Unity game's PlayerPrefs, or ``None`` if there are none to read.

    Never raises. A missing property list, a registry key that is not there, a
    platform with no such concept — all of them mean "nothing here", and none
    of them is a reason for a folder scan to fail.
    """
    if game.engine is not Engine.UNITY or not game.company or not game.project:
        return None
    try:
        store = open_prefs(system, game.company, game.project)
        entries = store.read()
    except SaveSmithError:
        return None
    except OSError:
        return None
    if not entries:
        return None
    return FoundPrefs(
        location=store.location,
        entries=tuple(entries[name] for name in sorted(entries)),
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m savesmith.core.discover <game folder>``.

    Also accepts a folder inside a Wine bottle: the bottle is detected from the
    path, so a Windows game on a Mac needs no special handling from the user.
    """
    import sys

    from savesmith.core.paths import RealSystem
    from savesmith.core.wine import machine_for

    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        print("usage: python -m savesmith.core.discover <game folder>...")
        return 2

    host = RealSystem()
    for name in arguments:
        folder = Path(name).expanduser()
        if not folder.is_dir():
            print(f"{folder}: not a folder")
            continue

        system, note = machine_for(folder, host)
        if note:
            print(f"({note})")

        game = examine(folder)
        for line in find_saves(game, system).explain():
            print(line)
        print()
    return 0


def _candidate_files(folder: Path, budget: int, *, strict: bool = False) -> list[Path]:
    """Files worth reading. ``strict`` demands the name itself suggest a save."""
    found: list[Path] = []

    def walk(path: Path, depth: int, save_ish: bool) -> None:
        if depth > _MAX_DEPTH or len(found) >= budget:
            return
        try:
            with os.scandir(path) as scan:
                children = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            return
        for entry in children:
            if len(found) >= budget:
                return
            child = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                walk(child, depth + 1, save_ish or _named_like_a_save(entry.name))
            elif _plausible(entry.name, entry, strict=strict and not save_ish):
                found.append(child)

    walk(folder, 0, False)
    return found


def _named_like_a_save(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _SAVE_HINTS)


def _plausible(name: str, entry: os.DirEntry[str], *, strict: bool) -> bool:
    lowered = name.lower()
    save_ish = _named_like_a_save(lowered)
    # A known non-save extension is disqualifying, unless the name itself says
    # "save" — some games really do call one savedata.bin.
    if Path(lowered).suffix in _IGNORED_SUFFIXES and not save_ish:
        return False
    if strict and not save_ish:
        return False
    try:
        size = entry.stat(follow_symlinks=False).st_size
    except OSError:
        return False
    return 0 < size <= _MAX_SAVE_BYTES


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
