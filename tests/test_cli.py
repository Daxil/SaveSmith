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
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine of our own, so the tests never touch the real one."""
    fake_home = tmp_path / "home"
    (fake_home / "Library" / "Application Support").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


@pytest.fixture
def plugin_installed(home: Path) -> Path:
    folder = home / "Library" / "Application Support" / "SaveSmith" / "plugins" / "cli-test-game"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    return folder


@pytest.fixture
def save(tmp_path: Path) -> Path:
    path = tmp_path / "save.dat"
    path.write_bytes(gzip.compress(json.dumps({"gold": 100, "kills": 3}).encode(), mtime=0))
    return path


def run(*argv: str) -> int:
    return cli.main(list(argv))


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
        self, home: Path, capsys: pytest.CaptureFixture[str]
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
        self, home: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("show", str(save), "--plugin", "no-such-plugin") == 1
        assert "no plugin called" in capsys.readouterr().err

    def test_a_save_nothing_can_read(
        self, home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        blob = tmp_path / "noise.bin"
        blob.write_bytes(bytes(range(256)) * 16)
        assert run("show", str(blob)) == 1
        assert "discover" in capsys.readouterr().err

    def test_an_invented_acknowledgement_lists_the_real_ones(
        self, home: Path, plugin_installed: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("set", str(save), "gold", "500", "--yes", "whatever") == 1
        assert "ban_risk" in capsys.readouterr().err

    def test_technical_detail_only_with_verbose(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run("identify", "/definitely/not/here.sav")
        assert "not/here.sav" not in capsys.readouterr().err
        run("-v", "identify", "/definitely/not/here.sav")
        assert "not/here.sav" in capsys.readouterr().err


class TestShowAndSet:
    def test_show_lists_fields_and_values(
        self, home: Path, plugin_installed: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("show", str(save)) == 0
        out = capsys.readouterr().out
        assert "Gold" in out
        assert "100" in out

    def test_show_marks_achievement_fields(
        self, home: Path, plugin_installed: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run("show", str(save))
        assert "achievement" in capsys.readouterr().out

    def test_show_uses_the_chosen_language(
        self, home: Path, plugin_installed: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run("--language", "ru", "show", str(save))
        assert "Золото" in capsys.readouterr().out

    def test_a_dry_run_writes_nothing(
        self, home: Path, plugin_installed: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = save.read_bytes()
        assert run("set", str(save), "gold", "500", "--dry-run") == 0
        assert save.read_bytes() == before
        assert "nothing was written" in capsys.readouterr().out

    def test_setting_a_value_writes_and_backs_up(
        self, home: Path, plugin_installed: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("set", str(save), "gold", "500") == 0
        assert json.loads(gzip.decompress(save.read_bytes()))["gold"] == 500
        assert "Backup:" in capsys.readouterr().out

    def test_an_achievement_field_needs_confirming(
        self, home: Path, plugin_installed: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("set", str(save), "kills", "999") == 1
        assert "achievement" in capsys.readouterr().err

        assert run("set", str(save), "kills", "999", "--yes", "achievements") == 0

    def test_a_value_out_of_range_is_refused_before_anything_is_written(
        self, home: Path, plugin_installed: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        before = save.read_bytes()
        assert run("set", str(save), "gold", "-5") == 1
        assert save.read_bytes() == before
        assert "smallest allowed" in capsys.readouterr().err


class TestBackups:
    def test_listing_and_restoring(
        self, home: Path, plugin_installed: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        original = save.read_bytes()
        run("set", str(save), "gold", "500")
        capsys.readouterr()

        assert run("backups", "cli-test-game") == 0
        assert "cli-test-game" not in capsys.readouterr().err

        assert run("backups", "cli-test-game", "--restore", "0") == 0
        assert save.read_bytes() == original

    def test_nothing_to_list(self, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert run("backups", "never-used") == 1
        assert "No backups" in capsys.readouterr().out

    def test_restoring_a_number_that_is_not_there(
        self, home: Path, plugin_installed: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run("set", str(save), "gold", "500")
        capsys.readouterr()
        assert run("backups", "cli-test-game", "--restore", "9") == 1


class TestPluginManagement:
    def test_export_then_install_elsewhere(
        self, home: Path, plugin_installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "exported.zip"
        assert run("plugins", "--export", "cli-test-game", "--output", str(target)) == 0
        assert target.is_file()

        assert run("plugins", "--remove", "cli-test-game") == 0
        assert run("plugins", "--install", str(target)) == 0
        assert "installed cli-test-game" in capsys.readouterr().out

    def test_removing_something_absent(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("plugins", "--remove", "not-installed") == 0
        assert "nothing to remove" in capsys.readouterr().out

    def test_verify_runs_the_gate(
        self,
        home: Path,
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
        self, home: Path, save: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("discover", str(save)) == 0
        out = capsys.readouterr().out
        assert "backup" in out
        assert "round_trip" in out

    def test_discover_writes_a_draft_manifest(
        self, home: Path, save: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "draft.json"
        assert run("discover", str(save), "--draft", "new-game", "--output", str(target)) == 0
        draft = json.loads(target.read_text(encoding="utf-8"))
        assert draft["id"] == "new-game"
        assert draft["confidence"] == "experimental"


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
