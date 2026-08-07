"""What SaveSmith can see on this machine.

Serves three audiences with one output:

* the CI smoke job, which proves the native Windows calls survive packaging;
* a user reporting "it cannot find my saves", who can paste this instead of
  being interviewed;
* us, when that report arrives.

Run it with ``uv run python -m savesmith.core.diagnostics``.

It only reads. No file is opened for writing, and no game data is printed —
only locations, so the output is safe to paste into a public issue.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from savesmith.core.console import use_utf8
from savesmith.core.errors import SaveSmithError, SteamNotFoundError
from savesmith.core.paths import PathResolver, RealSystem, SystemFacade
from savesmith.core.platform_ import Platform
from savesmith.core.steam import SteamInstall, SteamScan
from savesmith.core.wine import WinePrefix, scan_prefixes

_MISSING = "— not available on this platform"
# WINEUSER is not missing, it is contextual: it only has a value while
# scanning inside a bottle. Saying "not available" would read like a fault.
_CONTEXTUAL = {"WINEUSER": "— only has a value inside a Wine bottle"}


@dataclass
class Report:
    platform: Platform
    supported: bool
    home: Path
    username: str
    tokens: dict[str, Path | None] = field(default_factory=dict)
    steam: SteamScan | None = None
    steam_error: SaveSmithError | None = None
    prefixes: list[WinePrefix] = field(default_factory=list)


def collect(system: SystemFacade | None = None) -> Report:
    """Gather everything, degrading rather than failing.

    A machine with no Steam, no bottles and an unsupported OS still produces a
    report — that is the situation where one is most needed.
    """
    system = system or RealSystem()
    resolver = PathResolver(system)

    report = Report(
        platform=system.platform,
        supported=system.platform.is_supported,
        home=system.home(),
        username=system.username(),
        tokens=resolver.all_tokens(),
    )

    try:
        report.steam = SteamInstall.discover(system).scan()
    except SteamNotFoundError as exc:
        report.steam_error = exc

    report.prefixes = scan_prefixes(system)
    return report


def render(report: Report) -> str:
    lines: list[str] = [
        "SaveSmith diagnostics",
        "=" * 60,
        f"Platform : {report.platform.display_name}"
        + ("" if report.supported else "  (not supported)"),
        f"User     : {report.username}",
        f"Home     : {report.home}",
        "",
        "Path tokens",
        "-" * 60,
    ]
    for name, path in report.tokens.items():
        if path is None:
            lines.append(f"  {name:<16} {_CONTEXTUAL.get(name, _MISSING)}")
        else:
            mark = "ok     " if path.exists() else "missing"
            lines.append(f"  {name:<16} [{mark}] {path}")

    lines += ["", "Steam", "-" * 60]
    if report.steam_error is not None:
        lines.append(f"  {report.steam_error.user_message}")
    elif report.steam is not None:
        scan = report.steam
        lines.append(f"  Install: {scan.root}")
        lines.append(f"  Libraries: {len(scan.libraries)}, games: {len(scan.games)}")
        for library in scan.libraries:
            state = "ok" if library.available else "unavailable"
            label = f" ({library.label})" if library.label else ""
            lines.append(f"    [{state}] {library.path}{label}")
        for user in scan.users:
            lines.append(f"    account {user.account_id}: {user.path}")
        for problem in scan.problems:
            lines.append(f"    problem: {problem.user_message}")

    lines += ["", "Windows bottles (Whisky / CrossOver / Wine)", "-" * 60]
    if not report.prefixes:
        note = "none found" if report.platform is Platform.MACOS else "not applicable here"
        lines.append(f"  {note}")
    for prefix in report.prefixes:
        users = ", ".join(prefix.users) or "no profiles"
        lines.append(f"  {prefix.name} [{prefix.kind.value}] {prefix.path}")
        lines.append(f"    profiles: {users}")

    return "\n".join(lines)


def main() -> int:
    # Its own entry point, so its own encoding fix: the CI smoke job runs this
    # module directly, and a Russian Windows console has no em dash either.
    use_utf8(sys.stdout, sys.stderr)
    report = collect()
    print(render(report))
    # Always zero: an unsupported platform or a missing Steam is information,
    # not a failure, and the CI smoke job only checks that this runs at all.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
