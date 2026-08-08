"""SaveSmith offered as tools to somebody else's assistant.

Most of what is tested here is the boundary. A model may look at a save and it
may propose a description of its format; it may not change a save, and it may
not give the acknowledgements that stand in front of a change. Those exist
because an edited Elden Ring save can get a person banned from playing with
their friends, and that is not a consent anything can give on their behalf.

The other half is the loop the whole thing exists for: a wrong guess comes back
naming the step that broke and why, so the next guess can be better; a right
one comes back saying the file rebuilds byte for byte, which is the only thing
that counts as having understood a format.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from savesmith.core.paths import FakeSystem
from savesmith.core.store import PluginStore
from savesmith.mcp import MAX_WINDOW, Server

SAVE = {"party": {"gold": 4200, "steps": 18043}}

MANIFEST: dict[str, Any] = {
    "id": "coin-quest",
    "version": 1,
    "game": "Coin Quest",
    "engine": "test",
    "confidence": "probable",
    "risk": {"tier": "safe", "reason": {"en": "single player"}},
    "pipeline": [{"op": "gzip"}, {"op": "json_parse"}],
    "fields": [{"path": "party.gold", "label": {"en": "Gold"}, "type": "int"}],
}


@pytest.fixture
def server(fake_machine: FakeSystem) -> Server:
    return Server(system=fake_machine)


@pytest.fixture
def save(tmp_path: Path) -> Path:
    path = tmp_path / "file1.sav"
    path.write_bytes(gzip.compress(json.dumps(SAVE).encode(), mtime=0))
    return path


def call(server: Server, tool: str, **arguments: Any) -> dict[str, Any]:
    answer = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    assert answer is not None
    result: dict[str, Any] = answer["result"]
    return result


def text_of(result: dict[str, Any]) -> str:
    return str(result["content"][0]["text"])


class TestTheBoundary:
    """What a model is not given, and cannot ask for."""

    def test_nothing_here_can_change_a_save(self, server: Server) -> None:
        offered = {tool.name for tool in server.tools()}

        assert offered.isdisjoint({"set", "write", "poke", "acknowledge", "confirm_cloud"})

    def test_every_tool_is_a_way_of_looking_or_proposing(self, server: Server) -> None:
        """Named one by one, so adding a writing tool has to be deliberate."""
        assert {tool.name for tool in server.tools()} == {
            "list_games",
            "find_saves",
            "identify_save",
            "read_bytes",
            "try_pipeline",
            "list_operations",
            "search_number",
            "compare_saves",
            "propose_plugin",
        }

    def test_a_tool_nobody_has_is_a_readable_refusal_not_a_crash(
        self, server: Server
    ) -> None:
        result = call(server, "write_save", save="/anything")

        assert result["isError"] is True
        assert "no tool" in text_of(result)

    def test_a_window_of_bytes_is_bounded_however_much_is_asked_for(
        self, server: Server, save: Path
    ) -> None:
        """These bytes reach whoever is running the assistant."""
        result = call(server, "read_bytes", save=str(save), offset=0, length=10_000_000)

        # Sixteen bytes to a line, plus the heading.
        assert len(text_of(result).splitlines()) <= MAX_WINDOW // 16 + 2

    def test_a_missing_file_is_said_in_words(self, server: Server) -> None:
        result = call(server, "identify_save", save="/definitely/not/here.sav")

        assert result["isError"] is True
        assert "no file at" in text_of(result)


class TestWorkingOutAFormat:
    def test_a_wrong_guess_says_which_step_broke_and_why(
        self, server: Server, save: Path
    ) -> None:
        """Otherwise the next attempt is another shot in the dark."""
        result = call(
            server, "try_pipeline", save=str(save), steps=[{"op": "lzstring"}, {"op": "json_parse"}]
        )

        answer = text_of(result)
        assert "step 1" in answer
        assert "lzstring" in answer

    def test_a_right_guess_says_it_rebuilds_byte_for_byte(
        self, server: Server, save: Path
    ) -> None:
        result = call(
            server, "try_pipeline", save=str(save), steps=[{"op": "gzip"}, {"op": "json_parse"}]
        )

        answer = text_of(result)
        assert "IT FITS" in answer
        assert "gold" in answer

    def test_the_building_blocks_are_listed_with_their_settings(
        self, server: Server
    ) -> None:
        answer = text_of(call(server, "list_operations"))

        assert "lzstring" in answer
        assert "aes_decrypt" in answer

    def test_an_empty_pipeline_is_refused(self, server: Server, save: Path) -> None:
        result = call(server, "try_pipeline", save=str(save), steps=[])

        assert result["isError"] is True

    def test_a_number_on_the_screen_is_found_in_the_file(
        self, server: Server, save: Path
    ) -> None:
        answer = text_of(call(server, "search_number", save=str(save), value=4200))

        assert "party.gold" in answer

    def test_a_number_that_is_not_there_suggests_what_to_do_next(
        self, server: Server, save: Path
    ) -> None:
        answer = text_of(call(server, "search_number", save=str(save), value=999_999))

        assert "compare_saves" in answer


class TestProposingAPlugin:
    def test_a_plugin_that_proves_itself_is_installed_for_the_user(
        self, server: Server, save: Path, fake_machine: FakeSystem
    ) -> None:
        result = call(server, "propose_plugin", manifest=MANIFEST, saves=[str(save)])

        assert result["isError"] is False
        assert PluginStore.for_system(fake_machine).catalogue().by_id("coin-quest") is not None

    def test_installing_a_plugin_changes_no_save(
        self, server: Server, save: Path
    ) -> None:
        """The line the whole design rests on."""
        before = save.read_bytes()

        call(server, "propose_plugin", manifest=MANIFEST, saves=[str(save)])

        assert save.read_bytes() == before

    def test_a_plugin_that_cannot_rebuild_the_file_is_not_installed(
        self, server: Server, save: Path, fake_machine: FakeSystem
    ) -> None:
        wrong = {**MANIFEST, "pipeline": [{"op": "zlib"}, {"op": "json_parse"}]}

        answer = text_of(call(server, "propose_plugin", manifest=wrong, saves=[str(save)]))

        assert "Not installed" in answer
        assert PluginStore.for_system(fake_machine).catalogue().by_id("coin-quest") is None

    def test_a_plugin_proved_against_nothing_is_refused(self, server: Server) -> None:
        result = call(server, "propose_plugin", manifest=MANIFEST, saves=[])

        assert result["isError"] is True
        assert "guesses corrupt saves" in text_of(result)

    def test_a_working_plugin_is_pointed_at_the_way_to_share_it(
        self, server: Server, save: Path
    ) -> None:
        answer = text_of(call(server, "propose_plugin", manifest=MANIFEST, saves=[str(save)]))

        assert "plugins --submit" in answer


class TestTheProtocol:
    def test_it_introduces_itself(self, server: Server) -> None:
        answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

        assert answer is not None
        assert answer["result"]["serverInfo"]["name"] == "savesmith"
        assert answer["result"]["capabilities"] == {"tools": {}}

    def test_every_tool_carries_a_schema_a_client_can_read(self, server: Server) -> None:
        answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        assert answer is not None
        for tool in answer["result"]["tools"]:
            assert tool["description"].strip()
            assert tool["inputSchema"]["type"] == "object"

    def test_a_notification_gets_no_answer(self, server: Server) -> None:
        assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_a_line_that_is_not_json_does_not_end_the_conversation(
        self, server: Server
    ) -> None:
        answer = server.handle_line("this is not json")

        assert answer is not None
        assert "error" in answer


class TestTheResultIsVisibleAfterwards:
    """The half of the feature that is easy to forget to check.

    Installing a plugin is not the point; the point is that the person's game
    then opens with names on its numbers. A plugin an assistant writes usually
    has no `detect` block — nobody told it where saves live — so if plugins
    were ever matched by that block instead of by opening the file, the whole
    flow would keep reporting success and produce nothing anybody can see.
    """

    def test_a_plugin_with_no_detect_block_still_opens_the_save(
        self, server: Server, save: Path, fake_machine: FakeSystem
    ) -> None:
        from savesmith.rpc import Server as RpcServer

        assert "detect" not in MANIFEST
        call(server, "propose_plugin", manifest=MANIFEST, saves=[str(save)])

        answer = RpcServer(system=fake_machine).handle(
            {"jsonrpc": "2.0", "id": 1, "method": "open", "params": {"path": str(save)}}
        )

        assert answer is not None, "the window asks for exactly this"
        assert "error" not in answer, answer.get("error")
        assert answer["result"]["plugin"]["id"] == "coin-quest"
        assert [field["label"] for field in answer["result"]["fields"]] == ["Gold"]

    def test_what_the_user_installed_wins_over_what_shipped_with_us(
        self, server: Server, save: Path, fake_machine: FakeSystem
    ) -> None:
        """Their plugin describes their game; ours describes a whole engine."""
        from savesmith.core.repository import PluginRepository, bundled
        from savesmith.core.store import PluginStore

        call(server, "propose_plugin", manifest=MANIFEST, saves=[str(save)])
        raw = save.read_bytes()

        theirs = PluginRepository(PluginStore.for_system(fake_machine).root).match(raw)

        assert [plugin.id for plugin in theirs] == ["coin-quest"]
        assert bundled().match(raw) != theirs
