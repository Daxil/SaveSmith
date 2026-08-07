"""Finding a field by watching it change.

The decoder ladder can open a file without knowing anything about the game, but
it cannot say which of six thousand numbers is the gold. Asking a model to
guess from a hex dump is expensive and unreliable. Asking the player is neither:

    Save. Note that you have 12 400 gold. Spend some. Save again.

Two files and two known numbers pin the field down exactly. Everything here
serves that: find where a number lives, find what changed between two saves,
and cross the two.

It works at both levels. For a save the ladder decoded into fields, the
comparison is over paths and is exact. For a save that is still bytes — an
Elden Ring slot, say — it is a search over every plausible integer and float
encoding, which is the same trick a memory scanner uses, applied to a file.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from savesmith.core import fields as field_access
from savesmith.core.fields import PathStep

# Every way a game might plausibly store a whole number or a real one. Ordered
# so that the encodings games actually use come first when scores tie.
_NUMERIC_FORMATS: tuple[tuple[str, str], ...] = (
    ("int32-le", "<i"),
    ("uint32-le", "<I"),
    ("int64-le", "<q"),
    ("uint64-le", "<Q"),
    ("float32-le", "<f"),
    ("float64-le", "<d"),
    ("int16-le", "<h"),
    ("uint16-le", "<H"),
    ("int8", "<b"),
    ("uint8", "<B"),
    ("int32-be", ">i"),
    ("uint32-be", ">I"),
    ("int64-be", ">q"),
    ("uint64-be", ">Q"),
    ("float32-be", ">f"),
    ("float64-be", ">d"),
    ("int16-be", ">h"),
    ("uint16-be", ">H"),
)

# Beyond this many hits a value is too common to be useful on its own — small
# numbers like 1 or 100 appear everywhere in a large file.
_TOO_COMMON = 5000

ENCODINGS: tuple[str, ...] = tuple(name for name, _ in _NUMERIC_FORMATS)
"""Every way a number can be stored that SaveSmith knows how to find."""


def layout_for(encoding: str) -> str:
    """The struct format behind one of :data:`ENCODINGS`."""
    layouts = dict(_NUMERIC_FORMATS)
    if encoding not in layouts:
        raise ValueError(f"unknown encoding {encoding!r}; expected one of {', '.join(ENCODINGS)}")
    return layouts[encoding]


class _Reading(Protocol):
    """Anything that says "this number, at this offset, stored this way".

    Read-only properties rather than plain attributes: the classes that satisfy
    this are frozen dataclasses, and a protocol asking for a settable attribute
    would exclude every one of them.
    """

    @property
    def offset(self) -> int: ...

    @property
    def encoding(self) -> str: ...


def group_by_bytes[T: _Reading](readings: Sequence[T]) -> list[tuple[T, tuple[str, ...]]]:
    """Collapse readings that are literally the same bytes.

    ``int32-le`` and ``uint32-le`` at one offset are four bytes read two ways,
    not two candidates, and listing both doubles the work of whoever has to
    pick one. Different widths stay apart: four bytes and eight bytes really
    are different readings of the file.

    Returns each surviving reading with the names of the ones it stands for.
    """
    grouped: dict[tuple[int, int], list[T]] = {}
    for reading in readings:
        width = struct.calcsize(layout_for(reading.encoding))
        grouped.setdefault((reading.offset, width), []).append(reading)
    return [
        (same[0], tuple(other.encoding for other in same[1:]))
        for _key, same in sorted(grouped.items())
    ]


@dataclass(frozen=True)
class ValueSite:
    """A place in a file where a number is stored, and how."""

    offset: int
    encoding: str
    value: float

    def __str__(self) -> str:
        return f"0x{self.offset:X} as {self.encoding}"


@dataclass(frozen=True)
class FieldGuess:
    """A candidate field: it held one value before and another after."""

    offset: int
    encoding: str
    before: float
    after: float

    @property
    def size(self) -> int:
        return struct.calcsize(dict(_NUMERIC_FORMATS)[self.encoding])

    @property
    def address(self) -> str:
        """The same form ``search`` prints and ``poke`` accepts."""
        return f"0x{self.offset:X}:{self.encoding}"

    def __str__(self) -> str:
        return f"{self.address}: {_plain(self.before)} → {_plain(self.after)}"


@dataclass(frozen=True)
class PathChange:
    """A value that changed, named by where it lives in the decoded save."""

    path: tuple[PathStep, ...]
    before: Any
    after: Any

    @property
    def address(self) -> str:
        return field_access.render(self.path)

    def __str__(self) -> str:
        return f"{self.address}: {self.before!r} → {self.after!r}"


# ---------------------------------------------------------------------------
# Structured saves
# ---------------------------------------------------------------------------


def compare_structures(before: Any, after: Any) -> list[PathChange]:
    """Every value that differs between two decoded saves, by path.

    Additions and removals are reported too, as a change to or from ``None``:
    a field that only exists after the player did something is exactly the kind
    of thing worth finding.
    """
    return list(_walk(before, after, ()))


def _walk(before: Any, after: Any, path: tuple[PathStep, ...]) -> Iterator[PathChange]:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in {**before, **after}:
            yield from _walk(before.get(key), after.get(key), (*path, str(key)))
        return
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            yield from _walk(
                before[index] if index < len(before) else None,
                after[index] if index < len(after) else None,
                (*path, index),
            )
        return
    if before != after:
        yield PathChange(path=path, before=before, after=after)


def numeric_changes(changes: Sequence[PathChange]) -> list[PathChange]:
    """Only the changes worth offering as editable fields.

    Timestamps and play counters move on their own between two saves; numbers
    the player controls are what the search is for, and telling them apart is
    the user's job, not ours. This only strips out what is not a number at all.
    """
    return [
        change
        for change in changes
        if isinstance(change.before, int | float)
        and isinstance(change.after, int | float)
        and not isinstance(change.before, bool)
    ]


# ---------------------------------------------------------------------------
# Raw bytes
# ---------------------------------------------------------------------------


def find_value(raw: bytes, value: float, *, limit: int = _TOO_COMMON) -> list[ValueSite]:
    """Every offset where ``value`` is stored, in any plausible encoding.

    Implemented by encoding the number and searching for those bytes, rather
    than decoding every offset: the search runs at C speed, and a 3 MB save
    scans in milliseconds instead of minutes.
    """
    sites: list[ValueSite] = []
    for encoding, layout in _NUMERIC_FORMATS:
        # A number typed on a command line arrives as a float, and struct
        # refuses a float for an integer format. Without this, "--was 12400"
        # would quietly search only the floating-point encodings and find
        # nothing — which is exactly the shape of a bug nobody reports.
        wanted: float | int = value
        if layout[-1] not in "fd":
            if float(value) != int(value):
                continue  # not a whole number, so no integer encoding fits
            wanted = int(value)
        try:
            needle = struct.pack(layout, wanted)
        except (struct.error, OverflowError, ValueError):
            continue  # the number does not fit this encoding
        if len(needle) == 1 and len(raw) > 1024:
            continue  # a single byte matches far too often to mean anything
        start = 0
        while len(sites) < limit:
            found = raw.find(needle, start)
            if found == -1:
                break
            sites.append(ValueSite(offset=found, encoding=encoding, value=value))
            start = found + 1
    return sites


def narrow(
    before_raw: bytes,
    after_raw: bytes,
    before_value: float,
    after_value: float,
) -> list[FieldGuess]:
    """Offsets that held one value before and the other after.

    This is the whole method in one function. Either number alone appears all
    over a large file; the same offset holding both, in the same encoding, in
    two files taken minutes apart, is almost never a coincidence.
    """
    after_sites = {
        (site.offset, site.encoding) for site in find_value(after_raw, after_value)
    }
    guesses = [
        FieldGuess(
            offset=site.offset,
            encoding=site.encoding,
            before=before_value,
            after=after_value,
        )
        for site in find_value(before_raw, before_value)
        if (site.offset, site.encoding) in after_sites
    ]
    # Widest encoding first: a 32-bit hit at an offset usually also matches as
    # two 16-bit halves, and the wide one is the real field.
    guesses.sort(key=lambda guess: (-guess.size, guess.offset))
    return guesses


@dataclass
class ByteDiff:
    """Where two files of the same size differ."""

    ranges: list[tuple[int, int]] = field(default_factory=list)
    """(start, length) of each run of differing bytes."""
    size: int = 0

    @property
    def changed_bytes(self) -> int:
        return sum(length for _start, length in self.ranges)

    def summary(self) -> str:
        if not self.ranges:
            return "the two files are identical"
        return (
            f"{len(self.ranges)} regions differ, {self.changed_bytes} bytes of {self.size}"
        )


def compare_bytes(before: bytes, after: bytes, *, gap: int = 8) -> ByteDiff:
    """Runs of differing bytes, merged across small gaps.

    ``gap`` joins regions separated by a few identical bytes, because one
    logical field often shows up as two runs with a byte or two of shared value
    in the middle.
    """
    if len(before) != len(after):
        raise ValueError(
            "the two saves are different sizes, so their bytes cannot be lined up; "
            "compare them after decoding instead"
        )

    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for index, (left, right) in enumerate(zip(before, after, strict=True)):
        if left != right:
            if start is None:
                start = index
        elif start is not None and index - start >= 0:
            if ranges and start - sum(ranges[-1]) <= gap:
                previous_start = ranges[-1][0]
                ranges[-1] = (previous_start, index - previous_start)
            else:
                ranges.append((start, index - start))
            start = None
    if start is not None:
        ranges.append((start, len(before) - start))
    return ByteDiff(ranges=ranges, size=len(before))


def guesses_in_ranges(
    guesses: Sequence[FieldGuess], diff: ByteDiff
) -> list[FieldGuess]:
    """Keep only guesses that land inside a region that actually changed.

    A number can match by chance in a part of the file that never moved; if the
    bytes there are identical in both saves, it is not the field.
    """
    return [
        guess
        for guess in guesses
        if any(start <= guess.offset < start + length for start, length in diff.ranges)
    ]


def _plain(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)
