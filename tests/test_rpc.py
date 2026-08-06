"""The wire between the interface and the core."""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from typing import Any

import pytest

from savesmith.core.paths import RealSystem
from savesmith.rpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    SAVESMITH_ERROR,
    Server,
)

MANIFEST: dict[str, Any] = {
    "id": "rpc-test-game",
    "version": 1,
    "game": "RPC Test Game",
    "engine": "test",
    "confidence": "probable",
    "risk": {"tier": "safe", "reason": {"en": "single player", "ru": "одиночная"}},
    "pipeline": [{"op": "gzip"}, {"op": "json_parse"}],
    "fields": [
        {
            "path": "gold",
            "label": {"en": "Gold", "ru": "Золото"},
            "type": "int",
            "min": 0,
            "max": 999999,
            "group": {"en": "Resources", "ru": "Ресурсы"},
        },
        {"path": "kills", "label": {"en": "Kills"}, "type": "int", "achievement": True},
    ],
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    (fake_home / "Library" / "Application Support").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    plugins = fake_home / "Library" / "Application Support" / "SaveSmith" / "plugins"
    folder = plugins / str(MANIFEST["id"])
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    return fake_home


@pytest.fixture
def server(home: Path) -> Server:
    return Server(system=RealSystem())


@pytest.fixture
def save(tmp_path: Path) -> Path:
    path = tmp_path / "save.dat"
    path.write_bytes(gzip.compress(json.dumps({"gold": 100, "kills": 3}).encode(), mtime=0))
    return path


def call(server: Server, method: str, **params: Any) -> dict[str, Any]:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert response is not None
    return response


def result_of(server: Server, method: str, **params: Any) -> dict[str, Any]:
    response = call(server, method, **params)
    assert "error" not in response, response.get("error")
    result: dict[str, Any] = response["result"]
    return result


class TestTransport:
    def test_a_line_in_a_line_out(self, server: Server) -> None:
        sink = io.StringIO()
        server.serve(io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\n'), sink)
        answer = json.loads(sink.getvalue().strip())
        assert answer["id"] == 7
        assert answer["result"]["ok"]

    def test_blank_lines_are_ignored(self, server: Server) -> None:
        sink = io.StringIO()
        server.serve(io.StringIO('\n\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n\n'), sink)
        assert len(sink.getvalue().strip().splitlines()) == 1

    def test_broken_json_is_answered_not_crashed(self, server: Server) -> None:
        response = server.handle_line("{not json")
        assert response is not None
        assert response["error"]["code"] == INVALID_REQUEST

    def test_a_request_that_is_not_an_object(self, server: Server) -> None:
        response = server.handle_line("[1, 2, 3]")
        assert response is not None
        assert response["error"]["code"] == INVALID_REQUEST

    def test_an_unknown_method_lists_the_known_ones(self, server: Server) -> None:
        response = call(server, "fly_to_the_moon")
        assert response["error"]["code"] == METHOD_NOT_FOUND
        assert "identify" in response["error"]["message"]

    def test_a_notification_gets_no_answer(self, server: Server) -> None:
        assert server.handle({"jsonrpc": "2.0", "method": "ping"}) is None

    def test_parameters_must_be_an_object(self, server: Server) -> None:
        response = server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1, 2]}
        )
        assert response is not None
        assert response["error"]["code"] == INVALID_PARAMS


class TestErrors:
    def test_a_deliberate_error_carries_its_own_sentence(
        self, server: Server, tmp_path: Path
    ) -> None:
        response = call(server, "identify", path=str(tmp_path / "missing.sav"))
        assert response["error"]["code"] == SAVESMITH_ERROR
        assert "could not be read" in response["error"]["message"]
        assert response["error"]["data"]["code"] == "error"

    def test_a_bug_becomes_a_generic_message_not_a_stack_trace(
        self, server: Server, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def explode(*_args: object, **_kwargs: object) -> None:
            raise ZeroDivisionError("a real bug")

        monkeypatch.setattr(server, "_ping", explode)
        response = call(server, "ping")
        assert response["error"]["code"] == INTERNAL_ERROR
        assert "Traceback" not in response["error"]["message"]
        assert "Nothing was changed" in response["error"]["message"]
        # The trace still exists, in the log where it belongs.
        assert "ZeroDivisionError" in capsys.readouterr().err

    def test_a_missing_parameter_says_which(self, server: Server) -> None:
        response = call(server, "identify")
        assert response["error"]["code"] == INVALID_PARAMS
        assert "'path'" in response["error"]["message"]


class TestReading:
    def test_identify(self, server: Server, save: Path) -> None:
        result = result_of(server, "identify", path=str(save))
        assert result["solved"]
        assert result["candidates"][0]["description"] == "gzip → json_parse"

    def test_checksums(self, server: Server, tmp_path: Path) -> None:
        import zlib

        body = b'{"gold":1}'
        path = tmp_path / "c.sav"
        path.write_bytes(zlib.crc32(body).to_bytes(4, "little") + body)
        result = result_of(server, "checksums", path=str(path))
        assert result["checksums"][0]["algorithm"] == "crc32-le"

    def test_plugins_list(self, server: Server) -> None:
        result = result_of(server, "plugins.list")
        ids = {plugin["id"] for plugin in result["plugins"]}
        assert {"rpc-test-game", "the-invincible"} <= ids


class TestEditingSession:
    def test_opening_returns_everything_needed_to_draw_the_screen(
        self, server: Server, save: Path
    ) -> None:
        state = result_of(server, "open", path=str(save))
        assert state["session"]
        assert state["may_write"] is True
        assert {field["address"] for field in state["fields"]} == {"gold", "kills"}
        gold = next(field for field in state["fields"] if field["address"] == "gold")
        assert (gold["value"], gold["min"], gold["max"]) == (100, 0, 999999)

    def test_labels_come_back_in_the_language_asked_for(
        self, server: Server, save: Path
    ) -> None:
        state = result_of(server, "open", path=str(save), language="ru")
        gold = next(field for field in state["fields"] if field["address"] == "gold")
        assert gold["label"] == "Золото"
        assert gold["group"] == "Ресурсы"

    def test_setting_a_value_reports_the_new_state(self, server: Server, save: Path) -> None:
        opened = result_of(server, "open", path=str(save))
        state = result_of(server, "set", session=opened["session"], field="gold", value=500)
        assert state["change"] == {"field": "gold", "before": 100, "after": 500}
        assert state["pending"][0]["after"] == 500

    def test_nothing_is_written_until_asked(self, server: Server, save: Path) -> None:
        before = save.read_bytes()
        opened = result_of(server, "open", path=str(save))
        result_of(server, "set", session=opened["session"], field="gold", value=500)
        assert save.read_bytes() == before

    def test_writing(self, server: Server, save: Path) -> None:
        opened = result_of(server, "open", path=str(save))
        result_of(server, "set", session=opened["session"], field="gold", value=500)
        written = result_of(server, "write", session=opened["session"])
        assert written["written"]
        assert written["backup"]["folder"]
        assert json.loads(gzip.decompress(save.read_bytes()))["gold"] == 500

    def test_an_achievement_field_is_refused_until_acknowledged(
        self, server: Server, save: Path
    ) -> None:
        opened = result_of(server, "open", path=str(save))
        response = call(server, "set", session=opened["session"], field="kills", value=999)
        assert "achievement" in response["error"]["message"]

        result_of(server, "acknowledge", session=opened["session"], items=["achievements"])
        result_of(server, "set", session=opened["session"], field="kills", value=999)

    def test_an_invented_acknowledgement_is_refused(self, server: Server, save: Path) -> None:
        opened = result_of(server, "open", path=str(save))
        response = call(server, "acknowledge", session=opened["session"], items=["whatever"])
        assert response["error"]["code"] == INVALID_PARAMS
        assert "ban_risk" in json.dumps(response["error"]["data"])

    def test_a_forgotten_session_says_so_plainly(self, server: Server) -> None:
        response = call(server, "set", session="s999", field="gold", value=1)
        assert "no longer open" in response["error"]["message"]

    def test_closing(self, server: Server, save: Path) -> None:
        opened = result_of(server, "open", path=str(save))
        assert result_of(server, "close", session=opened["session"])["closed"]
        assert call(server, "write", session=opened["session"])["error"]

    def test_two_saves_open_at_once(self, server: Server, tmp_path: Path, save: Path) -> None:
        other = tmp_path / "other.dat"
        other.write_bytes(gzip.compress(json.dumps({"gold": 7, "kills": 0}).encode(), mtime=0))

        first = result_of(server, "open", path=str(save))
        second = result_of(server, "open", path=str(other))
        assert first["session"] != second["session"]

        result_of(server, "set", session=first["session"], field="gold", value=1)
        state = result_of(server, "set", session=second["session"], field="gold", value=2)
        assert state["pending"] == [{"field": "gold", "before": 7, "after": 2}]

    def test_a_save_no_plugin_can_read(self, server: Server, tmp_path: Path) -> None:
        blob = tmp_path / "noise.bin"
        blob.write_bytes(bytes(range(256)) * 16)
        response = call(server, "open", path=str(blob))
        assert "discovery" in response["error"]["message"]


class TestProgress:
    def test_discovery_reports_as_it_goes(self, server: Server, save: Path) -> None:
        """The user must see it working, not a frozen window."""
        sink = io.StringIO()
        request = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "discover", "params": {"path": str(save)}}
        )
        server.serve(io.StringIO(request + "\n"), sink)

        messages = [json.loads(line) for line in sink.getvalue().splitlines()]
        progress = [item for item in messages if item.get("method") == "progress"]
        assert len(progress) > 3
        assert any(item["params"]["kind"] == "stage" for item in progress)
        assert messages[-1]["result"]["solved"]

    def test_a_draft_manifest_can_be_asked_for(self, server: Server, save: Path) -> None:
        result = result_of(server, "discover", path=str(save), draft="new-game")
        assert result["draft"]["id"] == "new-game"
        assert result["draft"]["confidence"] == "experimental"


class TestBackupsOverTheWire:
    def test_listing_and_restoring(self, server: Server, save: Path) -> None:
        original = save.read_bytes()
        opened = result_of(server, "open", path=str(save))
        result_of(server, "set", session=opened["session"], field="gold", value=500)
        result_of(server, "write", session=opened["session"])

        listed = result_of(server, "backups.list", plugin="rpc-test-game")
        assert listed["backups"]

        result_of(server, "backups.restore", plugin="rpc-test-game", index=0)
        assert save.read_bytes() == original

    def test_restoring_something_that_is_not_there(self, server: Server) -> None:
        response = call(server, "backups.restore", plugin="rpc-test-game", index=3)
        assert "no backup number 3" in response["error"]["message"]


class TestPluginsOverTheWire:
    def test_export_remove_install(self, server: Server, tmp_path: Path) -> None:
        target = tmp_path / "exported.zip"
        result_of(server, "plugins.export", id="rpc-test-game", target=str(target))
        assert target.is_file()

        assert result_of(server, "plugins.remove", id="rpc-test-game")["removed"]
        installed = result_of(server, "plugins.install", path=str(target))
        assert installed["installed"]["id"] == "rpc-test-game"

    def test_installing_something_hostile_is_refused(
        self, server: Server, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip at all")
        response = call(server, "plugins.install", path=str(bad))
        assert "readable archive" in response["error"]["message"]
