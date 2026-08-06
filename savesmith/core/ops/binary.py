"""Byte-level steps: headers, XOR, base64."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import Any

from savesmith.core.ops._registry import Hints, Operation, Params, register


def _as_bytes(payload: Any, what: str) -> bytes:
    if isinstance(payload, bytes | bytearray):
        return bytes(payload)
    raise ValueError(f"{what} expects raw bytes but the previous step produced text")


# ---------------------------------------------------------------------------
# strip_prefix
# ---------------------------------------------------------------------------


def _strip_prefix_decode(payload: Any, params: Params, hints: Hints) -> bytes:
    raw = _as_bytes(payload, "strip_prefix")
    count = int(params["bytes"])
    if count < 0:
        raise ValueError("the plugin asks to strip a negative number of bytes")
    if len(raw) < count:
        raise ValueError(
            f"the file is only {len(raw)} bytes long but the plugin expects a "
            f"{count}-byte header"
        )
    # Kept verbatim rather than regenerated: these headers carry version bytes
    # and lengths we have no business inventing.
    hints["prefix"] = raw[:count]
    return raw[count:]


def _strip_prefix_encode(payload: Any, params: Params, hints: Mapping[str, Any]) -> bytes:
    raw = _as_bytes(payload, "strip_prefix")
    prefix = hints.get("prefix")
    if prefix is None:
        raise ValueError("the original file header was not recorded, so it cannot be restored")
    return bytes(prefix) + raw


register(
    Operation(
        name="strip_prefix",
        decode=_strip_prefix_decode,
        encode=_strip_prefix_encode,
        required_params=("bytes",),
        summary="Removes a fixed-size header and puts it back unchanged when saving",
    )
)


# ---------------------------------------------------------------------------
# xor
# ---------------------------------------------------------------------------


def _xor(payload: Any, params: Params, _hints: Any) -> bytes:
    raw = _as_bytes(payload, "xor")
    try:
        key = bytes.fromhex(str(params["key_hex"]))
    except ValueError as exc:
        raise ValueError(f"the plugin's XOR key is not valid hex ({exc})") from exc
    if not key:
        raise ValueError("the plugin's XOR key is empty")
    return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(raw))


register(
    Operation(
        name="xor",
        decode=_xor,
        # XOR is its own inverse, which is the whole reason games use it.
        encode=_xor,
        required_params=("key_hex",),
        summary="Applies a repeating XOR key",
    )
)


# ---------------------------------------------------------------------------
# base64
# ---------------------------------------------------------------------------

_ALPHABETS = ("standard", "urlsafe")


def _b64_decode(body: str, variant: str) -> bytes:
    if variant == "urlsafe":
        # Translated rather than passed to urlsafe_b64decode, which offers no
        # validation — and validation is the whole point here.
        body = body.replace("-", "+").replace("_", "/")
    # validate=True matters more than it looks: without it, Python quietly
    # drops every character outside the alphabet, so any prose at all
    # "decodes" successfully. The decoder ladder then reports base64 for
    # ordinary text files.
    return base64.b64decode(body, validate=True)


def _base64_decode(payload: Any, params: Params, hints: Hints) -> bytes:
    raw = _as_bytes(payload, "base64")
    variant = str(params.get("variant", "standard"))
    if variant not in _ALPHABETS:
        raise ValueError(f"unknown base64 variant {variant!r}")

    text = raw.decode("ascii", errors="strict") if _is_ascii(raw) else None
    if text is None:
        raise ValueError("the data is not base64 text")

    body = "".join(text.split())
    # Line wrapping and the trailing newline are part of the file, so they are
    # measured rather than assumed.
    hints["line_width"] = _line_width(text)
    hints["trailing"] = text[len(text.rstrip()) :]
    try:
        return _b64_decode(body, variant)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"the base64 text is malformed ({exc})") from exc


def _base64_encode(payload: Any, params: Params, hints: Mapping[str, Any]) -> bytes:
    raw = _as_bytes(payload, "base64")
    variant = str(params.get("variant", "standard"))
    encoder = base64.urlsafe_b64encode if variant == "urlsafe" else base64.b64encode
    text = encoder(raw).decode("ascii")
    width = int(hints.get("line_width", 0))
    if width > 0:
        text = "\n".join(text[start : start + width] for start in range(0, len(text), width))
    return (text + str(hints.get("trailing", ""))).encode("ascii")


def _is_ascii(raw: bytes) -> bool:
    return all(byte < 128 for byte in raw)


def _line_width(text: str) -> int:
    """0 when the text is one long line, otherwise the wrap width."""
    lines = [line for line in text.splitlines() if line]
    if len(lines) < 2:
        return 0
    first = len(lines[0])
    # Only the last line is allowed to be shorter.
    if all(len(line) == first for line in lines[:-1]) and len(lines[-1]) <= first:
        return first
    return 0


register(
    Operation(
        name="base64",
        decode=_base64_decode,
        encode=_base64_encode,
        optional_params=("variant",),
        summary="Decodes base64 text, preserving line wrapping",
    )
)
