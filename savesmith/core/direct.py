"""Editing a save nobody has written a plugin for yet.

A plugin is the good outcome: it names the fields, knows which ones feed
achievements, and carries a risk tier. Writing one takes a person. Meanwhile a
player with 12,400 runes and no plugin for their game is stuck, and the whole
premise of SaveSmith is that they should not be.

So: the decoder ladder opens the file, the player says which number they are
looking at on screen, and SaveSmith says where that number lives. Then they
change it there, and everything that was decoded on the way in is rebuilt on the
way out — compression, encryption, checksums, all of it — because the save is
written by running the same pipeline backwards.

Two shapes come out of the ladder and both are handled:

* **A structure** (JSON, GVAS, MessagePack) — the number is at a path such as
  ``playerData.geo``, and that is what the player is shown.
* **Bytes** (a decrypted FromSoftware slot, an unknown binary) — the number is
  at an offset in a particular encoding, written ``0x1F4C:uint32``.

This is deliberately more dangerous than editing through a plugin, and the
danger is not hidden: nothing is written without a backup, the change is
re-read out of the rebuilt file before it is allowed anywhere near the save, and
the caller has to confirm. What is *not* done is guessing which of forty
candidate offsets the player meant. That guess is the one that corrupts saves,
and only the player can make it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from savesmith.core import compare, detect
from savesmith.core import fields as field_access
from savesmith.core.backup import Backup, BackupStore
from savesmith.core.errors import FieldValueError, SaveSmithError
from savesmith.core.pipeline import Pipeline

_MAX_REPORTED = 200


class NoFormatError(SaveSmithError):
    code = "format_unknown"

    def __init__(self, path: Path) -> None:
        super().__init__(
            "SaveSmith could not work out the format of this file, so it cannot say "
            "where a number inside it lives. Try 'savesmith discover' on it first.",
            detail=str(path),
        )


class AddressError(SaveSmithError):
    code = "bad_address"


@dataclass(frozen=True)
class Site:
    """One place a number could be, and the address that names it."""

    address: str
    """Exactly what to pass back to change it — a path, or ``0x1F4C:uint32``."""

    value: float
    context: str = ""
    """Anything that helps tell two candidates apart, such as the field around it."""

    def __str__(self) -> str:
        where = f"   ({self.context})" if self.context else ""
        return f"{self.address} = {_plain(self.value)}{where}"


@dataclass(frozen=True)
class Leaf:
    """One value in a decoded save, named the way the game named it.

    No plugin is involved. A GVAS save writes ``ObjectiveTime`` and
    ``CheckpointName`` into the file itself, RPG Maker writes ``party._gold``;
    those are the developer's own names, not a guess. Listing them is the
    difference between "here is your save" and "type a number and hope".
    """

    address: str
    """What to pass back to change it."""

    name: str
    """The last step of the path — the field's own name."""

    group: str
    """Everything before it, so the interface can bucket what belongs together."""

    value: Any
    kind: str
    """number, text, flag or list — what the interface should draw."""

    @property
    def editable(self) -> bool:
        """Numbers only, for now: that is all ``change`` will write."""
        return self.kind == "number"


# A save with more values than this is a database, and a list that long helps
# nobody. The cap is generous enough that no ordinary save reaches it.
_MAX_FIELDS = 2000


@dataclass
class DirectSave:
    """A save opened without a plugin, through the decoder ladder."""

    path: Path
    raw: bytes
    pipeline: Pipeline | None
    value: Any
    """Whatever the pipeline produced: a structure, or bytes."""

    hints: list[dict[str, Any]] = field(default_factory=list)
    """What each decoding step recorded so the same file can be rebuilt."""

    description: str = "the file as it is on disk"
    changes: list[tuple[str, Any, Any]] = field(default_factory=list)

    @classmethod
    def open(cls, path: Path, *, max_depth: int = 3) -> DirectSave:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SaveSmithError(
                f"This file could not be read: {exc.strerror}.", detail=f"{path}: {exc}"
            ) from exc

        report = detect.identify(raw, max_depth=max_depth)
        best = _most_decoded(report)
        if best is None:
            # Not knowing the format is not a reason to refuse: plenty of saves
            # are plain binary with nothing wrapped around them, and searching
            # those bytes works perfectly well.
            return cls(path=path, raw=raw, pipeline=None, value=raw)
        decoded = best.pipeline.decode(raw)
        return cls(
            path=path,
            raw=raw,
            pipeline=best.pipeline,
            value=decoded.value,
            hints=decoded.hints,
            description=best.description,
        )

    @property
    def is_structured(self) -> bool:
        return isinstance(self.value, dict | list)

    # -- reading ---------------------------------------------------------

    def fields(self) -> list[Leaf]:
        """Every value in the save, with the name the game gave it.

        Only for structured saves. A file that decoded to bytes has no names in
        it to read, and inventing some would be worse than admitting it.
        """
        if not self.is_structured:
            return []
        return _leaves(self.value)

    # -- finding ---------------------------------------------------------

    def search(self, wanted: float, *, encoding: str | None = None) -> list[Site]:
        """Everywhere this number appears in the decoded save.

        ``encoding`` narrows a binary search to one way of storing a number,
        for when the player already knows which reading was the right one.
        """
        if self.is_structured:
            return _search_structure(self.value, wanted)
        return _search_bytes(_as_bytes(self.value), wanted, encoding=encoding)

    # -- changing --------------------------------------------------------

    def change(self, address: str, wanted: float) -> tuple[Any, Any]:
        """Set one number, in memory. Returns what it was and what it is now."""
        if self.is_structured:
            return self._change_path(address, wanted)
        return self._change_offset(address, wanted)

    def _change_path(self, address: str, wanted: float) -> tuple[Any, Any]:
        before = field_access.read(self.value, address)
        if isinstance(before, bool) or not isinstance(before, int | float):
            raise FieldValueError(
                address,
                f"holds {before!r}, which is not a number. SaveSmith only changes numbers "
                f"in a save it has no plugin for.",
            )
        # An integer stays an integer: writing 500.0 where the game wrote 500
        # changes the file more than the player asked for, and some formats
        # cannot even express the difference.
        keep_integer = isinstance(before, int) and float(wanted).is_integer()
        after: float = int(wanted) if keep_integer else wanted
        field_access.write(self.value, address, after)
        self.changes.append((address, before, after))
        return before, after

    def _change_offset(self, address: str, wanted: float) -> tuple[Any, Any]:
        offset, encoding = parse_address(address)
        layout = compare.layout_for(encoding)
        payload = bytearray(_as_bytes(self.value))
        size = struct.calcsize(layout)
        if offset + size > len(payload):
            raise AddressError(
                f"Offset 0x{offset:X} is past the end of this save "
                f"({len(payload)} bytes)."
            )
        before = struct.unpack_from(layout, payload, offset)[0]
        packed: float | int = wanted
        if layout[-1] not in "fd":
            if not float(wanted).is_integer():
                raise FieldValueError(
                    address, f"is stored as {encoding}, which cannot hold {wanted}"
                )
            packed = int(wanted)
        try:
            struct.pack_into(layout, payload, offset, packed)
        except (struct.error, OverflowError) as exc:
            raise FieldValueError(
                address, f"cannot hold {_plain(wanted)} as {encoding}"
            ) from exc
        self.value = bytes(payload)
        self.changes.append((address, before, packed))
        return before, packed

    # -- writing ---------------------------------------------------------

    def rebuild(self) -> bytes:
        """The whole file, with the changes in it and every wrapper put back."""
        if self.pipeline is None:
            return _as_bytes(self.value)
        return self.pipeline.encode(self.value, self.hints)

    def write(self, backups: BackupStore, *, plugin_id: str = "direct") -> Backup:
        """Back the save up, rebuild it, check the change survived, then write.

        The check is the point. Rebuilding runs compression, encryption and
        checksums in reverse, and any of them can quietly drop an edit. Reading
        the value back out of the finished bytes is the only way to know it did
        not.
        """
        if not self.changes:
            raise SaveSmithError("Nothing was changed, so there is nothing to write.")

        rebuilt = self.rebuild()
        self._verify(rebuilt)

        backup = backups.create(self.path, plugin_id=plugin_id)
        temporary = self.path.with_name(self.path.name + ".savesmith-new")
        try:
            temporary.write_bytes(rebuilt)
            temporary.replace(self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise SaveSmithError(
                f"The save could not be written: {exc.strerror}. "
                f"Nothing was changed; the backup is at {backup.folder}.",
                detail=f"{self.path}: {exc}",
            ) from exc
        return backup

    def _verify(self, rebuilt: bytes) -> None:
        if self.pipeline is None:
            return
        try:
            reread = self.pipeline.decode(rebuilt).value
        except SaveSmithError as exc:
            raise SaveSmithError(
                "The edited save could not be read back, so it was not written. "
                "Nothing on disk has changed.",
                detail=f"{self.path}: {exc.detail or exc.user_message}",
            ) from exc

        for address, _before, after in self.changes:
            actual = (
                field_access.read(reread, address)
                if self.is_structured
                else _read_offset(_as_bytes(reread), address)
            )
            if actual != after:
                raise SaveSmithError(
                    "The change did not survive rebuilding the save, so nothing was "
                    "written. This save's format needs a plugin rather than a direct edit.",
                    detail=f"{address}: expected {after!r}, read back {actual!r}",
                )


def _most_decoded(report: detect.Report) -> detect.Candidate | None:
    """The candidate that peeled off the most layers, not the shortest one.

    ``report.best`` is right for identifying a file: the simplest pipeline that
    explains it. For editing it is the wrong end of the list. An Elden Ring
    save is a BND4 archive of AES-encrypted slots, and stopping at ``bnd4``
    leaves the numbers behind an encryption layer where no search can reach
    them; going one step further to ``bnd4 → fromsoft_slot`` hands over the
    decrypted slot the player's runes are actually in.

    Only exact round-trips are considered, so the extra step can never cost the
    ability to put the file back together.
    """
    exact = [item for item in report.candidates if item.round_trip.exact_bytes]
    if not exact:
        return report.best
    return max(exact, key=lambda item: (len(item.pipeline.steps), item.structured))


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


def parse_address(address: str) -> tuple[int, str]:
    """``0x1F4C:uint32`` → ``(8012, "uint32")``."""
    text, _, encoding = address.partition(":")
    if not encoding:
        raise AddressError(
            f"The address '{address}' does not say how the number is stored. Write it as "
            f"0x1F4C:uint32, the way 'savesmith search' printed it."
        )
    if encoding not in compare.ENCODINGS:
        raise AddressError(
            f"There is no storage type called '{encoding}'. "
            f"Expected one of: {', '.join(compare.ENCODINGS)}."
        )
    try:
        offset = int(text, 0)
    except ValueError as exc:
        raise AddressError(
            f"The offset '{text}' is not a number; write it as 0x1F4C or 8012."
        ) from exc
    if offset < 0:
        raise AddressError("An offset cannot be negative.")
    return offset, encoding


def _read_offset(payload: bytes, address: str) -> Any:
    offset, encoding = parse_address(address)
    layout = compare.layout_for(encoding)
    if offset + struct.calcsize(layout) > len(payload):
        return None
    return struct.unpack_from(layout, payload, offset)[0]


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


def _leaves(root: Any) -> list[Leaf]:
    """Walk a decoded save and collect every value a person could read."""
    found: list[Leaf] = []

    def kind_of(node: Any) -> str:
        if isinstance(node, bool):
            return "flag"
        if isinstance(node, int | float):
            return "number"
        return "text"

    def walk(node: Any, steps: tuple[field_access.PathStep, ...]) -> None:
        if len(found) >= _MAX_FIELDS:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                walk(child, (*steps, str(key)))
            return
        if isinstance(node, list):
            # A list of numbers is one thing to a person — a list of structures
            # is many, so only the second is opened up.
            if node and all(isinstance(item, int | float | str) for item in node):
                found.append(_leaf(steps, node, "list"))
                return
            for index, child in enumerate(node):
                walk(child, (*steps, index))
            return
        if node is None or isinstance(node, bytes):
            return  # nothing a person can read or usefully change
        found.append(_leaf(steps, node, kind_of(node)))

    walk(root, ())
    return found


def _leaf(steps: tuple[field_access.PathStep, ...], value: Any, kind: str) -> Leaf:
    last = steps[-1] if steps else ""
    return Leaf(
        address=field_access.render(steps),
        name=str(last),
        group=field_access.render(steps[:-1]),
        value=value,
        kind=kind,
    )


def _search_structure(root: Any, wanted: float) -> list[Site]:
    """Every path in a decoded save whose value is this number."""
    found: list[Site] = []

    def walk(node: Any, steps: tuple[field_access.PathStep, ...]) -> None:
        if len(found) >= _MAX_REPORTED:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                walk(child, (*steps, str(key)))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, (*steps, index))
        elif isinstance(node, bool):
            return  # True is not 1 to anybody looking at their screen
        elif isinstance(node, int | float) and float(node) == float(wanted):
            address = field_access.render(steps)
            parent = field_access.render(steps[:-1]) if len(steps) > 1 else ""
            found.append(Site(address=address, value=float(node), context=parent))

    walk(root, ())
    return found


def _search_bytes(payload: bytes, wanted: float, *, encoding: str | None = None) -> list[Site]:
    """Offsets holding this number, with the same bytes reported once.

    ``int32-le`` and ``uint32-le`` at one offset are not two candidates: they
    are the same four bytes read two ways, and printing both doubles the list
    the player has to work through for no information. Widths that genuinely
    differ — four bytes against eight — stay separate, because they are
    different readings of the file.
    """
    if encoding is not None and encoding not in compare.ENCODINGS:
        raise AddressError(
            f"There is no storage type called '{encoding}'. "
            f"Expected one of: {', '.join(compare.ENCODINGS)}."
        )

    found = [
        site
        for site in compare.find_value(payload, wanted)
        if encoding is None or site.encoding == encoding
    ]
    return [
        Site(
            address=f"0x{primary.offset:X}:{primary.encoding}",
            value=primary.value,
            context=("also " + ", ".join(others)) if others else "",
        )
        for primary, others in compare.group_by_bytes(found)[:_MAX_REPORTED]
    ]


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    raise SaveSmithError(
        "This save decodes to something that is neither a structure nor plain bytes, "
        "so it cannot be edited without a plugin."
    )


def _plain(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)
