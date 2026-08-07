"""The command line.

A thin layer. Every rule already lives in the core — backups before writes,
acknowledgements before dangerous edits, the round-trip gate before a plugin is
believed — and this must not re-implement or relax any of it. It parses
arguments, calls the core, and prints what came back.

One rule of its own: a user never sees a traceback. Anything SaveSmith raises
on purpose carries a sentence written for a person, and that sentence is what
gets printed. Anything else is a bug, and says so.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from savesmith.agent.discovery import discover as run_discovery
from savesmith.core import checksum as checksum_module
from savesmith.core import compare, detect, diagnostics
from savesmith.core.backup import BackupStore
from savesmith.core.discover import examine, find_saves
from savesmith.core.errors import SaveSmithError
from savesmith.core.paths import RealSystem, SystemFacade
from savesmith.core.plugin import Plugin
from savesmith.core.repository import verify
from savesmith.core.risk import Acknowledgement, Assessment, RiskDatabase, assess
from savesmith.core.session import EditSession
from savesmith.core.steam import SteamInstall
from savesmith.core.store import PluginStore
from savesmith.core.wine import scan_prefixes

PROGRAM = "savesmith"


def main(argv: Sequence[str] | None = None, system: SystemFacade | None = None) -> int:
    """``system`` is injectable for the same reason everything else here is:
    so tests never touch the real machine, on any platform."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if not getattr(arguments, "handler", None):
        parser.print_help()
        return 2

    system = system or RealSystem()
    try:
        return int(arguments.handler(arguments, system))
    except SaveSmithError as error:
        # The whole point of user_message: this is what a person reads.
        print(f"\n{error.user_message}", file=sys.stderr)
        if arguments.verbose and error.detail:
            print(f"  ({error.detail})", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Nothing was changed.", file=sys.stderr)
        return 130


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_doctor(_arguments: argparse.Namespace, system: SystemFacade) -> int:
    print(diagnostics.render(diagnostics.collect(system)))
    return 0


def _cmd_scan(arguments: argparse.Namespace, system: SystemFacade) -> int:
    """What is installed on this machine, and where its saves might be."""
    try:
        scan = SteamInstall.discover(system).scan()
    except SaveSmithError as error:
        print(error.user_message)
        return 0

    print(f"Steam: {scan.root}")
    print(f"{len(scan.games)} game(s) installed\n")
    database = RiskDatabase.bundled()
    for game in scan.games:
        assessment = _assess_game(game.appid, game.install_dir, database)
        print(f"  [{assessment.tier.value:9}] {game.name}  (AppID {game.appid})")
        if arguments.verbose:
            print(f"              {game.install_dir}")
            for signal in assessment.signals:
                print(f"              · {signal.text.get(arguments.language)}")

    bottles = scan_prefixes(system)
    if bottles:
        print(f"\n{len(bottles)} Windows bottle(s):")
        for prefix in bottles:
            print(f"  {prefix.name} [{prefix.kind.value}] {prefix.path}")
    return 0


def _cmd_find(arguments: argparse.Namespace, system: SystemFacade) -> int:
    """Point at a game folder, get its saves."""
    game = examine(Path(arguments.folder).expanduser())
    for line in find_saves(game, system).explain():
        print(line)
    return 0


def _cmd_identify(arguments: argparse.Namespace, _system: SystemFacade) -> int:
    raw = _read(Path(arguments.file))
    report = detect.identify(raw)
    for line in report.explain():
        print(line)
    return 0 if report.solved else 1


def _cmd_checksum(arguments: argparse.Namespace, _system: SystemFacade) -> int:
    raw = _read(Path(arguments.file))
    found = checksum_module.find(raw, include_weak=arguments.weak)
    if not found:
        print("No checksum found in this file.")
        return 1
    for spec in found:
        print(f"  {spec.describe()}")
    return 0


def _cmd_discover(arguments: argparse.Namespace, system: SystemFacade) -> int:
    result = run_discovery(
        Path(arguments.file),
        backups=BackupStore.for_system(system),
        second_save=Path(arguments.second) if arguments.second else None,
        known_before=arguments.was,
        known_after=arguments.now,
    )
    for line in result.explain():
        print(line)
    if arguments.draft:
        import json

        draft = result.draft_manifest(arguments.draft, arguments.draft.replace("-", " ").title())
        target = Path(arguments.output or f"{arguments.draft}-manifest.json")
        target.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nDraft manifest written to {target}")
    return 0 if result.solved else 1


def _cmd_diff(arguments: argparse.Namespace, _system: SystemFacade) -> int:
    """Find a field by watching it change between two saves.

    The workflow this exists for: save, note the number, spend some, save
    again. Two files and two numbers pin a field down that no amount of
    staring at a hex dump would.
    """
    before_raw = _read(Path(arguments.before))
    after_raw = _read(Path(arguments.after))

    before_report = detect.identify(before_raw, max_depth=3)
    after_report = detect.identify(after_raw, max_depth=3)

    if before_report.solved and after_report.solved:
        assert before_report.best is not None and after_report.best is not None
        before = before_report.best.pipeline.decode(before_raw).value
        after = after_report.best.pipeline.decode(after_raw).value
        changes = compare.compare_structures(before, after)
        numeric = compare.numeric_changes(changes)

        print(f"Format: {before_report.best.description}")
        print(f"{len(changes)} value(s) changed, {len(numeric)} of them numbers\n")
        for change in (numeric if arguments.numbers_only else changes):
            print(f"  {change}")
        return 0 if changes else 1

    print("Neither save could be decoded, so comparing bytes.\n")
    if len(before_raw) != len(after_raw):
        print(
            f"The two files are different sizes ({len(before_raw)} and {len(after_raw)} "
            f"bytes), so their bytes cannot be lined up."
        )
        return 1

    byte_diff = compare.compare_bytes(before_raw, after_raw)
    print(byte_diff.summary())
    for start, length in byte_diff.ranges[:20]:
        print(f"  0x{start:X} … 0x{start + length:X}  ({length} bytes)")

    if arguments.was is None or arguments.now is None:
        print(
            "\nTell SaveSmith the number that changed with --was and --now, and it will "
            "say exactly where it lives."
        )
        return 1

    guesses = compare.guesses_in_ranges(
        compare.narrow(before_raw, after_raw, arguments.was, arguments.now), byte_diff
    )
    if not guesses:
        print(f"\nNo place holds {arguments.was:g} before and {arguments.now:g} after.")
        return 1
    print(f"\n{len(guesses)} candidate field(s):")
    for guess in guesses:
        print(f"  {guess}")
    return 0


def _cmd_show(arguments: argparse.Namespace, system: SystemFacade) -> int:
    session = _session(arguments, system)
    for line in session.explain(arguments.language):
        print(line)
    print()
    for view in session.view():
        label = view.spec.label.get(arguments.language)
        if not view.present:
            print(f"  {label:32} — not in this save")
            continue
        marks = []
        if view.spec.achievement:
            marks.append("achievement")
        if view.spec.online_linked:
            marks.append("online, not editable")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        print(f"  {label:32} {view.value!r}{suffix}   ({view.spec.address})")
    return 0


def _cmd_set(arguments: argparse.Namespace, system: SystemFacade) -> int:
    session = _session(arguments, system)
    session.acknowledge(*_acknowledgements(arguments.yes))
    if arguments.cloud_done:
        session.confirm_cloud_steps(1, 2, 3)

    change = session.set(arguments.field, arguments.value)
    print(f"{arguments.field}: {change.before!r} → {change.after!r}")

    if arguments.dry_run:
        print("\nDry run: nothing was written.")
        return 0
    if not session.may_write:
        print("\nNot written. Still needed:")
        for blocker in session.blockers:
            print(f"  {blocker}")
        print("\nPass --yes with the items above once you have read what they mean.")
        return 1

    backup = session.write(BackupStore.for_system(system))
    print(f"\nWritten. Backup: {backup.folder}")
    return 0


def _cmd_backups(arguments: argparse.Namespace, system: SystemFacade) -> int:
    store = BackupStore.for_system(system)
    backups = store.list_for(arguments.plugin)
    if not backups:
        print(f"No backups for '{arguments.plugin}'.")
        return 1
    if arguments.restore is None:
        for index, backup in enumerate(backups):
            print(f"  [{index}] {backup.label}  {backup.file.name}  ({backup.size} bytes)")
        print("\nRestore one with: savesmith backups <plugin> --restore <number>")
        return 0

    if not 0 <= arguments.restore < len(backups):
        print(f"There is no backup number {arguments.restore}.")
        return 1
    replaced = store.restore(backups[arguments.restore])
    print(f"Restored. What was there is saved at {replaced.folder}")
    return 0


def _cmd_plugins(arguments: argparse.Namespace, system: SystemFacade) -> int:
    store = PluginStore.for_system(system)

    if arguments.install:
        result = store.install_archive(_read(Path(arguments.install)), name=arguments.install)
        print(result.describe())
        return 0
    if arguments.export:
        target = Path(arguments.output or f"{arguments.export}.zip")
        print(f"Exported to {store.export(arguments.export, target)}")
        return 0
    if arguments.remove:
        print("Removed." if store.remove(arguments.remove) else "There was nothing to remove.")
        return 0

    catalogue = store.catalogue()
    for plugin in sorted(catalogue.plugins, key=lambda item: item.id):
        print(f"  {plugin.id:20} v{plugin.version:<3} {plugin.confidence.value:12} {plugin.game}")
    for problem in catalogue.problems:
        print(f"  ! {problem.user_message}")
    return 0


def _cmd_verify(arguments: argparse.Namespace, system: SystemFacade) -> int:
    """Run the round-trip gate for a plugin against a folder of real saves."""
    plugin = PluginStore.for_system(system).catalogue().by_id(arguments.plugin)
    if plugin is None:
        print(f"There is no plugin called '{arguments.plugin}'.")
        return 1
    corpus = sorted(path for path in Path(arguments.corpus).iterdir() if path.is_file())
    result = verify(plugin, corpus)
    for line in result.explain():
        print(line)
    return 0 if result.publishable else 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SaveSmithError(
            f"This file could not be read: {exc.strerror}.", detail=str(path)
        ) from exc


def _assess_game(appid: int, install_dir: Path, database: RiskDatabase) -> Assessment:
    game = examine(install_dir)
    return assess(
        database=database,
        appid=appid,
        anticheat=game.anticheat,
        anticheat_scanned=install_dir.is_dir(),
    )


def _session(arguments: argparse.Namespace, system: SystemFacade) -> EditSession:
    path = Path(arguments.file)
    raw = _read(path)
    store = PluginStore.for_system(system)

    if arguments.plugin:
        plugin = store.catalogue().by_id(arguments.plugin)
        if plugin is None:
            raise SaveSmithError(f"There is no plugin called '{arguments.plugin}'.")
    else:
        matches = _match(store, raw)
        if not matches:
            raise SaveSmithError(
                "No plugin can read this save. Try 'savesmith identify' to see what it is, "
                "or 'savesmith discover' to work the format out."
            )
        plugin = matches[0]

    game_anticheat: tuple[str, ...] = ()
    scanned = False
    if arguments.game_folder:
        game = examine(Path(arguments.game_folder).expanduser())
        game_anticheat, scanned = game.anticheat, True

    return EditSession.open(
        path,
        plugin,
        database=RiskDatabase.bundled(),
        anticheat=game_anticheat,
        anticheat_scanned=scanned,
    )


def _match(store: PluginStore, raw: bytes) -> list[Plugin]:
    from savesmith.core.repository import PluginRepository

    return PluginRepository(store.root).match(raw) or _bundled_match(raw)


def _bundled_match(raw: bytes) -> list[Plugin]:
    from savesmith.core.repository import bundled

    return bundled().match(raw)


def _acknowledgements(names: Sequence[str] | None) -> list[Acknowledgement]:
    if not names:
        return []
    known = {item.value: item for item in Acknowledgement}
    if "all" in names:
        return list(Acknowledgement)
    unknown = [name for name in names if name not in known]
    if unknown:
        raise SaveSmithError(
            f"Not something that can be confirmed: {', '.join(unknown)}. "
            f"Choose from: {', '.join(sorted(known))}, or 'all'."
        )
    return [known[name] for name in names]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Edit the saves of offline games, including games nothing else can read.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show technical detail too")
    parser.add_argument("--language", default="en", help="language for labels and warnings")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor", help="what SaveSmith can see on this machine")
    doctor.set_defaults(handler=_cmd_doctor)

    scan = subparsers.add_parser("scan", help="installed games and their risk tier")
    scan.set_defaults(handler=_cmd_scan)

    find = subparsers.add_parser("find", help="find the saves of a game, given its folder")
    find.add_argument("folder")
    find.set_defaults(handler=_cmd_find)

    identify = subparsers.add_parser("identify", help="work out the format of one save file")
    identify.add_argument("file")
    identify.set_defaults(handler=_cmd_identify)

    checksum = subparsers.add_parser("checksum", help="find the checksum inside a save")
    checksum.add_argument("file")
    checksum.add_argument("--weak", action="store_true", help="include short, unreliable ones")
    checksum.set_defaults(handler=_cmd_checksum)

    discover = subparsers.add_parser("discover", help="run the full discovery on an unknown save")
    discover.add_argument("file")
    discover.add_argument("--second", help="a second save, made after changing a known number")
    discover.add_argument("--was", type=float, help="the value before")
    discover.add_argument("--now", type=float, help="the value after")
    discover.add_argument("--draft", metavar="PLUGIN_ID", help="write a draft plugin manifest")
    discover.add_argument("--output", help="where to write the draft")
    discover.set_defaults(handler=_cmd_discover)

    diff = subparsers.add_parser(
        "diff", help="find a field by comparing two saves"
    )
    diff.add_argument("before")
    diff.add_argument("after")
    diff.add_argument("--was", type=float, help="the value before, if you know it")
    diff.add_argument("--now", type=float, help="the value after")
    diff.add_argument(
        "--numbers-only", action="store_true", help="hide changes that are not numbers"
    )
    diff.set_defaults(handler=_cmd_diff)

    show = subparsers.add_parser("show", help="list what can be edited in a save")
    _add_save_arguments(show)
    show.set_defaults(handler=_cmd_show)

    setter = subparsers.add_parser("set", help="change one value in a save")
    _add_save_arguments(setter)
    setter.add_argument("field")
    setter.add_argument("value")
    setter.add_argument(
        "--yes",
        nargs="*",
        metavar="ITEM",
        help="confirm a risk you have read: " + ", ".join(item.value for item in Acknowledgement),
    )
    setter.add_argument(
        "--cloud-done", action="store_true", help="the Steam Cloud steps have been carried out"
    )
    setter.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    setter.set_defaults(handler=_cmd_set)

    backups = subparsers.add_parser("backups", help="list or restore backups")
    backups.add_argument("plugin")
    backups.add_argument("--restore", type=int, metavar="N", help="restore backup number N")
    backups.set_defaults(handler=_cmd_backups)

    plugins = subparsers.add_parser("plugins", help="list, install, export or remove plugins")
    plugins.add_argument("--install", metavar="ARCHIVE")
    plugins.add_argument("--export", metavar="PLUGIN_ID")
    plugins.add_argument("--remove", metavar="PLUGIN_ID")
    plugins.add_argument("--output", help="where to write an export")
    plugins.set_defaults(handler=_cmd_plugins)

    verify_command = subparsers.add_parser(
        "verify", help="run the round-trip gate for a plugin over a folder of saves"
    )
    verify_command.add_argument("plugin")
    verify_command.add_argument("corpus")
    verify_command.set_defaults(handler=_cmd_verify)

    return parser


def _add_save_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file")
    parser.add_argument("--plugin", help="use this plugin instead of guessing")
    parser.add_argument(
        "--game-folder", help="the game's install folder, so anti-cheat can be checked"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
