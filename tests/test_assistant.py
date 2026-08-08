"""SaveSmith driving an assistant the person already has.

The assistant itself cannot be run here — it needs somebody's subscription and
several minutes — so what is tested is everything around it: that it is never
started without consent, that it is handed our tools and nothing else, that a
window opened from Finder can still find it, and that what comes back is read
correctly. The last one matters more than it sounds: a stream misread by one
layer of wrapping reported a plugin id with a paragraph of prose attached.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from savesmith.agent import assistant
from savesmith.agent.assistant import Assistant, AssistantError, Progress
from savesmith.agent.prompt import SYSTEM, TOOLS, task


@pytest.fixture
def claude(tmp_path: Path) -> Assistant:
    pretend = tmp_path / "claude"
    pretend.write_text("#!/bin/sh\nexit 0\n")
    pretend.chmod(0o755)
    return Assistant(id="claude", name="Claude Code", path=pretend)


@pytest.fixture
def save(tmp_path: Path) -> Path:
    path = tmp_path / "file1.sav"
    path.write_bytes(b"whatever")
    return path


class TestNotStartingWithoutBeingAsked:
    def test_it_refuses_without_consent(self, claude: Assistant, save: Path) -> None:
        """This sends part of somebody's save to somebody else's service."""
        with pytest.raises(AssistantError, match="nobody has agreed"):
            assistant.analyse(claude, save)

    def test_consent_is_not_something_a_default_can_supply(self) -> None:
        import inspect

        consented = inspect.signature(assistant.analyse).parameters["consented"]

        assert consented.default is False
        assert consented.kind is inspect.Parameter.KEYWORD_ONLY

    def test_a_missing_save_is_said_in_words(self, claude: Assistant, tmp_path: Path) -> None:
        with pytest.raises(AssistantError, match="no save file"):
            assistant.analyse(claude, tmp_path / "nope.sav", consented=True)


class TestWhatTheAssistantIsAllowed:
    def command(self, claude: Assistant, save: Path) -> list[str]:
        return assistant._command(claude, save, None, {"золото": 4200})

    def test_it_gets_our_tools_and_only_ours(self, claude: Assistant, save: Path) -> None:
        command = self.command(claude, save)

        allowed = command[command.index("--allowedTools") + 1 : command.index("--disallowedTools")]

        assert allowed == [f"mcp__savesmith__{name}" for name in TOOLS]

    def test_writing_and_running_things_is_denied_by_name(
        self, claude: Assistant, save: Path
    ) -> None:
        """An allow-list is the fence; this is the second fence."""
        command = self.command(claude, save)

        denied = command[command.index("--disallowedTools") + 1 : command.index("--output-format")]

        assert {"Bash", "Edit", "Write"} <= set(denied)

    def test_the_person_s_own_mcp_servers_do_not_join_in(
        self, claude: Assistant, save: Path
    ) -> None:
        assert "--strict-mcp-config" in self.command(claude, save)

    def test_it_is_pointed_at_our_own_server(self, claude: Assistant, save: Path) -> None:
        command = self.command(claude, save)

        config = json.loads(command[command.index("--mcp-config") + 1])

        assert list(config["mcpServers"]) == ["savesmith"]
        assert config["mcpServers"]["savesmith"]["args"][-1] == "mcp"

    def test_our_system_prompt_replaces_the_default_one(
        self, claude: Assistant, save: Path
    ) -> None:
        command = self.command(claude, save)

        assert command[command.index("--system-prompt") + 1] == SYSTEM

    def test_an_assistant_nobody_taught_it_to_run_is_refused(self, save: Path) -> None:
        stranger = Assistant(id="hal", name="HAL", path=Path("/bin/true"))

        with pytest.raises(AssistantError, match="does not know how to run"):
            assistant._command(stranger, save, None, None)


class TestFindingWhatIsInstalled:
    def test_a_window_opened_from_finder_still_finds_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An app launched from Finder has almost no PATH.

        Looking only there would tell somebody who uses Claude Code daily that
        they have no assistant installed, which is the worst way to be wrong.
        """
        local = tmp_path / ".local" / "bin"
        local.mkdir(parents=True)
        (local / "claude").write_text("#!/bin/sh\n")
        (local / "claude").chmod(0o755)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(assistant, "_ALSO_LOOK_IN", (str(local),))

        assert assistant._find("claude") == local / "claude"

    def test_nothing_installed_is_an_empty_list_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(assistant, "_ALSO_LOOK_IN", ())

        assert assistant.installed() == []

    def test_asking_for_one_that_is_not_here_names_what_is(self) -> None:
        with pytest.raises(AssistantError, match="ни одного"):
            assistant.named("codex", among=[])


class TestReadingWhatComesBack:
    def result(self, text: Any) -> dict[str, Any]:
        return {
            "message": {"content": [{"type": "tool_result", "content": text}]},
        }

    def test_the_plugin_id_is_read_out_of_the_tool_s_own_answer(self) -> None:
        """The assistant's summary is a claim; this is the evidence."""
        outcome = assistant.Outcome()
        said = [
            {
                "type": "text",
                "text": (
                    "coin-quest v1 — Coin Quest\n  file1.sav: exact\n\n"
                    "Installed on this machine: /home/x/plugins/coin-quest\n"
                    "The user now sees these fields in SaveSmith."
                ),
            }
        ]

        assistant._absorb(self.result(said), outcome)

        assert outcome.plugin_id == "coin-quest"

    def test_a_run_that_installed_nothing_is_not_a_success(self) -> None:
        outcome = assistant.Outcome()

        assistant._absorb(self.result([{"type": "text", "text": "Not installed."}]), outcome)

        assert not outcome.succeeded

    def test_tool_calls_become_words_a_person_can_read(self) -> None:
        event = {
            "message": {
                "content": [{"type": "tool_use", "name": "mcp__savesmith__try_pipeline"}]
            }
        }

        assert assistant._readable(event) == [Progress("Пробую разобрать…", kind="trying")]

    def test_an_essay_is_not_shown_but_a_remark_is(self) -> None:
        """The stream carries the model's thinking; a progress bar is not for it."""
        short = {"message": {"content": [{"type": "text", "text": "The ladder solved it."}]}}
        long = {"message": {"content": [{"type": "text", "text": "x" * 500}]}}

        assert len(assistant._readable(short)) == 1
        assert assistant._readable(long) == []

    def test_a_line_that_is_not_an_event_is_skipped_quietly(self) -> None:
        """Assistants print their own noise; none of it should stop a run."""
        events = list(assistant._events(iter(["", "not json", "{}", '{"a": 1}'])))

        assert events == [{}, {"a": 1}]


class TestThePrompt:
    def test_the_numbers_the_person_gave_are_stated_with_their_purpose(self) -> None:
        """They are what makes a field named rather than guessed."""
        text = task("/games/save.dat", None, {"золото": 4200})

        assert "search_number" in text
        assert "золото: 4200" in text

    def test_without_numbers_it_says_to_name_only_the_obvious(self) -> None:
        text = task("/games/save.dat", None, None)

        assert "gave no numbers" in text

    def test_the_system_prompt_forbids_the_two_claims_it_cannot_earn(self) -> None:
        assert "always `experimental`" in SYSTEM
        assert "never `safe`" in SYSTEM

    def test_it_is_told_there_is_nobody_to_ask(self) -> None:
        """A question asked headless is a program that hangs."""
        assert "Nobody is at the keyboard" in SYSTEM

    def test_every_tool_it_is_allowed_is_a_tool_that_exists(self) -> None:
        from savesmith.mcp import Server

        assert set(TOOLS) == {tool.name for tool in Server().tools()}


class TestSayingWhyItStopped:
    """A dead button with "it stopped" under it helps nobody.

    Every one of these is something the person can act on, and the first is by
    far the likeliest failure on a machine that has everything installed.
    """

    def one(self) -> Assistant:
        return Assistant(id="claude", name="Claude Code", path=Path("/x/claude"))

    def test_not_logged_in_says_how_to_log_in(self) -> None:
        why = assistant._why_it_stopped(self.one(), "Not logged in · Please run /login")

        assert "не выполнен вход" in why
        assert "claude" in why

    def test_a_spent_subscription_says_so_and_offers_the_other_way(self) -> None:
        why = assistant._why_it_stopped(self.one(), "Your credit balance is too low")

        assert "лимит" in why
        assert "вручную" in why

    def test_no_network_is_not_reported_as_a_broken_format(self) -> None:
        why = assistant._why_it_stopped(self.one(), "getaddrinfo ENOTFOUND api.anthropic.com")

        assert "сеть" in why

    def test_anything_else_still_says_nothing_was_changed(self) -> None:
        why = assistant._why_it_stopped(self.one(), "segmentation fault")

        assert "Ничего не изменено" in why


class TestWhatCountsAsTheAnswer:
    """The last thing an assistant prints is not always its answer."""

    def test_a_finished_run_reports_what_it_found(self) -> None:
        outcome = assistant.Outcome()

        assistant._absorb(
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "Формат оказался сохранением RPG Maker MV."},
            outcome,
        )

        assert "RPG Maker" in outcome.summary

    def test_a_service_message_does_not_become_the_result(self) -> None:
        """Seen in a real run: the plugin installed, and the summary read
        "You've hit your session limit" — which describes nothing about the
        format and reads like the work failed when it had not."""
        outcome = assistant.Outcome()
        outcome.plugin_id = "coin-quest"

        assistant._absorb(
            {"type": "result", "subtype": "error_during_execution", "is_error": True,
             "result": "You've hit your session limit · resets 5am"},
            outcome,
        )

        assert outcome.summary == ""
        assert assistant.summarise(outcome) == "Формат разобран, плагин установлен."
