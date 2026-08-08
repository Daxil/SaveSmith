"""The wire between the interface and the core.

One JSON object per line, in and out. Chosen over anything cleverer because it
survives being read by a person: when the interface misbehaves, the exchange
can be replayed by piping a text file into this program.

    {"jsonrpc":"2.0","id":1,"method":"open","params":{"path":"…/user1.dat"}}
    {"jsonrpc":"2.0","id":1,"result":{"session":"s1","fields":[…]}}

Three things this layer is responsible for and nothing else does:

**Sessions.** The interface opens a save, changes several fields over several
messages, and writes once. That state lives here, keyed by an id, so the front
end holds a string rather than a copy of the save.

**Progress.** Discovery takes a while and the user needs to see it working, so
long operations emit notifications as they go rather than a single answer at
the end.

**Errors that a person can read.** A deliberate error becomes a JSON-RPC error
with its own sentence. An unexpected one becomes a generic message and a note
in the log — a stack trace is a bug report, not something to put on screen.

Every rule about what may be edited and when still lives in the core. This
adds none and relaxes none.
"""

from __future__ import annotations

import base64
import json
import sys
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from savesmith.agent.discovery import discover as run_discovery
from savesmith.core import catalog, detect, diagnostics, direct, library, playerprefs
from savesmith.core import checksum as checksum_module
from savesmith.core.backup import BackupStore
from savesmith.core.console import use_utf8
from savesmith.core.discover import examine, look_at
from savesmith.core.errors import SaveSmithError
from savesmith.core.inventory import Stack
from savesmith.core.paths import RealSystem, SystemFacade
from savesmith.core.plugin import ContainerSpec, Plugin
from savesmith.core.repository import PluginRepository, bundled
from savesmith.core.risk import Acknowledgement, RiskDatabase, assess
from savesmith.core.session import EditSession
from savesmith.core.steam import SteamInstall
from savesmith.core.store import PluginStore
from savesmith.core.wine import scan_prefixes

PROTOCOL = "2.0"

# JSON-RPC reserves -32768..-32000. Ours sit outside that, and the machine
# readable name from the error itself travels alongside.
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SAVESMITH_ERROR = 1000


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class Server:
    """The core, exposed one line at a time."""

    system: SystemFacade = field(default_factory=RealSystem)
    sessions: dict[str, EditSession] = field(default_factory=dict)
    games: dict[str, Path] = field(default_factory=dict)
    """Where each session's game is installed, when that is known."""
    _catalogs: dict[tuple[str, str], catalog.Catalog] = field(default_factory=dict)
    _counter: int = 0

    # -- transport -------------------------------------------------------

    def serve(self, stream_in: IO[str] | None = None, stream_out: IO[str] | None = None) -> int:
        """Read requests until the input ends."""
        source = stream_in or sys.stdin
        sink = stream_out or sys.stdout
        # JSON-RPC is UTF-8 by definition, whatever the console codepage says.
        use_utf8(source, sink)
        for line in source:
            line = line.strip()
            if not line:
                continue
            response = self.handle_line(line, sink)
            if response is not None:
                _emit(sink, response)
        return 0

    def handle_line(self, line: str, sink: IO[str] | None = None) -> dict[str, Any] | None:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            return _error(None, INVALID_REQUEST, f"That was not valid JSON ({exc.msg}).")
        if not isinstance(request, dict):
            return _error(None, INVALID_REQUEST, "A request must be a JSON object.")
        return self.handle(request, sink)

    def handle(
        self, request: Mapping[str, Any], sink: IO[str] | None = None
    ) -> dict[str, Any] | None:
        identifier = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(method, str):
            return _error(identifier, INVALID_REQUEST, "A request must name a method.")
        if not isinstance(params, dict):
            return _error(identifier, INVALID_PARAMS, "Parameters must be a JSON object.")

        handler = self._methods().get(method)
        if handler is None:
            known = ", ".join(sorted(self._methods()))
            return _error(
                identifier, METHOD_NOT_FOUND, f"There is no method '{method}'. Known: {known}"
            )

        notify = _notifier(sink, method)
        try:
            result = handler(params, notify)
        except RpcError as exc:
            return _error(identifier, exc.code, exc.message, exc.data)
        except SaveSmithError as exc:
            return _error(
                identifier,
                SAVESMITH_ERROR,
                exc.user_message,
                {"code": exc.code, "detail": exc.detail},
            )
        except Exception:
            # A bug, not a user error. The trace goes to the log, never on screen.
            print(traceback.format_exc(), file=sys.stderr)
            return _error(
                identifier,
                INTERNAL_ERROR,
                "Something went wrong inside SaveSmith. Nothing was changed. "
                "The details are in the log.",
            )
        if identifier is None:
            return None  # a notification: no answer expected
        return {"jsonrpc": PROTOCOL, "id": identifier, "result": result}

    # -- methods ---------------------------------------------------------

    def _methods(self) -> dict[str, Callable[[dict[str, Any], Notify], Any]]:
        return {
            "ping": self._ping,
            "doctor": self._doctor,
            "scan": self._scan,
            "games": self._games,
            "find_saves": self._find_saves,
            "identify": self._identify,
            "checksums": self._checksums,
            "discover": self._discover,
            "assistants": self._assistants,
            "analyse": self._analyse,
            "fields": self._fields,
            "search": self._search,
            "poke": self._poke,
            "prefs.read": self._prefs_read,
            "prefs.set": self._prefs_set,
            "open": self._open,
            "set": self._set,
            "items.list": self._items_list,
            "items.catalog": self._items_catalog,
            "items.give": self._items_give,
            "items.set": self._items_set,
            "items.remove": self._items_remove,
            "acknowledge": self._acknowledge,
            "confirm_cloud": self._confirm_cloud,
            "write": self._write,
            "close": self._close,
            "backups.list": self._backups_list,
            "backups.restore": self._backups_restore,
            "plugins.list": self._plugins_list,
            "plugins.install": self._plugins_install,
            "plugins.remove": self._plugins_remove,
            "plugins.export": self._plugins_export,
        }

    def _ping(self, _params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        from savesmith import __version__

        return {"ok": True, "version": __version__}

    def _doctor(self, _params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        return {"text": diagnostics.render(diagnostics.collect(self.system))}

    def _scan(self, _params: dict[str, Any], notify: Notify) -> dict[str, Any]:
        games: list[dict[str, Any]] = []
        try:
            scan = SteamInstall.discover(self.system).scan()
        except SaveSmithError as exc:
            notify("steam", {"message": exc.user_message})
        else:
            for game in scan.games:
                games.append(
                    {
                        "appid": game.appid,
                        "name": game.name,
                        "install_dir": str(game.install_dir),
                        "installed": game.is_installed,
                    }
                )
        bottles = [
            {"name": prefix.name, "kind": prefix.kind.value, "path": str(prefix.path),
             "users": list(prefix.users)}
            for prefix in scan_prefixes(self.system)
        ]
        return {"games": games, "bottles": bottles}

    def _games(self, _params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        """Every game on this machine, for the interface to offer as a list.

        Cheap enough to call on start-up: it reads Steam's manifests and looks
        at the shape of a few folders, and never opens a save.
        """
        found = library.scan(self.system)
        database = RiskDatabase.bundled()
        games = []
        for game in found.sorted():
            tier = None
            if game.steam_appid is not None:
                tier = assess(
                    database=database,
                    appid=game.steam_appid,
                    anticheat=(),
                    anticheat_scanned=False,
                    plugin=None,
                    steam_cloud=False,
                ).tier.value
            games.append(
                {
                    "name": game.name,
                    "path": str(game.path),
                    "source": game.source,
                    "bottle": game.bottle,
                    "steam_appid": game.steam_appid,
                    "installed": game.installed,
                    "risk_tier": tier,
                }
            )
        return {"games": games, "problems": found.problems}

    def _find_saves(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        # Whatever the user pointed at: the folder, the .exe, the .app, or a
        # Wineskin wrapper with a whole bottle inside.
        look = look_at(Path(_require(params, "folder")), self.system)
        game, found = look.game, look.found
        described = self._plugins_for(found)
        return {
            # The bottle's name, not a sentence: the window says it in its
            # own language rather than reprinting an English note.
            "bottle": look.bottle,
            "folder": str(look.target.folder),
            "game": {
                "title": game.title,
                "engine": game.engine.value,
                "project": game.project,
                "steam_appid": game.steam_appid,
                "anticheat": list(game.anticheat),
            },
            "searched": [str(path) for path in found.searched],
            "prefs": (
                {
                    "location": found.prefs.location,
                    "entries": [
                        {"name": entry.name, "value": entry.value, "kind": entry.kind}
                        for entry in found.prefs.entries
                    ],
                }
                if found.prefs is not None
                else None
            ),
            "aside": {kind.value: count for kind, count in found.aside.items()},
            "saves": [
                {
                    "path": str(save.path),
                    "format": save.format,
                    "recognised": save.recognised,
                    # save / backup / settings / other. The window shows the
                    # saves and counts the rest; a person looking for their
                    # save does not want sixty-two of the game's own backups.
                    "kind": save.kind.value,
                    # The format is understood and rebuilds exactly, but its
                    # fields are unmapped. Editable by address, not by name.
                    "openable": save.openable,
                    # The plugin that reads this file by name, if one does.
                    # This and not `recognised` is what decides whether the
                    # window can show named fields: the generic ladder says
                    # nothing about Elden Ring, and a plugin says everything.
                    "plugin": described.get(str(save.path)),
                    # When a game is installed in two places — two bottles, a
                    # reinstall — the only honest way to tell which save is the
                    # one being played is when it was last written.
                    "modified": save.modified,
                    "size": save.size,
                }
                for save in found.saves
            ],
        }

    def _plugins_for(self, found: Any) -> dict[str, str]:
        """Which of the player's saves a plugin can read by name.

        Only the player's own saves are tried: matching means decoding, and
        decoding sixty-two of a game's rolling backups to learn what we already
        know about the first one is a great deal of work for no new answer.
        """
        store = PluginStore.for_system(self.system)
        repository = PluginRepository(store.root)
        matched: dict[str, str] = {}
        for save in found.player_saves:
            try:
                raw = save.path.read_bytes()
            except OSError:
                continue
            plugins = repository.match(raw) or bundled().match(raw)
            if plugins:
                matched[str(save.path)] = plugins[0].id
        return matched

    def _fields(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        """Every value in a save, named the way the game named it.

        No plugin involved: a GVAS file carries ``ObjectiveTime`` inside it and
        RPG Maker carries ``party._gold``. Listing those is the difference
        between showing somebody their save and asking them to guess a number.

        A save that decoded to bytes has no names in it, so the list is empty
        and the interface falls back to searching by value. Inventing labels
        for bytes would be worse than saying there are none.
        """
        save = direct.DirectSave.open(Path(_require(params, "path")))
        return {
            "format": save.description,
            "structured": save.is_structured,
            "fields": [
                {
                    "address": leaf.address,
                    "name": leaf.name,
                    "group": leaf.group,
                    "value": leaf.value,
                    "kind": leaf.kind,
                    "editable": leaf.editable,
                }
                for leaf in save.fields()
            ],
        }

    def _search(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        """Where a number lives in a save that has no plugin."""
        save = direct.DirectSave.open(Path(_require(params, "path")))
        sites = save.search(_require_number(params, "value"))
        return {
            "format": save.description,
            "structured": save.is_structured,
            "sites": [
                {"address": site.address, "value": site.value, "context": site.context}
                for site in sites
            ],
        }

    def _poke(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        """Change a number by address, in a game nothing is known about.

        ``confirmed`` is the same wall as the command line's --yes: an
        interface must have shown the player what it cannot tell them about
        this game before it gets here.
        """
        save = direct.DirectSave.open(Path(_require(params, "path")))
        address = str(_require(params, "address"))
        before, after = save.change(address, _require_number(params, "value"))
        risk = self._risk_of(params.get("game_folder"))

        if not params.get("confirmed"):
            raise RpcError(
                INVALID_PARAMS,
                "This game has no plugin, so SaveSmith cannot say what this number "
                "does or whether the game will accept it. Confirm to write it.",
            )
        if params.get("dry_run"):
            save.rebuild()
            return {
                "written": False, "address": address, "before": before,
                "after": after, "risk": risk,
            }

        backup = save.write(BackupStore.for_system(self.system))
        return {
            "written": True,
            "address": address,
            "before": before,
            "after": after,
            "backup": str(backup.folder),
            "risk": risk,
        }

    def _risk_of(self, folder: Any) -> dict[str, Any] | None:
        """What is known about the game a save belongs to, if anything.

        Editing without a plugin skips the risk classifier — there is no plugin
        to carry a tier. But an interface that knows which folder the game is
        installed in can still be told what the database says, and it should
        be: withholding a warning we hold is worse than having none.
        """
        if not folder:
            return None
        game = examine(Path(str(folder)))
        if game.steam_appid is None and not game.anticheat:
            return None
        assessment = assess(
            database=RiskDatabase.bundled(),
            appid=game.steam_appid,
            anticheat=game.anticheat,
            anticheat_scanned=True,
        )
        return {
            "tier": assessment.tier.value,
            "title": assessment.title or game.title,
            "signals": [
                {"en": signal.text.get("en"), "ru": signal.text.get("ru")}
                for signal in assessment.signals
            ],
        }

    def _prefs_read(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        store = self._prefs_store(params)
        entries = store.read()
        return {
            "location": store.location,
            "entries": [
                {"name": name, "value": entry.value, "kind": entry.kind}
                for name, entry in sorted(entries.items())
            ],
        }

    def _prefs_set(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        store = self._prefs_store(params)
        name = str(_require(params, "name"))
        entries = store.read()
        if name not in entries:
            raise RpcError(INVALID_PARAMS, f"There is no setting called '{name}' to change.")

        # The registry has no file to copy, and the rule that nothing changes
        # without a backup gets no exception for that.
        backups = BackupStore.for_system(self.system)
        backup = backups.create_from_bytes(
            f"{store.location}.json", store.export(), plugin_id="playerprefs"
        )
        before = entries[name].value
        store.write(name, playerprefs.coerce_like(before, _require_any(params, "value")))
        return {
            "written": True,
            "name": name,
            "before": before,
            "after": store.read()[name].value,
            "backup": str(backup.folder),
        }

    def _prefs_store(self, params: dict[str, Any]) -> playerprefs.PlayerPrefs:
        company, product = params.get("company"), params.get("product")
        folder = params.get("game_folder")
        if folder:
            game = examine(Path(str(folder)))
            company, product = company or game.company, product or game.project
        if not company or not product:
            raise RpcError(
                INVALID_PARAMS,
                "Unity settings are stored under a publisher and a product name. "
                "Give both, or the game's folder.",
            )
        return playerprefs.open_prefs(self.system, str(company), str(product))

    def _identify(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        raw = _read(Path(_require(params, "path")))
        report = detect.identify(raw)
        return {
            "solved": report.solved,
            "attempted": report.attempted,
            "summary": report.look.summary(),
            "candidates": [
                {
                    "description": candidate.description,
                    "structured": candidate.structured,
                    "exact": candidate.round_trip.exact_bytes,
                }
                for candidate in report.candidates[:10]
            ],
            "log": report.explain(),
        }

    def _checksums(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        raw = _read(Path(_require(params, "path")))
        found = checksum_module.find(raw, include_weak=bool(params.get("weak")))
        return {
            "checksums": [
                {
                    "algorithm": spec.algorithm,
                    "offset": spec.offset,
                    "covers": spec.coverage.value,
                    "description": spec.describe(),
                }
                for spec in found
            ]
        }

    def _discover(self, params: dict[str, Any], notify: Notify) -> dict[str, Any]:
        path = Path(_require(params, "path"))
        notify("stage", {"stage": "starting", "message": f"Looking at {path.name}"})
        result = run_discovery(
            path,
            backups=BackupStore.for_system(self.system),
            second_save=Path(params["second"]) if params.get("second") else None,
            known_before=params.get("was"),
            known_after=params.get("now"),
        )
        for stage in result.stages:
            notify("stage", {"stage": stage.stage.value, "ok": stage.ok, "message": stage.summary})
        return {
            "solved": result.solved,
            "log": result.explain(),
            "field_candidates": list(result.field_candidates),
            "draft": (
                result.draft_manifest(params["draft"], params.get("game", params["draft"]))
                if params.get("draft")
                else None
            ),
        }

    def _assistants(self, _params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        """Which assistants this machine has, for the window to offer."""
        from savesmith.agent import assistant

        return {"assistants": [one.described() for one in assistant.installed()]}

    def _analyse(self, params: dict[str, Any], notify: Notify) -> dict[str, Any]:
        """Have an assistant work out a save's format, start to finish.

        The window shows a button and a progress bar; everything between them
        happens here. Progress is streamed rather than summarised at the end
        because this takes minutes, and a person watching a frozen window
        concludes the program has hung.
        """
        from savesmith.agent import assistant

        save = Path(_require(params, "path"))
        numbers = {
            str(name): int(value)
            for name, value in (params.get("numbers") or {}).items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        chosen = assistant.named(str(params.get("assistant") or "claude"))

        outcome = assistant.analyse(
            chosen,
            save,
            game=Path(params["game_folder"]) if params.get("game_folder") else None,
            numbers=numbers,
            # Never defaulted to true: this sends parts of a save file outward,
            # and the window has to have asked.
            consented=bool(params.get("consented")),
            on_progress=lambda event: notify(
                "progress", {"text": event.text, "kind": event.kind}
            ),
        )
        return {
            "installed": outcome.succeeded,
            "plugin": outcome.plugin_id,
            "summary": assistant.summarise(outcome),
            "log": [{"text": event.text, "kind": event.kind} for event in outcome.events],
        }

    def _open(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        path = Path(_require(params, "path"))
        plugin = self._plugin_for(path, params.get("plugin"))

        anticheat: tuple[str, ...] = ()
        scanned = False
        folder: Path | None = None
        if params.get("game_folder"):
            folder = Path(params["game_folder"])
            game = look_at(folder, self.system).game
            anticheat, scanned = game.anticheat, True

        session = EditSession.open(
            path,
            plugin,
            database=RiskDatabase.bundled(),
            anticheat=anticheat,
            anticheat_scanned=scanned,
        )
        self._counter += 1
        key = f"s{self._counter}"
        self.sessions[key] = session
        if folder is not None:
            self.games[key] = folder
        return self._state(key, session, params.get("language", "en"))

    def _set(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        session = self._session(params)
        change = session.set(_require(params, "field"), params.get("value"))
        return {
            "change": {"field": change.address, "before": change.before, "after": change.after},
            **self._state(_require(params, "session"), session, params.get("language", "en")),
        }

    # -- items -----------------------------------------------------------

    def _items_list(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        """What is in the save's containers, named and pictured where possible."""
        key = _require(params, "session")
        session = self._session(params)
        language = params.get("language", "en")
        wanted = params.get("container")

        containers = []
        sheets: dict[str, Any] = {}
        for spec in session.plugin.containers:
            if wanted not in (None, spec.id):
                continue
            known = self._catalog_for(key, session, spec)
            sheets.update(_sheets_json(known))
            containers.append(
                {
                    "id": spec.id,
                    "label": spec.label.get(language),
                    "capacity": spec.capacity,
                    "max_count": spec.max_count,
                    "named": bool(known),
                    "source": known.source,
                    "stacks": [_stack_json(stack, known) for stack in session.stacks(spec.id)],
                }
            )
        return {"containers": containers, "sheets": sheets}

    def _items_catalog(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        """Everything this game *has*, which is what a pool to drag from means.

        Empty is a real answer: for a game whose data sits inside packed,
        encrypted archives, nobody has written down what its items are called
        until somebody installs a pack. The window says that rather than
        showing an empty box with no explanation.
        """
        key = _require(params, "session")
        session = self._session(params)
        spec = self._container(session, _require(params, "container"))
        known = self._catalog_for(key, session, spec)

        find = str(params.get("find") or "").strip()
        entries = known.search(find) if find else list(known.entries.values())
        limit = int(params.get("limit") or 200)
        held = {stack.item for stack in session.stacks(spec.id)}

        return {
            "container": spec.id,
            "named": bool(known),
            "source": known.source,
            "total": len(entries),
            "sheets": _sheets_json(known),
            "items": [
                {**_entry_json(entry), "held": entry.id in held} for entry in entries[:limit]
            ],
        }

    def _items_give(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        session = self._session(params)
        container = _require(params, "container")
        session.give_item(container, _require(params, "item"), int(params.get("count") or 1))
        return self._after_items(params, session, container)

    def _items_set(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        session = self._session(params)
        container = _require(params, "container")
        session.set_stack_count(
            container, _position(params), int(_require_number(params, "count"))
        )
        return self._after_items(params, session, container)

    def _items_remove(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        session = self._session(params)
        container = _require(params, "container")
        session.remove_stack(container, _position(params))
        return self._after_items(params, session, container)

    def _after_items(
        self, params: dict[str, Any], session: EditSession, container: str
    ) -> dict[str, Any]:
        """The changed container and the whole session state, in one answer.

        A window that redraws from what it was just handed cannot drift out of
        step with the core, which is the same reason every other method here
        returns the state too.
        """
        key = _require(params, "session")
        language = params.get("language", "en")
        spec = self._container(session, container)
        known = self._catalog_for(key, session, spec)
        return {
            "container": {
                "id": spec.id,
                "label": spec.label.get(language),
                "capacity": spec.capacity,
                "max_count": spec.max_count,
                "named": bool(known),
                "source": known.source,
                "stacks": [_stack_json(stack, known) for stack in session.stacks(spec.id)],
            },
            **self._state(key, session, language),
        }

    def _container(self, session: EditSession, name: str) -> ContainerSpec:
        spec = session.plugin.container(name)
        if spec is None:
            known = ", ".join(other.id for other in session.plugin.containers)
            raise RpcError(
                INVALID_PARAMS,
                f"This save has nothing called '{name}' to put things in.",
                {"containers": known},
            )
        return spec

    def _catalog_for(
        self, key: str, session: EditSession, spec: ContainerSpec
    ) -> catalog.Catalog:
        """Loaded once per session: it reads files and may carry an image."""
        cached = self._catalogs.get((key, spec.id))
        if cached is None:
            source = session.plugin.source
            cached = catalog.load(
                spec.catalog,
                plugin_folder=source.parent if source else None,
                game_folder=self.games.get(key),
            )
            self._catalogs[(key, spec.id)] = cached
        return cached

    def _acknowledge(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        session = self._session(params)
        known = {item.value: item for item in Acknowledgement}
        items = params.get("items") or []
        unknown = [name for name in items if name not in known]
        if unknown:
            raise RpcError(
                INVALID_PARAMS,
                f"Not something that can be confirmed: {', '.join(unknown)}.",
                {"known": sorted(known)},
            )
        session.acknowledge(*(known[name] for name in items))
        return self._state(_require(params, "session"), session, params.get("language", "en"))

    def _confirm_cloud(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        session = self._session(params)
        steps = [int(step) for step in (params.get("steps") or [1, 2, 3])]
        session.confirm_cloud_steps(*steps)
        return self._state(_require(params, "session"), session, params.get("language", "en"))

    def _write(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        session = self._session(params)
        backup = session.write(BackupStore.for_system(self.system))
        return {
            "written": True,
            "backup": {"folder": str(backup.folder), "label": backup.label},
            **self._state(_require(params, "session"), session, params.get("language", "en")),
        }

    def _close(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        self.sessions.pop(str(params.get("session")), None)
        return {"closed": True}

    def _backups_list(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        store = BackupStore.for_system(self.system)
        backups = store.list_for(_require(params, "plugin"))
        return {
            "backups": [
                {
                    "index": index,
                    "label": backup.label,
                    "file": str(backup.file),
                    "original": str(backup.original),
                    "size": backup.size,
                }
                for index, backup in enumerate(backups)
            ]
        }

    def _backups_restore(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        store = BackupStore.for_system(self.system)
        backups = store.list_for(_require(params, "plugin"))
        index = int(params.get("index", -1))
        if not 0 <= index < len(backups):
            raise RpcError(INVALID_PARAMS, f"There is no backup number {index}.")
        replaced = store.restore(backups[index])
        return {"restored": True, "previous_saved_at": str(replaced.folder)}

    def _plugins_list(self, _params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        catalogue = PluginStore.for_system(self.system).catalogue()
        return {
            "plugins": [_plugin_summary(plugin) for plugin in catalogue.plugins],
            "problems": [problem.user_message for problem in catalogue.problems],
        }

    def _plugins_install(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        archive = Path(_require(params, "path"))
        result = PluginStore.for_system(self.system).install_archive(
            _read(archive), name=archive.name
        )
        return {"installed": _plugin_summary(result.plugin), "message": result.describe()}

    def _plugins_remove(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        removed = PluginStore.for_system(self.system).remove(_require(params, "id"))
        return {"removed": removed}

    def _plugins_export(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        target = PluginStore.for_system(self.system).export(
            _require(params, "id"), Path(_require(params, "target"))
        )
        return {"path": str(target)}

    # -- helpers ---------------------------------------------------------

    def _session(self, params: dict[str, Any]) -> EditSession:
        key = str(params.get("session", ""))
        session = self.sessions.get(key)
        if session is None:
            raise RpcError(
                INVALID_PARAMS,
                "That save is no longer open. Open it again before editing.",
                {"session": key},
            )
        return session

    def _plugin_for(self, path: Path, wanted: str | None) -> Plugin:
        store = PluginStore.for_system(self.system)
        if wanted:
            plugin = store.catalogue().by_id(wanted)
            if plugin is None:
                raise RpcError(INVALID_PARAMS, f"There is no plugin called '{wanted}'.")
            return plugin

        raw = _read(path)
        matches = PluginRepository(store.root).match(raw) or bundled().match(raw)
        if not matches:
            raise RpcError(
                SAVESMITH_ERROR,
                "No plugin describes this save yet, so its values have no names. "
                "SaveSmith can work the format out — in the window that is the "
                "'Разобрать эту игру' button, and from the command line it is "
                "'savesmith discover'.",
            )
        return matches[0]

    def _state(self, key: str, session: EditSession, language: str) -> dict[str, Any]:
        """Everything the interface needs to draw the screen after any change.

        The session id is part of that. Leaving it out of every answer but
        the first one meant a window that replaced its state wholesale — the
        obvious way to use this — lost the id on the first acknowledgement
        and got "that save is no longer open" for its next call.
        """
        return {
            "session": key,
            "path": str(session.save.path),
            "plugin": _plugin_summary(session.plugin),
            "risk": {
                "tier": session.tier.value,
                "known": session.assessment.known,
                "signals": [
                    {"name": signal.name, "text": signal.text.get(language)}
                    for signal in session.assessment.signals
                ],
                "required": sorted(item.value for item in session.assessment.required),
            },
            "cloud": (
                {
                    "needed": session.cloud.needed,
                    "evidence": session.cloud.status.evidence,
                    "steps": [
                        {
                            "number": step.number,
                            "text": step.text.get(language),
                            "before_editing": step.before_editing,
                            "done": step.number in session.cloud.confirmed,
                        }
                        for step in _cloud_steps()
                    ],
                }
                if session.cloud is not None
                else None
            ),
            "fields": [
                {
                    "address": view.spec.address,
                    "label": view.spec.label.get(language),
                    "group": view.spec.group.get(language) if view.spec.group else None,
                    "type": view.spec.type.value,
                    "value": view.value,
                    "present": view.present,
                    "min": view.spec.minimum,
                    "max": view.spec.maximum,
                    "options": list(view.spec.options),
                    "warn": view.spec.warn.get(language) if view.spec.warn else None,
                    "advanced": view.spec.advanced,
                    "achievement": view.spec.achievement,
                    "online_linked": view.spec.online_linked,
                    "editable": view.spec.editable_by_default,
                }
                for view in session.view()
            ],
            "pending": [
                {"field": change.address, "before": change.before, "after": change.after}
                for change in session.pending
            ],
            "blockers": list(session.blockers),
            "may_write": session.may_write,
        }


def _stack_json(stack: Stack, known: catalog.Catalog) -> dict[str, Any]:
    entry = known.get(stack.item)
    return {
        "item": stack.item,
        "position": stack.position,
        "count": stack.count,
        "name": entry.name if entry else stack.item,
        "kind": entry.kind if entry else None,
        "description": entry.description if entry else None,
        "icon": _icon_json(entry.icon if entry else None),
    }


def _entry_json(entry: catalog.Entry) -> dict[str, Any]:
    return {
        "item": entry.id,
        "name": entry.name,
        "kind": entry.kind,
        "description": entry.description,
        "icon": _icon_json(entry.icon),
    }


def _icon_json(icon: catalog.Icon | None) -> dict[str, Any] | None:
    return None if icon is None else {"sheet": icon.sheet, "index": icon.index}


def _sheets_json(known: catalog.Catalog) -> dict[str, Any]:
    """Icon sheets as data URLs, whole.

    Whole because cutting a sprite sheet into a thousand images would mean
    decoding and re-encoding PNGs here for something the browser does with two
    CSS properties. The sheet is handed over once and every icon is a position
    in it.
    """
    return {
        sheet.id: {
            "url": "data:image/png;base64," + base64.b64encode(sheet.png).decode("ascii"),
            "tile": sheet.tile,
            "columns": sheet.columns,
        }
        for sheet in known.sheets.values()
    }


def _position(params: Mapping[str, Any]) -> str | int:
    """A place in a container: a number for a list, a key for a map."""
    value = _require_any(params, "position")
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise RpcError(INVALID_PARAMS, "'position' is a number or a key.")
    return value


type Notify = Callable[[str, dict[str, Any]], None]


def _cloud_steps() -> Iterator[Any]:
    from savesmith.core.cloud import STEPS

    return iter(STEPS)


def _notifier(sink: IO[str] | None, method: str) -> Notify:
    def notify(kind: str, payload: dict[str, Any]) -> None:
        if sink is None:
            return
        _emit(
            sink,
            {
                "jsonrpc": PROTOCOL,
                "method": "progress",
                "params": {"source": method, "kind": kind, **payload},
            },
        )

    return notify


def _emit(sink: IO[str], message: Mapping[str, Any]) -> None:
    sink.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    sink.flush()


def _error(
    identifier: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": PROTOCOL, "id": identifier, "error": error}


def _require(params: Mapping[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise RpcError(INVALID_PARAMS, f"This call needs a '{name}'.")
    return value


def _require_number(params: Mapping[str, Any], name: str) -> float:
    """A number, and not a bool — JSON says True is 1, a player does not."""
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RpcError(INVALID_PARAMS, f"This call needs '{name}' to be a number.")
    return float(value)


def _require_any(params: Mapping[str, Any], name: str) -> Any:
    """A value of whatever type the caller sent, as long as they sent one."""
    if params.get(name) is None:
        raise RpcError(INVALID_PARAMS, f"This call needs a '{name}'.")
    return params[name]


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SaveSmithError(
            f"This file could not be read: {exc.strerror}.", detail=str(path)
        ) from exc


def _plugin_summary(plugin: Plugin) -> dict[str, Any]:
    return {
        "id": plugin.id,
        "version": plugin.version,
        "game": plugin.game,
        "engine": plugin.engine,
        "confidence": plugin.confidence.value,
        "steam_appid": plugin.steam_appid,
        "risk_tier": plugin.risk.tier.value,
    }


def main() -> int:  # pragma: no cover - the entry point itself
    return Server().serve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
