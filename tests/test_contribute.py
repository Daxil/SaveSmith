"""Sending a plugin back to the project.

Two things are worth testing here and they are both refusals. A plugin that
cannot rebuild a save must not be offered to anybody — it is a way to corrupt
somebody else's file, not a contribution. And a manifest carrying its author's
user name or Steam id must not reach a public issue tracker, because that is
not something a person can take back afterwards.

The third thing is not a test but a property of the design: nothing here reads
a save file for any purpose other than the round-trip check, and nothing here
puts save bytes anywhere near what gets sent.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from savesmith.core import contribute
from savesmith.core.contribute import ContributionError, look_for_personal_data
from savesmith.core.paths import FakeSystem
from savesmith.core.plugin import Plugin

MANIFEST: dict[str, Any] = {
    "id": "coin-quest",
    "version": 1,
    "game": "Coin Quest",
    "engine": "test",
    "confidence": "probable",
    "risk": {"tier": "safe", "reason": {"en": "single player"}},
    "pipeline": [{"op": "gzip"}, {"op": "json_parse"}],
    "fields": [{"path": "gold", "label": {"en": "Gold"}, "type": "int"}],
}


@pytest.fixture
def plugin(tmp_path: Path) -> Plugin:
    folder = tmp_path / "plugin"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    return Plugin.load(folder)


@pytest.fixture
def save(tmp_path: Path) -> Path:
    path = tmp_path / "save.dat"
    path.write_bytes(gzip.compress(json.dumps({"gold": 100}).encode(), mtime=0))
    return path


class TestProvingItWorks:
    def test_a_plugin_that_rebuilds_the_save_is_ready_to_send(
        self, plugin: Plugin, save: Path
    ) -> None:
        submission = contribute.prepare(plugin, [save])

        assert submission.proved
        assert submission.clean

    def test_a_plugin_that_cannot_rebuild_is_not_offered_to_anybody(
        self, plugin: Plugin, tmp_path: Path
    ) -> None:
        """Not a contribution: a way to corrupt somebody else's save."""
        other = tmp_path / "not-really.dat"
        other.write_bytes(b"this is not gzip at all")

        assert not contribute.prepare(plugin, [other]).proved

    def test_with_nothing_to_check_against_it_refuses_to_guess(self, plugin: Plugin) -> None:
        with pytest.raises(ContributionError, match="proved on real data"):
            contribute.prepare(plugin, [])

    def test_a_plugin_with_no_manifest_on_disk_cannot_be_sent(self, save: Path) -> None:
        assert Plugin.from_mapping(MANIFEST).source is None

        with pytest.raises(ContributionError, match="nothing to send"):
            contribute.prepare(Plugin.from_mapping(MANIFEST), [save])


class TestNotSendingSomebodysPrivateBusiness:
    def test_a_steam_id_in_a_path_is_caught(self) -> None:
        manifest = json.dumps({"paths": ["EldenRing/76561197960271872/ER0000.sl2"]})

        found = look_for_personal_data(manifest)

        assert [leak.what for leak in found] == ["Steam ID"]
        assert "76561197960271872" in found[0].text

    @pytest.mark.parametrize(
        "path",
        [
            "C:\\Users\\danil\\AppData\\Roaming\\Game",
            "/Users/danil/Library/Application Support/Game",
            "/home/danil/.local/share/Game",
        ],
    )
    def test_an_absolute_home_path_is_caught_on_every_platform(self, path: str) -> None:
        """A hand-written manifest picks these up; a token-based one does not."""
        found = look_for_personal_data(json.dumps({"paths": [path]}))

        assert [leak.what for leak in found] == ["путь к домашней папке"]

    def test_the_user_s_own_name_is_caught_wherever_it_sits(
        self, fake_machine: FakeSystem
    ) -> None:
        manifest = json.dumps({"game": f"Game of {fake_machine.username()}"})

        found = look_for_personal_data(manifest, fake_machine)

        assert [leak.what for leak in found] == ["имя пользователя"]

    def test_a_manifest_written_with_tokens_is_clean(self) -> None:
        """What a generated manifest looks like, and it must not alarm anybody."""
        manifest = json.dumps({"paths": ["{APPDATA}/EldenRing/*/ER0000.sl2"]})

        assert look_for_personal_data(manifest) == []

    def test_the_line_is_named_so_it_can_be_found(self) -> None:
        manifest = '{\n  "a": 1,\n  "b": "/Users/danil/x"\n}'

        assert look_for_personal_data(manifest)[0].where == "строка 3"


class TestWhatActuallyTravels:
    def test_the_issue_carries_the_manifest_and_no_save(
        self, plugin: Plugin, save: Path
    ) -> None:
        submission = contribute.prepare(plugin, [save])

        body = submission.body()

        assert "Coin Quest" in body
        assert '"id": "coin-quest"' in body
        # The save's own bytes, and its name, have no business being in here.
        assert save.name not in body
        assert "сохранения не прикладываются" in body

    def test_the_link_is_a_prefilled_form_on_the_project_s_own_tracker(
        self, plugin: Plugin, save: Path
    ) -> None:
        url = contribute.prepare(plugin, [save]).url()

        parsed = urlparse(url)
        assert parsed.netloc == "github.com"
        assert parsed.path.endswith("/issues/new")
        query = parse_qs(parsed.query)
        assert query["labels"] == ["plugin"]
        assert "Coin Quest" in query["title"][0]

    def test_a_manifest_too_big_for_a_link_asks_for_a_file_instead(
        self, plugin: Plugin, save: Path
    ) -> None:
        """A URL has a length nobody agrees on and every browser gives up at."""
        submission = contribute.prepare(plugin, [save])
        submission.manifest = "x" * (contribute.MAX_URL + 1)

        assert submission.needs_attachment
        assert "приложи файл" in submission.body(with_manifest=False)
        assert len(submission.url()) <= contribute.MAX_URL

    def test_the_length_that_counts_is_the_encoded_one(
        self, plugin: Plugin, save: Path
    ) -> None:
        """Encoding Russian text triples it, and a browser truncates in silence."""
        submission = contribute.prepare(plugin, [save])
        submission.manifest = "я" * (contribute.MAX_URL // 4)

        # Comfortably under the limit as characters, far over it once encoded.
        assert len(submission.manifest) < contribute.MAX_URL
        assert submission.needs_attachment

    def test_the_summary_for_a_window_says_the_same_thing(
        self, plugin: Plugin, save: Path
    ) -> None:
        summary = json.loads(contribute.as_json(contribute.prepare(plugin, [save])))

        assert summary["plugin"] == "coin-quest"
        assert summary["proved"] is True
        assert summary["files"] == 1
        assert summary["leaks"] == []
