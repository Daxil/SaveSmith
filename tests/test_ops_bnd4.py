"""BND4 archives — the container FromSoftware saves come in."""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from savesmith.core import ops
from savesmith.core.pipeline import Pipeline

HEADER_SIZE = 0x40
ENTRY_SIZE = 0x20


def build(payloads: dict[str, bytes]) -> bytes:
    """A minimal but structurally faithful BND4."""
    count = len(payloads)
    names_start = HEADER_SIZE + count * ENTRY_SIZE

    name_blob = bytearray()
    name_offsets = []
    for name in payloads:
        name_offsets.append(names_start + len(name_blob))
        name_blob += name.encode("utf-16-le") + b"\x00\x00"

    data_start = names_start + len(name_blob)
    entries = bytearray()
    data = bytearray()
    for payload, name_offset in zip(payloads.values(), name_offsets, strict=True):
        entries += struct.pack("<i", 0x50)
        entries += struct.pack("<i", -1)
        entries += struct.pack("<q", len(payload))
        entries += struct.pack("<i", data_start + len(data))
        entries += struct.pack("<i", name_offset)
        entries += struct.pack("<q", 0)
        data += payload

    header = bytearray(b"\x00" * HEADER_SIZE)
    header[0:4] = b"BND4"
    struct.pack_into("<i", header, 0x0C, count)
    struct.pack_into("<q", header, 0x10, HEADER_SIZE)
    header[0x18:0x20] = b"00000001"
    return bytes(header) + bytes(entries) + bytes(name_blob) + bytes(data)


ARCHIVE = build(
    {
        "USER_DATA000": b"slot zero contents" + b"\x00" * 46,
        "USER_DATA001": b"slot one contents" + b"\x00" * 47,
        "USER_DATA010": b"menu data" + b"\xff" * 55,
    }
)


def pipeline(entry: str | int = "USER_DATA000") -> Pipeline:
    return Pipeline.from_manifest([{"op": "bnd4", "entry": entry}])


class TestReading:
    def test_the_chosen_entry_comes_out(self) -> None:
        assert pipeline().decode(ARCHIVE).value.startswith(b"slot zero contents")

    def test_entries_can_be_chosen_by_index(self) -> None:
        assert pipeline(1).decode(ARCHIVE).value.startswith(b"slot one contents")

    def test_the_other_entries_are_listed(self) -> None:
        """The GUI needs the slot list to ask which character to edit."""
        hints = pipeline().decode(ARCHIVE).hints[0]
        assert hints["entry_names"] == ["USER_DATA000", "USER_DATA001", "USER_DATA010"]

    def test_an_unknown_entry_lists_what_is_there(self) -> None:
        with pytest.raises(Exception, match="USER_DATA010"):
            pipeline("USER_DATA999").decode(ARCHIVE)

    def test_a_file_that_is_not_bnd4(self) -> None:
        with pytest.raises(ValueError, match="BND4"):
            ops.get("bnd4").decode(b"GVAS not this one", {}, {})

    def test_an_entry_pointing_outside_the_file(self) -> None:
        broken = bytearray(ARCHIVE)
        struct.pack_into("<q", broken, HEADER_SIZE + 0x08, 10**9)
        with pytest.raises(ValueError, match="outside the file"):
            ops.get("bnd4").decode(bytes(broken), {}, {})

    def test_an_absurd_entry_count(self) -> None:
        broken = bytearray(ARCHIVE)
        struct.pack_into("<i", broken, 0x0C, 10**7)
        with pytest.raises(ValueError, match="does not fit"):
            ops.get("bnd4").decode(bytes(broken), {}, {})


class TestWriting:
    def test_round_trip_is_byte_exact(self) -> None:
        assert pipeline().round_trip(ARCHIVE).passed

    def test_editing_one_entry_leaves_the_others_alone(self) -> None:
        """A save has one slot worth editing and eleven that must not move."""
        pipe = pipeline()
        decoded = pipe.decode(ARCHIVE)
        edited = bytearray(decoded.value)
        edited[0:4] = b"SLOT"

        rebuilt = pipe.encode(bytes(edited), decoded.hints, passthrough=False)
        assert len(rebuilt) == len(ARCHIVE)
        pairs = zip(ARCHIVE, rebuilt, strict=True)
        assert sum(1 for before, after in pairs if before != after) == 4
        assert pipeline("USER_DATA001").decode(rebuilt).value.startswith(b"slot one contents")

    def test_a_resized_slot_is_refused(self) -> None:
        """Slots are fixed-size buffers; growing one would shift every offset."""
        pipe = pipeline()
        decoded = pipe.decode(ARCHIVE)
        with pytest.raises(Exception, match="fixed"):
            pipe.encode(decoded.value + b"extra", decoded.hints, passthrough=False)


REAL_SAVE = Path(
    os.environ.get(
        "SAVESMITH_ELDEN_RING_SAVE",
        Path.home()
        / "Applications/elden ring.app/Contents/SharedSupport/prefix/drive_c/users"
        / "Wineskin/AppData/Roaming/EldenRing/76561197960271872/ER0000.sl2",
    )
)


@pytest.mark.skipif(not REAL_SAVE.is_file(), reason="no local Elden Ring save")
class TestRealArchive:
    """Opt-in, against a real 28 MB ER0000.sl2 on this machine.

    The file is not committed: it is 28 MB of someone's game progress.
    """

    def test_twelve_entries(self) -> None:
        hints = pipeline().decode(REAL_SAVE.read_bytes()).hints[0]
        assert len(hints["entry_names"]) == 12
        assert hints["entry_names"][0] == "USER_DATA000"

    def test_round_trip_is_byte_exact(self) -> None:
        assert pipeline().round_trip(REAL_SAVE.read_bytes()).passed

    def test_one_changed_byte_changes_one_byte(self) -> None:
        raw = REAL_SAVE.read_bytes()
        pipe = pipeline()
        decoded = pipe.decode(raw)
        edited = bytearray(decoded.value)
        edited[100] ^= 0xFF
        rebuilt = pipe.encode(bytes(edited), decoded.hints, passthrough=False)
        differing = sum(1 for before, after in zip(raw, rebuilt, strict=True) if before != after)
        assert differing == 1
