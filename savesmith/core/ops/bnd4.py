"""BND4 archives — the container FromSoftware saves come in.

An ``ER0000.sl2`` is a BND4 holding twelve entries: ten character slots plus
menu and system data. Each entry is encrypted separately, so this step only
opens the container and hands one entry to the next step in the pipeline::

    { "op": "bnd4", "entry": "USER_DATA000" }
    { "op": "aes_decrypt", ... }

**Everything outside the chosen entry is kept verbatim.** A 28 MB save has one
slot worth editing and eleven that must come back untouched, so the rest of the
file is spliced back exactly where it was rather than rebuilt from a model of a
format we only partly understand.

Entry sizes are fixed by the game: a slot is a fixed-size buffer, not a
variable-length record. Changing one is refused rather than shifting every
offset after it.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from typing import Any

from savesmith.core.ops._registry import Hints, Operation, Params, register

_MAGIC = b"BND4"
_HEADER_SIZE = 0x40
_ENTRY_SIZE = 0x20


def _entries(raw: bytes) -> list[tuple[str, int, int]]:
    """Every entry as (name, offset, size), in the order the header lists them."""
    count = struct.unpack_from("<i", raw, 0x0C)[0]
    if count < 0 or _HEADER_SIZE + count * _ENTRY_SIZE > len(raw):
        raise ValueError(f"the archive claims {count} entries, which does not fit the file")

    found = []
    for index in range(count):
        base = _HEADER_SIZE + index * _ENTRY_SIZE
        size = struct.unpack_from("<q", raw, base + 0x08)[0]
        offset = struct.unpack_from("<i", raw, base + 0x10)[0]
        name_offset = struct.unpack_from("<i", raw, base + 0x14)[0]
        if offset < 0 or size < 0 or offset + size > len(raw):
            raise ValueError(f"entry {index} points outside the file")
        found.append((_read_name(raw, name_offset), offset, size))
    return found


def _read_name(raw: bytes, offset: int) -> str:
    """Entry names are UTF-16, terminated by a zero character."""
    if offset <= 0 or offset >= len(raw):
        return ""
    end = raw.find(b"\x00\x00", offset)
    if end == -1:
        return ""
    # The terminator may straddle an odd boundary; align to the character grid.
    if (end - offset) % 2:
        end += 1
    return raw[offset:end].decode("utf-16-le", errors="replace")


def _decode(payload: Any, params: Params, hints: Hints) -> bytes:
    if not isinstance(payload, bytes | bytearray):
        raise ValueError("bnd4 expects raw bytes but the previous step produced text")
    raw = bytes(payload)
    if not raw.startswith(_MAGIC):
        raise ValueError("the file does not start with the archive marker 'BND4'")

    entries = _entries(raw)
    wanted = params.get("entry", 0)
    match = _select(entries, wanted)
    if match is None:
        names = ", ".join(name for name, _o, _s in entries)
        raise ValueError(f"the archive has no entry called {wanted!r}; it holds: {names}")

    name, offset, size = match
    hints["container"] = raw
    hints["offset"] = offset
    hints["size"] = size
    hints["entry_name"] = name
    hints["entry_names"] = [entry[0] for entry in entries]
    return raw[offset : offset + size]


def _select(
    entries: list[tuple[str, int, int]], wanted: Any
) -> tuple[str, int, int] | None:
    if isinstance(wanted, int) and not isinstance(wanted, bool):
        return entries[wanted] if 0 <= wanted < len(entries) else None
    text = str(wanted)
    return next((entry for entry in entries if entry[0] == text), None)


def _encode(payload: Any, _params: Params, hints: Mapping[str, Any]) -> bytes:
    if not isinstance(payload, bytes | bytearray):
        raise ValueError("bnd4 expects raw bytes but the previous step produced text")
    body = bytes(payload)
    container = hints.get("container")
    if container is None:
        raise ValueError("the rest of the archive was not recorded")

    offset = int(hints["offset"])
    size = int(hints["size"])
    if len(body) != size:
        raise ValueError(
            f"the entry is a fixed {size}-byte buffer but the new contents are "
            f"{len(body)} bytes; the game will not accept a resized slot"
        )
    raw = bytearray(container)
    raw[offset : offset + size] = body
    return bytes(raw)


register(
    Operation(
        name="bnd4",
        decode=_decode,
        encode=_encode,
        optional_params=("entry",),
        summary="Opens one entry of a BND4 archive, keeping the rest of the file untouched",
    )
)
