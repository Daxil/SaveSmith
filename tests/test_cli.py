"""The command line.

It must add no rules of its own and relax none of the core's, so these check
the flow through it rather than the logic underneath — plus the one rule that
is genuinely its own: a person never sees a traceback.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from savesmith import cli
from savesmith.core.errors import SaveSmithError
from savesmith.core.paths import FakeSystem
from savesmith.core.playerprefs import open_prefs
from savesmith.core.store import PluginStore
from tests.conftest import seed_player_prefs

MANIFEST: dict[str, Any] = {
    "id": "cli-test-game",
    "version": 1,
    "game": "CLI Test Game",
    "engine": "test",
    "confidence": "probable",
    "risk": {"tier": "safe", "reason": {"en": "single player"}},
    "pipeline": [{"op": "gzip"}, {"op": "json_parse"}],
    "fields": [
        {"path": "gold", "label": {"en": "Gold", "ru": "Золото"}, "type": "int", "min": 0},
        {"path": "kills", "label": {"en": "Kills"}, "type": "int", "achievement": True},
    ],
    "containers": [
        {
            "id": "bag",
            "label": {"en": "Items", "ru": "Предметы"},
            "path": "items",
            "shape": "map",
            "catalog": "rpgmaker:items",
        }
    ],
}


MACHINE: list[FakeSystem] = []


@pytest.fixture
def home(fake_machine: FakeSystem) -> FakeSystem:
    """The machine every command in this file runs against."""
    MACHINE.clear()
    MACHINE.append(fake_machine)
    return fake_machine


@pytest.fixture
def plugin_installed(home: FakeSystem) -> Path:
    folder = PluginStore.for_system(home).root / "cli-test-game"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    return folder


@pytest.fixture
def save(tmp_path: Path) -> Path:
    path = tmp_path / "save.dat"
    contents = {"gold": 100, "kills": 3, "items": {"1": 2}}
    path.write_bytes(gzip.compress(json.dumps(contents).encode(), mtime=0))
    return path


@pytest.fixture
def opaque(tmp_path: Path) -> Path:
    """A file nothing built in can open, so discovery reaches the model step."""
    path = tmp_path / "mystery.sav"
    path.write_bytes(b"\x11\x22\x33\x44" * 512)
    return path


def run(*argv: str) -> int:
    return cli.main(list(argv), system=MACHINE[0] if MACHINE else None)


class TestBasics:
    def test_no_command_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert run() == 2
        assert "usage" in capsys.readouterr().out

    def test_identify(self, save: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert run("identify", str(save)) == 0
        assert "gzip → json_parse" in capsys.readouterr().out

    def test_identify_something_unreadable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        blob = tmp_path / "noise.bin"
        blob.write_bytes(bytes(range(256)) * 16)
        assert run("identify", str(blob)) == 1

    def test_checksum(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        import zlib

        body = b'{"gold":1}'
        path = tmp_path / "c.sav"
        path.write_bytes(zlib.crc32(body).to_bytes(4, "little") + body)
        assert run("checksum", str(path)) == 0
        assert "crc32-le" in capsys.readouterr().out

    def test_plugins_lists_the_bundled_ones(
        self, home: FakeSystem, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("plugins") == 0
        assert "the-invincible" in capsys.readouterr().out


class TestErrorsAreHuman:
    def test_a_missing_file_says_so_without_a_traceback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("identify", "/definitely/not/here.sav") == 1
        captured = capsys.readouterr()
        assert "could not be read" in captured.err
        assert "Traceback" not in captured.err
        assert "Error" not in captured.err.split("\n")[0]

    def test_an_unknown_plugin_name(
        self, home: FakeSystem, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("show", str(save), "--plugin", "no-such-plugin") == 1
        assert "no plugin called" in capsys.readouterr().err

    def test_a_save_nothing_can_read(
        self, home: FakeSystem, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        blob = tmp_path / "noise.bin"
        blob.write_bytes(bytes(range(256)) * 16)
        assert run("show", str(blob)) == 1
        assert "discover" in capsys.readouterr().err

    def test_an_invented_acknowledgement_lists_the_real_ones(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert run("set", str(save), "gold", "500", "--yes", "whatever") == 1
        assert "ban_risk" in capsys.readouterr().err

    def test_technical_detail_only_with_verbose(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run("identify", "/definitely/not/here.sav")
        assert "here.sav" not in capsys.readouterr().err
        run("-v", "identify", "/definitely/not/here.sav")
        assert "here.sav" in capsys.readouterr().err


class TestShowAndSet:
    def test_show_lists_fields_and_values(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert run("show", str(save)) == 0
        out = capsys.readouterr().out
        assert "Gold" in out
        assert "100" in out

    def test_show_marks_achievement_fields(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run("show", str(save))
        assert "achievement" in capsys.readouterr().out

    def test_show_uses_the_chosen_language(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run("--language", "ru", "show", str(save))
        assert "Золото" in capsys.readouterr().out

    def test_a_dry_run_writes_nothing(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        before = save.read_bytes()
        assert run("set", str(save), "gold", "500", "--dry-run") == 0
        assert save.read_bytes() == before
        assert "nothing was written" in capsys.readouterr().out

    def test_setting_a_value_writes_and_backs_up(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert run("set", str(save), "gold", "500") == 0
        assert json.loads(gzip.decompress(save.read_bytes()))["gold"] == 500
        assert "Backup:" in capsys.readouterr().out

    def test_an_achievement_field_needs_confirming(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert run("set", str(save), "kills", "999") == 1
        assert "achievement" in capsys.readouterr().err

        assert run("set", str(save), "kills", "999", "--yes", "achievements") == 0

    def test_a_value_out_of_range_is_refused_before_anything_is_written(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        before = save.read_bytes()
        assert run("set", str(save), "gold", "-5") == 1
        assert save.read_bytes() == before
        assert "smallest allowed" in capsys.readouterr().err


class TestBackups:
    def test_listing_and_restoring(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        original = save.read_bytes()
        run("set", str(save), "gold", "500")
        capsys.readouterr()

        assert run("backups", "cli-test-game") == 0
        assert "cli-test-game" not in capsys.readouterr().err

        assert run("backups", "cli-test-game", "--restore", "0") == 0
        assert save.read_bytes() == original

    def test_nothing_to_list(self, home: FakeSystem, capsys: pytest.CaptureFixture[str]) -> None:
        assert run("backups", "never-used") == 1
        assert "No backups" in capsys.readouterr().out

    def test_restoring_a_number_that_is_not_there(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run("set", str(save), "gold", "500")
        capsys.readouterr()
        assert run("backups", "cli-test-game", "--restore", "9") == 1


class TestPluginManagement:
    def test_export_then_install_elsewhere(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        target = tmp_path / "exported.zip"
        assert run("plugins", "--export", "cli-test-game", "--output", str(target)) == 0
        assert target.is_file()

        assert run("plugins", "--remove", "cli-test-game") == 0
        assert run("plugins", "--install", str(target)) == 0
        assert "installed cli-test-game" in capsys.readouterr().out

    def test_removing_something_absent(
        self, home: FakeSystem, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("plugins", "--remove", "not-installed") == 0
        assert "nothing to remove" in capsys.readouterr().out

    def test_verify_runs_the_gate(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        tmp_path: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "one.dat").write_bytes(save.read_bytes())
        assert run("verify", "cli-test-game", str(corpus)) == 0
        assert "allowed: probable" in capsys.readouterr().out


class TestDiscovery:
    def test_discover_on_a_known_format(
        self, home: FakeSystem, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("discover", str(save)) == 0
        out = capsys.readouterr().out
        assert "backup" in out
        assert "round_trip" in out

    def test_discover_writes_a_draft_manifest(
        self, home: FakeSystem, save: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "draft.json"
        assert run("discover", str(save), "--draft", "new-game", "--output", str(target)) == 0
        draft = json.loads(target.read_text(encoding="utf-8"))
        assert draft["id"] == "new-game"
        assert draft["confidence"] == "experimental"

    def test_no_model_is_asked_unless_it_was_requested(
        self, home: FakeSystem, save: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The expensive step is opt-in. Nothing here should reach the network."""
        from savesmith.agent import writer as writer_module

        def refuse() -> object:
            raise AssertionError("discover built a model client without --model")

        monkeypatch.setattr(writer_module, "make_client", refuse)
        assert run("discover", str(save)) == 0

    def test_the_model_flag_reports_a_missing_key_rather_than_failing(
        self,
        home: FakeSystem,
        opaque: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from savesmith.agent import writer as writer_module

        monkeypatch.setattr(writer_module, "make_client", lambda: None)
        # An unreadable file with no model available: a report, not a crash.
        assert run("discover", str(opaque), "--model", "--budget", "0.25") == 1
        out = capsys.readouterr().out
        assert "at most $0.25" in out
        assert "ANTHROPIC_API_KEY" in out


class TestInterruption:
    def test_stopping_says_nothing_was_changed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def interrupt(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_cmd_doctor", interrupt)
        assert run("doctor") == 130
        assert "Nothing was changed" in capsys.readouterr().err


def test_a_bug_is_not_disguised_as_a_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only deliberate errors get the friendly sentence. A bug must surface."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise ZeroDivisionError("a real bug")

    monkeypatch.setattr(cli, "_cmd_doctor", explode)
    with pytest.raises(ZeroDivisionError):
        cli.main(["doctor"])


def test_savesmith_errors_carry_a_sentence() -> None:
    error = SaveSmithError("Something went wrong.")
    assert error.user_message.endswith(".")


class TestDiff:
    def test_two_decodable_saves_are_compared_by_field(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = tmp_path / "before.dat"
        after = tmp_path / "after.dat"
        before.write_bytes(gzip.compress(json.dumps({"gold": 100}).encode(), mtime=0))
        after.write_bytes(gzip.compress(json.dumps({"gold": 70}).encode(), mtime=0))

        assert run("diff", str(before), str(after)) == 0
        assert "gold: 100 → 70" in capsys.readouterr().out

    def test_identical_saves_report_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "a.dat"
        path.write_bytes(gzip.compress(json.dumps({"gold": 1}).encode(), mtime=0))
        assert run("diff", str(path), str(path)) == 1

    def test_opaque_saves_need_the_numbers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import struct

        body = bytearray(b"\x11\x22\x33\x44" * 512)
        struct.pack_into("<i", body, 0x100, 12400)
        before = tmp_path / "a.sav"
        before.write_bytes(bytes(body))
        struct.pack_into("<i", body, 0x100, 7300)
        after = tmp_path / "b.sav"
        after.write_bytes(bytes(body))

        assert run("diff", str(before), str(after)) == 1
        assert "--was and --now" in capsys.readouterr().out

        assert run("diff", str(before), str(after), "--was", "12400", "--now", "7300") == 0
        assert "0x100" in capsys.readouterr().out

    def test_saves_of_different_sizes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = tmp_path / "a.sav"
        second = tmp_path / "b.sav"
        first.write_bytes(bytes(range(256)) * 4)
        second.write_bytes(bytes(range(256)) * 8)
        assert run("diff", str(first), str(second)) == 1
        assert "different sizes" in capsys.readouterr().out


class TestShippedResources:
    """The packaged binary must find its own plugins and risk database."""

    def test_bundled_data_is_found_from_a_source_checkout(self) -> None:
        from savesmith import resources

        assert (resources.bundled_path("plugins") / "risk_db.json").is_file()
        assert not resources.is_frozen()

    def test_a_packaged_binary_looks_where_pyinstaller_unpacked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sys._MEIPASS is the only place a frozen build's data exists."""
        import sys

        from savesmith import resources

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert resources.is_frozen()
        assert resources.root() == tmp_path
        assert resources.bundled_path("plugins") == tmp_path / "plugins"
        assert "packaged binary" in resources.describe()

    def test_every_operation_is_listed_for_the_packaged_build(self) -> None:
        """PyInstaller cannot see an import that exists for its side effect,
        so each operation module is named in savesmith.spec by hand. This test
        fails when a new one is added and that list is not updated."""
        import re

        from savesmith.core import ops

        spec = Path(__file__).parent.parent / "savesmith.spec"
        listed = set(re.findall(r'"savesmith\.core\.ops\.(\w+)"', spec.read_text()))
        modules = {
            operation.decode.__module__.rsplit(".", 1)[-1] for operation in ops.all_operations()
        }
        assert modules <= listed, f"add to savesmith.spec: {sorted(modules - listed)}"


class TestPlayerPrefs:
    def test_reading_settings_that_are_not_in_a_file(
        self, home: FakeSystem, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Plenty of Unity games keep real progress here rather than in a save."""
        seed_player_prefs(home, "Studio", "Game", coins=250)

        assert run("prefs", "--company", "Studio", "--product", "Game") == 0
        assert "coins" in capsys.readouterr().out

    def test_changing_one_backs_it_up_first(
        self, home: FakeSystem, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seed_player_prefs(home, "Studio", "Game", coins=250)

        code = run("prefs", "--company", "Studio", "--product", "Game", "--set", "coins", "9999")
        assert code == 0
        assert "Backup:" in capsys.readouterr().out
        assert open_prefs(home, "Studio", "Game").read()["coins"].value == 9999

    def test_the_stored_type_is_kept(self, home: FakeSystem) -> None:
        """Turning a number into text would give the game something it cannot read."""
        seed_player_prefs(home, "Studio", "Game", coins=250)

        run("prefs", "--company", "Studio", "--product", "Game", "--set", "coins", "7")
        assert open_prefs(home, "Studio", "Game").read()["coins"].value == 7

    def test_it_asks_for_the_names_it_needs(
        self, home: FakeSystem, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("prefs") == 1
        assert "publisher and a product" in capsys.readouterr().err

    def test_a_game_with_nothing_stored(
        self, home: FakeSystem, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("prefs", "--company", "Nobody", "--product", "Nothing") == 1
        assert "Nothing stored here" in capsys.readouterr().out


class TestPointingAtAGameFolder:
    """The intended way in: a person knows where the game is installed, not
    where it decided to hide its saves."""

    def _rpgmaker(self, tmp_path: Path, *names: str) -> Path:
        game = tmp_path / "Folder Game"
        saves = game / "www" / "save"
        saves.mkdir(parents=True)
        for index, name in enumerate(names or ("file1.rpgsave",)):
            body = json.dumps({"gold": 100 + index, "kills": 3}).encode()
            (saves / name).write_bytes(gzip.compress(body, mtime=0))
        return game

    def test_show_accepts_the_game_folder(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        game = self._rpgmaker(tmp_path)
        assert run("show", str(game)) == 0
        out = capsys.readouterr().out
        assert "file1.rpgsave" in out, "it should say which save it opened"
        assert "Gold" in out and "100" in out

    def test_set_accepts_the_game_folder(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        game = self._rpgmaker(tmp_path)
        assert run("set", str(game), "gold", "500") == 0
        assert "Backup" in capsys.readouterr().out
        edited = game / "www" / "save" / "file1.rpgsave"
        assert json.loads(gzip.decompress(edited.read_bytes()))["gold"] == 500

    def test_several_saves_are_listed_rather_than_guessed_between(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Picking the wrong slot overwrites progress somebody wanted to keep."""
        game = self._rpgmaker(tmp_path, "file1.rpgsave", "file2.rpgsave")
        assert run("show", str(game)) == 1
        error = capsys.readouterr().err
        assert "file1.rpgsave" in error and "file2.rpgsave" in error
        assert "--slot" in error

    def test_a_slot_chooses_between_them(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        game = self._rpgmaker(tmp_path, "file1.rpgsave", "file2.rpgsave")
        assert run("show", str(game), "--slot", "2") == 0
        out = capsys.readouterr().out
        assert "file2.rpgsave" in out
        assert "101" in out

    def test_a_slot_that_does_not_exist_says_how_many_there_are(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        game = self._rpgmaker(tmp_path, "file1.rpgsave", "file2.rpgsave")
        assert run("show", str(game), "--slot", "9") == 1
        assert "has 2" in capsys.readouterr().err

    def test_a_folder_with_nothing_readable_says_where_to_look_next(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        game = tmp_path / "Empty Game"
        (game / "www" / "save").mkdir(parents=True)
        assert run("show", str(game)) == 1
        error = capsys.readouterr().err
        assert "No save files were found" in error
        assert "savesmith find" in error

    def test_a_unity_game_is_sent_to_the_prefs_command(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Its progress is real, it is just not in a file."""
        game = tmp_path / "Coin Quest"
        (game / "CoinQuest_Data").mkdir(parents=True)
        (game / "CoinQuest_Data" / "app.info").write_text(
            "Tiny Studio\nCoin Quest\n", encoding="utf-8"
        )
        seed_player_prefs(home, "Tiny Studio", "Coin Quest", coins=250)

        assert run("show", str(game)) == 1
        assert "savesmith prefs" in capsys.readouterr().err


class TestSearchAndPoke:
    """The workflow for a game nobody has written a plugin for."""

    def test_search_reports_where_the_number_lives(
        self, home: FakeSystem, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("search", str(save), "100") == 0
        out = capsys.readouterr().out
        assert "gold = 100" in out
        assert "savesmith poke" in out, "it should say what to do next"

    def test_search_finds_nothing_and_suggests_the_diff(
        self, home: FakeSystem, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("search", str(save), "777") == 1
        assert "savesmith diff" in capsys.readouterr().out

    def test_poke_refuses_until_the_warning_is_acknowledged(
        self, home: FakeSystem, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = save.read_bytes()
        assert run("poke", str(save), "gold", "500") == 1
        assert save.read_bytes() == before
        assert "--yes" in capsys.readouterr().out

    def test_poke_writes_and_backs_up(
        self, home: FakeSystem, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("poke", str(save), "gold", "500", "--yes") == 0
        assert json.loads(gzip.decompress(save.read_bytes()))["gold"] == 500
        assert "Backup:" in capsys.readouterr().out

    def test_a_dry_run_proves_it_rebuilds_and_writes_nothing(
        self, home: FakeSystem, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = save.read_bytes()
        assert run("poke", str(save), "gold", "500", "--dry-run") == 0
        assert save.read_bytes() == before
        assert "does rebuild" in capsys.readouterr().out

    def test_poke_needs_no_plugin_at_all(
        self, home: FakeSystem, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The whole point: no plugin is installed in this test."""
        import struct

        body = bytearray(b"\x00" * 128)
        struct.pack_into("<i", body, 0x20, 12400)
        path = tmp_path / "slot.bin"
        path.write_bytes(bytes(body))

        assert run("search", str(path), "12400") == 0
        assert "0x20:int32-le" in capsys.readouterr().out
        assert run("poke", str(path), "0x20:int32-le", "99999", "--yes") == 0
        assert struct.unpack_from("<i", path.read_bytes(), 0x20)[0] == 99999

    def test_a_bad_address_is_explained(
        self, home: FakeSystem, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "slot.bin"
        path.write_bytes(b"\x00" * 128)
        assert run("poke", str(path), "0x20", "1", "--yes") == 1
        assert "0x1F4C:uint32" in capsys.readouterr().err

    def test_search_accepts_a_game_folder(
        self, home: FakeSystem, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        game = tmp_path / "Folder Game"
        saves = game / "www" / "save"
        saves.mkdir(parents=True)
        (saves / "file1.rpgsave").write_bytes(
            gzip.compress(json.dumps({"gold": 12400}).encode(), mtime=0)
        )
        assert run("search", str(game), "12400") == 0
        assert "gold = 12400" in capsys.readouterr().out

    def test_what_is_known_about_the_game_is_shown_before_writing(
        self, home: FakeSystem, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Editing without a plugin skips the risk classifier — unless the
        player pointed at the game's folder, in which case we know plenty."""
        game = tmp_path / "ELDEN RING"
        saves = game / "www" / "save"
        saves.mkdir(parents=True)
        (game / "steam_appid.txt").write_text("1245620", encoding="utf-8")
        (saves / "file1.rpgsave").write_bytes(
            gzip.compress(json.dumps({"runes": 12400}).encode(), mtime=0)
        )

        assert run("poke", str(game), "runes", "99999", "--yes") == 0
        out = capsys.readouterr().out
        assert "blocked" in out
        assert "detect modified saves" in out
        assert "Easy Anti-Cheat" in out, "and that this build has none"

    def test_a_lone_file_says_nothing_it_does_not_know(
        self, home: FakeSystem, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run("poke", str(save), "gold", "500", "--yes")
        assert "Risk for" not in capsys.readouterr().out

    def test_search_narrows_to_one_storage_type(
        self, home: FakeSystem, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import struct

        body = bytearray(b"\x00" * 128)
        struct.pack_into("<i", body, 0x20, 12400)
        path = tmp_path / "slot.bin"
        path.write_bytes(bytes(body))

        assert run("search", str(path), "12400", "--type", "uint32-le") == 0
        out = capsys.readouterr().out
        assert "0x20:uint32-le" in out
        assert "int16" not in out


class TestDiffPeelsTheWrappers:
    """Comparing two compressed or encrypted saves byte for byte says only
    that everything changed. Both files get opened the way poke opens them."""

    def _wrapped(self, path: Path, value: int) -> Path:
        import struct

        body = bytearray(b"\x00" * 256)
        struct.pack_into("<i", body, 0x30, value)
        path.write_bytes(gzip.compress(bytes(body), mtime=0))
        return path

    def test_the_payload_is_compared_not_the_container(
        self, home: FakeSystem, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = self._wrapped(tmp_path / "a.sav", 12400)
        after = self._wrapped(tmp_path / "b.sav", 8000)

        assert run("diff", str(before), str(after), "--was", "12400", "--now", "8000") == 0
        out = capsys.readouterr().out
        assert "gzip" in out, "it should say which layers it peeled off"
        assert "0x30:int32-le: 12400 → 8000" in out
        assert "of 256" in out, "the size of the payload, not of the compressed file"

    def test_the_candidate_is_printed_ready_to_paste(
        self, home: FakeSystem, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = self._wrapped(tmp_path / "a.sav", 12400)
        after = self._wrapped(tmp_path / "b.sav", 8000)
        run("diff", str(before), str(after), "--was", "12400", "--now", "8000")
        out = capsys.readouterr().out
        assert "savesmith poke" in out
        assert "(also uint32-le)" in out, "the other reading is mentioned, not listed twice"


class TestServingTheWindow:
    """The window's only way in. If this breaks, the interface has nothing to
    talk to — and it breaks silently, because nobody runs it by hand."""

    def test_it_answers_on_stdin_and_stdout(
        self, home: FakeSystem, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import io
        import sys

        monkeypatch.setattr(sys, "stdin", io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'))

        assert run("rpc") == 0

        answer = json.loads(capsys.readouterr().out.strip())
        assert answer["result"]["ok"] is True

    def test_it_uses_the_machine_it_was_given(
        self, home: FakeSystem, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Otherwise the window would read the real machine while every test
        and every sandbox thinks it is reading a fake one."""
        import io
        import sys

        monkeypatch.setattr(
            sys, "stdin", io.StringIO('{"jsonrpc":"2.0","id":1,"method":"doctor"}\n')
        )

        assert run("rpc") == 0

        answer = json.loads(capsys.readouterr().out.strip())
        assert str(home.home_dir) in answer["result"]["text"]


class TestItems:
    """Inventories, from the side a person types at."""

    def a_game_with_its_data(self, tmp_path: Path) -> Path:
        """An RPG Maker install, which is where the names come from."""
        data = tmp_path / "game" / "www" / "data"
        data.mkdir(parents=True)
        (data / "Items.json").write_text(
            json.dumps([None, {"id": 1, "name": "Potion", "iconIndex": 176}]), encoding="utf-8"
        )
        return tmp_path / "game"

    def test_the_inventory_is_listed_by_id_when_nothing_names_it(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert run("items", str(save)) == 0
        out = capsys.readouterr().out
        assert "Items: 1 things" in out
        assert "the game's own numbers" in out

    def test_the_games_own_files_give_the_names(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        game = self.a_game_with_its_data(tmp_path)

        assert run("items", str(save), "--game-folder", str(game)) == 0

        out = capsys.readouterr().out
        assert "Potion" in out
        assert "×2" in out

    def test_giving_something_by_name_writes_it(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        game = self.a_game_with_its_data(tmp_path)

        assert run("items", str(save), "--game-folder", str(game), "--give", "Potion", "5") == 0

        assert "Written" in capsys.readouterr().out
        assert json.loads(gzip.decompress(save.read_bytes()))["items"] == {"1": 7}

    def test_a_dry_run_writes_nothing(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        before = save.read_bytes()

        assert run("items", str(save), "--set", "1", "9", "--dry-run") == 0

        assert "bag/1: 2 → 9" in capsys.readouterr().out
        assert save.read_bytes() == before

    def test_a_name_that_could_mean_two_things_changes_nothing(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        data = tmp_path / "game" / "www" / "data"
        data.mkdir(parents=True)
        (data / "Items.json").write_text(
            json.dumps(
                [None, {"id": 1, "name": "Potion of Life"}, {"id": 2, "name": "Potion of Death"}]
            ),
            encoding="utf-8",
        )

        assert run("items", str(save), "--game-folder", str(tmp_path / "game"),
                   "--give", "Potion") == 1

        assert "could mean any of these" in capsys.readouterr().err

    def test_one_thing_that_fits_two_containers_asks_which(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The normal case for a game nobody has written names for.

        A bare id matches every container equally, and "which of these two
        identical lines did you mean" is not a question anybody can answer.
        The question is which container, so that is what gets asked.
        """
        manifest = json.loads((plugin_installed / "manifest.json").read_text(encoding="utf-8"))
        manifest["containers"].append(
            {
                "id": "chest",
                "label": {"en": "Chest"},
                "path": "chest",
                "shape": "map",
            }
        )
        (plugin_installed / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        contents = {"gold": 100, "kills": 3, "items": {"1": 2}, "chest": {"4": 1}}
        save.write_bytes(gzip.compress(json.dumps(contents).encode(), mtime=0))

        assert run("items", str(save), "--give", "9") == 1

        message = capsys.readouterr().err
        assert "could go in more than one place" in message
        assert "--container bag" in message

    def test_a_thing_nobody_has_heard_of_is_refused(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert run("items", str(save), "--give", "Excalibur") == 1
        assert "Nothing here is called" in capsys.readouterr().err

    def test_removing_takes_it_out(
        self,
        home: FakeSystem,
        plugin_installed: Path,
        save: Path,
    ) -> None:
        assert run("items", str(save), "--remove", "1") == 0
        assert json.loads(gzip.decompress(save.read_bytes()))["items"] == {}
