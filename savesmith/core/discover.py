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
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from savesmith.core import detect
from savesmith.core.detect import Report
from savesmith.core.paths import PathResolver, SystemFacade, locate

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


@dataclass
class Discovery:
    game: GameFolder
    searched: list[Path] = field(default_factory=list)
    saves: list[FoundSave] = field(default_factory=list)

    @property
    def recognised(self) -> list[FoundSave]:
        return [save for save in self.saves if save.recognised]

    def explain(self) -> list[str]:
        lines = [self.game.summary(), ""]
        lines += [f"Looked in: {path}" for path in self.searched]
        if not self.saves:
            lines.append("No save files found in any of those places.")
            return lines
        lines.append("")
        for save in self.saves[:20]:
            mark = "✓" if save.recognised else "?"
            lines.append(f"  {mark} {save.path.name} — {save.format} ({save.size} bytes)")
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
    return result


def main(argv: list[str] | None = None) -> int:
    """``python -m savesmith.core.discover <game folder>``.

    Also accepts a folder inside a Wine bottle: the bottle is detected from the
    path, so a Windows game on a Mac needs no special handling from the user.
    """
    import sys

    from savesmith.core.paths import RealSystem
    from savesmith.core.wine import is_prefix, scan_prefixes

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

        system: SystemFacade = host
        bottle = _bottle_containing(folder)
        if bottle is not None:
            nearby = scan_prefixes(host, [bottle.parent])
            prefix = next((p for p in nearby if p.path == bottle), None)
            if prefix is None and is_prefix(bottle):
                prefix = next(iter(scan_prefixes(host, [bottle])), None)
            if prefix is not None and prefix.users:
                system = prefix.system(prefix.users[0])
                print(f"(inside the Windows bottle {prefix.name})")

        game = examine(folder)
        for line in find_saves(game, system).explain():
            print(line)
        print()
    return 0


def _bottle_containing(folder: Path) -> Path | None:
    """Walk up looking for a drive_c, so a game inside a bottle just works."""
    for parent in [folder, *folder.parents]:
        if parent.name == "drive_c" and (parent.parent / "drive_c").is_dir():
            return parent.parent
    return None


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
