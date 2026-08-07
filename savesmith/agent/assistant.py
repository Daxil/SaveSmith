"""Driving an assistant the person already has, from inside SaveSmith.

The difference between this and :mod:`savesmith.mcp` is who is in charge. There,
SaveSmith is a box of tools and somebody else's agent decides what to do with
them — which means a person typing at that agent. Here SaveSmith runs the
assistant itself: it writes the prompt, launches the program, watches what comes
back and installs the result. The person presses a button and reads a progress
bar. They never open a terminal and never write a prompt, because writing the
prompt is our job and we know the method better than they do.

**Why drive a program the user installed instead of calling an API.** Calling
Anthropic directly needs an API key, which means an account and a card, for the
one feature most likely to be somebody's first reason to open SaveSmith. The
assistants people already have — Claude Code, Codex — are billed by their own
subscriptions. Detecting one and using it costs the user nothing extra and costs
this project nothing at all.

**The assistant gets our tools and no others.** It is launched with
``--strict-mcp-config`` so the person's own MCP servers do not join in, with an
allow-list naming exactly the SaveSmith tools, and with everything else denied —
no file writing, no shell. So a general-purpose coding agent is, for the length
of this run, a thing that can look at one save file and propose a description of
it. And SaveSmith's own tools cannot write to a save, which is the boundary that
actually matters.

**Nothing starts without being asked for.** Running this sends parts of the
save to whoever the assistant talks to. That is the user's own subscription and
their own machine, but it is still their data leaving, so the window asks first
and this module refuses to be called without ``consented``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from savesmith.agent.prompt import SYSTEM, TOOLS, task
from savesmith.core.errors import SaveSmithError

# How long one analysis may take before it is stopped. Working a format out is
# a dozen tool calls and some thinking; a run still going after this is stuck,
# and a progress bar that never ends is worse than a refusal.
TIMEOUT_SECONDS = 900


class AssistantError(SaveSmithError):
    """The assistant could not be run, or would not finish."""

    code = "assistant"


@dataclass(frozen=True)
class Assistant:
    """An assistant found on this machine, and how to run it headless."""

    id: str
    name: str
    path: Path

    def described(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "path": str(self.path)}


# Every assistant this knows how to drive. Adding one is a row here plus a
# branch in `_command`, and nothing else.
_KNOWN = (
    ("claude", "Claude Code"),
    ("codex", "Codex CLI"),
)

# A window opened from Finder or the Start menu inherits almost no PATH — on
# macOS it is /usr/bin:/bin:/usr/sbin:/sbin and nothing else. Looking only at
# PATH would mean "no assistant found" on the machine of somebody who has one
# installed and uses it daily, which is the worst possible way to be wrong.
_ALSO_LOOK_IN = (
    "~/.local/bin",
    "~/.claude/local",
    "~/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.bun/bin",
    "~/.volta/bin",
    "~/AppData/Local/Programs",
    "~/AppData/Roaming/npm",
)


def _find(name: str) -> Path | None:
    found = shutil.which(name)
    if found:
        return Path(found)
    for folder in _ALSO_LOOK_IN:
        for candidate in (name, f"{name}.exe", f"{name}.cmd"):
            path = Path(folder).expanduser() / candidate
            if path.is_file() and os.access(path, os.X_OK):
                return path
    return None


def installed() -> list[Assistant]:
    """Which assistants this machine has, in the order they are preferred."""
    found = []
    for identifier, name in _KNOWN:
        path = _find(identifier)
        if path is not None:
            found.append(Assistant(id=identifier, name=name, path=path))
    return found


@dataclass
class Progress:
    """One line of what is happening, written for a person to read."""

    text: str
    kind: str = "step"
    """``step``, ``trying``, ``found``, ``failed`` or ``done``."""


@dataclass
class Outcome:
    plugin_id: str | None = None
    summary: str = ""
    events: list[Progress] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.plugin_id is not None


def backend_command() -> list[str]:
    """How to start SaveSmith's own MCP server, from wherever this is running.

    Frozen by PyInstaller the executable *is* SaveSmith; from a source checkout
    it is Python with the module. Getting this wrong means the assistant is
    handed a server it cannot start, and every tool call fails for a reason
    nobody would guess from the outside.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "mcp"]
    return [sys.executable, "-m", "savesmith", "mcp"]


def analyse(
    assistant: Assistant,
    save: Path,
    *,
    game: Path | None = None,
    numbers: Mapping[str, int] | None = None,
    consented: bool = False,
    on_progress: Callable[[Progress], None] | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> Outcome:
    """Have the assistant work out one save's format. Sends data outward."""
    if not consented:
        raise AssistantError(
            "SaveSmith did not start the assistant, because nobody has agreed to "
            "it yet. Working the format out means showing parts of this save file "
            "to the assistant, which is somebody else's service."
        )
    if not save.is_file():
        raise AssistantError(f"There is no save file at {save}.")

    command = _command(assistant, save, game, numbers)
    report = on_progress or (lambda _event: None)
    report(Progress(f"Спрашиваю {assistant.name}…", kind="step"))

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            # A working directory of its own: the assistant has no file tools,
            # but nothing is gained by starting it inside the user's project.
            cwd=str(save.parent),
        )
    except OSError as exc:
        raise AssistantError(
            f"{assistant.name} could not be started: {exc.strerror}.",
            detail=" ".join(command),
        ) from exc

    outcome = Outcome()
    try:
        assert process.stdout is not None
        for event in _events(process.stdout):
            for progress in _readable(event):
                outcome.events.append(progress)
                report(progress)
            _absorb(event, outcome)
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        raise AssistantError(
            f"{assistant.name} was still going after {int(timeout / 60)} minutes, "
            f"so SaveSmith stopped it. Nothing was changed."
        ) from None
    finally:
        if process.poll() is None:
            process.kill()

    if process.returncode not in (0, None) and not outcome.succeeded:
        stderr = (process.stderr.read() if process.stderr else "") or ""
        said = "\n".join([stderr, *(event.text for event in outcome.events)])
        raise AssistantError(_why_it_stopped(assistant, said), detail=stderr.strip()[:2000])
    return outcome


# What an assistant says when it will not start, and what a person can do about
# it. Without this the window shows "stopped without working the format out",
# which is true, useless, and leaves somebody staring at a dead button —
# especially for the first of these, which is the single likeliest way this
# fails on a machine that has everything installed.
_REASONS = (
    (
        ("not logged in", "please run /login", "invalid api key", "authentication"),
        "{name} установлен, но в него не выполнен вход. Открой терминал, запусти "
        "{command} и войди — после этого SaveSmith сможет им пользоваться.",
    ),
    (
        ("credit balance", "quota", "rate limit", "usage limit", "exceeded"),
        "У подписки на {name} закончился лимит. SaveSmith тут ничем не поможет — "
        "либо подожди, пока лимит обновится, либо разбери сохранение вручную: "
        "скажи программе число, которое видишь в игре.",
    ),
    (
        ("enotfound", "getaddrinfo", "network", "econnrefused", "connect"),
        "{name} не смог выйти в сеть. Проверь соединение и попробуй ещё раз — "
        "ничего не изменено.",
    ),
)


def _why_it_stopped(assistant: Assistant, said: str) -> str:
    lowered = said.lower()
    for markers, message in _REASONS:
        if any(marker in lowered for marker in markers):
            return message.format(name=assistant.name, command=assistant.path.name)
    return (
        f"{assistant.name} остановился, не разобрав формат. Ничего не изменено. "
        f"Можно попробовать ещё раз или разобрать сохранение вручную."
    )


def _command(
    assistant: Assistant,
    save: Path,
    game: Path | None,
    numbers: Mapping[str, int] | None,
) -> list[str]:
    servers = json.dumps(
        {"mcpServers": {"savesmith": {"command": backend_command()[0],
                                      "args": backend_command()[1:]}}}
    )
    allowed = [f"mcp__savesmith__{name}" for name in TOOLS]
    prompt = task(str(save), str(game) if game else None, dict(numbers or {}))

    if assistant.id == "claude":
        return [
            str(assistant.path),
            "--print",
            # Deliberately *not* --bare, tempting as it looks. It would keep the
            # person's hooks and project settings out of our run, but it also
            # restricts authentication to an API key, and the entire point of
            # driving an assistant somebody already has is that their
            # subscription pays for it. A live run said "Not logged in" and
            # stopped, which is what that trade looks like from the outside.
            "--strict-mcp-config",
            "--mcp-config",
            servers,
            "--system-prompt",
            SYSTEM,
            "--allowedTools",
            *allowed,
            # Said twice on purpose: an allow-list that a future version starts
            # treating as advisory would otherwise hand a coding agent a shell
            # on somebody's machine.
            "--disallowedTools",
            "Bash",
            "Edit",
            "Write",
            "WebFetch",
            "--output-format",
            "stream-json",
            "--verbose",
            prompt,
        ]

    if assistant.id == "codex":
        return [
            str(assistant.path),
            "exec",
            "--json",
            prompt,
        ]

    raise AssistantError(f"SaveSmith does not know how to run {assistant.name}.")


def _events(stream: Iterator[str]) -> Iterator[Mapping[str, object]]:
    """Whatever the assistant printed, as objects; anything else ignored."""
    for line in stream:
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


# What each tool means in words a person watching a progress bar can read.
_MEANING = {
    "find_saves": "Ищу сохранения",
    "identify_save": "Смотрю, что это за файл",
    "read_bytes": "Читаю начало файла",
    "list_operations": "Перебираю, из чего он может быть собран",
    "try_pipeline": "Пробую разобрать",
    "search_number": "Ищу, где лежит твоё число",
    "compare_saves": "Сравниваю два сохранения",
    "propose_plugin": "Проверяю, что файл собирается обратно",
    "list_games": "Смотрю установленные игры",
}


def _readable(event: Mapping[str, object]) -> list[Progress]:
    """Turn one machine event into nothing, or into a line worth showing.

    Most of what streams past is of no interest to somebody waiting: token
    counts, message ids, the model's own deliberation. What they want to know
    is that it is still working and roughly on what.
    """
    message = event.get("message")
    if not isinstance(message, Mapping):
        return []

    lines: list[Progress] = []
    content = message.get("content")
    if not isinstance(content, list):
        return []

    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "tool_use":
            name = str(part.get("name", "")).replace("mcp__savesmith__", "")
            meaning = _MEANING.get(name)
            if meaning:
                lines.append(Progress(f"{meaning}…", kind="trying"))
        elif part.get("type") == "text":
            text = str(part.get("text", "")).strip()
            # The model's running commentary is worth showing — it is the only
            # thing in the stream written for a person — but not its essays.
            if 0 < len(text) <= 400:
                lines.append(Progress(text, kind="step"))
    return lines


def _absorb(event: Mapping[str, object], outcome: Outcome) -> None:
    """Notice the two things that decide whether this worked."""
    if event.get("type") == "result":
        result = event.get("result")
        if isinstance(result, str):
            outcome.summary = result.strip()

    message = event.get("message")
    if not isinstance(message, Mapping):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, Mapping) or part.get("type") != "tool_result":
            continue
        # The installation line the propose_plugin tool prints on success. This
        # is the only evidence that anything was actually achieved; the
        # assistant's own summary is a claim, not a fact.
        marker = "Installed on this machine:"
        text = _said(part.get("content"))
        if marker in text:
            outcome.plugin_id = Path(text.split(marker, 1)[1].strip().splitlines()[0]).name


def _said(content: object) -> str:
    """The text of a tool result, whichever shape the client wrapped it in.

    Serialising the wrapper instead of unwrapping it turns every newline into a
    literal backslash-n, and then a line-by-line read of the result quietly
    returns the entire answer as one line — which is how a plugin id came back
    with a paragraph of prose attached to it.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return ""


def summarise(outcome: Outcome) -> str:
    """One line for the window, whatever happened."""
    if outcome.succeeded:
        return outcome.summary or "Формат разобран, плагин установлен."
    return outcome.summary or "Формат разобрать не удалось. Ничего не изменено."


def named(identifier: str, among: Sequence[Assistant] | None = None) -> Assistant:
    """The assistant with this id, or a refusal naming what there is."""
    found = list(among if among is not None else installed())
    for assistant in found:
        if assistant.id == identifier:
            return assistant
    known = ", ".join(one.id for one in found) or "ни одного"
    raise AssistantError(
        f"На этом компьютере нет помощника '{identifier}'. Есть: {known}."
    )
