"""Backups, and the rule that nothing is written without one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from savesmith.core.backup import BackupStore
from savesmith.core.errors import BackupError
from savesmith.core.paths import FakeSystem
from savesmith.core.platform_ import Platform


@pytest.fixture
def save(tmp_path: Path) -> Path:
    path = tmp_path / "game" / "user1.dat"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original contents")
    return path


@pytest.fixture
def store(tmp_path: Path) -> BackupStore:
    return BackupStore(tmp_path / "appdata" / "backups")


class TestCreating:
    def test_the_copy_is_a_plain_file(self, store: BackupStore, save: Path) -> None:
        """Restorable by hand, without SaveSmith, years from now."""
        backup = store.create(save, plugin_id="hollow-knight")
        assert backup.file.read_bytes() == b"original contents"
        assert backup.file.name == "user1.dat"
        assert backup.folder.parent.name == "hollow-knight"

    def test_the_timestamp_avoids_characters_windows_forbids(
        self, store: BackupStore, save: Path
    ) -> None:
        backup = store.create(save, plugin_id="hk")
        assert ":" not in backup.folder.name
        assert backup.folder.name.endswith("Z")

    def test_metadata_records_where_it_came_from(self, store: BackupStore, save: Path) -> None:
        backup = store.create(save, plugin_id="hk")
        payload = json.loads((backup.folder / "backup.json").read_text(encoding="utf-8"))
        assert payload["original"] == str(save)
        assert payload["size"] == len(b"original contents")

    def test_two_backups_in_the_same_second_do_not_collide(
        self, store: BackupStore, save: Path
    ) -> None:
        """Trying a few values in a row is ordinary use."""
        first = store.create(save, plugin_id="hk")
        second = store.create(save, plugin_id="hk")
        assert first.folder != second.folder
        assert first.file.exists() and second.file.exists()

    def test_backups_do_not_live_next_to_the_save(self, store: BackupStore, save: Path) -> None:
        """Steam Cloud syncs whole folders for some games."""
        store.create(save, plugin_id="hk")
        assert list(save.parent.iterdir()) == [save]

    def test_a_missing_original_fails_loudly(self, store: BackupStore, tmp_path: Path) -> None:
        with pytest.raises(BackupError) as caught:
            store.create(tmp_path / "nope.dat", plugin_id="hk")
        assert "nothing was changed" in caught.value.user_message

    def test_an_odd_plugin_id_cannot_escape_the_backup_folder(
        self, store: BackupStore, save: Path
    ) -> None:
        backup = store.create(save, plugin_id="../../etc")
        assert store.root in backup.folder.parents

    def test_the_store_finds_its_own_folder_from_the_system(self, tmp_path: Path) -> None:
        system = FakeSystem(platform=Platform.MACOS, home_dir=tmp_path)
        store = BackupStore.for_system(system)
        assert store.root.parts[-3:] == ("Application Support", "SaveSmith", "backups")


class TestListing:
    def test_newest_first(self, store: BackupStore, save: Path) -> None:
        first = store.create(save, plugin_id="hk")
        save.write_bytes(b"later contents")
        second = store.create(save, plugin_id="hk")
        listed = store.list_for("hk")
        assert [backup.folder for backup in listed] == [second.folder, first.folder]

    def test_filtering_by_the_original_file(self, store: BackupStore, save: Path) -> None:
        other = save.parent / "user2.dat"
        other.write_bytes(b"second slot")
        store.create(save, plugin_id="hk")
        store.create(other, plugin_id="hk")
        assert len(store.list_for("hk", original=other)) == 1

    def test_an_unknown_plugin_lists_nothing(self, store: BackupStore) -> None:
        assert store.list_for("never-seen") == []

    def test_a_damaged_backup_folder_is_skipped_not_fatal(
        self, store: BackupStore, save: Path
    ) -> None:
        good = store.create(save, plugin_id="hk")
        broken = good.folder.with_name("2020-01-01T00-00-00Z")
        broken.mkdir()
        (broken / "backup.json").write_text("{ not json", encoding="utf-8")
        assert [backup.folder for backup in store.list_for("hk")] == [good.folder]

    def test_stray_folders_are_ignored(self, store: BackupStore, save: Path) -> None:
        store.create(save, plugin_id="hk")
        (store.root / "hk" / "notes").mkdir()
        assert len(store.list_for("hk")) == 1


class TestRestoring:
    def test_restoring_brings_the_old_contents_back(
        self, store: BackupStore, save: Path
    ) -> None:
        backup = store.create(save, plugin_id="hk")
        save.write_bytes(b"edited badly")
        store.restore(backup)
        assert save.read_bytes() == b"original contents"

    def test_restoring_backs_up_what_it_replaces(self, store: BackupStore, save: Path) -> None:
        """One misclick must not lose the newer save."""
        backup = store.create(save, plugin_id="hk")
        save.write_bytes(b"the newer save")
        replaced = store.restore(backup)
        assert replaced.file.read_bytes() == b"the newer save"

    def test_restoring_to_a_different_file(self, store: BackupStore, save: Path) -> None:
        backup = store.create(save, plugin_id="hk")
        target = save.parent / "user2.dat"
        target.write_bytes(b"slot two")
        store.restore(backup, target=target)
        assert target.read_bytes() == b"original contents"

    def test_a_missing_backup_copy_is_reported(self, store: BackupStore, save: Path) -> None:
        backup = store.create(save, plugin_id="hk")
        backup.file.unlink()
        with pytest.raises(BackupError, match="missing from disk"):
            store.restore(backup)


class TestPruning:
    def test_keeps_the_newest(self, store: BackupStore, save: Path) -> None:
        for index in range(4):
            save.write_bytes(f"version {index}".encode())
            store.create(save, plugin_id="hk")
        removed = store.prune("hk", keep=2)
        assert len(removed) == 2
        assert len(store.list_for("hk")) == 2

    def test_keeping_nothing_is_refused(self, store: BackupStore) -> None:
        """Deleting every copy is never what someone means."""
        with pytest.raises(ValueError):
            store.prune("hk", keep=0)
