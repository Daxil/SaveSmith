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
