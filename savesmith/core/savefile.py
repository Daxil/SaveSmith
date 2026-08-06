"""One save file, opened through one plugin.

This is the object the CLI and the GUI both work with, and the only place that
writes to a player's save. Three rules are built into its shape rather than
left to the caller's discipline:

* **No write without a backup.** ``write`` takes a :class:`BackupStore` as a
  required argument and makes the copy before it touches anything. There is no
  code path that writes a save file without one, which is what the milestone's
  definition of done asks for.
* **No silent writes.** Changes accumulate as a visible list and are applied
  only when ``write`` is called.
* **Fields tied to online services are never editable**, and fields that feed
  achievements need an explicit opt-in. Both are refused here, in the core, not
  only greyed out in the interface.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from savesmith.core import fields as field_access
from savesmith.core.backup import Backup, BackupStore
from savesmith.core.errors import (
    FieldPathError,
    FieldValueError,
    SaveInUseError,
    SaveSmithError,
)
from savesmith.core.pipeline import Decoded
from savesmith.core.plugin import FieldSpec, Plugin


@dataclass(frozen=True)
class Change:
    address: str
    before: Any
    after: Any


@dataclass(frozen=True)
class FieldView:
    """A field as it exists in this particular save.

    ``present`` is false when the plugin describes a value this save does not
    have — a different game version, or a player who has not reached that part
    yet. The editor shows it as unavailable rather than pretending.
    """

    spec: FieldSpec
    present: bool
    value: Any


class SaveFile:
    def __init__(self, path: Path, plugin: Plugin, decoded: Decoded, raw: bytes) -> None:
        self.path = path
        self.plugin = plugin
        self._decoded = decoded
        self._original_bytes = raw
        self._changes: dict[str, Change] = {}

    @classmethod
    def open(cls, path: Path, plugin: Plugin) -> SaveFile:
        try:
            raw = path.read_bytes()
        except PermissionError as exc:
            raise SaveInUseError(str(path), detail=str(exc)) from exc
        except OSError as exc:
            raise SaveSmithError(
                f"This save file could not be opened: {exc.strerror}.",
                detail=f"{path}: {exc}",
            ) from exc
        return cls(path, plugin, plugin.pipeline.decode(raw), raw)

    @property
    def data(self) -> Any:
        """The decoded structure. The advanced view shows this raw."""
        return self._decoded.value

    @property
    def pending(self) -> tuple[Change, ...]:
        return tuple(self._changes.values())

    @property
    def modified(self) -> bool:
        return bool(self._changes)

    # -- reading ---------------------------------------------------------

    def view(self) -> list[FieldView]:
        """Every field the plugin lists, in the author's order."""
        views = []
        for spec in self.plugin.fields:
            try:
                value = field_access.read(self._decoded.value, spec.path)
            except FieldPathError:
                views.append(FieldView(spec=spec, present=False, value=None))
            else:
                views.append(FieldView(spec=spec, present=True, value=value))
        return views

    def read(self, address: str) -> Any:
        return field_access.read(self._decoded.value, self._spec(address).path)

    # -- editing ---------------------------------------------------------

    def set(self, address: str, value: Any, *, allow_achievements: bool = False) -> Change:
        """Stage one change. Nothing reaches the disk until ``write``."""
        spec = self._spec(address)
        label = spec.label.get()

        if spec.online_linked:
            raise FieldValueError(
                label,
                "this value is shared with an online service, so SaveSmith will not "
                "change it. Editing it risks the account, not just the save.",
            )
        if spec.achievement and not allow_achievements:
            raise FieldValueError(
                label,
                "this value feeds achievements or statistics. Turn on achievement "
                "editing in the advanced settings if you are sure.",
            )

        coerced = spec.coerce(value)
        before = field_access.read(self._decoded.value, spec.path)
        field_access.write(self._decoded.value, spec.path, coerced)
        change = Change(address=spec.address, before=before, after=coerced)
        self._changes[spec.address] = change
        return change

    def revert(self, address: str) -> None:
        change = self._changes.pop(address, None)
        if change is None:
            return
        field_access.write(self._decoded.value, self._spec(address).path, change.before)

    def revert_all(self) -> None:
        for address in list(self._changes):
            self.revert(address)

    def _spec(self, address: str) -> FieldSpec:
        spec = self.plugin.field(address)
        if spec is None:
            raise FieldPathError(address, "this plugin does not describe such a field.")
        return spec

    # -- writing ---------------------------------------------------------

    def write(self, backups: BackupStore, *, force: bool = False) -> Backup:
        """Back up, then replace the save file. Returns the backup.

        ``force`` skips the check that the file has not changed underneath us.
        That check exists because the game writing its own save while SaveSmith
        holds an older copy is the one way a well-meaning edit destroys hours
        of progress.
        """
        if not force:
            self._assert_unchanged_on_disk()

        backup = backups.create(self.path, plugin_id=self.plugin.id)
        payload = self.plugin.pipeline.encode(self._decoded.value, self._decoded.hints)
        self._replace_contents(payload)
        self._original_bytes = payload
        self._changes.clear()
        return backup

    def _assert_unchanged_on_disk(self) -> None:
        try:
            current = self.path.read_bytes()
        except PermissionError as exc:
            raise SaveInUseError(str(self.path), detail=str(exc)) from exc
        except OSError as exc:
            raise SaveSmithError(
                f"This save file could not be re-read before writing: {exc.strerror}.",
                detail=f"{self.path}: {exc}",
            ) from exc
        if current != self._original_bytes:
            raise SaveSmithError(
                "The game has saved over this file since SaveSmith opened it. "
                "Nothing was changed. Close the game, then open the save again.",
                detail=f"{self.path} changed on disk",
            )

    def _replace_contents(self, payload: bytes) -> None:
        """Write through a temporary file in the same folder, then rename.

        A half-written save is worse than no edit at all, and writing in place
        leaves one behind if the machine loses power mid-write. The rename is
        atomic on both Windows and macOS.
        """
        folder = self.path.parent
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=".savesmith-", suffix=".tmp")
        temporary_path = Path(temporary)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Carry over permissions and timestamps so the game sees the file
            # it expects.
            shutil.copystat(self.path, temporary_path)
            temporary_path.replace(self.path)
        except PermissionError as exc:
            temporary_path.unlink(missing_ok=True)
            raise SaveInUseError(str(self.path), detail=str(exc)) from exc
        except OSError as exc:
            temporary_path.unlink(missing_ok=True)
            raise SaveSmithError(
                f"The save file could not be written: {exc.strerror}. "
                f"Your backup is untouched.",
                detail=f"{self.path}: {exc}",
            ) from exc
