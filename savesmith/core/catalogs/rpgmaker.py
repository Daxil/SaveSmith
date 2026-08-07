"""Item names and icons out of an RPG Maker game's own files.

The best case there is, and worth taking as the model for every other engine:
the data is plain JSON sitting in the game folder, the icons are one sprite
sheet beside it, and nothing has to be downloaded, decrypted or guessed. What a
patch changes in the game, SaveSmith sees the same day.

MV keeps all of it under ``www/``; MZ dropped that folder and keeps ``data/``
and ``img/`` at the top. Both are checked, in that order, because a folder that
has ``www/`` is MV and there is no ambiguity to resolve.

Three files, three kinds of thing, because that is how the engine stores them —
``$dataItems``, ``$dataWeapons``, ``$dataArmors`` are separate arrays and a
party keeps three separate bags. Each array is indexed by id with a ``null`` in
slot zero, which is the engine's own convention, not a mistake to work around.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from savesmith.core.catalog import Catalog, Entry, Icon, Sheet, reader

# The engine's own numbers. Every stock IconSet is 16 columns of 32-pixel
# tiles, and a game that replaces the sheet keeps the grid.
ICON_TILE = 32
ICON_COLUMNS = 16
SHEET = "rpgmaker-iconset"

_FILES = {
    "items": "Items.json",
    "weapons": "Weapons.json",
    "armors": "Armors.json",
}


def _roots(game: Path) -> list[Path]:
    """Where the data might be: MV puts everything under www/, MZ does not."""
    return [game / "www", game]


def _read_json(game: Path, name: str) -> list[Any] | None:
    for root in _roots(game):
        path = root / "data" / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, list) else None
    return None


def _icon_sheet(game: Path) -> Sheet | None:
    for root in _roots(game):
        path = root / "img" / "system" / "IconSet.png"
        if path.is_file():
            try:
                return Sheet(id=SHEET, png=path.read_bytes(), tile=ICON_TILE, columns=ICON_COLUMNS)
            except OSError:
                return None
    return None


def _catalog_of(game: Path, kind: str) -> Catalog:
    rows = _read_json(game, _FILES[kind])
    if rows is None:
        return Catalog()

    entries = {}
    for row in rows:
        # Slot zero is null by the engine's convention, and a game in
        # development leaves holes further along too.
        if not isinstance(row, dict) or not row.get("name"):
            continue
        identifier = row.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int):
            continue
        icon_index = row.get("iconIndex")
        entries[str(identifier)] = Entry(
            id=str(identifier),
            name=str(row["name"]),
            kind=kind,
            description=_description(row.get("description")),
            icon=(
                Icon(sheet=SHEET, index=icon_index)
                if isinstance(icon_index, int) and not isinstance(icon_index, bool)
                else None
            ),
        )
    if not entries:
        return Catalog()

    sheet = _icon_sheet(game)
    return Catalog(
        entries=entries,
        sheets={sheet.id: sheet} if sheet else {},
        source="the game's own data files",
    )


def _description(value: Any) -> str | None:
    """RPG Maker descriptions carry the engine's own line breaks."""
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())


@reader("rpgmaker:items")
def items(game: Path) -> Catalog:
    return _catalog_of(game, "items")


@reader("rpgmaker:weapons")
def weapons(game: Path) -> Catalog:
    return _catalog_of(game, "weapons")


@reader("rpgmaker:armors")
def armors(game: Path) -> Catalog:
    return _catalog_of(game, "armors")
