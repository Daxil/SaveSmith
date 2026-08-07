"""Reading a FromSoftware character slot: level, runes, attributes.

The layout is data, one table per game, because Dark Souls III, Sekiro, Elden
Ring and Armored Core VI share the shape and differ only in the sizes. What is
tested here is the mechanism and — above all — that a layout which does not fit
is refused rather than believed.

The self-check that matters is the game's own arithmetic: the character's level
must equal the attributes' sum minus the class's starting total plus one. No
wrong offset satisfies that by accident, so a mistyped layout fails on the first
save it meets instead of quietly showing somebody a plausible wrong number and
writing it back.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import Any

import pytest

from savesmith.core.ops import fromsoft_character  # noqa: F401  registers the op
from savesmith.core.ops._registry import get

STARTING_TOTAL = 80
ATTRIBUTES = ("vigor", "mind", "endurance", "strength", "dexterity", "intelligence",
              "faith", "arcane")


def layout(walk: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "walk": walk if walk is not None else [{"skip": 0x10}, {"mark": "character"}],
        "fields": {
            "base": "character",
            "level": 0x60,
            "runes": 0x64,
            "runes_memory": 0x68,
            "name": {"offset": 0x94, "bytes": 0x20},
            "attributes": {
                "offset": 0x34,
                "starting_total": STARTING_TOTAL,
                "names": list(ATTRIBUTES),
            },
        },
    }


def a_slot(
    *,
    stats: tuple[int, ...] = (10,) * 8,
    level: int | None = None,
    runes: int = 12400,
    name: str = "Танкред",
    base: int = 0x10,
    size: int = 0x400,
) -> bytes:
    """A slot with one character block in it, arranged like the real thing."""
    buf = bytearray(size)
    if level is None:
        level = sum(stats) - STARTING_TOTAL + 1
    for index, value in enumerate(stats):
        struct.pack_into("<I", buf, base + 0x34 + index * 4, value)
    struct.pack_into("<I", buf, base + 0x60, level)
    struct.pack_into("<I", buf, base + 0x64, runes)
    struct.pack_into("<I", buf, base + 0x68, runes * 2)
    encoded = name.encode("utf-16-le")
    buf[base + 0x94 : base + 0x94 + len(encoded)] = encoded
    return bytes(buf)


WEAPON_ID = 0x02719C40  # a weapon: top nibble 0
ARMOUR_ID = 0x100D4670  # armour: top nibble 1


def a_slot_with_inventory(
    *,
    rows: Sequence[tuple[int, int, int]] = (),
    capacity: int = 8,
) -> tuple[bytes, dict[str, Any]]:
    """A slot arranged as the real thing is: item table, character, inventory.

    Two entries go into the item table — a weapon and a piece of armour — so
    that handles which have to be looked up are exercised alongside the ones
    that carry their own id.
    """
    table = bytearray()
    table += struct.pack("<2I", 0x80800100, WEAPON_ID) + bytes(13)
    table += struct.pack("<2I", 0x90800101, ARMOUR_ID) + bytes(8)
    table += struct.pack("<2I", 0, 0)  # an empty entry, as most of the table is
    table += struct.pack("<2I", 0, 0)

    character = bytearray(0x200)
    stats = (10,) * 8
    for index, value in enumerate(stats):
        struct.pack_into("<I", character, 0x34 + index * 4, value)
    struct.pack_into("<I", character, 0x60, sum(stats) - STARTING_TOTAL + 1)
    struct.pack_into("<I", character, 0x64, 5000)
    struct.pack_into("<I", character, 0x68, 5000)
    name = "ok".encode("utf-16-le")
    character[0x94 : 0x94 + len(name)] = name

    inventory = bytearray(4 + capacity * 12)
    struct.pack_into("<I", inventory, 0, len(rows))
    for index, row in enumerate(rows):
        struct.pack_into("<3I", inventory, 4 + index * 12, *row)

    buf = bytes(table) + bytes(character) + bytes(inventory) + bytes(8) + b"tail"
    described = {
        "walk": [
            {"gaitems": {"count": 4, "weapon_extra": 13, "armour_extra": 8}},
            {"mark": "character"},
            {"skip": 0x200},
            {"inventory": [{"name": "held", "capacity": capacity}]},
        ],
        "fields": layout()["fields"],
    }
    return buf, described


@pytest.fixture
def op():  # type: ignore[no-untyped-def]
    return get("fromsoft_character")


class TestReading:
    def test_the_named_values_come_out(self, op) -> None:  # type: ignore[no-untyped-def]
        payload = a_slot(stats=(99,) * 8, runes=1_000_000, name="nn")

        values = op.decode(payload, layout(), {})

        assert values["name"] == "nn"
        assert values["runes"] == 1_000_000
        assert values["level"] == 713  # 8 × 99 − 80 + 1, the game's own sum
        assert values["attributes"]["vigor"] == 99

    def test_attributes_are_named_not_numbered(self, op) -> None:  # type: ignore[no-untyped-def]
        values = op.decode(a_slot(stats=(11, 12, 13, 14, 15, 16, 17, 18)), layout(), {})

        assert values["attributes"] == dict(zip(ATTRIBUTES, range(11, 19), strict=True))


class TestRefusingAWrongLayout:
    """The point of the exercise. A guess that reads must not be believed."""

    def test_a_level_that_contradicts_the_attributes_is_refused(self, op) -> None:  # type: ignore[no-untyped-def]
        """Exactly what a misplaced character block looks like."""
        payload = a_slot(stats=(99,) * 8, level=42)

        with pytest.raises(ValueError, match="wrong part of the slot"):
            op.decode(payload, layout(), {})

    def test_a_block_in_the_wrong_place_is_refused(self, op) -> None:  # type: ignore[no-untyped-def]
        payload = a_slot(base=0x10)
        wrong = layout([{"skip": 0x40}, {"mark": "character"}])

        with pytest.raises(ValueError):
            op.decode(payload, wrong, {})

    def test_a_name_that_is_not_text_is_refused(self, op) -> None:  # type: ignore[no-untyped-def]
        payload = bytearray(a_slot())
        payload[0x10 + 0x94 : 0x10 + 0x96] = b"\x00\xd8"  # a lone UTF-16 surrogate

        with pytest.raises(ValueError, match="readable text"):
            op.decode(bytes(payload), layout(), {})

    def test_a_walk_past_the_end_is_refused(self, op) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValueError, match=r"does not fit|past the end"):
            op.decode(a_slot(size=0x200), layout([{"skip": 0x9000}, {"mark": "character"}]), {})

    def test_claiming_to_describe_the_whole_slot_is_checked(self, op) -> None:  # type: ignore[no-untyped-def]
        """A layout may say it covers everything. Then it had better."""
        described = layout()
        described["fields"]["complete"] = True

        with pytest.raises(ValueError, match="whole slot"):
            op.decode(a_slot(), described, {})


class TestWritingBack:
    def test_an_untouched_slot_rebuilds_byte_for_byte(self, op) -> None:  # type: ignore[no-untyped-def]
        payload = a_slot()
        hints: dict[str, Any] = {}

        assert op.encode(op.decode(payload, layout(), hints), layout(), hints) == payload

    def test_changing_runes_touches_only_those_four_bytes(self, op) -> None:  # type: ignore[no-untyped-def]
        """A slot is megabytes of things nobody here understands."""
        payload = a_slot(runes=12400)
        hints: dict[str, Any] = {}
        values = op.decode(payload, layout(), hints)
        values["runes"] = 777

        rebuilt = op.encode(values, layout(), hints)

        differing = [i for i, (a, b) in enumerate(zip(payload, rebuilt, strict=True)) if a != b]
        assert all(0x10 + 0x64 <= i < 0x10 + 0x68 for i in differing)

    def test_the_name_is_left_alone(self, op) -> None:  # type: ignore[no-untyped-def]
        payload = a_slot(name="Танкред")
        hints: dict[str, Any] = {}
        values = op.decode(payload, layout(), hints)
        values["name"] = "кто-то другой"

        rebuilt = op.encode(values, layout(), hints)

        assert op.decode(rebuilt, layout(), {})["name"] == "Танкред"

    def test_a_slot_that_was_never_read_cannot_be_written(self, op) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValueError, match="not recorded"):
            op.encode({"runes": 1}, layout(), {})


class TestTheInventory:
    """What the character is carrying, and how it goes back."""

    def test_things_are_named_by_kind_and_number(self, op) -> None:  # type: ignore[no-untyped-def]
        payload, described = a_slot_with_inventory(
            rows=[
                (0xB0000E0F, 10, 1),  # a consumable: its id is in the handle
                (0x80800100, 1, 2),  # a weapon: the item table says which
                (0x90800101, 1, 3),  # armour, likewise
                (0xA0000460, 1, 4),  # a talisman
            ]
        )

        held = op.decode(payload, described, {})["inventory"]["held"]

        assert [row["item"] for row in held] == [
            "goods:3599",
            "weapon:41000000",
            "protector:870000",
            "accessory:1120",
        ]
        assert held[0]["count"] == 10

    def test_gaps_are_kept_rather_than_tidied_away(self, op) -> None:  # type: ignore[no-untyped-def]
        """The game's own count spans them, so removing one shifts everything."""
        payload, described = a_slot_with_inventory(
            rows=[(0xB0000E0F, 1, 1), (0, 0, 0), (0xB0000E10, 1, 3)]
        )

        held = op.decode(payload, described, {})["inventory"]["held"]

        assert len(held) == 3
        assert held[1]["item"] is None

    def test_a_handle_the_item_table_does_not_know_stays_unnamed(self, op) -> None:  # type: ignore[no-untyped-def]
        payload, described = a_slot_with_inventory(rows=[(0xC0800999, 1, 1)])

        held = op.decode(payload, described, {})["inventory"]["held"]

        assert held[0]["item"] is None
        assert held[0]["handle"] == 0xC0800999

    def test_an_untouched_inventory_rebuilds_byte_for_byte(self, op) -> None:  # type: ignore[no-untyped-def]
        payload, described = a_slot_with_inventory(
            rows=[(0xB0000E0F, 10, 1), (0x80800100, 1, 2)]
        )
        hints: dict[str, Any] = {}

        assert op.encode(op.decode(payload, described, hints), described, hints) == payload

    def test_changing_a_count_touches_only_that_number(self, op) -> None:  # type: ignore[no-untyped-def]
        payload, described = a_slot_with_inventory(rows=[(0xB0000E0F, 10, 1)])
        hints: dict[str, Any] = {}
        values = op.decode(payload, described, hints)
        values["inventory"]["held"][0]["count"] = 99

        rebuilt = op.encode(values, described, hints)

        differing = [i for i, (a, b) in enumerate(zip(payload, rebuilt, strict=True)) if a != b]
        assert len(differing) == 1  # 10 and 99 differ in one byte of the four

    def test_naming_a_different_consumable_moves_the_row_to_it(self, op) -> None:  # type: ignore[no-untyped-def]
        """The handle follows the item, not the other way round."""
        payload, described = a_slot_with_inventory(rows=[(0xB0000E0F, 1, 1)])
        hints: dict[str, Any] = {}
        values = op.decode(payload, described, hints)
        values["inventory"]["held"][0]["item"] = "goods:1007"

        rebuilt = op.encode(values, described, hints)

        assert op.decode(rebuilt, described, {})["inventory"]["held"][0]["item"] == "goods:1007"

    def test_a_weapon_that_is_not_in_the_item_table_is_refused(self, op) -> None:  # type: ignore[no-untyped-def]
        """Its reinforcement lives elsewhere, so a bare handle would dangle."""
        payload, described = a_slot_with_inventory(rows=[(0xB0000E0F, 1, 1)])
        hints: dict[str, Any] = {}
        values = op.decode(payload, described, hints)
        values["inventory"]["held"][0]["item"] = "weapon:2000000"

        with pytest.raises(ValueError, match="reinforcement"):
            op.encode(values, described, hints)

    def test_more_rows_than_the_table_holds_is_refused(self, op) -> None:  # type: ignore[no-untyped-def]
        payload, described = a_slot_with_inventory(rows=[(0xB0000E0F, 1, 1)], capacity=2)
        hints: dict[str, Any] = {}
        values = op.decode(payload, described, hints)
        values["inventory"]["held"] = [
            {"item": "goods:1", "count": 1, "order": index} for index in range(3)
        ]

        with pytest.raises(ValueError, match="holds 2 items"):
            op.encode(values, described, hints)

    def test_a_count_the_layout_cannot_believe_is_refused(self, op) -> None:  # type: ignore[no-untyped-def]
        """A wrong offset reads a huge number where the count should be."""
        payload, described = a_slot_with_inventory(rows=[(0xB0000E0F, 1, 1)])
        broken = dict(described)
        broken["walk"] = [*described["walk"][:2], {"skip": 0x204}, described["walk"][3]]

        with pytest.raises(ValueError, match="wrong part of the slot"):
            op.decode(payload, broken, {})

    def test_a_removed_row_leaves_nothing_behind(self, op) -> None:  # type: ignore[no-untyped-def]
        payload, described = a_slot_with_inventory(
            rows=[(0xB0000E0F, 1, 1), (0xB0000E10, 5, 2)]
        )
        hints: dict[str, Any] = {}
        values = op.decode(payload, described, hints)
        del values["inventory"]["held"][1]

        rebuilt = op.encode(values, described, hints)
        held = op.decode(rebuilt, described, {})["inventory"]["held"]

        assert [row["item"] for row in held] == ["goods:3599"]
        # The vacated row is zeroed, not left behind as a ghost past the count.
        ghost = struct.pack("<3I", 0xB0000E10, 5, 2)
        assert ghost in payload
        assert ghost not in rebuilt
