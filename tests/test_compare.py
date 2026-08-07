"""Finding a field by watching it change."""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from savesmith.core.compare import (
    compare_bytes,
    compare_structures,
    find_value,
    guesses_in_ranges,
    narrow,
    numeric_changes,
)
from savesmith.core.pipeline import Pipeline

CORPUS = Path(__file__).parent / "corpus" / "unreal-gvas" / "the-invincible"


class TestStructuredDiff:
    def test_a_changed_number(self) -> None:
        changes = compare_structures({"gold": 100}, {"gold": 70})
        assert [str(change) for change in changes] == ["gold: 100 → 70"]

    def test_untouched_values_are_not_reported(self) -> None:
        before = {"gold": 100, "name": "Ann", "hp": 50}
        after = {"gold": 70, "name": "Ann", "hp": 50}
        assert len(compare_structures(before, after)) == 1

    def test_nested_paths(self) -> None:
        before = {"player": {"stats": {"gold": 100}}}
        after = {"player": {"stats": {"gold": 70}}}
        assert compare_structures(before, after)[0].address == "player.stats.gold"

    def test_inside_lists(self) -> None:
        before = {"items": [{"count": 1}, {"count": 2}]}
        after = {"items": [{"count": 1}, {"count": 9}]}
        assert compare_structures(before, after)[0].address == "items[1].count"

    def test_a_field_that_only_appears_afterwards(self) -> None:
        """Exactly the kind of thing worth finding: doing something created it."""
        changes = compare_structures({"gold": 1}, {"gold": 1, "questDone": True})
        assert changes[0].address == "questDone"
        assert (changes[0].before, changes[0].after) == (None, True)

    def test_a_field_that_disappears(self) -> None:
        changes = compare_structures({"buff": 5}, {})
        assert (changes[0].before, changes[0].after) == (5, None)

    def test_lists_that_grew(self) -> None:
        changes = compare_structures({"items": [1]}, {"items": [1, 2]})
        assert changes[0].address == "items[1]"

    def test_identical_saves_produce_nothing(self) -> None:
        assert compare_structures({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}) == []

    def test_only_numbers_are_offered_as_fields(self) -> None:
        before = {"gold": 100, "checkpoint": "start", "alive": True}
        after = {"gold": 70, "checkpoint": "cave", "alive": False}
        numeric = numeric_changes(compare_structures(before, after))
        assert [change.address for change in numeric] == ["gold"]

    def test_booleans_are_not_treated_as_numbers(self) -> None:
        """True == 1 in Python; offering it as a number field would be absurd."""
        assert numeric_changes(compare_structures({"f": True}, {"f": False})) == []


class TestFindingAValue:
    def test_a_number_is_found_in_every_encoding_that_fits(self) -> None:
        raw = struct.pack("<i", 12400)
        encodings = {site.encoding for site in find_value(raw, 12400)}
        assert "int32-le" in encodings

    def test_a_float_is_found(self) -> None:
        raw = b"\x00" * 16 + struct.pack("<f", 2.5) + b"\x00" * 16
        assert any(site.encoding == "float32-le" for site in find_value(raw, 2.5))

    def test_big_endian_is_covered(self) -> None:
        raw = struct.pack(">i", 999999)
        assert any(site.encoding == "int32-be" for site in find_value(raw, 999999))

    def test_single_bytes_are_ignored_in_large_files(self) -> None:
        """A one-byte value matches thousands of times and means nothing."""
        raw = os.urandom(65536)
        assert all(site.encoding not in ("int8", "uint8") for site in find_value(raw, 7))

    def test_a_number_too_large_for_narrow_encodings_is_skipped(self) -> None:
        sites = find_value(struct.pack("<q", 2**40), 2**40)
        assert all("16" not in site.encoding for site in sites)


class TestNarrowing:
    def build(self, value: int, decoys: tuple[int, ...] = ()) -> bytearray:
        blob = bytearray(b"\x00" * 65536)
        struct.pack_into("<i", blob, 0x400, value)
        for offset in decoys:
            struct.pack_into("<i", blob, offset, value)
        return blob

    def test_two_files_pin_the_field_down(self) -> None:
        """The whole method: either number alone is everywhere; both is not."""
        before = self.build(12400, decoys=(0x100, 0x800, 0x2000))
        after = bytearray(before)
        struct.pack_into("<i", after, 0x400, 7300)

        guesses = narrow(bytes(before), bytes(after), 12400, 7300)
        assert {guess.offset for guess in guesses} == {0x400}

    def test_the_widest_encoding_is_offered_first(self) -> None:
        """A 32-bit value also matches as 16-bit halves; the wide one is real."""
        before = self.build(12400)
        after = bytearray(before)
        struct.pack_into("<i", after, 0x400, 7300)
        guesses = narrow(bytes(before), bytes(after), 12400, 7300)
        assert guesses[0].size >= guesses[-1].size

    def test_nothing_is_found_when_the_value_did_not_move(self) -> None:
        blob = bytes(self.build(12400))
        assert narrow(blob, blob, 12400, 7300) == []

    def test_it_works_on_a_realistic_amount_of_noise(self) -> None:
        size = 512 * 1024
        before = bytearray(os.urandom(size))
        struct.pack_into("<i", before, 0x1A2B4, 12400)
        after = bytearray(before)
        struct.pack_into("<i", after, 0x1A2B4, 7300)
        for offset in (0x100, 0x2000, 0x30000):
            struct.pack_into("<f", after, offset, 3.14159)

        guesses = narrow(bytes(before), bytes(after), 12400, 7300)
        assert {guess.offset for guess in guesses} == {0x1A2B4}


class TestByteDiff:
    def test_a_single_changed_field(self) -> None:
        before = bytearray(b"\x00" * 1024)
        after = bytearray(before)
        struct.pack_into("<i", after, 100, 999)
        diff = compare_bytes(bytes(before), bytes(after))
        assert diff.ranges and diff.ranges[0][0] == 100

    def test_identical_files(self) -> None:
        blob = b"\x01" * 512
        diff = compare_bytes(blob, blob)
        assert diff.ranges == []
        assert "identical" in diff.summary()

    def test_nearby_runs_are_merged(self) -> None:
        """One field often shows up as two runs with matching bytes between."""
        before = bytearray(b"\x00" * 256)
        after = bytearray(before)
        after[10] = 1
        after[14] = 1
        assert len(compare_bytes(bytes(before), bytes(after), gap=8).ranges) == 1

    def test_distant_runs_stay_apart(self) -> None:
        before = bytearray(b"\x00" * 256)
        after = bytearray(before)
        after[10] = 1
        after[200] = 1
        assert len(compare_bytes(bytes(before), bytes(after), gap=8).ranges) == 2

    def test_different_sizes_say_what_to_do_instead(self) -> None:
        with pytest.raises(ValueError, match="after decoding"):
            compare_bytes(b"\x00" * 10, b"\x00" * 20)

    def test_guesses_outside_changed_regions_are_dropped(self) -> None:
        """A chance match in a part of the file that never moved is not the field."""
        before = bytearray(b"\x00" * 4096)
        struct.pack_into("<i", before, 0x100, 12400)
        struct.pack_into("<i", before, 0x800, 12400)
        after = bytearray(before)
        struct.pack_into("<i", after, 0x100, 7300)
        struct.pack_into("<i", after, 0x800, 7300)

        diff = compare_bytes(bytes(before), bytes(after))
        diff.ranges = [(0x100, 4)]  # pretend only the first region moved
        guesses = narrow(bytes(before), bytes(after), 12400, 7300)
        assert {guess.offset for guess in guesses_in_ranges(guesses, diff)} == {0x100}


@pytest.mark.skipif(not CORPUS.is_dir(), reason="corpus not present")
class TestRealSaves:
    def test_two_real_saves_differ_in_named_fields(self) -> None:
        pipeline = Pipeline.from_manifest([{"op": "gvas"}])
        before = pipeline.decode((CORPUS / "MenuSettingsSave.sav").read_bytes()).value
        after = pipeline.decode((CORPUS / "ComicsSave.sav").read_bytes()).value

        changes = compare_structures(before, after)
        assert any(change.address.startswith("properties.") for change in changes)

    def test_a_save_compared_with_itself_shows_nothing(self) -> None:
        pipeline = Pipeline.from_manifest([{"op": "gvas"}])
        value = pipeline.decode((CORPUS / "ComicsSave.sav").read_bytes()).value
        assert compare_structures(value, value) == []


class TestNumbersFromACommandLine:
    def test_a_whole_number_given_as_a_float_still_finds_integers(self) -> None:
        """--was 12400 arrives as 12400.0; struct refuses a float for '<i'."""
        blob = bytearray(b"\x00" * 4096)
        struct.pack_into("<i", blob, 0x100, 12400)
        encodings = {site.encoding for site in find_value(bytes(blob), 12400.0)}
        assert "int32-le" in encodings

    def test_a_fractional_number_only_matches_floats(self) -> None:
        blob = bytearray(b"\x00" * 4096)
        struct.pack_into("<f", blob, 0x100, 2.5)
        encodings = {site.encoding for site in find_value(bytes(blob), 2.5)}
        assert encodings and all("float" in name for name in encodings)

    def test_narrowing_works_with_floats_from_the_command_line(self) -> None:
        before = bytearray(b"\x00" * 4096)
        struct.pack_into("<i", before, 0x200, 999)
        after = bytearray(before)
        struct.pack_into("<i", after, 0x200, 111)
        guesses = narrow(bytes(before), bytes(after), 999.0, 111.0)
        assert {guess.offset for guess in guesses} == {0x200}
