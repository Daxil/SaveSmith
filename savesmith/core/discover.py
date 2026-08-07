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
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from savesmith.core import detect
from savesmith.core.detect import Report
from savesmith.core.errors import SaveSmithError
from savesmith.core.paths import PathResolver, SystemFacade, locate
from savesmith.core.playerprefs import Entry, open_prefs
from savesmith.core.target import Target, resolve
from savesmith.core.wine import is_prefix, machine_for, name_of

# A Windows user profile, swept only when the game itself could not be named.
_PROFILE_TOKENS = ("SAVEDGAMES", "APPDATA", "LOCALAPPDATA", "LOCALLOW", "DOCUMENTS")

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
    installed_here: bool = True
    """False when this folder holds no game — a save folder pointed at directly.

    It decides how hard the folder itself is searched. An install folder is
    thousands of files of which none is a save, so only save-sounding names are
    read there; a folder somebody pointed at on purpose is read in full.
    """

    @property
    def has_anticheat(self) -> bool:
        return bool(self.anticheat)

    @property
    def names(self) -> tuple[str, ...]:
        """Every name this game might have written on a folder.

        ``ELDEN RING`` installs itself, then saves to ``EldenRing``; the
        executable is ``eldenring``. All three are the same word to a person
        and none of them match as strings, so all of them are tried.
        """
        candidates = [self.project, self.title]
        return tuple(dict.fromkeys(name for name in candidates if name))

    def summary(self) -> str:
        parts = [self.title, f"engine: {self.engine.value}"]
        if self.steam_appid:
            parts.append(f"Steam {self.steam_appid}")
        if self.anticheat:
            parts.append("anti-cheat: " + ", ".join(self.anticheat))
        return " | ".join(parts)


class Kind(StrEnum):
    """What a file found next to a game actually is.

    A folder of saves is mostly not saves. The Invincible keeps sixty-two
    rolling backups, four settings files and one save the player made; listing
    all sixty-seven as "saves" is not thoroughness, it is refusing to answer
    the question. Nobody knows what their save file is called — that is the
    whole reason they opened SaveSmith.
    """

    SAVE = "save"
    """The player's progress. This is the answer."""
    BACKUP = "backup"
    """A copy the game made of a save. Real, and not what was asked for."""
    SETTINGS = "settings"
    """Volume, key bindings, which menu was open. Not progress."""
    OTHER = "other"
    """Not a save at all — logs, manifests, whatever the walk turned up."""


# A copy of a save, made by the game itself.
_BACKUP_RE = re.compile(
    r"(_backup_?\d*|_bak\d*|[.\-_]backup|[.\-_]copy|\bcopy\s*\d*\b|_old|_prev(ious)?)",
    re.IGNORECASE,
)
_BACKUP_SUFFIXES = (".bak", ".backup", ".old", ".prev", ".tmp", ".temp")

# Options and menu state. Checked before "save", because a game will happily
# call a settings file MenuSettingsSave.sav.
_SETTINGS_WORDS = (
    "setting", "config", "option", "prefs", "preference", "keybind", "bindings",
    "input", "controls", "graphic", "video", "audio", "sound", "menu", "language",
    "profile",
)

# Formats a game writes its options in. No game stores progress in an .ini.
_SETTINGS_SUFFIXES = (".ini", ".cfg", ".conf", ".config", ".yaml", ".yml", ".toml")

# Below this a file holds nothing worth editing. An empty two-byte .ini will
# happily "decode" as base64 and present itself as a save otherwise.
_MIN_SAVE_BYTES = 32

_ASIDE_WORDS = {
    Kind.BACKUP: ("backup the game made", "backups the game made"),
    Kind.SETTINGS: ("settings file", "settings files"),
    Kind.OTHER: ("other file", "other files"),
}


def classify(name: str, *, openable: bool, size: int = _MIN_SAVE_BYTES) -> Kind:
    """What this file is, from its name, its size, and whether it could be read.

    Names, mostly. Reading every candidate to guess its purpose would be slower
    and no more certain: a game that names its backups ``_backup_7`` has told
    us plainly, and one that does not cannot be second-guessed from bytes.
    """
    lowered = name.lower()
    stem = Path(lowered).stem

    if lowered.endswith(_BACKUP_SUFFIXES) or _BACKUP_RE.search(stem):
        return Kind.BACKUP
    if lowered.endswith(_SETTINGS_SUFFIXES):
        return Kind.SETTINGS
    if any(word in lowered for word in _SETTINGS_WORDS):
        return Kind.SETTINGS
    if not openable or size < _MIN_SAVE_BYTES:
        # Nothing known opens it, or there is nothing in it. Whatever it is,
        # showing it to somebody looking for their save is noise.
        return Kind.OTHER
    return Kind.SAVE


@dataclass(frozen=True)
class FoundSave:
    path: Path
    location: str
    """Where this came from, in words, for the interface."""
    report: Report
    modified: float = 0.0
    """When it was last written. The save being played is the recent one."""

    @property
    def recognised(self) -> bool:
        """Fields can be read out of it, so a plugin can offer them by name."""
        return self.report.solved

    @property
    def openable(self) -> bool:
        """The format is understood and rebuilds exactly, fields or not.

        Editing by address works on these — which is the whole documented way
        into an Elden Ring save — so calling them unrecognised is both wrong
        and discouraging.
        """
        return self.report.openable

    @property
    def kind(self) -> Kind:
        return classify(self.path.name, openable=self.openable, size=self.size)

    @property
    def format(self) -> str:
        best = self.report.best
        return best.description if best else "unknown"

    @property
    def size(self) -> int:
        return self.report.look.size

    @property
    def rank(self) -> tuple[int, str]:
        """Listing order: readable ones first, then by name.

        By name and not by date, deliberately. ``--slot 2`` has to mean the
        same file tomorrow as it does today — it decides which save gets
        overwritten — and a game's own numbering lives in the file names.
        """
        return (0 if self.recognised else 1, str(self.path))

    @property
    def freshness(self) -> tuple[int, float]:
        """Which one is being played, for picking a single best answer."""
        return (1 if self.recognised else 0, self.modified)


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
    def openable(self) -> list[FoundSave]:
        """Saves whose format is understood, whether or not fields came out."""
        return [save for save in self.saves if save.openable]

    def of_kind(self, kind: Kind) -> list[FoundSave]:
        return [save for save in self.saves if save.kind is kind]

    @property
    def player_saves(self) -> list[FoundSave]:
        """The answer to "where is my save", best first.

        This is what an interface shows. The game's own backups, its settings
        and everything else the walk turned up are counted, not listed.
        """
        return sorted(self.of_kind(Kind.SAVE), key=lambda save: save.rank)

    @property
    def best_save(self) -> FoundSave | None:
        """The one most likely to be the game in progress: the newest."""
        saves = self.player_saves
        return max(saves, key=lambda save: save.freshness) if saves else None

    @property
    def aside(self) -> dict[Kind, int]:
        """How many of everything else there was, for one honest sentence."""
        counted = {
            kind: len(self.of_kind(kind))
            for kind in (Kind.BACKUP, Kind.SETTINGS, Kind.OTHER)
        }
        return {kind: count for kind, count in counted.items() if count}

    @property
    def found_anything(self) -> bool:
        return bool(self.saves) or bool(self.prefs and self.prefs.entries)

    def explain(self, *, verbose: bool = False) -> list[str]:
        """The answer, not the search.

        Only the player's own saves are listed. Everything else is counted in
        one line: somebody asking where their save is does not want to be
        handed sixty-two files and asked to work it out.
        """
        lines = [self.game.summary(), ""]
        if verbose or not self.found_anything:
            lines += [f"Looked in: {path}" for path in self.searched]
            if self.prefs is not None:
                lines.append(f"Looked in: {self.prefs.location} (Unity settings)")
        if not self.found_anything:
            lines.append("No save files found in any of those places.")
            return lines

        saves = self.player_saves
        if saves:
            lines.append(f"{len(saves)} save(s):" if len(saves) > 1 else "The save:")
            for save in saves[:20]:
                mark = "" if save.recognised else " — editable by address only"
                lines.append(
                    f"  {save.path.name}  ({save.format}, {save.size} bytes){mark}"
                )
                lines.append(f"      {save.path}")
            if any(not save.recognised for save in saves):
                lines.append(
                    "\n  'editable by address only' means the file is understood and "
                    "rebuilds exactly, but nobody has mapped what its bytes mean yet:"
                    "\n      savesmith search <file> <the number you see in the game>"
                )
        elif self.saves:
            lines.append(
                "Nothing here looks like a save the player made — only the game's "
                "own backups and settings."
            )

        aside = self.aside
        if aside:
            described = ", ".join(
                f"{count} {_ASIDE_WORDS[kind][0 if count == 1 else 1]}"
                for kind, count in aside.items()
            )
            lines.append(f"\n(Also {described}, which are the game's, not yours.)")
        if self.prefs is not None and self.prefs.entries:
            lines += ["", f"Unity settings ({len(self.prefs.entries)} of them):"]
            shown = self.prefs.numbers or self.prefs.entries
            for entry in shown[:20]:
                lines.append(f"  ✓ {entry.name} = {entry.value} ({entry.kind})")
            if len(shown) > 20:
                lines.append(f"  … and {len(shown) - 20} more")
            lines.append("  Change one with: savesmith prefs --game-folder <folder> --set NAME VAL")
        return lines


def examine(folder: Path, name: str | None = None) -> GameFolder:
    """Work out what this installed game is, from its own files.

    ``name`` is what the user pointed at, when they pointed at an executable or
    a Mac application rather than a folder. It beats the folder's own name:
    ``Game`` and ``Binaries`` are packaging, ``eldenring`` is the game.
    """
    entries = _entries(folder)
    lowered = {name_.lower(): name_ for name_ in entries}

    engine, project, company, evidence = _engine_of(folder, entries, lowered)
    return GameFolder(
        path=folder,
        title=name or folder.name,
        engine=engine,
        evidence=evidence,
        project=project or name,
        company=company,
        steam_appid=_steam_appid(folder),
        anticheat=_anticheat(folder),
        installed_here=_looks_installed(engine, entries),
    )


def _looks_installed(engine: Engine, entries: list[str]) -> bool:
    """Whether a game is installed in this folder, as opposed to saved in it.

    Pointing at a folder full of save files is a perfectly ordinary thing to
    do, and the answer must not be "no saves found" merely because none of them
    happens to be called ``save``.
    """
    if engine is not Engine.UNKNOWN:
        return True
    if len(entries) > 40:
        return True
    return any(
        name.lower().endswith((".exe", ".app", ".dll", ".pck", ".win", "_data"))
        for name in entries
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


def squash(name: str) -> str:
    """A name with everything a person would not pronounce taken out.

    ``ELDEN RING``, ``Elden Ring`` and ``eldenring`` are one game; as strings
    they are three. Comparing the squashed forms is what lets a folder named
    after the install be matched against the folder the game saves into.
    """
    return "".join(character for character in name.lower() if character.isalnum())


def _named_like(base: Path | None, *names: str) -> list[Path]:
    """Children of ``base`` that are one of these games, spelling aside."""
    if base is None:
        return []
    wanted = {squash(name) for name in names if name}
    if not wanted:
        return []
    try:
        with os.scandir(base) as scan:
            children = [entry for entry in scan if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    return [Path(entry.path) for entry in children if squash(entry.name) in wanted]


@dataclass(frozen=True)
class Place:
    """One folder to look in, and how hard to look.

    ``strict`` means only files whose names sound like saves are read. It is on
    for haystacks — an install folder, a whole Windows user profile — and off
    for a folder that is named after this game and therefore holds its things.
    """

    path: Path
    strict: bool = False


def save_locations(game: GameFolder, system: SystemFacade) -> list[Path]:
    """Everywhere this game might keep saves, most likely first."""
    return [place.path for place in _places(game, system)]


def _places(game: GameFolder, system: SystemFacade) -> list[Place]:
    resolver = PathResolver(system)
    names = game.names
    name = game.project or game.title
    places: list[Path | None] = []

    def token(key: str, *parts: str) -> Path | None:
        base = resolver.token(key)
        return base.joinpath(*parts) if base is not None else None

    def under(key: str, *parts: str) -> list[Path]:
        """Folders named after the game under a known base, plus a tail."""
        base = resolver.token(key)
        if base is not None and parts:
            base = base.joinpath(*parts)
        return _named_like(base, *names)

    match game.engine:
        case Engine.UNREAL:
            places += [
                folder / "Saved" / "SaveGames" for folder in under("LOCALAPPDATA")
            ]
            places += [game.path / name / "Saved" / "SaveGames"]
        case Engine.UNITY:
            if game.company and game.project:
                places += [
                    token("LOCALLOW", game.company, game.project),
                    token("APPDATA", game.company, game.project),
                    token("APPDATA", f"unity.{game.company}.{game.project}"),
                ]
            # Plenty of Unity games ignore the company folder entirely.
            places += under("LOCALLOW")
        case Engine.RPGMAKER:
            places += [game.path / "www" / "save", game.path / "save"]
        case Engine.GODOT:
            places += under("APPDATA", "Godot", "app_userdata")
        case Engine.GAMEMAKER:
            places += under("LOCALAPPDATA") + under("APPDATA")
        case Engine.UNKNOWN:
            pass

    # Places any game might use, whatever built it. Matched by name rather than
    # joined by name: a game installed as "ELDEN RING" saves into "EldenRing",
    # and joining the two spellings finds a folder that is not there.
    for key in ("SAVEDGAMES", "APPDATA", "LOCALAPPDATA", "LOCALLOW"):
        places += under(key)
    places += under("DOCUMENTS", "My Games")
    places += under("DOCUMENTS")

    found = [Place(path) for path in places if path is not None]

    # A bottle is an entire Windows filesystem. Walking it as though it were a
    # game folder turns up registry hives and no saves, so its user profile is
    # swept for save-sounding names instead, and only after the folders that
    # carry the game's own name.
    if is_prefix(game.path):
        found += [
            Place(path, strict=True)
            for path in (resolver.token(key) for key in _PROFILE_TOKENS)
            if path is not None
        ]
    else:
        found.append(Place(game.path, strict=game.installed_here))

    seen: list[Place] = []
    for place in found:
        if place.path.is_dir() and all(place.path != kept.path for kept in seen):
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
    places = _places(game, system)
    result.searched = [place.path for place in places]

    seen: set[Path] = set()
    for place in places:
        own_folder = place.path == game.path
        label = "the game's own folder" if own_folder else str(place.path)
        for path in _candidate_files(place.path, max_files, strict=place.strict):
            resolved = path.resolve()
            if resolved in seen:
                continue  # reached through a parent location as well
            seen.add(resolved)
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                modified = 0.0
            result.saves.append(
                FoundSave(
                    path=path,
                    location=label,
                    report=detect.identify(raw, max_depth=3),
                    modified=modified,
                )
            )

    # Recognised first, then by name so repeated runs read the same.
    result.saves.sort(key=lambda save: (not save.recognised, str(save.path)))
    result.prefs = find_prefs(game, system)
    return result


@dataclass(frozen=True)
class Look:
    """One path the user pointed at, and everything that followed from it."""

    target: Target
    system: SystemFacade
    found: Discovery
    notes: tuple[str, ...] = ()
    bottle: str | None = None
    """The bottle's name, so an interface can word it in its own language."""

    @property
    def game(self) -> GameFolder:
        return self.found.game


def look_at(path: Path, host: SystemFacade) -> Look:
    """The whole journey from "the user pointed here" to "these are the saves".

    Every front end goes through this, so that pointing at an executable, at a
    Mac application, at a bottle wrapper or at a folder all behave the same
    everywhere rather than in whichever one was written last.
    """
    target = resolve(path)
    # A game inside a bottle keeps its saves in that bottle's AppData, not on
    # the Mac. Asking the host would find a real, existing, empty folder.
    system, bottle = machine_for(target.folder, host)
    game = examine(target.folder, target.name)
    notes = tuple(note for note in (target.note, bottle) if note)
    return Look(
        target=target,
        system=system,
        found=find_saves(game, system),
        notes=notes,
        bottle=name_of(target.bottle) if target.bottle is not None else None,
    )


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

    from savesmith.core.console import use_utf8
    from savesmith.core.paths import RealSystem
    from savesmith.core.wine import machine_for

    use_utf8(sys.stdout, sys.stderr)

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
