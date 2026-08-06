"""The plugin collection and the round-trip gate.

The gate is the whole reason a stranger can trust a plugin someone else wrote,
so most of this is about what it refuses.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from savesmith.core.plugin import Confidence, Plugin
from savesmith.core.repository import PluginRepository, bundled, verify

CORPUS = Path(__file__).parent / "corpus"
INVINCIBLE = CORPUS / "unreal-gvas" / "the-invincible"

MANIFEST = {
    "id": "test-game",
    "version": 1,
    "game": "Test Game",
    "engine": "test",
    "confidence": "probable",
    "risk": {"tier": "safe", "reason": {"en": "single player"}},
    "pipeline": [{"op": "gzip"}, {"op": "json_parse"}],
    "fields": [],
}


def write_plugin(root: Path, plugin_id: str, **overrides: object) -> Path:
    folder = root / plugin_id
    folder.mkdir(parents=True)
    data = dict(MANIFEST, id=plugin_id, **overrides)
    (folder / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return folder


class TestLoading:
    def test_the_bundled_plugins_all_load(self) -> None:
        catalogue = bundled().load()
        assert catalogue.problems == [], [p.user_message for p in catalogue.problems]
        assert {plugin.id for plugin in catalogue.plugins} >= {
            "the-invincible",
            "rpgmaker-mv",
            "unity-es3",
        }

    def test_one_broken_plugin_does_not_hide_the_others(self, tmp_path: Path) -> None:
        write_plugin(tmp_path, "good-one")
        broken = tmp_path / "broken-one"
        broken.mkdir()
        (broken / "manifest.json").write_text("{ not json", encoding="utf-8")

        catalogue = PluginRepository(tmp_path).load()
        assert [plugin.id for plugin in catalogue.plugins] == ["good-one"]
        assert len(catalogue.problems) == 1

    def test_folders_without_a_manifest_are_skipped(self, tmp_path: Path) -> None:
        write_plugin(tmp_path, "real")
        (tmp_path / "notes").mkdir()
        assert len(PluginRepository(tmp_path).load().plugins) == 1

    def test_a_missing_folder_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert PluginRepository(tmp_path / "nowhere").load().plugins == []

    def test_lookup_by_id_and_appid(self) -> None:
        catalogue = bundled().load()
        assert catalogue.by_id("the-invincible") is not None
        assert catalogue.by_id("nope") is None
        assert catalogue.by_appid(731040)


class TestMatching:
    def test_a_plugin_is_offered_only_if_it_can_open_the_file(self, tmp_path: Path) -> None:
        """Opening the file is the test, not the file's name."""
        write_plugin(tmp_path, "gzip-game")
        write_plugin(tmp_path, "plain-game", pipeline=[{"op": "json_parse"}])

        raw = gzip.compress(json.dumps({"gold": 1}).encode(), mtime=0)
        matched = PluginRepository(tmp_path).match(raw)
        assert [plugin.id for plugin in matched] == ["gzip-game"]

    def test_the_steam_appid_breaks_a_tie(self, tmp_path: Path) -> None:
        write_plugin(tmp_path, "aaa-generic")
        write_plugin(tmp_path, "zzz-specific", steam_appid=12345)
        raw = gzip.compress(json.dumps({"gold": 1}).encode(), mtime=0)
        matched = PluginRepository(tmp_path).match(raw, appid=12345)
        assert matched[0].id == "zzz-specific"

    def test_magic_bytes_rule_a_plugin_out_early(self, tmp_path: Path) -> None:
        write_plugin(tmp_path, "unreal-only", detect={"magic_hex": "47564153"})
        raw = gzip.compress(json.dumps({"gold": 1}).encode(), mtime=0)
        assert PluginRepository(tmp_path).match(raw) == []

    def test_nothing_matches_an_unknown_file(self, tmp_path: Path) -> None:
        write_plugin(tmp_path, "gzip-game")
        assert PluginRepository(tmp_path).match(b"\x00\x01\x02\x03") == []


class TestTheGate:
    def gzip_save(self, path: Path, value: int = 1) -> Path:
        path.write_bytes(gzip.compress(json.dumps({"gold": value}).encode(), mtime=0))
        return path

    def test_a_faithful_plugin_keeps_its_claim(self, tmp_path: Path) -> None:
        plugin = Plugin.from_mapping(MANIFEST)
        files = [self.gzip_save(tmp_path / f"save{index}.dat", index) for index in range(3)]
        result = verify(plugin, files)
        assert result.passed
        assert result.confidence is Confidence.PROBABLE
        assert result.publishable

    def test_one_bad_file_is_enough_to_demote_the_plugin(self, tmp_path: Path) -> None:
        """The corpus is a whole; passing most of it is not passing."""
        plugin = Plugin.from_mapping(MANIFEST)
        files = [self.gzip_save(tmp_path / "good.dat")]
        broken = tmp_path / "broken.dat"
        broken.write_bytes(b"not gzip at all")
        files.append(broken)

        result = verify(plugin, files)
        assert not result.passed
        assert result.confidence is Confidence.EXPERIMENTAL
        assert not result.publishable

    def test_an_unreadable_file_is_reported_not_swallowed(self, tmp_path: Path) -> None:
        plugin = Plugin.from_mapping(MANIFEST)
        result = verify(plugin, [tmp_path / "missing.dat"])
        assert not result.passed
        assert "could not be read" in result.results[0].describe()

    def test_an_empty_corpus_proves_nothing(self, tmp_path: Path) -> None:
        """Claiming to work without ever being run is exactly what the gate stops."""
        result = verify(Plugin.from_mapping(MANIFEST), [])
        assert not result.passed
        assert result.confidence is Confidence.EXPERIMENTAL

    def test_the_gate_never_promotes(self, tmp_path: Path) -> None:
        """Passing bytes is not a person confirming the game loaded the save."""
        plugin = Plugin.from_mapping(dict(MANIFEST, confidence="experimental"))
        files = [self.gzip_save(tmp_path / "save.dat")]
        assert verify(plugin, files).confidence is Confidence.EXPERIMENTAL

    def test_a_verified_claim_survives_a_passing_run(self, tmp_path: Path) -> None:
        plugin = Plugin.from_mapping(dict(MANIFEST, confidence="verified"))
        files = [self.gzip_save(tmp_path / "save.dat")]
        assert verify(plugin, files).confidence is Confidence.VERIFIED

    def test_a_verified_claim_is_destroyed_by_a_failing_run(self, tmp_path: Path) -> None:
        plugin = Plugin.from_mapping(dict(MANIFEST, confidence="verified"))
        broken = tmp_path / "broken.dat"
        broken.write_bytes(b"not gzip")
        assert verify(plugin, [broken]).confidence is Confidence.EXPERIMENTAL

    def test_the_report_is_readable(self, tmp_path: Path) -> None:
        plugin = Plugin.from_mapping(MANIFEST)
        lines = verify(plugin, [self.gzip_save(tmp_path / "save.dat")]).explain()
        assert any("claimed" in line and "allowed" in line for line in lines)


@pytest.mark.skipif(not INVINCIBLE.is_dir(), reason="corpus not present")
class TestBundledPluginsAgainstTheCorpus:
    """Milestone 2's definition of done: the gate, on real files."""

    def test_the_invincible_rebuilds_every_corpus_file(self) -> None:
        plugin = bundled().load().by_id("the-invincible")
        assert plugin is not None
        result = verify(plugin, sorted(INVINCIBLE.glob("*.sav")))
        assert result.passed, "\n".join(result.explain())
        assert result.publishable

    def test_its_fields_are_reachable_in_a_real_save(self) -> None:
        from savesmith.core.savefile import SaveFile

        plugin = bundled().load().by_id("the-invincible")
        assert plugin is not None
        save = SaveFile.open(INVINCIBLE / "MenuSettingsSave.sav", plugin)
        present = {view.spec.address for view in save.view() if view.present}
        assert "properties.SaveVersion" in present

    def test_a_field_absent_from_one_save_is_simply_unavailable(self) -> None:
        """Different save files of one game hold different things."""
        from savesmith.core.savefile import SaveFile

        plugin = bundled().load().by_id("the-invincible")
        assert plugin is not None
        save = SaveFile.open(INVINCIBLE / "ComicsSave.sav", plugin)
        assert not any(view.present for view in save.view())
