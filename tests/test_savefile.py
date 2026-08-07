"""Opening, editing and writing one save file."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from savesmith.core.backup import BackupStore
from savesmith.core.errors import FieldPathError, FieldValueError, SaveSmithError
from savesmith.core.plugin import Plugin
from savesmith.core.savefile import Change, SaveFile

SAVE: dict[str, Any] = {
    "playerData": {"geo": 42, "maxHealth": 5, "kills": 7, "rank": 3},
    "party": {"items": [{"id": "nail", "count": 1, "uuid": "a"}]},
}

MANIFEST: dict[str, Any] = {
    "id": "test-game",
    "version": 1,
    "game": "Test Game",
    "engine": "test",
    "confidence": "probable",
    "risk": {"tier": "safe", "reason": {"en": "single player"}},
    "pipeline": [{"op": "gzip"}, {"op": "json_parse"}],
    "fields": [
        {
            "path": "playerData.geo",
            "label": {"en": "Geo", "ru": "Гео"},
            "type": "int",
            "min": 0,
            "max": 999999,
        },
        {"path": "playerData.maxHealth", "label": {"en": "Health"}, "type": "int", "max": 11},
        {
            "path": "playerData.kills",
            "label": {"en": "Kills"},
            "type": "int",
            "achievement": True,
        },
        {
            "path": "playerData.rank",
            "label": {"en": "Online rank"},
            "type": "int",
            "online_linked": True,
        },
        {"path": "playerData.essence", "label": {"en": "Essence"}, "type": "int"},
    ],
    "containers": [
        {
            "id": "bag",
            "label": {"en": "inventory"},
            "path": "party.items",
            "shape": "list",
            "key": "id",
            "count": "count",
        }
    ],
}


@pytest.fixture
def plugin() -> Plugin:
    return Plugin.from_mapping(MANIFEST)


@pytest.fixture
def save_path(tmp_path: Path) -> Path:
    path = tmp_path / "game" / "user1.dat"
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress(json.dumps(SAVE, indent=2).encode(), mtime=0))
    return path


@pytest.fixture
def store(tmp_path: Path) -> BackupStore:
    return BackupStore(tmp_path / "appdata" / "backups")


@pytest.fixture
def save(save_path: Path, plugin: Plugin) -> SaveFile:
    return SaveFile.open(save_path, plugin)


class TestReading:
    def test_values_come_out(self, save: SaveFile) -> None:
        assert save.read("playerData.geo") == 42

    def test_a_field_missing_from_this_save_is_shown_as_unavailable(
        self, save: SaveFile
    ) -> None:
        """Save layouts differ by game version; that is not an error."""
        views = {view.spec.address: view for view in save.view()}
        assert views["playerData.geo"].present
        assert not views["playerData.essence"].present
        assert views["playerData.essence"].value is None

    def test_fields_keep_the_authors_order(self, save: SaveFile) -> None:
        assert next(view.spec.address for view in save.view()) == "playerData.geo"

    def test_an_unknown_address_is_refused(self, save: SaveFile) -> None:
        with pytest.raises(FieldPathError, match="does not describe"):
            save.read("playerData.nothing")


class TestEditing:
    def test_a_change_is_staged_not_written(self, save: SaveFile, save_path: Path) -> None:
        before = save_path.read_bytes()
        save.set("playerData.geo", 1000)
        assert save.modified
        assert save_path.read_bytes() == before, "nothing reaches disk until write()"

    def test_bounds_come_from_the_plugin(self, save: SaveFile) -> None:
        with pytest.raises(FieldValueError, match="11"):
            save.set("playerData.maxHealth", 99)

    def test_text_is_converted_when_it_can_be(self, save: SaveFile) -> None:
        assert save.set("playerData.geo", "1234").after == 1234

    def test_online_linked_fields_are_refused_outright(self, save: SaveFile) -> None:
        """Refused in the core, not merely greyed out in the interface."""
        with pytest.raises(FieldValueError) as caught:
            save.set("playerData.rank", 1)
        assert "online service" in caught.value.user_message

    def test_achievement_fields_need_an_explicit_opt_in(self, save: SaveFile) -> None:
        with pytest.raises(FieldValueError, match="achievement"):
            save.set("playerData.kills", 999)
        assert save.set("playerData.kills", 999, allow_achievements=True).after == 999

    def test_editing_a_field_this_save_lacks(self, save: SaveFile) -> None:
        with pytest.raises(FieldPathError):
            save.set("playerData.essence", 10)

    def test_reverting_puts_the_old_value_back(self, save: SaveFile) -> None:
        save.set("playerData.geo", 1000)
        save.revert("playerData.geo")
        assert save.read("playerData.geo") == 42
        assert not save.modified

    def test_the_pending_list_shows_before_and_after(self, save: SaveFile) -> None:
        save.set("playerData.geo", 1000)
        change = save.pending[0]
        assert (change.before, change.after) == (42, 1000)


class TestWriting:
    def test_a_backup_is_made_before_the_file_changes(
        self, save: SaveFile, save_path: Path, store: BackupStore
    ) -> None:
        original = save_path.read_bytes()
        save.set("playerData.geo", 1000)
        backup = save.write(store)
        assert backup.file.read_bytes() == original

    def test_the_edit_survives_a_reopen(
        self, save: SaveFile, save_path: Path, store: BackupStore, plugin: Plugin
    ) -> None:
        save.set("playerData.geo", 1000)
        save.write(store)
        assert SaveFile.open(save_path, plugin).read("playerData.geo") == 1000

    def test_untouched_values_are_left_alone(
        self, save: SaveFile, save_path: Path, store: BackupStore, plugin: Plugin
    ) -> None:
        save.set("playerData.geo", 1000)
        save.write(store)
        reopened = SaveFile.open(save_path, plugin)
        assert reopened.read("playerData.maxHealth") == 5
        assert reopened.read("playerData.kills") == 7

    def test_writing_with_no_changes_reproduces_the_file(
        self, save: SaveFile, save_path: Path, store: BackupStore
    ) -> None:
        original = save_path.read_bytes()
        save.write(store)
        assert save_path.read_bytes() == original

    def test_pending_changes_are_cleared_after_writing(
        self, save: SaveFile, store: BackupStore
    ) -> None:
        save.set("playerData.geo", 1000)
        save.write(store)
        assert not save.modified

    def test_a_save_the_game_rewrote_underneath_us_is_refused(
        self, save: SaveFile, save_path: Path, store: BackupStore
    ) -> None:
        """The one way a well-meaning edit destroys hours of progress."""
        save.set("playerData.geo", 1000)
        save_path.write_bytes(gzip.compress(json.dumps({"playerData": {}}).encode(), mtime=0))
        with pytest.raises(SaveSmithError) as caught:
            save.write(store)
        assert "saved over this file" in caught.value.user_message

    def test_no_temporary_files_are_left_behind(
        self, save: SaveFile, save_path: Path, store: BackupStore
    ) -> None:
        save.set("playerData.geo", 1000)
        save.write(store)
        assert [path.name for path in save_path.parent.iterdir()] == ["user1.dat"]

    def test_a_failed_write_leaves_the_original_intact(
        self, save: SaveFile, save_path: Path, store: BackupStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = save_path.read_bytes()
        save.set("playerData.geo", 1000)

        def explode(*_args: Any, **_kwargs: Any) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("os.replace", explode)
        with pytest.raises(SaveSmithError) as caught:
            save.write(store)
        assert save_path.read_bytes() == original
        assert "backup is untouched" in caught.value.user_message


class TestWriteRequiresABackupStore:
    def test_the_signature_makes_it_impossible_to_skip(self) -> None:
        """The rule is enforced by the shape of the API, not by discipline."""
        import inspect

        signature = inspect.signature(SaveFile.write)
        backups = signature.parameters["backups"]
        assert backups.default is inspect.Parameter.empty
        assert backups.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


class TestItems:
    """Containers go through the same file, the same staging, the same backup."""

    def test_what_is_in_the_bag_comes_out(self, save: SaveFile) -> None:
        assert [(stack.item, stack.count) for stack in save.stacks("bag")] == [("nail", 1)]

    def test_giving_something_shows_up_as_a_pending_change(self, save: SaveFile) -> None:
        save.give_item("bag", "elixir", 3)

        assert save.modified
        assert Change(address="bag/elixir", before=0, after=3) in save.pending

    def test_a_count_change_reads_as_the_number_a_player_sees(self, save: SaveFile) -> None:
        save.give_item("bag", "nail", 4)

        assert Change(address="bag/nail", before=1, after=5) in save.pending

    def test_nothing_reaches_the_file_until_it_is_written(
        self, save: SaveFile, save_path: Path, plugin: Plugin
    ) -> None:
        save.give_item("bag", "elixir", 3)

        reopened = SaveFile.open(save_path, plugin)

        assert [stack.item for stack in reopened.stacks("bag")] == ["nail"]

    def test_a_written_save_holds_the_new_item(
        self, save: SaveFile, save_path: Path, store: BackupStore, plugin: Plugin
    ) -> None:
        save.give_item("bag", "elixir", 3)
        save.write(store)

        reopened = SaveFile.open(save_path, plugin)

        assert [(s.item, s.count) for s in reopened.stacks("bag")] == [("nail", 1), ("elixir", 3)]
        assert not save.modified

    def test_reverting_a_container_puts_it_back_as_it_was_opened(self, save: SaveFile) -> None:
        save.give_item("bag", "elixir", 3)
        save.set_stack_count("bag", 0, 9)

        save.revert("bag")

        assert [(s.item, s.count) for s in save.stacks("bag")] == [("nail", 1)]
        assert not save.modified

    def test_reverting_one_container_leaves_a_field_change_alone(self, save: SaveFile) -> None:
        save.set("playerData.geo", 1000)
        save.give_item("bag", "elixir")

        save.revert("bag")

        assert save.read("playerData.geo") == 1000

    def test_a_container_this_plugin_does_not_have_says_so(self, save: SaveFile) -> None:
        with pytest.raises(SaveSmithError, match="describes no 'chest'"):
            save.give_item("chest", "elixir")
