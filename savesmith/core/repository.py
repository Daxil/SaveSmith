"""The collection of plugins, and the gate that decides what they may claim.

Two jobs.

**Loading.** Plugins are folders with a ``manifest.json``. One broken plugin
must not take the rest down with it, so failures are collected and reported
rather than raised.

**The gate.** A plugin may only claim to work if it can rebuild a real save
byte for byte. The check runs over a corpus, and its result can only ever
*lower* what the manifest claims — never raise it. Nobody's optimism about
their own plugin gets to set the confidence a stranger sees.

``verified`` in particular cannot be earned here at all. It means a human
started the game and confirmed the edited save loads, and no amount of
byte-comparison substitutes for that.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from savesmith.core.errors import PluginValidationError, SaveSmithError
from savesmith.core.pipeline import RoundTrip
from savesmith.core.plugin import MANIFEST_NAME, Confidence, Plugin


@dataclass(frozen=True)
class FileResult:
    path: Path
    round_trip: RoundTrip | None
    error: SaveSmithError | None = None

    @property
    def passed(self) -> bool:
        return self.round_trip is not None and self.round_trip.exact_bytes

    def describe(self) -> str:
        if self.error is not None:
            return f"{self.path.name}: could not be read — {self.error.user_message}"
        assert self.round_trip is not None
        return f"{self.path.name}: {'exact' if self.passed else self.round_trip.detail}"


@dataclass
class Verification:
    """What a plugin proved on a corpus of real saves."""

    plugin: Plugin
    results: list[FileResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Every file rebuilt exactly. An empty corpus proves nothing."""
        return bool(self.results) and all(result.passed for result in self.results)

    @property
    def confidence(self) -> Confidence:
        """What the plugin may claim after being checked.

        The gate lowers and never raises. A plugin that fails on even one file
        is experimental whatever its manifest says.

        Passing does not promote anything: ``verified`` stays the author's
        assertion that a person started the game and watched the edited save
        load. Byte comparison cannot establish that, so it neither grants it
        nor takes it away.
        """
        return self.plugin.confidence if self.passed else Confidence.EXPERIMENTAL

    @property
    def publishable(self) -> bool:
        """Whether this may go into the shared plugin repository."""
        return self.confidence is not Confidence.EXPERIMENTAL

    def explain(self) -> list[str]:
        lines = [f"{self.plugin.id} v{self.plugin.version} — {self.plugin.game}"]
        if not self.results:
            lines.append("  no corpus files, so nothing was proved")
        lines += [f"  {result.describe()}" for result in self.results]
        lines.append(
            f"  claimed: {self.plugin.confidence.value} → allowed: {self.confidence.value}"
        )
        return lines


def verify(plugin: Plugin, files: list[Path]) -> Verification:
    """Run the round-trip gate for one plugin over a set of real saves."""
    verification = Verification(plugin=plugin)
    for path in sorted(files):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            verification.results.append(
                FileResult(
                    path=path,
                    round_trip=None,
                    error=SaveSmithError(
                        f"The corpus file could not be read: {exc.strerror}.",
                        detail=str(path),
                    ),
                )
            )
            continue
        try:
            verification.results.append(
                FileResult(path=path, round_trip=plugin.pipeline.round_trip(raw))
            )
        except SaveSmithError as exc:
            verification.results.append(FileResult(path=path, round_trip=None, error=exc))
    return verification


@dataclass
class Catalogue:
    plugins: list[Plugin] = field(default_factory=list)
    problems: list[PluginValidationError] = field(default_factory=list)

    def by_id(self, plugin_id: str) -> Plugin | None:
        return next((plugin for plugin in self.plugins if plugin.id == plugin_id), None)

    def by_appid(self, appid: int) -> list[Plugin]:
        return [plugin for plugin in self.plugins if plugin.steam_appid == appid]


class PluginRepository:
    """A folder of plugin folders."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self) -> Catalogue:
        """Read every plugin. A broken one is reported, not fatal."""
        catalogue = Catalogue()
        try:
            with os.scandir(self.root) as scan:
                folders = sorted(entry.path for entry in scan if entry.is_dir())
        except OSError:
            return catalogue

        for folder in folders:
            path = Path(folder)
            if not (path / MANIFEST_NAME).is_file():
                continue
            try:
                catalogue.plugins.append(Plugin.load(path))
            except PluginValidationError as exc:
                catalogue.problems.append(exc)
        return catalogue

    def match(self, raw: bytes, *, appid: int | None = None) -> list[Plugin]:
        """Plugins whose pipeline can actually open this file, best first.

        Opening the file is the test, rather than the file name: a plugin that
        claims a format but cannot read it is no use, and one that reads it is
        worth offering even if the game was installed somewhere unexpected.
        """
        catalogue = self.load()
        scored: list[tuple[tuple[int, int, str], Plugin]] = []
        for plugin in catalogue.plugins:
            if plugin.detect.magic_hex and not raw.startswith(
                bytes.fromhex(plugin.detect.magic_hex)
            ):
                continue
            try:
                plugin.pipeline.decode(raw)
            except SaveSmithError:
                continue
            scored.append(
                (
                    (
                        0 if appid is not None and plugin.steam_appid == appid else 1,
                        _confidence_rank(plugin.confidence),
                        plugin.id,
                    ),
                    plugin,
                )
            )
        scored.sort(key=lambda item: item[0])
        return [plugin for _key, plugin in scored]


def _confidence_rank(confidence: Confidence) -> int:
    return {
        Confidence.VERIFIED: 0,
        Confidence.PROBABLE: 1,
        Confidence.EXPERIMENTAL: 2,
    }[confidence]


def bundled() -> PluginRepository:
    """The plugins shipped with this build.

    Milestone 8 moves these to a repository of their own that updates without
    the app; until then everything reads them through here, so that move is a
    change of path rather than a change of code.
    """
    return PluginRepository(Path(__file__).resolve().parent.parent.parent / "plugins")
