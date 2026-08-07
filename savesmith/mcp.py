"""SaveSmith as a set of tools an assistant can use.

The point is one specific job: a game nobody has written a plugin for. Working
out where such a game keeps its numbers is the hard, patient part of this
project, and until now there were two ways to get it done — do it by hand with
``search`` and ``diff``, or pay for an API key so ``discover --model`` could ask
a model. This is the third: bring whatever assistant you already have.

**The model does not write code here, and that is the whole design.**
``discover --model`` has a model write a Python codec, which then has to be run
in a sandbox that — as that module says plainly — is not a jail. Through these
tools a model instead *composes operations SaveSmith already has*: try gzip
then JSON; no; try base64, then LZString, then JSON; yes, and it rebuilds byte
for byte. Every step is run by our own verified code. There is nothing
untrusted to execute, so there is nothing to sandbox.

**What a model may do, and what it may never do.**

It may look: at the games installed, at where a game keeps its saves, at a
window of bytes, at what the decoder ladder makes of a file. It may experiment:
propose a pipeline and be told exactly how it failed. It may propose a finished
plugin, which is checked against the user's own saves by the same round-trip
gate everything else passes.

It may not change a save. Not one byte, under any argument. ``set``, ``write``,
``poke`` and the acknowledgements are deliberately absent from this file, and
the reason is not squeamishness: those acknowledgements exist because an edited
Elden Ring save can get a person banned from playing with their friends. That
consent has to be given by the person it belongs to, reading the words on their
own screen. A model clicking through it on their behalf would leave the whole
of milestone 3 as decoration.

**The bytes go to whoever the user connected.** This is worth saying loudly
rather than burying: when an assistant running in somebody else's cloud reads a
save through these tools, that save's contents leave the machine — the same
thing ``contribute.py`` refuses to do. The difference is that this is the
user's own assistant, on their own subscription, at their own request. So the
tools hand out bounded windows rather than whole files, and every description
here says where the data is going.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from savesmith.core import compare, detect, direct, library
from savesmith.core.console import use_utf8
from savesmith.core.discover import look_at
from savesmith.core.errors import SaveSmithError
from savesmith.core.paths import RealSystem, SystemFacade
from savesmith.core.pipeline import Pipeline
from savesmith.core.plugin import Plugin
from savesmith.core.repository import verify
from savesmith.core.store import PluginStore

PROTOCOL = "2024-11-05"
NAME = "savesmith"

# A window into a file, not the file. Enough to recognise a header, a magic
# number or a run of text; not enough to be a way of exfiltrating a save one
# call at a time without the user noticing the size of what they are sending.
MAX_WINDOW = 4096

# How much of a decoded structure comes back. A model needs the shape and the
# names, not a megabyte of somebody's inventory.
MAX_PREVIEW = 4000

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: Mapping[str, Any]
    run: Callable[[Mapping[str, Any]], str]

    def described(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


def _text(where: Mapping[str, Any], key: str) -> str:
    value = where.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SaveSmithError(f"'{key}' is missing.")
    return value


def _save(where: Mapping[str, Any], key: str = "save") -> Path:
    path = Path(_text(where, key)).expanduser()
    if not path.is_file():
        raise SaveSmithError(f"There is no file at {path}.")
    return path


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SaveSmithError(f"That file could not be read: {exc.strerror}.") from exc


def _clip(text: str, limit: int = MAX_PREVIEW) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n… {len(text) - limit} more characters"


@dataclass
class Server:
    """The tools, and the line-by-line protocol they are offered over."""

    system: SystemFacade = field(default_factory=RealSystem)

    # -- the tools -------------------------------------------------------

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="list_games",
                description=(
                    "Every game installed on this machine: Steam, Steam inside Windows "
                    "bottles, and applications built with a game engine. Start here when "
                    "the user names a game rather than a path."
                ),
                schema={"type": "object", "properties": {}},
                run=self._list_games,
            ),
            Tool(
                name="find_saves",
                description=(
                    "Where a game keeps its saves, and what SaveSmith already makes of "
                    "each file. Takes what the user can point at: an install folder, an "
                    ".exe, a .app, or the save folder itself. 'recognised' means a plugin "
                    "reads it by name already, in which case there is nothing to work out."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "game": {
                            "type": "string",
                            "description": "Path to the game, its folder, or its saves.",
                        }
                    },
                    "required": ["game"],
                },
                run=self._find_saves,
            ),
            Tool(
                name="identify_save",
                description=(
                    "Run the built-in decoder ladder over one save and report what it "
                    "found: the magic bytes, entropy, how printable it is, and any "
                    "combination of known operations that unwraps it. Do this before "
                    "guessing at pipelines — it often answers the question outright."
                ),
                schema={
                    "type": "object",
                    "properties": {"save": {"type": "string"}},
                    "required": ["save"],
                },
                run=self._identify,
            ),
            Tool(
                name="read_bytes",
                description=(
                    f"A window of at most {MAX_WINDOW} bytes as hex and ASCII. This is "
                    f"the user's save file and its contents will reach whoever is running "
                    f"you — ask for the part you need, not the whole file."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "save": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 0, "default": 0},
                        "length": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_WINDOW,
                            "default": 256,
                        },
                    },
                    "required": ["save"],
                },
                run=self._read_bytes,
            ),
            Tool(
                name="try_pipeline",
                description=(
                    "The main tool. Propose the steps that unwrap this format and they "
                    "are run by SaveSmith's own code — you are not writing any. Reports "
                    "what came out, and whether encoding it again reproduces the file "
                    "byte for byte, which is the only thing that counts as understanding "
                    "the format. On failure it names the step that broke and why, so the "
                    "next attempt can be a better guess rather than another shot in the "
                    "dark. Call list_operations for what the steps can be."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "save": {"type": "string"},
                        "steps": {
                            "type": "array",
                            "description": 'Operations in reading order, e.g. '
                            '[{"op": "gzip"}, {"op": "json_parse"}]',
                            "items": {"type": "object"},
                        },
                    },
                    "required": ["save", "steps"],
                },
                run=self._try_pipeline,
            ),
            Tool(
                name="list_operations",
                description=(
                    "Every operation a pipeline step can use, with the settings each one "
                    "takes. These are the building blocks: a format is described by "
                    "composing them, never by writing new code."
                ),
                schema={"type": "object", "properties": {}},
                run=self._list_operations,
            ),
            Tool(
                name="search_number",
                description=(
                    "Where a number the user can see in the game lives in the save. Works "
                    "on encrypted and compressed files: every layer is peeled off first. "
                    "Returns paths for structured saves and byte offsets for binary ones."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "save": {"type": "string"},
                        "value": {"type": "integer"},
                    },
                    "required": ["save", "value"],
                },
                run=self._search,
            ),
            Tool(
                name="compare_saves",
                description=(
                    "Two saves of the same game, taken before and after one number "
                    "changed in the game. This is how forty candidate addresses become "
                    "one — ask the user for a before and an after rather than guessing."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "before": {"type": "string"},
                        "after": {"type": "string"},
                        "was": {"type": "integer"},
                        "now": {"type": "integer"},
                    },
                    "required": ["before", "after"],
                },
                run=self._compare,
            ),
            Tool(
                name="propose_plugin",
                description=(
                    "Offer a finished plugin. It is validated, then run against the "
                    "user's own saves: every one must rebuild byte for byte or it is "
                    "refused and nothing is installed. A plugin that passes is installed "
                    "on this machine only — the user then edits in SaveSmith itself, and "
                    "can offer it to the project with 'savesmith plugins --submit'. "
                    "Installing a plugin cannot change a save; only the user can."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "manifest": {
                            "type": "object",
                            "description": "The whole manifest, as JSON.",
                        },
                        "saves": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Saves to prove it against. At least one.",
                        },
                    },
                    "required": ["manifest", "saves"],
                },
                run=self._propose_plugin,
            ),
        ]

    # -- what each one does ----------------------------------------------

    def _list_games(self, _arguments: Mapping[str, Any]) -> str:
        found = library.scan(self.system)
        if not found.games:
            return "No games found. Ask the user to point at the game's folder."
        lines = [f"{len(found.games)} game(s):"]
        for game in found.sorted():
            lines.append(f"  {game.name}  [{game.source}]\n      {game.path}")
        lines += [f"  ! {problem}" for problem in found.problems]
        return "\n".join(lines)

    def _find_saves(self, arguments: Mapping[str, Any]) -> str:
        look = look_at(Path(_text(arguments, "game")).expanduser(), self.system)
        lines = [f"{look.game.title} — engine: {look.game.engine.value}"]
        lines += [f"({note})" for note in look.notes]
        saves = look.found.player_saves
        if not saves:
            lines.append("No player saves found here.")
        for save in saves:
            state = (
                "a plugin already reads this by field name — nothing to work out"
                if save.recognised
                else "unwraps and rebuilds exactly, but nobody has mapped what the bytes mean"
                if save.openable
                else "format unknown"
            )
            lines.append(f"  {save.path}\n      {save.format}, {save.size} байт — {state}")
        return "\n".join(lines)

    def _identify(self, arguments: Mapping[str, Any]) -> str:
        report = detect.identify(_read(_save(arguments)))
        return "\n".join(report.explain())

    def _read_bytes(self, arguments: Mapping[str, Any]) -> str:
        raw = _read(_save(arguments))
        offset = max(0, int(arguments.get("offset", 0) or 0))
        length = min(MAX_WINDOW, max(1, int(arguments.get("length", 256) or 256)))
        window = raw[offset : offset + length]
        if not window:
            return f"The file is {len(raw)} bytes; there is nothing at offset {offset}."

        lines = [f"{len(raw)} bytes in all; showing {offset}–{offset + len(window)}:"]
        for row in range(0, len(window), 16):
            chunk = window[row : row + 16]
            hexed = " ".join(f"{byte:02x}" for byte in chunk)
            ascii_ = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
            lines.append(f"  {offset + row:08x}  {hexed:<47}  {ascii_}")
        return "\n".join(lines)

    def _list_operations(self, _arguments: Mapping[str, Any]) -> str:
        from savesmith.core import ops

        lines = ["The building blocks a pipeline step can use:"]
        for operation in sorted(ops.all_operations(), key=lambda item: item.name):
            settings = ", ".join(sorted(operation.known_params())) or "no settings"
            lines.append(f"  {operation.name:20} {operation.summary}")
            lines.append(f"  {'':20} settings: {settings}")
        return "\n".join(lines)

    def _try_pipeline(self, arguments: Mapping[str, Any]) -> str:
        """Run a proposed pipeline and say exactly how it went.

        The round trip is the verdict. A pipeline that decodes something but
        cannot put the file back together has not understood the format, it has
        found a way to corrupt it.
        """
        steps = arguments.get("steps")
        if not isinstance(steps, list) or not steps:
            raise SaveSmithError("'steps' is a list of operations, in reading order.")
        raw = _read(_save(arguments))

        try:
            pipeline = Pipeline.from_manifest(steps, plugin_id="proposed")
        except SaveSmithError as exc:
            return f"The pipeline could not be assembled: {exc.user_message}"

        try:
            decoded = pipeline.decode(raw)
        except SaveSmithError as exc:
            return f"It did not decode: {exc.user_message}\n{exc.detail or ''}".strip()

        result = pipeline.round_trip(raw)
        verdict = (
            "IT FITS: the file rebuilds byte for byte."
            if result.exact_bytes
            else f"It decoded, but does not rebuild: {result.detail}"
        )
        return f"{verdict}\n\nWhat came out:\n{_describe(decoded.value)}"

    def _search(self, arguments: Mapping[str, Any]) -> str:
        value = arguments.get("value")
        if isinstance(value, bool) or not isinstance(value, int):
            raise SaveSmithError("'value' is the whole number the user sees in the game.")
        # Opened the way the command line opens it: every layer the ladder can
        # peel off, peeled off. Searching an encrypted slot for a number would
        # find nothing and prove nothing.
        save = direct.DirectSave.open(_save(arguments))
        sites = save.search(value)
        if not sites:
            return (
                f"{value} is not stored anywhere in this file. Format: {save.description}. "
                f"If the game shows it rounded or scaled, ask for the exact figure; if it "
                f"still is not there, two saves and compare_saves will find it."
            )
        lines = [f"{value} is in {len(sites)} place(s); format: {save.description}"]
        lines += [f"  {site.address}   {site.context}" for site in sites[:40]]
        if len(sites) > 40:
            lines.append(
                f"  … and {len(sites) - 40} more. That many candidates is the moment to "
                f"ask for two saves and call compare_saves, not to guess."
            )
        return "\n".join(lines)

    def _compare(self, arguments: Mapping[str, Any]) -> str:
        before = direct.DirectSave.open(_save(arguments, "before"))
        after = direct.DirectSave.open(_save(arguments, "after"))
        was, now = arguments.get("was"), arguments.get("now")

        if before.is_structured and after.is_structured:
            changes = compare.compare_structures(before.value, after.value)
            numeric = compare.numeric_changes(changes)
            lines = [
                f"Format: {before.description}",
                f"{len(changes)} value(s) changed, {len(numeric)} of them numbers",
            ]
            lines += [f"  {change}" for change in (numeric or changes)[:40]]
            return "\n".join(lines)

        before_raw = before.value if isinstance(before.value, bytes) else before.raw
        after_raw = after.value if isinstance(after.value, bytes) else after.raw
        if len(before_raw) != len(after_raw):
            return (
                f"The two saves are different sizes ({len(before_raw)} and "
                f"{len(after_raw)} bytes), so their bytes cannot be lined up."
            )

        diff = compare.compare_bytes(before_raw, after_raw)
        lines = [f"Format: {before.description}. Comparing bytes.", diff.summary()]
        lines += [
            f"  0x{start:X} … 0x{start + length:X}  ({length} bytes)"
            for start, length in diff.ranges[:20]
        ]
        if not isinstance(was, int) or not isinstance(now, int):
            lines.append(
                "\nSay which number changed — 'was' and 'now' — and the exact place "
                "becomes visible."
            )
            return "\n".join(lines)

        guesses = compare.guesses_in_ranges(
            compare.narrow(before_raw, after_raw, was, now), diff
        )
        if not guesses:
            lines.append(f"\nNo place holds {was} before and {now} after.")
            return "\n".join(lines)
        lines.append(f"\n{len(compare.group_by_bytes(guesses))} candidate field(s):")
        lines += [
            f"  {guess}" + (f"   (also {', '.join(others)})" if others else "")
            for guess, others in compare.group_by_bytes(guesses)
        ]
        return "\n".join(lines)

    def _propose_plugin(self, arguments: Mapping[str, Any]) -> str:
        manifest = arguments.get("manifest")
        if not isinstance(manifest, dict):
            raise SaveSmithError("'manifest' is the whole plugin manifest, as an object.")
        names = arguments.get("saves")
        if not isinstance(names, list) or not names:
            raise SaveSmithError(
                "'saves' must name at least one save file. A plugin nobody proved on "
                "real data is a guess, and guesses corrupt saves."
            )

        # The prompt asks for these, and asking is not enforcing. A model that
        # claims 'verified' is claiming somebody started the game and watched
        # the edited save load, which nothing here can have done; a model that
        # claims 'safe' is telling a person there is no risk. Both are lowered
        # rather than argued about.
        manifest = _no_claims_it_cannot_make(manifest)

        try:
            plugin = Plugin.from_mapping(manifest)
        except SaveSmithError as exc:
            return f"The manifest was not accepted: {exc.user_message}"

        saves = [Path(str(name)).expanduser() for name in names]
        result = verify(plugin, saves)
        lines = list(result.explain())

        if not result.passed:
            lines.append("")
            lines.append(
                "Not installed. Until the file rebuilds byte for byte this is not a "
                "description of the format, it is a way to corrupt it."
            )
            return "\n".join(lines)

        store = PluginStore.for_system(self.system)
        folder = store.root / plugin.id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines += [
            "",
            f"Installed on this machine: {folder}",
            "The user now sees these fields in SaveSmith and can change them there,"
            " with the backup and the acknowledgements. Nothing has been changed: a"
            " plugin describes a format, and only a person writes to a save.",
            "",
            f"If it works in the game, this is worth giving to everybody else:"
            f"\n  savesmith plugins --submit {plugin.id} --saves \"<the game>\"",
        ]
        return "\n".join(lines)

    # -- protocol --------------------------------------------------------

    def serve(self, stream_in: IO[str] | None = None, stream_out: IO[str] | None = None) -> int:
        source = stream_in or sys.stdin
        sink = stream_out or sys.stdout
        use_utf8(source, sink)
        for line in source:
            line = line.strip()
            if not line:
                continue
            answer = self.handle_line(line)
            if answer is not None:
                sink.write(json.dumps(answer, ensure_ascii=False) + "\n")
                sink.flush()
        return 0

    def handle_line(self, line: str) -> dict[str, Any] | None:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            return _failure(None, INVALID_PARAMS, "That was not JSON.")
        if not isinstance(request, dict):
            return _failure(None, INVALID_PARAMS, "A request must be an object.")
        return self.handle(request)

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        identifier = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(params, Mapping):
            return _failure(identifier, INVALID_PARAMS, "Parameters must be an object.")

        match method:
            case "initialize":
                result: Any = {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": NAME, "version": _version()},
                }
            case "tools/list":
                result = {"tools": [tool.described() for tool in self.tools()]}
            case "tools/call":
                return self._call(identifier, params)
            case "ping":
                result = {}
            case _ if isinstance(method, str) and method.startswith("notifications/"):
                # Nothing to answer, and nothing to do: this server holds no
                # state between calls.
                return None
            case _:
                if identifier is None:
                    return None
                return _failure(identifier, METHOD_NOT_FOUND, f"There is no method '{method}'.")

        if identifier is None:
            return None
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def _call(self, identifier: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            return _content(identifier, "Arguments must be an object.", failed=True)

        tool = next((one for one in self.tools() if one.name == name), None)
        if tool is None:
            known = ", ".join(one.name for one in self.tools())
            return _content(
                identifier, f"There is no tool '{name}'. There is: {known}", failed=True
            )

        try:
            return _content(identifier, _clip(tool.run(arguments)))
        except SaveSmithError as exc:
            # A refusal is an answer the model should read and act on, not a
            # transport failure.
            return _content(identifier, exc.user_message, failed=True)
        except Exception:
            print(traceback.format_exc(), file=sys.stderr)
            return _content(
                identifier,
                "Something broke inside SaveSmith. Nothing was changed; the details "
                "are in the log.",
                failed=True,
            )


def _no_claims_it_cannot_make(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Lower two claims in a proposed manifest that only a person can earn.

    ``verified`` means somebody launched the game and watched the edited save
    load. ``safe`` means somebody is confident nothing bad follows from editing
    it. Neither is a thing that can be established by reading a file, so
    neither survives arriving from a tool call, however sincerely it was meant.
    """
    checked = dict(manifest)
    checked["confidence"] = "experimental"

    risk = dict(checked.get("risk") or {})
    if str(risk.get("tier", "")) == "safe" or "tier" not in risk:
        risk["tier"] = "caution"
    checked["risk"] = risk
    return checked


def _describe(value: Any) -> str:
    """The shape of what came out, in as few characters as say it."""
    if isinstance(value, bytes | bytearray):
        return f"{len(value)} bytes of binary — another decoding step is needed"
    if isinstance(value, Mapping):
        keys = ", ".join(str(key) for key in list(value)[:40])
        return f"an object with fields: {keys}\n\n{_clip(_as_json(value))}"
    if isinstance(value, Sequence) and not isinstance(value, str):
        return f"a list of {len(value)} items\n\n{_clip(_as_json(value))}"
    return _clip(str(value))


def _as_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def _content(identifier: Any, text: str, *, failed: bool = False) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "result": {"content": [{"type": "text", "text": text}], "isError": failed},
    }


def _failure(identifier: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _version() -> str:
    from savesmith import __version__

    return __version__


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - entry point
    _ = argv
    return Server().serve()
