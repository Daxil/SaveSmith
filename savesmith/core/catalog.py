"""What the things in a container are called, and what they look like.

An id is not an answer. "goods:1007" is exactly as useful to a player as the
hex offset it came from, and an editor that shows a wall of numbers has moved
the problem rather than solved it. This is where a number becomes a name and a
picture.

**Where the names come from, in order of how much they can be trusted.**

1. *The installed game's own files.* RPG Maker keeps its items in plain JSON —
   ``www/data/Items.json`` — and its icons in one sprite sheet beside them.
   Nothing here is guessed, nothing goes stale on a patch, and the user already
   told SaveSmith where the game is.
2. *A pack the plugin author shipped*, ``items.json`` next to the manifest.
   This is how a game whose data is locked inside packed, encrypted archives —
   Elden Ring, most Unreal titles — gets real names and icons: somebody
   extracts them once, from their own copy, and installs the result as part of
   the plugin.
3. *Nothing*, and the item shows as its bare id. Which is honest, and still
   lets counts be changed.

Downloaded id tables from the internet are deliberately not part of that list.
They go stale on every patch and their provenance is nobody's to vouch for. A
user who has one puts it in a plugin, where it is visible and versioned.

**Pictures are not cut up here.** A sheet is handed over whole, with the tile
size and the column count, and whoever draws it shows the one tile it needs —
a browser does this with two CSS properties. Cropping would mean decoding and
re-encoding PNGs in Python for no gain.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from savesmith.core.errors import SaveSmithError


class CatalogError(SaveSmithError):
    """A catalog was found and cannot be read."""

    code = "catalog"


@dataclass(frozen=True)
class Sheet:
    """A grid of icons in one image, as games actually ship them."""

    id: str
    png: bytes
    tile: int = 32
    columns: int = 16


@dataclass(frozen=True)
class Icon:
    sheet: str
    index: int


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    kind: str | None = None
    description: str | None = None
    icon: Icon | None = None


@dataclass(frozen=True)
class Catalog:
    """Everything known about the things a game has, by id."""

    entries: Mapping[str, Entry] = field(default_factory=dict)
    sheets: Mapping[str, Sheet] = field(default_factory=dict)
    source: str = ""
    """Where this came from, in words a person can read."""

    def __bool__(self) -> bool:
        return bool(self.entries)

    def get(self, item: str) -> Entry | None:
        return self.entries.get(item)

    def name_of(self, item: str) -> str:
        """The readable name, or the id itself. Never nothing."""
        entry = self.entries.get(item)
        return entry.name if entry else item

    def search(self, text: str) -> list[Entry]:
        """Everything that could be meant by what was typed.

        Narrowing in three steps — exact, then ignoring case, then anywhere in
        the name — so that a name which is a prefix of ten others still selects
        itself. Callers are expected to refuse when this returns more than one
        rather than pick the first.
        """
        wanted = text.strip()
        if not wanted:
            return []
        if wanted in self.entries:
            return [self.entries[wanted]]
        exact = [entry for entry in self.entries.values() if entry.name == wanted]
        if exact:
            return exact
        folded = wanted.casefold()
        insensitive = [entry for entry in self.entries.values() if entry.name.casefold() == folded]
        if insensitive:
            return insensitive
        return sorted(
            (entry for entry in self.entries.values() if folded in entry.name.casefold()),
            key=lambda entry: entry.name,
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

# Readers that get their answers out of an installed game. Keyed by the name a
# manifest's container puts in its 'catalog' field.
_FROM_GAME: dict[str, Callable[[Path], Catalog]] = {}


def reader(name: str) -> Callable[[Callable[[Path], Catalog]], Callable[[Path], Catalog]]:
    def register(function: Callable[[Path], Catalog]) -> Callable[[Path], Catalog]:
        _FROM_GAME[name] = function
        return function

    return register


def load(
    name: str | None,
    *,
    plugin_folder: Path | None = None,
    game_folder: Path | None = None,
) -> Catalog:
    """The best catalog available for a container, or an empty one.

    Never raises for a missing catalog: an editor without names still works,
    and refusing to open an inventory because nobody wrote down what item 1007
    is called would be the wrong trade.
    """
    if not name:
        return Catalog()

    from savesmith.core import catalogs  # noqa: F401  registers the game readers

    reader_for_game = _FROM_GAME.get(name)
    if reader_for_game is not None and game_folder is not None:
        found = reader_for_game(game_folder)
        if found:
            return found

    if plugin_folder is not None:
        found = from_plugin(plugin_folder)
        if found:
            return found

    return Catalog()


CATALOG_FILE = "items.json"


def from_plugin(folder: Path) -> Catalog:
    """A pack shipped beside a manifest: names, kinds and icon sheets.

    ::

        {
          "source": "extracted from the game with UXM",
          "sheets": [{"id": "menu", "file": "icons.png", "tile": 40, "columns": 16}],
          "items": [{"id": "goods:1007", "name": "Rune Arc", "icon": ["menu", 12]}]
        }
    """
    path = folder / CATALOG_FILE if folder.is_dir() else folder
    if not path.is_file():
        return Catalog()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(
            "The item list beside this plugin could not be read, so items will "
            "show as bare numbers.",
            detail=f"{path}: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise CatalogError(
            "The item list beside this plugin is not in the expected form.",
            detail=f"{path} holds {type(data).__name__}",
        )

    sheets = {}
    for entry in data.get("sheets", ()):
        if not isinstance(entry, dict) or "id" not in entry or "file" not in entry:
            continue
        image = path.parent / str(entry["file"])
        if not image.is_file():
            continue
        sheets[str(entry["id"])] = Sheet(
            id=str(entry["id"]),
            png=image.read_bytes(),
            tile=int(entry.get("tile", 32)),
            columns=int(entry.get("columns", 16)),
        )

    entries = {}
    for entry in data.get("items", ()):
        if not isinstance(entry, dict) or "id" not in entry:
            continue
        identifier = str(entry["id"])
        entries[identifier] = Entry(
            id=identifier,
            name=str(entry.get("name", identifier)),
            kind=_optional_text(entry.get("kind")),
            description=_optional_text(entry.get("description")),
            icon=_icon(entry.get("icon")),
        )

    return Catalog(
        entries=entries,
        sheets=sheets,
        source=str(data.get("source") or "a list shipped with the plugin"),
    )


def from_ids(items: Mapping[str, str] | None = None) -> Catalog:
    """The last resort: what the save itself holds, named by its own ids."""
    return Catalog(
        entries={
            identifier: Entry(id=identifier, name=name)
            for identifier, name in (items or {}).items()
        },
        source="the save file itself",
    )


def _icon(value: Any) -> Icon | None:
    if isinstance(value, list | tuple) and len(value) == 2:
        return Icon(sheet=str(value[0]), index=int(value[1]))
    if isinstance(value, dict) and "sheet" in value and "index" in value:
        return Icon(sheet=str(value["sheet"]), index=int(value["index"]))
    return None


def _optional_text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value.strip() else None
