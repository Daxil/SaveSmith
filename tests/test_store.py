"""Installing plugins from somewhere else.

A plugin archive is somebody else's file, so most of this is about what gets
refused.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from savesmith.core.paths import FakeSystem
from savesmith.core.platform_ import Platform
from savesmith.core.store import (
    MAX_ENTRIES,
    PluginInstallError,
    PluginStore,
    describe_limits,
)

MANIFEST: dict[str, Any] = {
    "id": "shared-game",
    "version": 2,
    "game": "Shared Game",
    "engine": "test",
    "confidence": "probable",
    "risk": {"tier": "safe", "reason": {"en": "single player"}},
    "pipeline": [{"op": "json_parse"}],
    "fields": [],
}


def archive(files: dict[str, bytes] | None = None, **overrides: Any) -> bytes:
    manifest = dict(MANIFEST, **overrides)
    members = {f"{manifest['id']}/manifest.json": json.dumps(manifest).encode()}
    members.update(files or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, data in members.items():
            zipped.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> PluginStore:
    return PluginStore(tmp_path / "plugins")


class TestInstalling:
    def test_a_plugin_from_an_archive(self, store: PluginStore) -> None:
        result = store.install_archive(archive())
        assert result.plugin.id == "shared-game"
        assert (store.root / "shared-game" / "manifest.json").is_file()
        assert not result.updated

    def test_a_plugin_from_a_folder(self, store: PluginStore, tmp_path: Path) -> None:
        folder = tmp_path / "source" / "shared-game"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        assert store.install_folder(folder).plugin.version == 2

    def test_an_archive_without_a_wrapping_folder(self, store: PluginStore) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            zipped.writestr("manifest.json", json.dumps(MANIFEST))
        assert store.install_archive(buffer.getvalue()).plugin.id == "shared-game"

    def test_a_codec_is_installed_alongside(self, store: PluginStore) -> None:
        data = archive({"shared-game/codec.py": b"def decode(data):\n    return data\n"})
        result = store.install_archive(data)
        assert (result.folder / "codec.py").is_file()

    def test_the_installed_plugin_can_be_loaded_back(self, store: PluginStore) -> None:
        store.install_archive(archive())
        assert store.installed().by_id("shared-game") is not None

    def test_installed_plugins_shadow_the_bundled_ones(self, store: PluginStore) -> None:
        """An update can be installed without waiting for a release."""
        store.install_archive(archive(id="the-invincible", version=99))
        found = store.catalogue().by_id("the-invincible")
        assert found is not None and found.version == 99

    def test_bundled_plugins_are_still_offered(self, store: PluginStore) -> None:
        ids = {plugin.id for plugin in store.catalogue().plugins}
        assert "rpgmaker-mv" in ids


class TestVersions:
    def test_a_newer_version_replaces_the_old_one(self, store: PluginStore) -> None:
        store.install_archive(archive(version=2))
        result = store.install_archive(archive(version=3))
        assert result.updated
        assert result.replaced == 2
        assert "updated shared-game from v2 to v3" in result.describe()

    def test_an_older_version_is_refused(self, store: PluginStore) -> None:
        """Otherwise a stale copy silently undoes someone's update."""
        store.install_archive(archive(version=5))
        with pytest.raises(PluginInstallError, match="newer than"):
            store.install_archive(archive(version=4))

    def test_a_downgrade_can_be_asked_for_explicitly(self, store: PluginStore) -> None:
        store.install_archive(archive(version=5))
        result = store.install_archive(archive(version=4), allow_downgrade=True)
        assert result.plugin.version == 4

    def test_reinstalling_the_same_version_is_allowed(self, store: PluginStore) -> None:
        store.install_archive(archive(version=2))
        assert store.install_archive(archive(version=2)).plugin.version == 2


class TestRefusingHostileArchives:
    def test_a_path_that_escapes_the_folder(self, store: PluginStore) -> None:
        """The oldest trick against archive extraction, and it still works often."""
        data = archive({"../../evil.py": b"pwned"})
        with pytest.raises(PluginInstallError, match="escape"):
            store.install_archive(data)

    def test_an_absolute_path(self, store: PluginStore) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            zipped.writestr("manifest.json", json.dumps(MANIFEST))
            zipped.writestr("/etc/passwd", "root")
        with pytest.raises(PluginInstallError, match="escape"):
            store.install_archive(buffer.getvalue())

    def test_too_many_files(self, store: PluginStore) -> None:
        extra = {f"shared-game/file{index}.json": b"{}" for index in range(MAX_ENTRIES + 5)}
        with pytest.raises(PluginInstallError, match="more than"):
            store.install_archive(archive(extra))

    def test_a_zip_bomb(self, store: PluginStore) -> None:
        """A few kilobytes that unpack into gigabytes."""
        data = archive({"shared-game/big.txt": b"\x00" * (9 * 1024 * 1024)})
        with pytest.raises(PluginInstallError, match="larger than allowed"):
            store.install_archive(data)

    def test_a_file_type_a_plugin_has_no_business_containing(self, store: PluginStore) -> None:
        with pytest.raises(PluginInstallError, match="not something a plugin may contain"):
            store.install_archive(archive({"shared-game/payload.exe": b"MZ"}))

    def test_deep_nesting(self, store: PluginStore) -> None:
        with pytest.raises(PluginInstallError, match="deeper"):
            store.install_archive(archive({"a/b/c/manifest.json": b"{}"}))

    def test_something_that_is_not_a_zip(self, store: PluginStore) -> None:
        with pytest.raises(PluginInstallError, match="readable archive"):
            store.install_archive(b"this is not a zip file at all")

    def test_an_archive_with_no_manifest(self, store: PluginStore) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            zipped.writestr("notes/readme.md", "hello")
        with pytest.raises(PluginInstallError, match=r"no manifest\.json"):
            store.install_archive(buffer.getvalue())

    def test_two_plugins_in_one_archive(self, store: PluginStore) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            zipped.writestr("one/manifest.json", json.dumps(dict(MANIFEST, id="one")))
            zipped.writestr("two/manifest.json", json.dumps(dict(MANIFEST, id="two")))
        with pytest.raises(PluginInstallError, match="more than one plugin"):
            store.install_archive(buffer.getvalue())

    def test_an_invalid_manifest_never_reaches_the_plugin_folder(
        self, store: PluginStore
    ) -> None:
        """Checked while still in a temporary folder."""
        with pytest.raises(PluginInstallError, match="not a valid plugin"):
            store.install_archive(archive(risk=None))
        assert not (store.root / "shared-game").exists()


class TestDownloading:
    def test_installing_from_a_url(self, store: PluginStore) -> None:
        class Fetcher:
            def fetch(self, url: str) -> bytes:
                assert url.endswith(".zip")
                return archive()

        result = store.install_from("https://example.invalid/shared-game.zip", Fetcher())
        assert result.plugin.id == "shared-game"

    def test_a_download_failure_is_reported_plainly(self, store: PluginStore) -> None:
        class Fetcher:
            def fetch(self, url: str) -> bytes:
                raise OSError("no route to host")

        with pytest.raises(PluginInstallError, match="could not be downloaded"):
            store.install_from("https://example.invalid/x.zip", Fetcher())


class TestRemovingAndExporting:
    def test_removing(self, store: PluginStore) -> None:
        store.install_archive(archive())
        assert store.remove("shared-game")
        assert store.installed().by_id("shared-game") is None

    def test_removing_something_that_is_not_there(self, store: PluginStore) -> None:
        assert not store.remove("never-installed")

    def test_exporting_produces_an_installable_archive(
        self, store: PluginStore, tmp_path: Path
    ) -> None:
        store.install_archive(archive({"shared-game/codec.py": b"# codec\n"}))
        exported = store.export("shared-game", tmp_path / "out" / "shared-game.zip")
        assert exported.is_file()

        elsewhere = PluginStore(tmp_path / "other")
        result = elsewhere.install_archive(exported.read_bytes())
        assert result.plugin.id == "shared-game"
        assert (result.folder / "codec.py").is_file()

    def test_exporting_a_bundled_plugin(self, store: PluginStore, tmp_path: Path) -> None:
        exported = store.export("the-invincible", tmp_path / "inv.zip")
        assert exported.is_file()

    def test_exporting_something_unknown(self, store: PluginStore, tmp_path: Path) -> None:
        with pytest.raises(PluginInstallError, match="no such plugin"):
            store.export("never-heard-of-it", tmp_path / "x.zip")


class TestWhereItLives:
    def test_the_store_finds_its_folder_from_the_system(self, tmp_path: Path) -> None:
        system = FakeSystem(platform=Platform.MACOS, home_dir=tmp_path)
        store = PluginStore.for_system(system)
        assert store.root.parts[-3:] == ("Application Support", "SaveSmith", "plugins")

    def test_the_limits_are_listed_for_auditing(self) -> None:
        text = " ".join(describe_limits())
        assert "sandbox" in text
        assert "'..'" in text
