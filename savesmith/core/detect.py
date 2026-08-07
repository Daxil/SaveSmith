"""Working out what an unknown save file is, without asking a model.

This is the part that has to carry the product. A save editor that only knows
the games someone wrote a plugin for is a list of games; the point here is to
hand SaveSmith a file it has never seen and get a working pipeline back.

Two stages, cheapest first.

**A quick look** (:func:`inspect`) — size, magic bytes, entropy, how much of it
is printable. Costs microseconds and answers "is this even worth trying" and
"is this encrypted".

**The ladder** (:func:`identify`) — every combination of the operations
SaveSmith already has, tried breadth-first: plain JSON, gzip, zlib, base64,
MessagePack, GVAS, BND4, FromSoftware slots, and any nesting of them. A file
that comes apart this way needs no model, no key and no guesswork, and the
result is a pipeline a plugin can be written around.

Only what fails both stages needs the expensive path with an agent — and by
then the useful context (what the ladder tried, where it stopped, what the
entropy looks like) has already been collected for it.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

from savesmith.core import ops
from savesmith.core.errors import SaveSmithError
from savesmith.core.pipeline import Pipeline, RoundTrip, Step

# Operations that need no settings, so the ladder can try them blind. Ordered
# cheapest and most common first; the search is breadth-first, so this order
# decides which of two equally short answers wins.
_LADDER: tuple[str, ...] = (
    "json_parse",
    "gzip",
    "zlib",
    "base64",
    # RPG Maker MV and MZ, which is most of the indie games on Steam that have
    # a save worth editing at all. Without it here the ladder calls a
    # .rpgsave "unknown" while the bundled plugin opens it perfectly well.
    "lzstring",
    "msgpack",
    "gvas",
    "bnd4",
    "fromsoft_slot",
)

# Recognisable openings. Not authoritative — a container may be encrypted or
# wrapped — but they turn the search from guessing into checking.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"GVAS", "Unreal Engine save"),
    (b"BND4", "FromSoftware archive"),
    (b"\x1f\x8b", "gzip"),
    (b"PK\x03\x04", "zip archive"),
    (b"{", "JSON object"),
    (b"[", "JSON array"),
    (b"\x78\x01", "zlib (no compression)"),
    (b"\x78\x9c", "zlib (default)"),
    (b"\x78\xda", "zlib (best)"),
    (b"RIFF", "RIFF container"),
    (b"SQLite format 3", "SQLite database"),
)

# Above this, the bytes carry no structure we could exploit: the file is
# encrypted or already compressed by something we do not recognise.
_ENCRYPTED_ENTROPY = 7.9


@dataclass(frozen=True)
class Look:
    """The cheap first glance at a file."""

    size: int
    entropy: float
    printable_ratio: float
    magic: str | None
    head: bytes

    @property
    def looks_encrypted(self) -> bool:
        return self.entropy >= _ENCRYPTED_ENTROPY

    @property
    def looks_textual(self) -> bool:
        return self.printable_ratio > 0.9

    def summary(self) -> str:
        parts = [f"{self.size} bytes", f"entropy {self.entropy:.2f}"]
        if self.magic:
            parts.append(self.magic)
        if self.looks_encrypted:
            parts.append("encrypted or already compressed")
        elif self.looks_textual:
            parts.append("looks like text")
        return ", ".join(parts)


@dataclass(frozen=True)
class Candidate:
    """A pipeline that opened the file, and how well it did."""

    steps: tuple[Step, ...]
    value: Any
    round_trip: RoundTrip

    @property
    def pipeline(self) -> Pipeline:
        return Pipeline(self.steps)

    @property
    def description(self) -> str:
        return " → ".join(step.op for step in self.steps) or "plain bytes"

    @property
    def structured(self) -> bool:
        """Whether the result has named fields, rather than being more bytes."""
        return isinstance(self.value, dict | list)

    @property
    def score(self) -> tuple[int, int, int]:
        """Sort key, best first. Compared as a tuple, so order matters.

        Structure first: a file that became named fields is worth more than one
        that became different bytes. Then faithfulness, because a pipeline that
        cannot rebuild the file is not usable for editing. Then brevity — of two
        answers that both work, the shorter is more likely to be what the game
        actually does.
        """
        return (
            0 if self.structured else 1,
            0 if self.round_trip.exact_bytes else 1,
            len(self.steps),
        )


@dataclass
class Report:
    """Everything one attempt at an unknown file learned.

    Passed to the discovery agent when the ladder comes up empty; by then it
    holds most of what the agent would otherwise spend a model call working out.
    """

    look: Look
    candidates: list[Candidate] = field(default_factory=list)
    attempted: int = 0

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def solved(self) -> bool:
        """A candidate good enough to build a plugin on without a model."""
        best = self.best
        return best is not None and best.structured and best.round_trip.exact_bytes

    def explain(self) -> list[str]:
        """Lines for the progress log, so the user sees what is happening."""
        lines = [f"File: {self.look.summary()}", f"Combinations tried: {self.attempted}"]
        if not self.candidates:
            lines.append("Nothing in the known formats fits this file.")
            if self.look.looks_encrypted:
                lines.append("The contents look encrypted; a key will be needed.")
            return lines
        for candidate in self.candidates[:5]:
            state = "exact" if candidate.round_trip.exact_bytes else candidate.round_trip.detail
            shape = "fields" if candidate.structured else "bytes"
            lines.append(f"  {candidate.description} → {shape} ({state})")
        return lines


def inspect(raw: bytes) -> Look:
    """The cheap look. No decoding, no allocation of any size."""
    sample = raw[:65536]
    return Look(
        size=len(raw),
        entropy=_entropy(sample),
        printable_ratio=_printable_ratio(sample),
        magic=_magic(raw),
        head=raw[:32],
    )


def identify(
    raw: bytes,
    *,
    max_depth: int = 4,
    max_attempts: int = 200,
) -> Report:
    """Try every combination of known operations, best answer first.

    Breadth-first, so the shortest pipeline that works is found before longer
    ones that merely also work. ``max_attempts`` bounds the search: with eight
    operations and four levels the space is small, but a pathological file that
    decodes into itself would otherwise run forever.
    """
    report = Report(look=inspect(raw))

    queue: deque[tuple[tuple[Step, ...], Any]] = deque([((), raw)])
    seen: set[tuple[str, ...]] = set()

    while queue and report.attempted < max_attempts:
        steps, value = queue.popleft()
        if len(steps) >= max_depth or not isinstance(value, bytes | bytearray):
            continue

        for name in _LADDER:
            report.attempted += 1
            candidate_steps = (*steps, Step(op=name))
            key = tuple(step.op for step in candidate_steps)
            if key in seen:
                continue
            seen.add(key)

            outcome = _try(raw, candidate_steps)
            if outcome is None:
                continue
            report.candidates.append(outcome)
            if isinstance(outcome.value, bytes | bytearray):
                queue.append((candidate_steps, outcome.value))

    report.candidates.sort(key=lambda item: item.score)
    return report


def _try(raw: bytes, steps: tuple[Step, ...]) -> Candidate | None:
    """Run a whole pipeline from the start. Failure is the normal outcome."""
    try:
        pipeline = Pipeline(steps)
        decoded = pipeline.decode(raw)
        result = pipeline.round_trip(raw)
    except SaveSmithError:
        return None
    except Exception:
        # An operation that raises something unexpected on a file it was never
        # meant to see is still just a failed guess, not a crash.
        return None
    return Candidate(steps=steps, value=decoded.value, round_trip=result)


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    printable = sum(1 for byte in data if 32 <= byte < 127 or byte in (9, 10, 13))
    return printable / len(data)


def _magic(raw: bytes) -> str | None:
    for signature, description in _MAGIC:
        if raw.startswith(signature):
            return description
    return None


def main(argv: list[str] | None = None) -> int:
    """Throw any file at the ladder: ``python -m savesmith.core.detect <file>``."""
    import sys
    from pathlib import Path

    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        print("usage: python -m savesmith.core.detect <save file>...")
        return 2

    for name in arguments:
        path = Path(name)
        print(f"\n{path}")
        print("-" * min(len(str(path)), 78))
        try:
            raw = path.read_bytes()
        except OSError as exc:
            print(f"  could not be read: {exc.strerror}")
            continue
        report = identify(raw)
        for line in report.explain():
            print(f"  {line}")
        if report.solved:
            print("  → this format can be edited without any further work.")
    return 0


def known_operations() -> tuple[str, ...]:
    """What the ladder tries. Shown in the interface, so it is not a black box."""
    return _LADDER


def all_operation_names() -> tuple[str, ...]:
    """Everything registered, including steps that need settings from a plugin."""
    return ops.names()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
