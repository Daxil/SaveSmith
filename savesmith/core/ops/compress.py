"""Compression steps.

Both gzip and zlib record enough to rebuild their container: the compression
level, and for gzip the timestamp, the stored filename and the OS byte. Games
never look at any of that, but the round-trip gate does, and a plugin that
cannot reproduce a byte it did not have to change has not been understood
properly.

The compression level is recovered by trying every level and comparing the
result. That sounds crude; it costs microseconds on a save file and it is the
only way, since the level is not stored anywhere in the stream.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Mapping
from typing import Any

from savesmith.core.ops._registry import Hints, Operation, Params, register

_LEVELS = (6, 9, 1, 2, 3, 4, 5, 7, 8)  # most common first
_DEFAULT_LEVEL = 6

_FTEXT, _FHCRC, _FEXTRA, _FNAME, _FCOMMENT = 1, 2, 4, 8, 16


def _as_bytes(payload: Any, what: str) -> bytes:
    if isinstance(payload, bytes | bytearray):
        return bytes(payload)
    raise ValueError(f"{what} expects raw bytes but the previous step produced text")


def _detect_level(raw_deflate: bytes, payload: bytes, wbits: int) -> int | None:
    """Which level reproduces this exact stream, if any.

    ``None`` means the file was compressed by something other than zlib — .NET
    and Java both ship deflate implementations that make different choices.
    The data is still read correctly; only re-encoding will differ.
    """
    for level in _LEVELS:
        compressor = zlib.compressobj(level, zlib.DEFLATED, wbits)
        if compressor.compress(payload) + compressor.flush() == raw_deflate:
            return level
    return None


# ---------------------------------------------------------------------------
# gzip
# ---------------------------------------------------------------------------


def _gzip_decode(payload: Any, _params: Params, hints: Hints) -> bytes:
    raw = _as_bytes(payload, "gzip")
    if len(raw) < 18 or raw[0:2] != b"\x1f\x8b":
        raise ValueError("the data does not start with a gzip header")
    if raw[2] != 8:
        raise ValueError(f"the gzip stream uses compression method {raw[2]}, which is not deflate")

    flags = raw[3]
    hints["mtime"] = struct.unpack("<I", raw[4:8])[0]
    hints["xfl"] = raw[8]
    hints["os"] = raw[9]
    hints["flags"] = flags

    offset = 10
    if flags & _FEXTRA:
        extra_length = struct.unpack("<H", raw[offset : offset + 2])[0]
        hints["extra"] = raw[offset + 2 : offset + 2 + extra_length]
        offset += 2 + extra_length
    if flags & _FNAME:
        end = raw.index(b"\x00", offset)
        hints["name"] = raw[offset:end]
        offset = end + 1
    if flags & _FCOMMENT:
        end = raw.index(b"\x00", offset)
        hints["comment"] = raw[offset:end]
        offset = end + 1
    if flags & _FHCRC:
        offset += 2

    body = raw[offset:-8]
    try:
        decompressed = zlib.decompressobj(-zlib.MAX_WBITS).decompress(body)
    except zlib.error as exc:
        raise ValueError(f"the compressed data is damaged ({exc})") from exc

    stored_crc, stored_size = struct.unpack("<II", raw[-8:])
    if stored_size != len(decompressed) & 0xFFFFFFFF:
        raise ValueError("the file claims a different uncompressed size than it contains")
    if stored_crc != zlib.crc32(decompressed):
        raise ValueError("the checksum inside the compressed data does not match")

    hints["level"] = _detect_level(body, decompressed, -zlib.MAX_WBITS)
    return decompressed


def _gzip_encode(payload: Any, _params: Params, hints: Mapping[str, Any]) -> bytes:
    raw = _as_bytes(payload, "gzip")
    level = hints.get("level")
    flags = int(hints.get("flags", 0))

    header = bytearray(b"\x1f\x8b\x08")
    header.append(flags)
    header += struct.pack("<I", int(hints.get("mtime", 0)))
    header.append(int(hints.get("xfl", 0)))
    header.append(int(hints.get("os", 255)))
    if flags & _FEXTRA:
        extra = bytes(hints.get("extra", b""))
        header += struct.pack("<H", len(extra)) + extra
    if flags & _FNAME:
        header += bytes(hints.get("name", b"")) + b"\x00"
    if flags & _FCOMMENT:
        header += bytes(hints.get("comment", b"")) + b"\x00"
    if flags & _FHCRC:
        header += struct.pack("<H", zlib.crc32(bytes(header)) & 0xFFFF)

    compressor = zlib.compressobj(
        _DEFAULT_LEVEL if level is None else int(level), zlib.DEFLATED, -zlib.MAX_WBITS
    )
    body = compressor.compress(raw) + compressor.flush()
    trailer = struct.pack("<II", zlib.crc32(raw), len(raw) & 0xFFFFFFFF)
    return bytes(header) + body + trailer


register(
    Operation(
        name="gzip",
        decode=_gzip_decode,
        encode=_gzip_encode,
        summary="Unpacks a gzip stream, remembering its header so it can be rebuilt",
    )
)


# ---------------------------------------------------------------------------
# zlib
# ---------------------------------------------------------------------------


def _zlib_decode(payload: Any, params: Params, hints: Hints) -> bytes:
    raw = _as_bytes(payload, "zlib")
    wbits = int(params.get("wbits", zlib.MAX_WBITS))
    try:
        decompressed = zlib.decompressobj(wbits).decompress(raw)
    except zlib.error as exc:
        raise ValueError(f"the data is not a valid zlib stream ({exc})") from exc
    hints["level"] = _detect_level(raw, decompressed, wbits)
    return decompressed


def _zlib_encode(payload: Any, params: Params, hints: Mapping[str, Any]) -> bytes:
    raw = _as_bytes(payload, "zlib")
    wbits = int(params.get("wbits", zlib.MAX_WBITS))
    level = hints.get("level")
    compressor = zlib.compressobj(
        _DEFAULT_LEVEL if level is None else int(level), zlib.DEFLATED, wbits
    )
    return compressor.compress(raw) + compressor.flush()


register(
    Operation(
        name="zlib",
        decode=_zlib_decode,
        encode=_zlib_encode,
        optional_params=("wbits",),
        summary="Unpacks a raw zlib stream",
    )
)
