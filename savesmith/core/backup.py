"""Copies of a save file before it is touched.

Plain files with a timestamp in the name. No archive format of our own: a
player who wants to restore a save without SaveSmith installed, or three years
from now, must be able to drag the file back by hand.

Layout::

    {SAVESMITH_DATA}/backups/<plugin-id>/2026-08-06T18-42-11Z/
        user1.dat
        backup.json

Deliberately **not** next to the save file. Steam Cloud syncs whole folders for
some games, and a folder full of backups would eat the quota, upload junk, and
in the worst case be restored over the real save.

``shutil.copy2`` rather than ``copy``: it keeps modification times and extended
attributes, which on macOS includes the quarantine and Finder metadata a game
may care about.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from savesmith.core import paths
from savesmith.core.errors import BackupError
from savesmith.core.paths import SystemFacade

# Colons are legal in a POSIX filename and forbidden on Windows, so the ISO
# timestamp is written with hyphens.
_STAMP_FORMAT = "%Y-%m-%dT%H-%M-%SZ"
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z(?:-\d+)?$")
_METADATA_NAME = "backup.json"


@dataclass(frozen=True)
class Backup:
    """One saved copy."""

    folder: Path
    file: Path
    original: Path
    created_at: datetime
    plugin_id: str
    size: int

    @property
    def label(self) -> str:
        """What the restore list shows."""
        return self.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")


class BackupStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_system(cls, system: SystemFacade) -> BackupStore:
        base = paths.PathResolver(system).token("SAVESMITH_DATA")
        if base is None:
            raise BackupError(
                "<app data>", "SaveSmith could not work out where to keep its own files."
            )
        return cls(base / "backups")

    # -- writing ---------------------------------------------------------

    def create(self, source: Path, *, plugin_id: str = "unknown") -> Backup:
        """Copy a save file aside. Raises rather than returning a failure.

        Callers treat this as a precondition for writing: no backup, no edit.
        """
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise BackupError(
                str(source),
                "The original file could not be read.",
                detail=f"{source}: {exc.strerror}",
            ) from exc

        created_at = datetime.now(UTC)
        folder = self._unique_folder(plugin_id, created_at)
        try:
            folder.mkdir(parents=True)
            destination = folder / source.name
            shutil.copy2(source, destination)
        except OSError as exc:
            raise BackupError(
                str(source),
                "Check that there is free disk space and that SaveSmith may write "
                "to its own folder.",
                detail=f"{folder}: {exc.strerror}",
            ) from exc

        backup = Backup(
            folder=folder,
            file=destination,
            original=source,
            created_at=created_at,
            plugin_id=plugin_id,
            size=size,
        )
        self._write_metadata(backup)
        return backup

    def _unique_folder(self, plugin_id: str, moment: datetime) -> Path:
        base = self.root / _safe_name(plugin_id) / moment.strftime(_STAMP_FORMAT)
        if not base.exists():
            return base
        # Two edits in the same second are ordinary when a user is trying
        # values out.
        for suffix in range(2, 1000):
            candidate = base.with_name(f"{base.name}-{suffix}")
            if not candidate.exists():
                return candidate
        raise BackupError(str(base), "Too many backups were made in the same second.")

    def _write_metadata(self, backup: Backup) -> None:
        payload = {
            "original": str(backup.original),
            "file": backup.file.name,
            "created_at": backup.created_at.isoformat(),
            "plugin_id": backup.plugin_id,
            "size": backup.size,
        }
        try:
            (backup.folder / _METADATA_NAME).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            # The copy itself is what matters; a missing note only costs the
            # restore list some context.
            raise BackupError(
                str(backup.original),
                "The backup copy was made but its description could not be written.",
                detail=f"{backup.folder}: {exc.strerror}",
            ) from exc

    # -- reading ---------------------------------------------------------

    def list_for(self, plugin_id: str, *, original: Path | None = None) -> list[Backup]:
        """Newest first. Unreadable folders are skipped, not fatal."""
        folder = self.root / _safe_name(plugin_id)
        if not folder.is_dir():
            return []
        found: list[Backup] = []
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name, reverse=True)
        except OSError:
            return []
        for entry in entries:
            if not entry.is_dir() or not _STAMP_RE.match(entry.name):
                continue
            backup = self._read(Path(entry.path))
            if backup is None:
                continue
            if original is not None and backup.original != original:
                continue
            found.append(backup)
        return found

    def _read(self, folder: Path) -> Backup | None:
        try:
            payload = json.loads((folder / _METADATA_NAME).read_text(encoding="utf-8"))
            file = folder / str(payload["file"])
            return Backup(
                folder=folder,
                file=file,
                original=Path(str(payload["original"])),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
                plugin_id=str(payload.get("plugin_id", "unknown")),
                size=int(payload.get("size", 0)),
            )
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return None

    # -- restoring -------------------------------------------------------

    def restore(self, backup: Backup, *, target: Path | None = None) -> Backup:
        """Put a backup back, after backing up what is there now.

        Restoring overwrites a file, so it is a write like any other and gets
        the same protection. The returned value is the safety copy of what was
        replaced — without it, one misclick would lose the newer save.
        """
        destination = target or backup.original
        if not backup.file.is_file():
            raise BackupError(str(backup.file), "The backup copy is missing from disk.")

        replaced = self.create(destination, plugin_id=backup.plugin_id)
        try:
            shutil.copy2(backup.file, destination)
        except OSError as exc:
            raise BackupError(
                str(destination),
                "The backup could not be copied back into place.",
                detail=f"{destination}: {exc.strerror}",
            ) from exc
        return replaced

    def prune(self, plugin_id: str, *, keep: int) -> list[Backup]:
        """Delete all but the newest ``keep`` backups. Returns what went.

        Never called automatically in milestone 2: deleting someone's only
        copy of a save to save a few kilobytes is not a trade SaveSmith gets to
        make on its own.
        """
        if keep < 1:
            raise ValueError("keep must be at least 1")
        removed = []
        for backup in self.list_for(plugin_id)[keep:]:
            shutil.rmtree(backup.folder, ignore_errors=True)
            removed.append(backup)
        return removed


def _safe_name(text: str) -> str:
    """A plugin id is already restricted, but ids also arrive from files."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", text).strip("._")
    return cleaned or "unknown"
