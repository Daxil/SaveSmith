"""MessagePack.

Used by Unity games that want something smaller and faster than JSON. Unlike
JSON there is no formatting to preserve, but there is a subtler trap: the same
value has several valid encodings (a small integer fits in one byte or five),
and packers disagree about which to choose. When the game's packer made
different choices than ours, the round-trip gate says so instead of quietly
rewriting every number in the file.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import msgpack

from savesmith.core.ops._registry import Hints, Operation, Params, register


def _decode(payload: Any, params: Params, _hints: Hints) -> Any:
    if not isinstance(payload, bytes | bytearray):
        raise ValueError("msgpack expects raw bytes but the previous step produced text")
    try:
        return msgpack.unpackb(
            bytes(payload),
            raw=False,
            # Some engines use integers or floats as map keys; refusing them
            # would make the file unreadable for no benefit.
            strict_map_key=False,
        )
    except (msgpack.exceptions.ExtraData, ValueError, TypeError) as exc:
        raise ValueError(f"the data is not valid MessagePack ({exc})") from exc


def _encode(payload: Any, params: Params, _hints: Mapping[str, Any]) -> bytes:
    try:
        packed = msgpack.packb(payload, use_bin_type=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"this value cannot be written as MessagePack ({exc})") from exc
    if packed is None:  # pragma: no cover - msgpack only returns None for None input
        raise ValueError("nothing to write")
    return bytes(packed)


register(
    Operation(
        name="msgpack",
        decode=_decode,
        encode=_encode,
        summary="Reads MessagePack data",
    )
)
