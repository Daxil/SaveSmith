"""A FromSoftware character slot, turned into values with names on them.

Dark Souls III, Sekiro, Elden Ring, Armored Core VI and Nightreign all ship the
same shape: a BND4 archive of fixed-size character slots, each with an MD5
header and optional AES. What differs between them is not the shape but the
*sizes* — how long PlayerGameData is, how many inventory rows there are, where
the level sits inside the character block. So the walk lives here and the
numbers live in the plugin manifest, one table per game.

**Why a walk and not a table of addresses.** A slot is not a flat record. Parts
of it are variable length — the inventory, the region list, five length-prefixed
blobs near the end — and each one shifts everything after it. Runes are not "at
0x1B4C"; they are at "+0x64 from wherever PlayerGameData turned out to start".
An absolute address read off one player's save is a coincidence, and writing
through it into somebody else's is how a playthrough gets destroyed.

The walk locates the blocks worth naming; it does not claim to describe the
whole slot. An Elden Ring slot is a 2.5 MB fixed-size buffer of which the walk
covers the first 2.1 MB, and the rest is real data nobody here needs to
understand. Saying the walk must reach the end would be asserting a completeness
this does not have.

**What it does check, and it refuses rather than guesses:**

* every read lands inside the buffer,
* the character's name decodes as UTF-16,
* the level equals the attributes' sum minus the starting total plus one —
  the game's own arithmetic, which no wrong offset satisfies by accident, and
  which validates the block's position and three offsets inside it at once.

A layout that has not been proven against a real save of its game therefore
fails loudly the first time it is used, instead of quietly producing plausible
nonsense. That is what makes it safe to ship the mechanism for a family of
games while only some of their layouts have been verified.

**Nothing outside the named fields is rebuilt.** The original bytes are kept in
the hints and the changed values are written back into a copy of them, so a
27 MB slot comes back byte for byte except where the player asked for a change.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from savesmith.core.ops._registry import Hints, Operation, Params, register

_U32 = "<I"

# Every field this operation knows how to read, and how wide it is. Their
# offsets are per-game and come from the manifest; their existence does not.
_SCALARS: dict[str, str] = {
    "level": _U32,
    "runes": _U32,
    "runes_memory": _U32,
}

# An inventory row: the handle of the thing, how many of it, and where the game
# shows it in the list.
_ROW = 12

# The top nibble says what kind of thing something is — and the handles and the
# ids do not use the same numbering, so both tables are needed. This is the
# family's convention rather than one game's numbers, which is why it lives
# here and not in a manifest.
_BY_HANDLE: dict[int, str] = {
    0x8: "weapon",
    0x9: "protector",
    0xA: "accessory",
    0xB: "goods",
    0xC: "gem",
}
_BY_ITEM_ID: dict[int, str] = {
    0x0: "weapon",
    0x1: "protector",
    0x2: "accessory",
    0x4: "goods",
    0x8: "gem",
}
# Weapons, armour and ashes of war carry per-copy state — reinforcement, the
# ash fitted to a weapon — which lives in the item table, so their handle is a
# reference into it. Talismans and consumables have no such state and carry
# their own id in the low bits of the handle instead.
_NEEDS_ITEM_TABLE = frozenset({"weapon", "protector", "gem"})
_NIBBLES = {name: nibble for nibble, name in _BY_HANDLE.items()}


@dataclass(frozen=True)
class _Table:
    """Where one inventory table sits inside the slot."""

    name: str
    count_at: int
    rows_at: int
    capacity: int


class _Walk:
    """Follows a slot's own structure, marking the blocks worth naming."""

    def __init__(self, payload: bytes, steps: Sequence[Mapping[str, Any]]) -> None:
        self.buf = payload
        self.at = 0
        self.marks: dict[str, int] = {}
        self.item_table: dict[int, int] = {}
        self.tables: list[_Table] = []
        for index, step in enumerate(steps):
            self._one(step, index)

    def _one(self, step: Mapping[str, Any], index: int) -> None:
        where = f"walk step {index + 1}"
        if "skip" in step:
            self.at += int(step["skip"])
        elif "mark" in step:
            self.marks[str(step["mark"])] = self.at
        elif "gaitems" in step:
            self._gaitems(step["gaitems"])
        elif "inventory" in step:
            self._inventory(step["inventory"])
        elif "prefixed" in step:
            # A u32 count, then that many fixed-size records.
            count = self._u32(self.at, where)
            self.at += 4 + count * int(step["prefixed"])
        elif "blobs" in step:
            # Several length-prefixed byte strings in a row.
            for _ in range(int(step["blobs"])):
                length = self._u32(self.at, where)
                self.at += 4 + length
        else:
            raise ValueError(f"{where} is not something this operation understands: {step}")
        if self.at > len(self.buf):
            raise ValueError(f"{where} ran past the end of the slot; the layout does not fit")

    def _u32(self, at: int, where: str) -> int:
        if at + 4 > len(self.buf):
            raise ValueError(f"{where} reads past the end of the slot")
        value: int = struct.unpack_from(_U32, self.buf, at)[0]
        return value

    def _gaitems(self, spec: Mapping[str, Any]) -> None:
        """The item-handle table: fixed count, records of varying length.

        A weapon carries reinforcement and an ash of war; armour carries less;
        an empty entry is just the pair. The category is in the id's top nibble.
        """
        count = int(spec["count"])
        weapon_extra = int(spec.get("weapon_extra", 13))
        armour_extra = int(spec.get("armour_extra", 8))
        for _ in range(count):
            handle = self._u32(self.at, "the item table")
            item_id = self._u32(self.at + 4, "the item table")
            self.at += 8
            if item_id in (0, 0xFFFFFFFF):
                continue
            # Kept, because the inventory names weapons, armour and ashes of
            # war by handle and only this table says which thing a handle is.
            self.item_table[handle] = item_id
            category = item_id & 0xF0000000
            if category == 0:
                self.at += weapon_extra
            elif category == 0x10000000:
                self.at += armour_extra

    def _inventory(self, tables: Sequence[Mapping[str, Any]]) -> None:
        """Capped tables of twelve-byte rows, one after another, then counters.

        Each table is a u32 count followed by ``capacity`` rows, of which only
        the first ``count`` are in use — and "in use" includes gaps, rows the
        game zeroed when something was dropped. The count spans them, so they
        are part of the table's shape rather than something to tidy away.
        """
        for entry in tables:
            name = str(entry["name"])
            capacity = int(entry["capacity"])
            count = self._u32(self.at, f"the {name} inventory")
            if count > capacity:
                raise ValueError(
                    f"the {name} inventory claims {count} items but holds at most "
                    f"{capacity}; the layout is reading the wrong part of the slot"
                )
            self.tables.append(
                _Table(name=name, count_at=self.at, rows_at=self.at + 4, capacity=capacity)
            )
            self.at += 4 + capacity * _ROW
        # Two counters the game keeps after the tables. Nothing here reads them.
        self.at += 8


def _item_of(handle: int, item_table: Mapping[int, int]) -> str | None:
    """What thing this handle refers to, as ``kind:number``.

    ``None`` for a gap, and for a handle whose item table entry is missing —
    an identity nobody can name is not one to invent. The row keeps its handle
    either way, so a file with such a row still rebuilds exactly.
    """
    kind = _BY_HANDLE.get(handle >> 28)
    if kind is None:
        return None
    if kind not in _NEEDS_ITEM_TABLE:
        return f"{kind}:{handle & 0x0FFFFFFF}"
    item_id = item_table.get(handle)
    if item_id is None:
        return None
    # The table is the authority on what the thing is: a handle only says where
    # to look, and the two numberings do not agree.
    named = _BY_ITEM_ID.get(item_id >> 28)
    return None if named is None else f"{named}:{item_id & 0x0FFFFFFF}"


def _read_inventory(buf: bytes, walk: _Walk) -> dict[str, list[dict[str, Any]]]:
    inventory: dict[str, list[dict[str, Any]]] = {}
    for table in walk.tables:
        count: int = struct.unpack_from(_U32, buf, table.count_at)[0]
        rows = []
        for index in range(count):
            handle, quantity, order = struct.unpack_from(
                "<3I", buf, table.rows_at + index * _ROW
            )
            rows.append(
                {
                    "item": _item_of(handle, walk.item_table),
                    "count": quantity,
                    "order": order,
                    "handle": handle,
                }
            )
        inventory[table.name] = rows
    return inventory


def _handle_for(row: Any, where: str, item_table: Mapping[int, int]) -> int:
    """The handle to write for a row, given what the row now says it is.

    ``item`` is what an editor changes; the handle is how the save records it.
    For talismans and consumables the handle *is* the id, so a row can be
    pointed at a different thing simply by naming it. For weapons, armour and
    ashes of war it is a reference into the item table, where the reinforcement
    and the fitted ash live — those cannot be conjured, so naming one that is
    not already in the table is refused instead of writing a handle that points
    at nothing.
    """
    item = _record(row, where).get("item")
    if item is None:
        return _u32_of(row, "handle", where)
    kind, _, number = str(item).partition(":")
    nibble = _NIBBLES.get(kind)
    if nibble is None or not number.isdigit():
        raise ValueError(f"'{item}' is not an item this save can hold; expected kind:number")
    identifier = int(number)
    if kind not in _NEEDS_ITEM_TABLE:
        return nibble << 28 | identifier
    handle = _u32_of(row, "handle", where)
    if item_table.get(handle, -1) & 0x0FFFFFFF != identifier:
        raise ValueError(
            f"'{item}' cannot be put into the {where} inventory this way: weapons, "
            f"armour and ashes of war carry their own reinforcement in a separate "
            f"table, and SaveSmith does not create entries there yet. Their count "
            f"can still be changed."
        )
    return handle


def _write_inventory(
    buf: bytearray,
    tables: Sequence[_Table],
    inventory: Mapping[str, Any],
    item_table: Mapping[int, int],
) -> None:
    for table in tables:
        rows = inventory.get(table.name)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise ValueError(f"the {table.name} inventory is a list of items")
        if len(rows) > table.capacity:
            raise ValueError(
                f"the {table.name} inventory holds {table.capacity} items and "
                f"{len(rows)} were given"
            )
        was: int = struct.unpack_from(_U32, buf, table.count_at)[0]
        for index, row in enumerate(rows):
            struct.pack_into(
                "<3I",
                buf,
                table.rows_at + index * _ROW,
                _handle_for(row, table.name, item_table),
                _u32_of(row, "count", table.name),
                _u32_of(row, "order", table.name),
            )
        # Rows the list no longer has are cleared rather than left behind: the
        # count is what the game reads, but a stale row inside the capacity is
        # the kind of leftover that turns up later as a ghost item.
        for index in range(len(rows), was):
            struct.pack_into("<3I", buf, table.rows_at + index * _ROW, 0, 0, 0)
        struct.pack_into(_U32, buf, table.count_at, len(rows))


def _record(row: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError(f"an item in the {where} inventory is not a record: {row!r}")
    return row


def _u32_of(row: Any, key: str, where: str) -> int:
    value = _record(row, where).get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{key}' of an item in the {where} inventory is a whole number")
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"'{key}' of an item in the {where} inventory is out of range: {value}")
    return value


def _layout(params: Params) -> tuple[Sequence[Mapping[str, Any]], Mapping[str, Any]]:
    walk = params.get("walk")
    fields = params.get("fields")
    if not isinstance(walk, list) or not walk:
        raise ValueError("this plugin gives no 'walk' for the character slot")
    if not isinstance(fields, dict):
        raise ValueError("this plugin gives no 'fields' for the character slot")
    return walk, fields


def _read_name(buf: bytes, at: int, width: int) -> str:
    raw = buf[at : at + width]
    if len(raw) < width:
        raise ValueError("the slot ends before the character's name")
    try:
        text = raw.decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "the character's name is not readable text, so the layout does not "
            "fit this save"
        ) from exc
    return text.split("\x00")[0]


def _attributes(buf: bytes, base: int, spec: Mapping[str, Any]) -> list[int]:
    at = base + int(spec["offset"])
    names = list(spec.get("names", ()))
    return [struct.unpack_from(_U32, buf, at + i * 4)[0] for i in range(len(names))]


def _check(values: Mapping[str, Any], fields: Mapping[str, Any], end: int, size: int) -> None:
    """The questions a wrong layout cannot answer correctly.

    ``complete`` is for a layout that really does describe every byte of the
    slot. Elden Ring's does not, and pretending otherwise would turn a true
    statement about a different game into a false one about this one.
    """
    if fields.get("complete") and end != size:
        raise ValueError(
            f"the walk ended at {end} but the slot is {size} bytes; this layout "
            f"claims to describe the whole slot and does not"
        )
    attributes = fields.get("attributes")
    if not isinstance(attributes, dict) or "starting_total" not in attributes:
        return
    stats = values.get("attributes")
    level = values.get("level")
    if not isinstance(stats, dict) or not isinstance(level, int):
        return
    expected = sum(stats.values()) - int(attributes["starting_total"]) + 1
    if expected != level:
        raise ValueError(
            f"the level says {level} but the attributes add up to {expected}; the "
            f"layout is reading the wrong part of the slot"
        )


def _decode(payload: Any, params: Params, hints: Hints) -> dict[str, Any]:
    if not isinstance(payload, bytes | bytearray):
        raise ValueError("fromsoft_character expects the raw bytes of one slot")
    buf = bytes(payload)
    walk, fields = _layout(params)

    marks = _Walk(buf, walk)
    base_name = str(fields.get("base", "character"))
    base = marks.marks.get(base_name)
    if base is None:
        raise ValueError(f"the walk never marked '{base_name}', so nothing can be read")

    values: dict[str, Any] = {}
    for name, layout in _SCALARS.items():
        where = fields.get(name)
        if where is None:
            continue
        values[name] = struct.unpack_from(layout, buf, base + int(where))[0]

    naming = fields.get("name")
    if isinstance(naming, dict):
        values["name"] = _read_name(buf, base + int(naming["offset"]), int(naming["bytes"]))

    attributes = fields.get("attributes")
    if isinstance(attributes, dict):
        names = list(attributes.get("names", ()))
        values["attributes"] = dict(zip(names, _attributes(buf, base, attributes), strict=True))

    if marks.tables:
        values["inventory"] = _read_inventory(buf, marks)

    _check(values, fields, marks.at, len(buf))

    # Everything not named here comes back untouched, so a slot the size of a
    # small film rebuilds exactly.
    hints["slot_bytes"] = buf
    hints["slot_base"] = base
    hints["slot_tables"] = tuple(marks.tables)
    hints["slot_item_table"] = marks.item_table
    return values


def _encode(payload: Any, params: Params, hints: Mapping[str, Any]) -> bytes:
    if not isinstance(payload, dict):
        raise ValueError("fromsoft_character rebuilds a slot from its named values")
    original = hints.get("slot_bytes")
    base = hints.get("slot_base")
    if not isinstance(original, bytes | bytearray) or not isinstance(base, int):
        raise ValueError("the original slot was not recorded, so it cannot be rebuilt")

    _, fields = _layout(params)
    buf = bytearray(original)

    for name, layout in _SCALARS.items():
        where = fields.get(name)
        if where is None or name not in payload:
            continue
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"'{name}' holds a whole number")
        try:
            struct.pack_into(layout, buf, base + int(where), value)
        except struct.error as exc:
            raise ValueError(f"'{name}' cannot hold {value}: {exc}") from exc

    attributes = fields.get("attributes")
    if isinstance(attributes, dict) and isinstance(payload.get("attributes"), dict):
        at = base + int(attributes["offset"])
        for index, key in enumerate(attributes.get("names", ())):
            value = payload["attributes"].get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                struct.pack_into(_U32, buf, at + index * 4, value)

    inventory = payload.get("inventory")
    tables = hints.get("slot_tables") or ()
    if isinstance(inventory, dict) and tables:
        _write_inventory(buf, tables, inventory, hints.get("slot_item_table") or {})

    # The name is left alone on purpose: it is variable-width UTF-16 in a fixed
    # buffer the game also uses for the save's menu entry, and renaming a
    # character is not what anybody opens a save editor for.
    return bytes(buf)


register(
    Operation(
        name="fromsoft_character",
        decode=_decode,
        encode=_encode,
        required_params=("walk", "fields"),
        summary="Reads a FromSoftware character slot: level, runes, attributes",
    )
)
