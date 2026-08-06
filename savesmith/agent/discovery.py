"""Working out an unknown save format, step by explicit step.

Written as a state machine rather than "hand the folder to a model and hope".
Each step is cheap or expensive, and they are ordered so that the expensive one
runs last and least often:

1. **Back up.** A copy before anything is read. No exceptions, no conditions.
2. **Look at it.** Size, magic bytes, entropy, how much is printable.
3. **Try what we know.** Every combination of the built-in operations. This
   step is meant to close most games on its own, and when it does, the file is
   understood without a model, a key or a guess.
4. **Compare two saves.** If the player made a second save after changing a
   known number, the field falls out of the difference.
5. **Ask a model.** Only here, only for what survived, and only with what the
   earlier steps learned — a hex dump of the interesting part, the entropy
   report, the diff. Never the whole file.
6. **Find the checksum.** Otherwise half of games reject the result.
7. **Prove it.** Rebuild the file and compare byte for byte.
8. **Ask the player.** Load the game and confirm. Only this grants ``verified``.

The report says which steps ran, what each found, and what stopped it. A
failure at step 5 with a good report is a useful outcome: somebody can read it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from savesmith.agent.sandbox import Limits, Result, run
from savesmith.core import compare, detect
from savesmith.core.backup import Backup, BackupStore
from savesmith.core.checksum import ChecksumSpec
from savesmith.core.checksum import find as find_checksums
from savesmith.core.detect import Look, Report
from savesmith.core.errors import SaveSmithError
from savesmith.core.pipeline import Pipeline, RoundTrip, Step

_HEXDUMP_BYTES = 512
_HEXDUMP_WIDTH = 16


class Stage(StrEnum):
    BACKUP = "backup"
    CLASSIFY = "classify"
    LADDER = "ladder"
    DIFF = "diff"
    MODEL = "model"
    CHECKSUM = "checksum"
    ROUND_TRIP = "round_trip"
    LIVE_CHECK = "live_check"


@dataclass(frozen=True)
class StageResult:
    stage: Stage
    ok: bool
    summary: str
    detail: str = ""

    def __str__(self) -> str:
        mark = "✓" if self.ok else "·"
        return f"{mark} {self.stage.value}: {self.summary}"


@dataclass(frozen=True)
class CodecRequest:
    """Everything a model is given. Deliberately not the whole file.

    A save can be hundreds of megabytes and most of it is noise. What helps is
    the shape: the head and tail in hex, what the ladder already ruled out,
    where the entropy sits, and which bytes moved between two saves.
    """

    look: Look
    head_hex: str
    tail_hex: str
    tried: tuple[str, ...]
    diff_ranges: tuple[tuple[int, int], ...] = ()
    field_guesses: tuple[str, ...] = ()
    max_budget_usd: float = 1.0

    def as_prompt(self) -> str:
        lines = [
            "An unknown game save file. Work out how to decode it.",
            "",
            f"Size: {self.look.size} bytes",
            f"Entropy: {self.look.entropy:.2f}"
            + (" (looks encrypted or compressed)" if self.look.looks_encrypted else ""),
            f"Printable characters: {self.look.printable_ratio:.0%}",
            f"Recognised opening: {self.look.magic or 'none'}",
            "",
            "Decoders already tried and rejected: " + ", ".join(self.tried),
            "",
            "First bytes:",
            self.head_hex,
            "",
            "Last bytes:",
            self.tail_hex,
        ]
        if self.diff_ranges:
            spans = ", ".join(f"0x{start:X}+{length}" for start, length in self.diff_ranges[:20])
            lines += ["", "Bytes that changed between two saves: " + spans]
        if self.field_guesses:
            lines += ["", "Candidate fields from the diff:", *self.field_guesses[:20]]
        lines += [
            "",
            "Write a Python module with a function decode(data: bytes) that returns the",
            "decoded structure, and encode(value) that rebuilds the original bytes.",
            "It runs with no network and no subprocesses.",
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class CodecProposal:
    source: str
    explanation: str = ""
    cost_usd: float = 0.0


class CodecWriter(Protocol):
    """Whatever writes a codec for a format nothing else could read.

    A protocol so the state machine can be tested without a model, an API key
    or a network, and so that the expensive step stays replaceable.
    """

    def propose(self, request: CodecRequest) -> CodecProposal | None: ...


class NoCodecWriter:
    """The default: decline, and say why.

    Used when no API key is configured. The discovery still runs every free
    step and produces a report someone can act on.
    """

    def propose(self, request: CodecRequest) -> CodecProposal | None:
        return None


@dataclass
class Discovery:
    """The outcome of one attempt at an unknown file."""

    source: Path
    stages: list[StageResult] = field(default_factory=list)
    backup: Backup | None = None
    look: Look | None = None
    ladder: Report | None = None
    checksums: tuple[ChecksumSpec, ...] = ()
    round_trip: RoundTrip | None = None
    pipeline: Pipeline | None = None
    codec: CodecProposal | None = None
    codec_result: Result | None = None
    field_candidates: tuple[str, ...] = ()
    cost_usd: float = 0.0

    @property
    def solved(self) -> bool:
        return self.round_trip is not None and self.round_trip.exact_bytes

    @property
    def needs_a_person(self) -> bool:
        """True once everything automatic has succeeded and only the game can confirm."""
        return self.solved

    def explain(self) -> list[str]:
        lines = [f"Discovery for {self.source.name}", ""]
        lines += [str(result) for result in self.stages]
        if self.checksums:
            lines += ["", "Checksums found:"]
            lines += [f"  {spec.describe()}" for spec in self.checksums]
        if self.field_candidates:
            lines += ["", "Candidate fields:", *[f"  {item}" for item in self.field_candidates]]
        if self.cost_usd:
            lines.append(f"\nModel cost: ${self.cost_usd:.2f}")
        return lines

    def draft_manifest(self, plugin_id: str, game: str) -> dict[str, Any]:
        """A manifest someone can save as a plugin and fill in the fields of.

        Confidence is never better than ``experimental`` here: the round-trip
        gate can raise it once there is a corpus, and only a person who started
        the game can make it ``verified``.
        """
        steps: list[dict[str, Any]] = []
        if self.checksums:
            best = self.checksums[0]
            steps.append(
                {
                    "op": "checksum",
                    "algorithm": best.algorithm,
                    "offset": best.offset,
                    "covers": best.coverage.value,
                }
            )
        if self.pipeline is not None:
            steps += [{"op": step.op, **dict(step.params)} for step in self.pipeline.steps]

        checksum_block = steps[0] if steps and steps[0]["op"] == "checksum" else None
        return {
            "schema": 1,
            "id": plugin_id,
            "version": 1,
            "game": game,
            "engine": "unknown",
            "confidence": "experimental",
            "detect": {"paths": {}, "magic_hex": self.look.head[:4].hex() if self.look else None},
            "risk": {
                "tier": "caution",
                "reason": {
                    "en": "Discovered automatically; nothing is known about this game yet.",
                    "ru": "Разобрано автоматически; про эту игру пока ничего не известно.",
                },
                "steam_cloud": False,
            },
            "checksum": (
                {
                    "algorithm": checksum_block["algorithm"],
                    "offset": checksum_block["offset"],
                    "covers": checksum_block["covers"],
                }
                if checksum_block
                else None
            ),
            "pipeline": [step for step in steps if step["op"] != "checksum"],
            "fields": [],
        }


def discover(
    path: Path,
    *,
    backups: BackupStore,
    second_save: Path | None = None,
    known_before: float | None = None,
    known_after: float | None = None,
    writer: CodecWriter | None = None,
    max_budget_usd: float = 1.0,
) -> Discovery:
    """Run every step in order and report what happened at each."""
    result = Discovery(source=path)
    writer = writer or NoCodecWriter()

    # 1. Backup, before anything reads a byte.
    try:
        result.backup = backups.create(path, plugin_id="discovery")
        result.stages.append(
            StageResult(Stage.BACKUP, True, f"copied to {result.backup.folder.name}")
        )
    except SaveSmithError as exc:
        result.stages.append(StageResult(Stage.BACKUP, False, exc.user_message))
        return result  # no backup, no further work

    raw = path.read_bytes()

    # 2. A cheap look.
    result.look = detect.inspect(raw)
    result.stages.append(StageResult(Stage.CLASSIFY, True, result.look.summary()))

    # 3. Everything already known.
    result.ladder = detect.identify(raw)
    best = result.ladder.best
    if best is not None:
        result.pipeline = best.pipeline
        result.stages.append(
            StageResult(
                Stage.LADDER,
                result.ladder.solved,
                f"{best.description}" + ("" if result.ladder.solved else " (bytes, not fields)"),
                detail=best.round_trip.detail,
            )
        )
    else:
        result.stages.append(
            StageResult(Stage.LADDER, False, "nothing known fits", detail=result.look.summary())
        )

    # 4. Two saves and a number the player told us.
    diff_ranges: tuple[tuple[int, int], ...] = ()
    if second_save is not None:
        diff_ranges = _compare_saves(result, raw, second_save, known_before, known_after)

    # 5. The expensive step, for what is left.
    if not result.ladder.solved:
        _ask_for_a_codec(result, raw, diff_ranges, writer, max_budget_usd)

    # 6. The checksum, whatever route got us here.
    result.checksums = tuple(find_checksums(raw))
    result.stages.append(
        StageResult(
            Stage.CHECKSUM,
            bool(result.checksums),
            result.checksums[0].describe() if result.checksums else "none found",
        )
    )

    # 7. Prove it rebuilds.
    if result.pipeline is not None:
        try:
            result.round_trip = result.pipeline.round_trip(raw)
            result.stages.append(
                StageResult(
                    Stage.ROUND_TRIP,
                    result.round_trip.exact_bytes,
                    "byte-identical" if result.round_trip.exact_bytes else result.round_trip.detail,
                )
            )
        except SaveSmithError as exc:
            result.stages.append(StageResult(Stage.ROUND_TRIP, False, exc.user_message))
    else:
        result.stages.append(StageResult(Stage.ROUND_TRIP, False, "no pipeline to test"))

    # 8. Only a person can do this one.
    result.stages.append(
        StageResult(
            Stage.LIVE_CHECK,
            False,
            "waiting for someone to start the game and confirm the save loads"
            if result.solved
            else "not reached",
        )
    )
    return result


def _compare_saves(
    result: Discovery,
    raw: bytes,
    second_save: Path,
    known_before: float | None,
    known_after: float | None,
) -> tuple[tuple[int, int], ...]:
    try:
        other = second_save.read_bytes()
    except OSError as exc:
        result.stages.append(StageResult(Stage.DIFF, False, f"could not read {second_save.name}"))
        result.stages[-1] = StageResult(Stage.DIFF, False, str(exc.strerror or "unreadable"))
        return ()

    if result.ladder is not None and result.ladder.solved and result.pipeline is not None:
        # Both files decode, so compare fields rather than bytes.
        try:
            before = result.pipeline.decode(raw).value
            after = result.pipeline.decode(other).value
        except SaveSmithError as exc:
            result.stages.append(StageResult(Stage.DIFF, False, exc.user_message))
            return ()
        changes = compare.numeric_changes(compare.compare_structures(before, after))
        result.field_candidates = tuple(str(change) for change in changes)
        result.stages.append(
            StageResult(Stage.DIFF, bool(changes), f"{len(changes)} numbers changed")
        )
        return ()

    if len(raw) != len(other):
        result.stages.append(
            StageResult(Stage.DIFF, False, "the two saves are different sizes")
        )
        return ()

    byte_diff = compare.compare_bytes(raw, other)
    guesses = []
    if known_before is not None and known_after is not None:
        guesses = compare.guesses_in_ranges(
            compare.narrow(raw, other, known_before, known_after), byte_diff
        )
        result.field_candidates = tuple(str(guess) for guess in guesses)
    result.stages.append(
        StageResult(
            Stage.DIFF,
            bool(guesses) or bool(byte_diff.ranges),
            byte_diff.summary() + (f", {len(guesses)} candidate field(s)" if guesses else ""),
        )
    )
    return tuple(byte_diff.ranges)


def _ask_for_a_codec(
    result: Discovery,
    raw: bytes,
    diff_ranges: tuple[tuple[int, int], ...],
    writer: CodecWriter,
    max_budget_usd: float,
) -> None:
    assert result.look is not None
    request = CodecRequest(
        look=result.look,
        head_hex=hexdump(raw[:_HEXDUMP_BYTES]),
        tail_hex=hexdump(raw[-_HEXDUMP_BYTES:]),
        tried=detect.known_operations(),
        diff_ranges=diff_ranges,
        field_guesses=result.field_candidates,
        max_budget_usd=max_budget_usd,
    )

    proposal = writer.propose(request)
    if proposal is None:
        result.stages.append(
            StageResult(
                Stage.MODEL,
                False,
                "not attempted (no model configured)",
                detail="everything free was tried; this file needs a codec written for it",
            )
        )
        return

    result.codec = proposal
    result.cost_usd += proposal.cost_usd
    sandboxed = run(proposal.source, payload=raw, limits=Limits())
    result.codec_result = sandboxed
    result.stages.append(
        StageResult(
            Stage.MODEL,
            sandboxed.ok,
            "the proposed codec ran" if sandboxed.ok else sandboxed.error,
            detail=proposal.explanation,
        )
    )


def hexdump(raw: bytes, *, width: int = _HEXDUMP_WIDTH) -> str:
    """A trimmed hex dump. Only ever a slice — the whole file never leaves."""
    lines = []
    for offset in range(0, len(raw), width):
        chunk = raw[offset : offset + width]
        hexed = " ".join(f"{byte:02x}" for byte in chunk).ljust(width * 3 - 1)
        text = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        lines.append(f"{offset:08x}  {hexed}  {text}")
    return "\n".join(lines)


def build_pipeline(steps: Sequence[Step]) -> Pipeline:
    return Pipeline(steps)
