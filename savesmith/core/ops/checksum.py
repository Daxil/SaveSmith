"""Keeping a save's checksum right.

Written as an ordinary pipeline step, which means it belongs first in the list:
running forwards it is the first thing that sees the file, so running backwards
it is the last thing to touch it — exactly when the rest of the bytes are final
and the checksum can be computed over them.

Reading only checks. A save whose checksum is already wrong is not refused —
another tool may have edited it, or the game may never verify it — but the fact
is recorded so the interface can say so.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from savesmith.core.checksum import ChecksumSpec, spec_from_mapping
from savesmith.core.ops._registry import Hints, Operation, Params, register


def _spec_of(params: Params) -> ChecksumSpec:
    try:
        return spec_from_mapping(dict(params), where="checksum step")
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _decode(payload: Any, params: Params, hints: Hints) -> bytes:
    if not isinstance(payload, bytes | bytearray):
        raise ValueError("checksum expects raw bytes but the previous step produced text")
    raw = bytes(payload)
    spec = _spec_of(params)
    if spec.offset + spec.size > len(raw):
        raise ValueError(
            f"the plugin puts a checksum at byte {spec.offset}, past the end of "
            f"this {len(raw)}-byte file"
        )
    hints["held"] = spec.verify(raw)
    return raw


def _encode(payload: Any, params: Params, _hints: Mapping[str, Any]) -> bytes:
    if not isinstance(payload, bytes | bytearray):
        raise ValueError("checksum expects raw bytes but the previous step produced text")
    return _spec_of(params).apply(bytes(payload))


register(
    Operation(
        name="checksum",
        decode=_decode,
        encode=_encode,
        required_params=("algorithm", "offset"),
        optional_params=("covers",),
        summary="Checks the save's checksum when reading and recalculates it when writing",
    )
)
