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


class _Walk:
    """Follows a slot's own structure, marking the blocks worth naming."""

    def __init__(self, payload: bytes, steps: Sequence[Mapping[str, Any]]) -> None:
        self.buf = payload
        self.at = 0
        self.marks: dict[str, int] = {}
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
            category = item_id & 0xF0000000
            if category == 0:
                self.at += weapon_extra
            elif category == 0x10000000:
                self.at += armour_extra
            _ = handle  # read for the bounds check; nothing here needs its value

    def _inventory(self, caps: Sequence[int]) -> None:
        """Two capped tables of twelve-byte rows, then two counters."""
        common, key = int(caps[0]), int(caps[1])
        self.at += 4 + common * 12 + 4 + key * 12 + 8


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

    _check(values, fields, marks.at, len(buf))

    # Everything not named here comes back untouched, so a slot the size of a
    # small film rebuilds exactly.
    hints["slot_bytes"] = buf
    hints["slot_base"] = base
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
