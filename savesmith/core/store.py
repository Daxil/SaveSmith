"""Installing plugins, and not getting hurt by them.

A plugin arrives as an archive from somebody else's computer, so it is treated
as hostile input until proven otherwise:

* **No path escapes.** An entry called ``../../.ssh/authorized_keys`` is the
  oldest trick against archive extraction and it still works on plenty of
  software. Every name is checked before anything is written.
* **No bombs.** A few kilobytes can expand into gigabytes. Both the number of
  entries and the total unpacked size are capped.
* **Nothing unexpected.** Only a manifest, an optional codec, and a few plain
  data files. No executables, no symlinks, no directories full of surprises.
* **Validated before installed.** The manifest is parsed and checked while it
  is still in a temporary folder. A plugin that would not load never lands in
  the user's plugin directory at all.

Installing does not make a plugin trusted — ``codec.py`` still only ever runs
in the sandbox. This layer's job is that unpacking one cannot hurt anybody.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from savesmith.core.errors import PluginValidationError, SaveSmithError
from savesmith.core.paths import PathResolver, SystemFacade
from savesmith.core.plugin import MANIFEST_NAME, Plugin
from savesmith.core.repository import Catalogue, PluginRepository, bundled

MAX_ENTRIES = 64
MAX_UNPACKED_BYTES = 16 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 8 * 1024 * 1024

# Everything a plugin is allowed to contain.
ALLOWED_NAMES = frozenset({MANIFEST_NAME, "codec.py", "README.md", "LICENSE", "risk_db.json"})
ALLOWED_SUFFIXES = frozenset({".json", ".md", ".py", ".txt"})


class PluginInstallError(SaveSmithError):
    """An archive could not be installed as a plugin."""

    code = "plugin_install_failed"

    def __init__(self, name: str, reason: str, *, detail: str | None = None) -> None:
        super().__init__(
            f"The plugin '{name}' was not installed: {reason}",
            detail=detail or f"{name}: {reason}",
            plugin=name,
        )


class Fetcher(Protocol):
    """Whatever brings bytes back from a URL.

    A protocol so that installing from the internet is testable without any
    network, and so the network code stays in one replaceable place.
    """

    def fetch(self, url: str) -> bytes: ...


@dataclass(frozen=True)
class InstallResult:
    plugin: Plugin
    folder: Path
    replaced: int | None = None
    """The version that was there before, if this was an update."""

    @property
    def updated(self) -> bool:
        return self.replaced is not None

    def describe(self) -> str:
        if self.replaced is None:
            return f"installed {self.plugin.id} v{self.plugin.version}"
        return f"updated {self.plugin.id} from v{self.replaced} to v{self.plugin.version}"


class PluginStore:
    """The user's own plugin folder, alongside the ones shipped with the app."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_system(cls, system: SystemFacade) -> PluginStore:
        base = PathResolver(system).token("SAVESMITH_DATA")
        if base is None:
            raise SaveSmithError(
                "SaveSmith could not work out where to keep its own files.",
                detail="SAVESMITH_DATA unavailable",
            )
        return cls(base / "plugins")

    # -- reading ---------------------------------------------------------

    def installed(self) -> Catalogue:
        return PluginRepository(self.root).load()

    def catalogue(self) -> Catalogue:
        """Everything available: installed plugins first, then the bundled ones.

        A user's copy of a plugin wins over the one shipped with the app, so an
        update can be installed without waiting for a release.
        """
        combined = self.installed()
        seen = {plugin.id for plugin in combined.plugins}
        for plugin in bundled().load().plugins:
            if plugin.id not in seen:
                combined.plugins.append(plugin)
        return combined

    def version_of(self, plugin_id: str) -> int | None:
        plugin = self.installed().by_id(plugin_id)
        return plugin.version if plugin else None

    # -- installing ------------------------------------------------------

    def install_folder(self, folder: Path, *, allow_downgrade: bool = False) -> InstallResult:
        """Install from a plugin folder that is already on disk."""
        plugin = self._validate(folder, folder.name)
        return self._place(plugin, folder, allow_downgrade=allow_downgrade)

    def install_archive(
        self, data: bytes, *, name: str = "plugin", allow_downgrade: bool = False
    ) -> InstallResult:
        """Install from a zip archive, checking it before anything is written."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="savesmith-plugin-") as workspace:
            staging = Path(workspace) / "unpacked"
            staging.mkdir()
            _unpack(data, staging, name)
            folder = _plugin_root(staging, name)
            plugin = self._validate(folder, name)
            return self._place(plugin, folder, allow_downgrade=allow_downgrade)

    def install_from(
        self, url: str, fetcher: Fetcher, *, allow_downgrade: bool = False
    ) -> InstallResult:
        try:
            data = fetcher.fetch(url)
        except Exception as exc:  # the fetcher is somebody else's code
            raise PluginInstallError(url, "it could not be downloaded", detail=str(exc)) from exc
        return self.install_archive(data, name=url.rsplit("/", 1)[-1],
                                    allow_downgrade=allow_downgrade)

    def _validate(self, folder: Path, name: str) -> Plugin:
        try:
            return Plugin.load(folder)
        except PluginValidationError as exc:
            raise PluginInstallError(
                name, "it is not a valid plugin", detail=exc.user_message
            ) from exc

    def _place(self, plugin: Plugin, folder: Path, *, allow_downgrade: bool) -> InstallResult:
        existing = self.version_of(plugin.id)
        if existing is not None and plugin.version < existing and not allow_downgrade:
            raise PluginInstallError(
                plugin.id,
                f"the installed version {existing} is newer than the version "
                f"{plugin.version} being installed",
            )

        target = self.root / plugin.id
        self.root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(folder, target)
        # Re-read from where it now lives, so `source` points at the real file.
        return InstallResult(plugin=Plugin.load(target), folder=target, replaced=existing)

    def remove(self, plugin_id: str) -> bool:
        target = self.root / plugin_id
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        return True

    # -- exporting -------------------------------------------------------

    def export(self, plugin_id: str, target: Path) -> Path:
        """Write a plugin out as a zip somebody else can install."""
        catalogue = self.catalogue()
        plugin = catalogue.by_id(plugin_id)
        if plugin is None or plugin.source is None:
            raise PluginInstallError(plugin_id, "there is no such plugin to export")

        folder = plugin.source.parent
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(folder.iterdir()):
                if path.is_file() and _name_allowed(path.name):
                    archive.write(path, arcname=f"{plugin.id}/{path.name}")
        return target


def _unpack(data: bytes, destination: Path, name: str) -> None:
    """Extract an archive, refusing anything that tries to misbehave."""
    try:
        archive = zipfile.ZipFile(_as_stream(data))
    except zipfile.BadZipFile as exc:
        raise PluginInstallError(name, "it is not a readable archive", detail=str(exc)) from exc

    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise PluginInstallError(
                name, f"it contains {len(entries)} files, more than the {MAX_ENTRIES} allowed"
            )

        total = 0
        for entry in entries:
            if entry.is_dir():
                continue
            _check_name(entry.filename, name)
            if entry.file_size > MAX_SINGLE_FILE_BYTES:
                raise PluginInstallError(name, f"'{entry.filename}' is larger than allowed")
            total += entry.file_size
            if total > MAX_UNPACKED_BYTES:
                raise PluginInstallError(
                    name, "it unpacks to more data than a plugin could reasonably need"
                )

        for entry in entries:
            if entry.is_dir():
                continue
            target = destination / entry.filename
            # Resolved and re-checked: the name test above is belt, this is braces.
            if not _inside(destination, target):
                raise PluginInstallError(name, f"'{entry.filename}' points outside the folder")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, length=64 * 1024)


def _as_stream(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)


def _check_name(filename: str, plugin: str) -> None:
    path = Path(filename)
    # `anchor`, not `is_absolute()`: on Windows a path like "/etc/passwd" has
    # no drive, so is_absolute() is False while the path is still rooted — and
    # that is exactly the entry an attacker puts in an archive. A backslash is
    # also suspicious in its own right, since zip entries use forward slashes.
    rooted = bool(path.anchor) or filename.startswith(("/", "\\"))
    if rooted or ".." in path.parts or "\\" in filename:
        raise PluginInstallError(plugin, f"'{filename}' tries to escape the plugin folder")
    if len(path.parts) > 2:
        raise PluginInstallError(plugin, f"'{filename}' is nested deeper than a plugin should be")
    if not _name_allowed(path.name):
        raise PluginInstallError(
            plugin,
            f"'{path.name}' is not something a plugin may contain; allowed: "
            + ", ".join(sorted(ALLOWED_NAMES)),
        )


def _name_allowed(name: str) -> bool:
    return name in ALLOWED_NAMES or Path(name).suffix in ALLOWED_SUFFIXES


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _plugin_root(staging: Path, name: str) -> Path:
    """Find the folder holding manifest.json, whether or not the zip nested it."""
    if (staging / MANIFEST_NAME).is_file():
        return staging
    candidates = [child for child in staging.iterdir() if (child / MANIFEST_NAME).is_file()]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise PluginInstallError(name, f"there is no {MANIFEST_NAME} in the archive")
    raise PluginInstallError(name, "the archive contains more than one plugin")


def describe_limits() -> Iterable[str]:
    """What an archive is held to, for the log and for anyone auditing it."""
    return (
        f"at most {MAX_ENTRIES} files",
        f"at most {MAX_UNPACKED_BYTES // (1024 * 1024)} MB unpacked",
        "no absolute paths, no '..', no nesting deeper than one folder",
        "only: " + ", ".join(sorted(ALLOWED_NAMES)),
        "codec.py is installed but only ever runs in the sandbox",
    )
