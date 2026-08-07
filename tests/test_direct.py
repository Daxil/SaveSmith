"""Editing a save that has no plugin.

The dangerous half of SaveSmith, so these lean on the guards rather than the
happy path: no write without a backup, no write without reading the change back
out of the rebuilt file, and no guessing which of several candidates the player
meant.
"""

from __future__ import annotations

import gzip
import json
import struct
from pathlib import Path

import pytest

from savesmith.core.backup import BackupStore
from savesmith.core.direct import AddressError, DirectSave, parse_address
from savesmith.core.errors import FieldValueError, SaveSmithError


@pytest.fixture
def store(tmp_path: Path) -> BackupStore:
    return BackupStore(tmp_path / "backups")


@pytest.fixture
def structured(tmp_path: Path) -> Path:
    path = tmp_path / "save.dat"
    body = {"player": {"gold": 12400, "hp": 50}, "bank": 12400, "name": "Ann", "won": True}
    path.write_bytes(gzip.compress(json.dumps(body).encode(), mtime=0))
    return path


@pytest.fixture
def binary(tmp_path: Path) -> Path:
    """A plain binary save: no wrapper, nothing the ladder can peel off."""
    body = bytearray(b"\x00" * 256)
    struct.pack_into("<i", body, 0x40, 12400)
    struct.pack_into("<f", body, 0x80, 1.5)
    path = tmp_path / "slot.bin"
    path.write_bytes(bytes(body))
    return path


class TestFindingANumber:
    def test_a_structured_save_answers_with_paths(self, structured: Path) -> None:
        sites = DirectSave.open(structured).search(12400)
        assert {site.address for site in sites} == {"player.gold", "bank"}

    def test_the_surrounding_field_is_shown_to_tell_them_apart(self, structured: Path) -> None:
        sites = {site.address: site for site in DirectSave.open(structured).search(12400)}
        assert sites["player.gold"].context == "player"

    def test_true_is_not_offered_as_the_number_one(self, structured: Path) -> None:
        """Nobody looking at their screen sees 'won' as a 1."""
        assert [site.address for site in DirectSave.open(structured).search(1)] == []

    def test_a_binary_save_answers_with_offsets(self, binary: Path) -> None:
        sites = DirectSave.open(binary).search(12400)
        assert any(site.address == "0x40:int32-le" for site in sites)

    def test_the_same_bytes_are_reported_once(self, binary: Path) -> None:
        """int32-le and uint32-le at one offset are one candidate, not two.

        On a real Elden Ring save this cut the list from 24 lines to 5.
        """
        sites = DirectSave.open(binary).search(12400)
        addresses = [site.address for site in sites]
        assert addresses.count("0x40:int32-le") == 1
        assert "0x40:uint32-le" not in addresses
        four_byte = next(site for site in sites if site.address == "0x40:int32-le")
        assert "uint32-le" in four_byte.context, "the other reading is still mentioned"

    def test_widths_that_differ_are_kept_apart(self, binary: Path) -> None:
        """Four bytes and eight bytes are genuinely different readings."""
        sites = {site.address for site in DirectSave.open(binary).search(12400)}
        assert "0x40:int32-le" in sites
        assert any("int16" in address or "int64" in address for address in sites)

    def test_one_storage_type_at_a_time(self, binary: Path) -> None:
        sites = DirectSave.open(binary).search(12400, encoding="uint32-le")
        assert [site.address for site in sites] == ["0x40:uint32-le"]

    def test_a_storage_type_nobody_has_heard_of(self, binary: Path) -> None:
        with pytest.raises(AddressError):
            DirectSave.open(binary).search(12400, encoding="quadword")

    def test_a_number_that_is_not_there(self, structured: Path) -> None:
        assert DirectSave.open(structured).search(777) == []

    def test_the_wrapper_is_peeled_off_first(self, structured: Path) -> None:
        """Searching the compressed bytes would find nothing at all."""
        save = DirectSave.open(structured)
        assert save.description == "gzip → json_parse"
        assert save.search(12400)


class TestChangingIt:
    def test_a_path_is_changed_and_the_file_rebuilt(
        self, structured: Path, store: BackupStore
    ) -> None:
        save = DirectSave.open(structured)
        assert save.change("player.gold", 99999) == (12400, 99999)
        save.write(store)

        written = json.loads(gzip.decompress(structured.read_bytes()))
        assert written["player"]["gold"] == 99999
        assert written["bank"] == 12400, "only the address given was touched"

    def test_an_integer_stays_an_integer(self, structured: Path, store: BackupStore) -> None:
        save = DirectSave.open(structured)
        save.change("player.gold", 500.0)
        save.write(store)
        assert "500," in gzip.decompress(structured.read_bytes()).decode()

    def test_an_offset_is_changed_in_place(self, binary: Path, store: BackupStore) -> None:
        save = DirectSave.open(binary)
        assert save.change("0x40:int32-le", 99999) == (12400, 99999)
        save.write(store)
        assert struct.unpack_from("<i", binary.read_bytes(), 0x40)[0] == 99999
        assert len(binary.read_bytes()) == 256, "the file did not change size"

    def test_a_float_field_keeps_its_type(self, binary: Path, store: BackupStore) -> None:
        save = DirectSave.open(binary)
        save.change("0x80:float32-le", 9.25)
        save.write(store)
        assert struct.unpack_from("<f", binary.read_bytes(), 0x80)[0] == pytest.approx(9.25)

    def test_a_value_the_encoding_cannot_hold(self, binary: Path) -> None:
        save = DirectSave.open(binary)
        with pytest.raises(FieldValueError) as caught:
            save.change("0x40:int32-le", 9e18)
        assert "cannot hold" in caught.value.user_message

    def test_a_fraction_where_the_game_stores_a_whole_number(self, binary: Path) -> None:
        with pytest.raises(FieldValueError):
            DirectSave.open(binary).change("0x40:int32-le", 1.5)

    def test_an_offset_past_the_end(self, binary: Path) -> None:
        with pytest.raises(AddressError) as caught:
            DirectSave.open(binary).change("0xFFFF:int32-le", 1)
        assert "past the end" in caught.value.user_message

    def test_a_path_holding_something_that_is_not_a_number(self, structured: Path) -> None:
        with pytest.raises(FieldValueError) as caught:
            DirectSave.open(structured).change("name", 5)
        assert "not a number" in caught.value.user_message


class TestTheGuards:
    def test_nothing_is_written_without_a_backup(
        self, structured: Path, store: BackupStore
    ) -> None:
        save = DirectSave.open(structured)
        save.change("player.gold", 1)
        backup = save.write(store)

        assert backup.file.is_file()
        assert json.loads(gzip.decompress(backup.file.read_bytes()))["player"]["gold"] == 12400

    def test_writing_nothing_is_refused(self, structured: Path, store: BackupStore) -> None:
        with pytest.raises(SaveSmithError) as caught:
            DirectSave.open(structured).write(store)
        assert "nothing to write" in caught.value.user_message

    def test_a_change_that_does_not_survive_rebuilding_is_not_written(
        self, structured: Path, store: BackupStore
    ) -> None:
        """The check that matters: compression, encryption and checksums all run
        again on the way out, and any of them could quietly drop an edit."""
        save = DirectSave.open(structured)
        before = structured.read_bytes()
        # A change that was never applied to the structure stands in for one
        # that a rebuilding step silently discarded.
        save.changes.append(("player.gold", 12400, 99999))

        with pytest.raises(SaveSmithError) as caught:
            save.write(store)
        assert "did not survive" in caught.value.user_message
        assert structured.read_bytes() == before, "the save must be untouched"

    def test_the_original_is_still_readable_afterwards(
        self, structured: Path, store: BackupStore
    ) -> None:
        save = DirectSave.open(structured)
        save.change("player.gold", 99999)
        save.write(store)
        assert DirectSave.open(structured).search(99999)

    def test_an_unreadable_file_says_so_in_a_sentence(self, tmp_path: Path) -> None:
        with pytest.raises(SaveSmithError) as caught:
            DirectSave.open(tmp_path / "not-here.sav")
        assert "could not be read" in caught.value.user_message


class TestAddresses:
    def test_hex_and_decimal_both_work(self) -> None:
        assert parse_address("0x40:uint32-le") == (64, "uint32-le")
        assert parse_address("64:uint32-le") == (64, "uint32-le")

    def test_an_address_without_a_type(self) -> None:
        with pytest.raises(AddressError) as caught:
            parse_address("0x40")
        assert "0x1F4C:uint32" in caught.value.user_message

    def test_a_type_nobody_has_heard_of(self) -> None:
        with pytest.raises(AddressError) as caught:
            parse_address("0x40:quadword")
        assert "no storage type called" in caught.value.user_message

    def test_a_nonsense_offset(self) -> None:
        with pytest.raises(AddressError):
            parse_address("banana:uint32-le")

    def test_a_negative_offset(self) -> None:
        with pytest.raises(AddressError):
            parse_address("-4:uint32-le")
