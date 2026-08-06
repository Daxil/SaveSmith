"""Finding the checksum a game hides in its save, and keeping it right.

Without this, half the games in the world reject an edited save or silently
"repair" it back. The checksum is rarely documented, but it is always sitting
in the file next to the data it covers, which is enough to find it.

The search does not try every offset — that would mean hashing a large file
thousands of times. It works the other way round: hash a handful of plausible
ranges, then look for those digest bytes anywhere in the file. One hash and one
fast search per candidate, and it finds checksums at offsets nobody guessed.

Once found, the checksum becomes a pipeline step like any other, recalculated
on the way out. A stale checksum is worse than no edit at all: the game either
refuses the save or restores an older one, and the player loses the lot.
"""

from __future__ import annotations

import hashlib
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum


class Coverage(StrEnum):
    """Which part of the file a checksum is computed over."""

    AFTER = "after"
    """Everything after the checksum field. The most common arrangement."""
    BEFORE = "before"
    """Everything from the start of the file up to the checksum field."""
    WHOLE_ZEROED = "whole_zeroed"
    """The entire file with the checksum field itself set to zero."""


@dataclass(frozen=True)
class Algorithm:
    name: str
    size: int
    compute: Callable[[bytes], bytes]


def _int_algorithm(name: str, function: Callable[[bytes], int], size: int) -> list[Algorithm]:
    """Integer checksums exist in both byte orders and games use both."""
    return [
        Algorithm(f"{name}-le", size, lambda data: function(data).to_bytes(size, "little")),
        Algorithm(f"{name}-be", size, lambda data: function(data).to_bytes(size, "big")),
    ]


def _sum_bytes(data: bytes, width: int) -> int:
    return sum(data) % (1 << (width * 8))


def _xor_bytes(data: bytes) -> int:
    result = 0
    for byte in data:
        result ^= byte
    return result


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


ALGORITHMS: tuple[Algorithm, ...] = (
    *_int_algorithm("crc32", lambda data: zlib.crc32(data), 4),
    *_int_algorithm("adler32", lambda data: zlib.adler32(data), 4),
    *_int_algorithm("crc16", _crc16_ccitt, 2),
    *_int_algorithm("sum32", lambda data: _sum_bytes(data, 4), 4),
    *_int_algorithm("sum16", lambda data: _sum_bytes(data, 2), 2),
    Algorithm("xor8", 1, lambda data: bytes([_xor_bytes(data)])),
    Algorithm("md5", 16, lambda data: hashlib.md5(data).digest()),
    Algorithm("sha1", 20, lambda data: hashlib.sha1(data).digest()),
    Algorithm("sha256", 32, lambda data: hashlib.sha256(data).digest()),
)

_BY_NAME = {algorithm.name: algorithm for algorithm in ALGORITHMS}

# One-byte and two-byte checksums match by chance constantly. They are still
# searched for, but only reported when nothing wider fits.
_WEAK = frozenset({"xor8", "crc16-le", "crc16-be", "sum16-le", "sum16-be"})


@dataclass(frozen=True)
class ChecksumSpec:
    """Where a checksum lives and what it covers."""

    algorithm: str
    offset: int
    coverage: Coverage

    @property
    def size(self) -> int:
        return _BY_NAME[self.algorithm].size

    @property
    def weak(self) -> bool:
        return self.algorithm in _WEAK

    def covered(self, raw: bytes) -> bytes:
        """The bytes this checksum is computed over."""
        end = self.offset + self.size
        match self.coverage:
            case Coverage.AFTER:
                return raw[end:]
            case Coverage.BEFORE:
                return raw[: self.offset]
            case Coverage.WHOLE_ZEROED:
                return raw[: self.offset] + bytes(self.size) + raw[end:]

    def compute(self, raw: bytes) -> bytes:
        return _BY_NAME[self.algorithm].compute(self.covered(raw))

    def stored(self, raw: bytes) -> bytes:
        return raw[self.offset : self.offset + self.size]

    def verify(self, raw: bytes) -> bool:
        if self.offset < 0 or self.offset + self.size > len(raw):
            return False
        return self.compute(raw) == self.stored(raw)

    def apply(self, raw: bytes) -> bytes:
        """Return the file with the checksum brought up to date."""
        if self.offset < 0 or self.offset + self.size > len(raw):
            raise ValueError(
                f"the checksum belongs at byte {self.offset} but the file is "
                f"{len(raw)} bytes long"
            )
        patched = bytearray(raw)
        # Zero it first: for whole-file coverage the field's own bytes are part
        # of what is hashed, so leaving the old value in would poison the result.
        patched[self.offset : self.offset + self.size] = bytes(self.size)
        digest = _BY_NAME[self.algorithm].compute(
            ChecksumSpec(self.algorithm, self.offset, self.coverage).covered(bytes(patched))
        )
        patched[self.offset : self.offset + self.size] = digest
        return bytes(patched)

    def describe(self) -> str:
        return f"{self.algorithm} at 0x{self.offset:X}, covering {self.coverage.value}"


def find(raw: bytes, *, include_weak: bool = False) -> list[ChecksumSpec]:
    """Every checksum arrangement that actually holds for this file.

    Strongest first: a 32-byte SHA-256 that fits is certainly the real thing,
    while a one-byte XOR that fits proves almost nothing.
    """
    found: dict[tuple[str, int, Coverage], ChecksumSpec] = {}

    for spec in _candidates(raw):
        if spec.weak and not include_weak:
            continue
        key = (spec.algorithm, spec.offset, spec.coverage)
        if key in found:
            continue
        if spec.verify(raw):
            found[key] = spec

    return sorted(found.values(), key=lambda spec: (-spec.size, spec.offset))


def _candidates(raw: bytes) -> Iterator[ChecksumSpec]:
    """Plausible arrangements, cheapest to check first.

    Rather than testing every offset, each candidate range is hashed once and
    the digest is looked for in the file. That turns "where could it be" into a
    single fast search per range.
    """
    length = len(raw)
    if length < 8:
        return

    for algorithm in ALGORITHMS:
        size = algorithm.size
        if size >= length:
            continue

        # The two arrangements that cover a contiguous run, so the digest can
        # be computed without knowing where it is stored.
        for coverage, data in (
            (Coverage.AFTER, raw[size:]),
            (Coverage.BEFORE, raw[:-size]),
        ):
            digest = algorithm.compute(data)
            start = 0
            while True:
                at = raw.find(digest, start)
                if at == -1:
                    break
                yield ChecksumSpec(algorithm.name, at, coverage)
                start = at + 1

        # Whole-file coverage cannot be searched for, because the field is part
        # of the input. Only the places a header realistically puts it.
        for offset in _header_offsets(length, size):
            yield ChecksumSpec(algorithm.name, offset, Coverage.WHOLE_ZEROED)


def _header_offsets(length: int, size: int) -> Iterator[int]:
    seen: set[int] = set()
    for offset in (*range(0, min(64, length - size), 4), max(0, length - size)):
        if offset not in seen and 0 <= offset <= length - size:
            seen.add(offset)
            yield offset


def spec_from_mapping(data: dict[str, object], *, where: str = "checksum") -> ChecksumSpec:
    """Build a spec from a plugin manifest, complaining precisely if it cannot."""
    algorithm = str(data.get("algorithm", ""))
    if algorithm not in _BY_NAME:
        known = ", ".join(sorted(_BY_NAME))
        raise ValueError(f"{where}: unknown algorithm {algorithm!r}; known ones are {known}")

    offset = data.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError(f"{where}: 'offset' must be a whole number of 0 or more")

    try:
        coverage = Coverage(str(data.get("covers", Coverage.AFTER.value)))
    except ValueError:
        allowed = ", ".join(item.value for item in Coverage)
        raise ValueError(f"{where}: 'covers' must be one of: {allowed}") from None

    return ChecksumSpec(algorithm=algorithm, offset=offset, coverage=coverage)
