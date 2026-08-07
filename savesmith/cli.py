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
from typing import Any

from savesmith.agent.discovery import discover as run_discovery
from savesmith.agent.writer import DEFAULT_MODEL
from savesmith.core import checksum as checksum_module
from savesmith.core import compare, detect, diagnostics, direct, library, playerprefs
from savesmith.core.backup import BackupStore
from savesmith.core.console import use_utf8
from savesmith.core.discover import Discovery, GameFolder, Look, examine, look_at
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
    # Before anything is printed: Windows defaults to a codepage that has no
    # arrow and no Cyrillic, and every line of Russian output would raise.
    use_utf8(sys.stdout, sys.stderr)

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


def _cmd_games(arguments: argparse.Namespace, system: SystemFacade) -> int:
    """Every game on this machine, wherever it is installed.

    Steam on the host, Steam inside each Wine bottle, the bottles themselves,
    and Mac applications built by a game engine. The path printed beside each
    one is exactly what the other commands take.
    """
    found = library.scan(system)
    if not found.games:
        print(
            "No games found. SaveSmith looks at Steam, at Wine bottles and at "
            "Mac applications; anything installed elsewhere still works if you "
            "point at it:\n  savesmith find \"<the game's folder, .exe or .app>\""
        )
        for problem in found.problems:
            print(f"\n  {problem}")
        return 0

    database = RiskDatabase.bundled()
    source = None
    for game in found.sorted():
        if game.source != source:
            source = game.source
            print(f"\n{source}:")
        tier = ""
        if game.steam_appid is not None:
            tier = f"[{_assess_game(game.steam_appid, game.path, database).tier.value}] "
        print(f"  {tier}{game.name}")
        if arguments.verbose or not tier:
            print(f"      {_quote(game.path)}")

    print("\nOpen one with:\n  savesmith find \"<the path above>\"")
    for problem in found.problems:
        print(f"\n  {problem}")
    return 0


def _cmd_find(arguments: argparse.Namespace, system: SystemFacade) -> int:
    """Point at a game — its folder, its .exe or its .app — and get its saves."""
    look = look_at(Path(arguments.folder).expanduser(), system)
    for note in look.notes:
        print(f"({note})")
    for line in look.found.explain(verbose=arguments.verbose):
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
    writer = None
    if arguments.model:
        from savesmith.agent.writer import ModelCodecWriter

        writer = ModelCodecWriter(
            model=arguments.model,
            log=lambda message: print(f"  … {message}"),
        )
        print(
            f"A model may be asked to write a codec if nothing known fits, "
            f"spending at most ${arguments.budget:.2f}.\n"
        )

    result = run_discovery(
        Path(arguments.file),
        backups=BackupStore.for_system(system),
        second_save=Path(arguments.second) if arguments.second else None,
        known_before=arguments.was,
        known_after=arguments.now,
        writer=writer,
        max_budget_usd=arguments.budget,
    )
    for line in result.explain():
        print(line)
    if arguments.draft:
        import json

        draft = result.draft_manifest(arguments.draft, arguments.draft.replace("-", " ").title())
        target = Path(arguments.output or f"{arguments.draft}-manifest.json")
        target.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nDraft manifest written to {target}")
        if result.codec is not None:
            # Next to the manifest, because a plugin that needs a codec loads
            # it from its own folder.
            codec_path = target.with_name("codec.py")
            codec_path.write_text(result.codec.source, encoding="utf-8")
            note = "" if result.codec_verified else "  (NOT verified — read it before using it)"
            print(f"Codec written to {codec_path}{note}")
    return 0 if result.solved else 1


def _cmd_diff(arguments: argparse.Namespace, _system: SystemFacade) -> int:
    """Find a field by watching it change between two saves.

    The workflow this exists for: save, note the number, spend some, save
    again. Two files and two numbers pin a field down that no amount of
    staring at a hex dump would.
    """
    # Opened the same way 'search' and 'poke' open a save: every layer the
    # ladder can peel off, peeled off. Comparing two encrypted Elden Ring
    # slots byte for byte would show that everything changed and mean nothing.
    before_save = direct.DirectSave.open(Path(arguments.before))
    after_save = direct.DirectSave.open(Path(arguments.after))

    if before_save.is_structured and after_save.is_structured:
        changes = compare.compare_structures(before_save.value, after_save.value)
        numeric = compare.numeric_changes(changes)

        print(f"Format: {before_save.description}")
        print(f"{len(changes)} value(s) changed, {len(numeric)} of them numbers\n")
        for change in (numeric if arguments.numbers_only else changes):
            print(f"  {change}")
        return 0 if changes else 1

    before_raw = _payload_of(before_save)
    after_raw = _payload_of(after_save)
    print(f"Format: {before_save.description}. Comparing bytes.\n")
    if len(before_raw) != len(after_raw):
        print(
            f"The two saves are different sizes ({len(before_raw)} and {len(after_raw)} "
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
    collapsed = compare.group_by_bytes(guesses)
    print(f"\n{len(collapsed)} candidate field(s):")
    for guess, others in collapsed:
        also = f"   (also {', '.join(others)})" if others else ""
        print(f"  {guess}{also}")
    print(f"\nChange one with: savesmith poke {_quote(Path(arguments.after))} <address> <value>")
    return 0


def _payload_of(save: direct.DirectSave) -> bytes:
    """The decoded bytes of a save, or its raw bytes if it decodes to a structure."""
    return save.value if isinstance(save.value, bytes) else save.raw


def _cmd_rpc(_arguments: argparse.Namespace, system: SystemFacade) -> int:
    """Serve JSON-RPC on stdin and stdout, for the window to talk to.

    Deliberately stdio and not a local HTTP port: a port with no authentication
    is reachable by any page the user has open in a browser, and this process
    can rewrite save files. A pipe is only reachable by whoever started it.
    """
    from savesmith.rpc import Server

    return Server(system=system).serve()


def _cmd_search(arguments: argparse.Namespace, system: SystemFacade) -> int:
    """Where does this number live in this save?

    The workflow for a game with no plugin: look at the screen, type the number
    you can see, and get back every place it is stored.
    """
    target = _one_save(arguments, system)
    save = direct.DirectSave.open(target)
    sites = save.search(arguments.value, encoding=arguments.type)

    print(f"{target.name}: {save.description}")
    if not sites:
        print(f"\n{_plain(arguments.value)} is not stored anywhere in this save.")
        print(
            "If the game shows it rounded or scaled, try the exact figure. If it is "
            "still not there, 'savesmith diff' with two saves finds it by watching it change."
        )
        return 1

    print(f"\n{len(sites)} place(s) hold {_plain(arguments.value)}:\n")
    for site in sites[:40]:
        print(f"  {site}")
    if len(sites) > 40:
        print(f"  … and {len(sites) - 40} more")
    if len(sites) > 1:
        print(
            "\nSeveral places hold this number, and only you know which one is the "
            "one on screen. Save again after it changes and run 'savesmith diff' to "
            "narrow it down, or try the change and check in-game."
        )
    print(f"\nChange one with: savesmith poke {_quote(target)} <address> <new value> --yes")
    return 0


def _cmd_poke(arguments: argparse.Namespace, system: SystemFacade) -> int:
    """Change one number in a save that has no plugin.

    Blunter than ``set``: there is no field description, no risk tier and no
    range to check against, because nobody has written any of that down for
    this game yet. What there is: a backup, and a check that the change
    survived being rebuilt.
    """
    target, game = _one_save_and_game(arguments, system)
    save = direct.DirectSave.open(target)
    before, after = save.change(arguments.address, arguments.value)

    print(f"{target.name}: {save.description}")
    print(f"{arguments.address}: {before!r} → {after!r}")
    _warn_about_the_game(game, arguments.language)

    if arguments.dry_run:
        save.rebuild()  # prove it can be put back together, then throw it away
        print("\nDry run: nothing was written. The save does rebuild with this change.")
        return 0

    if not arguments.yes:
        print(
            "\nNot written. This game has no plugin, so SaveSmith cannot tell you "
            "what this number does, whether it feeds an achievement, or whether the "
            "game will accept it. Add --yes once you have read that sentence."
        )
        return 1

    backup = save.write(BackupStore.for_system(system))
    print(f"\nWritten. Backup: {backup.folder}")
    return 0


def _warn_about_the_game(game: GameFolder | None, language: str) -> None:
    """What is known about this game, before anything is written.

    Editing without a plugin skips the whole risk classifier, because there is
    no plugin to carry a tier. But when the player pointed at the game's
    folder, the game is identified — and the database may well have plenty to
    say about it. Saying nothing then would be hiding a warning we hold.
    """
    if game is None or game.steam_appid is None:
        return
    assessment = assess(
        database=RiskDatabase.bundled(),
        appid=game.steam_appid,
        anticheat=game.anticheat,
        anticheat_scanned=True,
    )
    print(f"\nRisk for {assessment.title or game.title}: {assessment.tier.value}")
    for signal in assessment.signals:
        print(f"  · {signal.text.get(language)}")


def _one_save(arguments: argparse.Namespace, system: SystemFacade) -> Path:
    return _one_save_and_game(arguments, system)[0]


def _one_save_and_game(
    arguments: argparse.Namespace, system: SystemFacade
) -> tuple[Path, GameFolder | None]:
    """A save file, whether the user named one or pointed at a game."""
    look = look_at(Path(arguments.file).expanduser(), system)
    if look.target.save_file is not None:
        return look.target.save_file, None
    for note in look.notes:
        print(f"({note})")
    game, found = look.game, look.found
    saves = found.player_saves
    if not saves:
        raise SaveSmithError(_nothing_editable(game, found))
    slot = arguments.slot
    if slot is None and len(saves) > 1:
        listing = "\n".join(
            f"  {index}. {save.path.name}  ({save.format}, {save.size} bytes)"
            for index, save in enumerate(saves[:20], start=1)
        )
        raise SaveSmithError(
            f"{game.title} has {len(saves)} save files:\n{listing}\n\n"
            f"Choose one with --slot, or name the file directly."
        )
    index = (slot or 1) - 1
    if not 0 <= index < len(saves):
        raise SaveSmithError(
            f"There is no save {slot} here; {game.title} has {len(saves)}."
        )
    return saves[index].path, game


def _quote(path: Path) -> str:
    text = str(path)
    return f'"{text}"' if " " in text else text


def _plain(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


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


def _cmd_prefs(arguments: argparse.Namespace, system: SystemFacade) -> int:
    """Unity settings, which are not in the save file at all.

    Plenty of Unity games keep real progress here: unlocked levels, currency,
    whether the tutorial was finished. A tool that only reads save files tells
    those players their game is not supported.
    """
    company, product = arguments.company, arguments.product
    if arguments.game_folder:
        game = examine(Path(arguments.game_folder).expanduser())
        company = company or game.company
        product = product or game.project
    if not company or not product:
        raise SaveSmithError(
            "Unity settings are stored under a publisher and a product name. "
            "Give both, or point at the game's folder with --game-folder."
        )

    store = playerprefs.open_prefs(system, company, product)
    entries = store.read()
    print(f"{store.location}\n")
    if not entries:
        print("Nothing stored here.")
        return 1

    for name, entry in sorted(entries.items()):
        print(f"  {name:32} {entry.value!r}   ({entry.kind})")

    if not arguments.set:
        return 0

    name, raw_value = arguments.set
    backup = BackupStore.for_system(system).create_from_bytes(
        f"{company}.{product}.playerprefs", store.export(), plugin_id="playerprefs"
    )
    store.write(name, _typed(raw_value, entries[name].kind if name in entries else "string"))
    print(f"\n{name} → {raw_value}\nBackup: {backup.folder}")
    return 0


def _typed(raw: str, kind: str) -> Any:
    """Keep the type the game stored, rather than turning a number into text."""
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes")
    return raw


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


# How many of a folder's save files are opened to see which plugin reads them.
# Games with dozens of slots exist; reading all of them to answer "which one"
# would be slow for no gain.
_MAX_CANDIDATES = 12


def _session(arguments: argparse.Namespace, system: SystemFacade) -> EditSession:
    store = PluginStore.for_system(system)
    look = look_at(Path(arguments.file).expanduser(), system)

    game = None
    if look.target.save_file is not None:
        target = look.target.save_file
        if arguments.game_folder:
            game = look_at(Path(arguments.game_folder).expanduser(), system).game
    else:
        # Pointing at the game is the intended way in: the person editing a
        # save knows where the game is, not where it decided to hide its saves.
        for note in look.notes:
            print(f"({note})")
        game, target = _save_in_folder(look, store, slot=arguments.slot)
        print(f"{game.title}: {target.name}\n")

    raw = _read(target)

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

    return EditSession.open(
        target,
        plugin,
        database=RiskDatabase.bundled(),
        anticheat=game.anticheat if game else (),
        anticheat_scanned=game is not None,
    )


def _save_in_folder(
    look: Look, store: PluginStore, *, slot: int | None
) -> tuple[GameFolder, Path]:
    """Turn a game into the one save file to edit.

    Refuses to guess between several editable saves. Picking the wrong slot
    overwrites progress the player wanted to keep, and no amount of "probably
    the newest one" reasoning is worth that.
    """
    game, found = look.game, look.found
    editable = _editable_saves(found, store)

    if not editable:
        raise SaveSmithError(_nothing_editable(game, found))

    if slot is not None:
        if not 1 <= slot <= len(editable):
            raise SaveSmithError(
                f"There is no save {slot} here; {game.title} has {len(editable)} "
                f"that SaveSmith can read."
            )
        return game, editable[slot - 1][0]

    if len(editable) > 1:
        listing = "\n".join(
            f"  {index}. {path.name}  ({plugin.id}, {path.stat().st_size} bytes)"
            for index, (path, plugin) in enumerate(editable, start=1)
        )
        raise SaveSmithError(
            f"{game.title} has {len(editable)} saves SaveSmith can read:\n{listing}\n\n"
            f"Choose one with --slot, or name the file directly."
        )
    return game, editable[0][0]


def _editable_saves(discovery: Discovery, store: PluginStore) -> list[tuple[Path, Plugin]]:
    """The saves in a folder that some plugin can actually open."""
    editable: list[tuple[Path, Plugin]] = []
    # The player's own saves only. A folder of sixty-two rolling backups would
    # otherwise fill the budget before the save itself was ever opened.
    for save in discovery.player_saves[:_MAX_CANDIDATES]:
        try:
            raw = save.path.read_bytes()
        except OSError:
            continue
        matches = _match(store, raw)
        if matches:
            editable.append((save.path, matches[0]))
    return editable


def _nothing_editable(game: GameFolder, discovery: Discovery) -> str:
    if discovery.prefs is not None and discovery.prefs.entries:
        return (
            f"{game.title} keeps its progress in Unity settings rather than a save file. "
            f"Use 'savesmith prefs --game-folder \"{game.path}\"' to see and change them."
        )
    if discovery.saves:
        return (
            f"{len(discovery.saves)} file(s) were found for {game.title}, but no plugin can "
            f"read any of them. 'savesmith find' lists them, and 'savesmith discover <file>' "
            f"works a format out."
        )
    return (
        f"No save files were found for {game.title}. 'savesmith find' shows every place "
        f"that was looked in."
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

    games = subparsers.add_parser("games", help="every game found on this machine")
    games.set_defaults(handler=_cmd_games)

    find = subparsers.add_parser(
        "find", help="find the saves of a game, given its folder, .exe or .app"
    )
    find.add_argument("folder", metavar="game")
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
    discover.add_argument(
        "--model",
        nargs="?",
        const=DEFAULT_MODEL,
        default=None,
        metavar="NAME",
        help=(
            "if nothing known fits the file, ask a model to write a codec for it. "
            f"Costs money and needs an API key. Defaults to {DEFAULT_MODEL}."
        ),
    )
    discover.add_argument(
        "--budget",
        type=float,
        default=1.0,
        metavar="USD",
        help="the most --model may spend on one file (default: 1.00)",
    )
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

    rpc_command = subparsers.add_parser(
        "rpc", help="serve JSON-RPC on stdin and stdout (used by the window)"
    )
    rpc_command.set_defaults(handler=_cmd_rpc)

    search = subparsers.add_parser(
        "search", help="find where a number lives in a save with no plugin"
    )
    search.add_argument("file", help="a save file, or the game's install folder")
    search.add_argument("value", type=float, help="the number you can see in the game")
    search.add_argument("--slot", type=int, metavar="N", help="which save, if there are several")
    search.add_argument(
        "--type",
        metavar="NAME",
        help="only this way of storing a number, such as uint32-le "
        "(binary saves only; " + ", ".join(compare.ENCODINGS) + ")",
    )
    search.set_defaults(handler=_cmd_search)

    poke = subparsers.add_parser(
        "poke", help="change a number in a save with no plugin, by address"
    )
    poke.add_argument("file", help="a save file, or the game's install folder")
    poke.add_argument("address", help="a path, or an offset such as 0x1F4C:uint32")
    poke.add_argument("value", type=float)
    poke.add_argument("--slot", type=int, metavar="N", help="which save, if there are several")
    poke.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    poke.add_argument(
        "--yes",
        action="store_true",
        help="confirm that nothing is known about this game or this number",
    )
    poke.set_defaults(handler=_cmd_poke)

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

    prefs = subparsers.add_parser(
        "prefs", help="Unity settings, which are not kept in the save file"
    )
    prefs.add_argument("--company", help="the publisher name Unity stores things under")
    prefs.add_argument("--product", help="the product name")
    prefs.add_argument("--game-folder", help="read both names from the game's folder instead")
    prefs.add_argument(
        "--set", nargs=2, metavar=("NAME", "VALUE"), help="change one setting, after backing up"
    )
    prefs.set_defaults(handler=_cmd_prefs)

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
    parser.add_argument("file", help="a save file, or the game's install folder")
    parser.add_argument("--plugin", help="use this plugin instead of guessing")
    parser.add_argument(
        "--slot",
        type=int,
        metavar="N",
        help="which save to use, when the folder holds more than one",
    )
    parser.add_argument(
        "--game-folder",
        help="the game's install folder, so anti-cheat can be checked "
        "(not needed when you pointed at the folder already)",
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
