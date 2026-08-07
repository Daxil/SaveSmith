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

import json
import sys
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from savesmith.agent.discovery import discover as run_discovery
from savesmith.core import checksum as checksum_module
from savesmith.core import detect, diagnostics, direct, playerprefs
from savesmith.core.backup import BackupStore
from savesmith.core.discover import examine, find_saves
from savesmith.core.errors import SaveSmithError
from savesmith.core.paths import RealSystem, SystemFacade
from savesmith.core.plugin import Plugin
from savesmith.core.repository import PluginRepository, bundled
from savesmith.core.risk import Acknowledgement, RiskDatabase
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
    _counter: int = 0

    # -- transport -------------------------------------------------------

    def serve(self, stream_in: IO[str] | None = None, stream_out: IO[str] | None = None) -> int:
        """Read requests until the input ends."""
        source = stream_in or sys.stdin
        sink = stream_out or sys.stdout
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
            "find_saves": self._find_saves,
            "identify": self._identify,
            "checksums": self._checksums,
            "discover": self._discover,
            "search": self._search,
            "poke": self._poke,
            "prefs.read": self._prefs_read,
            "prefs.set": self._prefs_set,
            "open": self._open,
            "set": self._set,
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

    def _find_saves(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        folder = Path(_require(params, "folder"))
        game = examine(folder)
        found = find_saves(game, self.system)
        return {
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
            "saves": [
                {
                    "path": str(save.path),
                    "format": save.format,
                    "recognised": save.recognised,
                    "size": save.size,
                }
                for save in found.saves
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

        if not params.get("confirmed"):
            raise RpcError(
                INVALID_PARAMS,
                "This game has no plugin, so SaveSmith cannot say what this number "
                "does or whether the game will accept it. Confirm to write it.",
            )
        if params.get("dry_run"):
            save.rebuild()
            return {"written": False, "address": address, "before": before, "after": after}

        backup = save.write(BackupStore.for_system(self.system))
        return {
            "written": True,
            "address": address,
            "before": before,
            "after": after,
            "backup": str(backup.folder),
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

    def _open(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        path = Path(_require(params, "path"))
        plugin = self._plugin_for(path, params.get("plugin"))

        anticheat: tuple[str, ...] = ()
        scanned = False
        if params.get("game_folder"):
            game = examine(Path(params["game_folder"]))
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
        return {"session": key, **self._state(session, params.get("language", "en"))}

    def _set(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        session = self._session(params)
        change = session.set(_require(params, "field"), params.get("value"))
        return {
            "change": {"field": change.address, "before": change.before, "after": change.after},
            **self._state(session, params.get("language", "en")),
        }

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
        return self._state(session, params.get("language", "en"))

    def _confirm_cloud(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        session = self._session(params)
        steps = [int(step) for step in (params.get("steps") or [1, 2, 3])]
        session.confirm_cloud_steps(*steps)
        return self._state(session, params.get("language", "en"))

    def _write(self, params: dict[str, Any], _notify: Notify) -> dict[str, Any]:
        session = self._session(params)
        backup = session.write(BackupStore.for_system(self.system))
        return {
            "written": True,
            "backup": {"folder": str(backup.folder), "label": backup.label},
            **self._state(session, params.get("language", "en")),
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
                "No plugin can read this save yet. Run discovery on it to work out the format.",
            )
        return matches[0]

    def _state(self, session: EditSession, language: str) -> dict[str, Any]:
        """Everything the interface needs to draw the screen after any change."""
        return {
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
