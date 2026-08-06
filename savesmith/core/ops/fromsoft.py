"""FromSoftware save slots: the MD5 header, and encryption that may not be there.

Inside a BND4 archive each slot is laid out as::

    [16 bytes MD5 of everything after it][payload]

optionally wrapped in AES-128-CBC with the initialisation vector stored in
front. Whether a given file is wrapped cannot be assumed: pirated builds and
the Seamless Co-op mod both write slots in the clear, and a save previously
opened by another tool may have been left decrypted. Guessing wrong destroys
the save.

So it is not guessed. The MD5 header is its own oracle: if the checksum matches
the bytes as they are, the slot is plain; if it matches after decryption, the
key is right. If neither matches, something is wrong and we stop rather than
write rubbish over someone's playthrough.

The key itself lives in the plugin manifest, not here. It is a per-game
constant, and hard-coding one game into the core is exactly the coupling the
plugin format exists to avoid.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from savesmith.core.ops._registry import Hints, Operation, Params, register

_DIGEST = 16
_BLOCK = 16


def _as_bytes(payload: Any) -> bytes:
    if isinstance(payload, bytes | bytearray):
        return bytes(payload)
    raise ValueError("fromsoft_slot expects raw bytes but the previous step produced text")


def _checksum_holds(block: bytes) -> bool:
    return len(block) > _DIGEST and hashlib.md5(block[_DIGEST:]).digest() == block[:_DIGEST]


def _key_from(params: Params) -> bytes | None:
    raw = params.get("key_hex")
    if raw is None:
        return None
    try:
        key = bytes.fromhex(str(raw))
    except ValueError as exc:
        raise ValueError(f"the plugin's key is not valid hex ({exc})") from exc
    if len(key) not in (16, 24, 32):
        raise ValueError(f"the plugin's key is {len(key)} bytes; AES needs 16, 24 or 32")
    return key


def _decode(payload: Any, params: Params, hints: Hints) -> bytes:
    blob = _as_bytes(payload)
    if len(blob) <= _DIGEST:
        raise ValueError("the save slot is too short to contain anything")

    # Case one: no encryption at all. Common in pirated builds and with
    # Seamless Co-op, and the checksum proves it rather than a guess.
    if _checksum_holds(blob):
        hints["encrypted"] = False
        return blob[_DIGEST:]

    key = _key_from(params)
    if key is None:
        raise ValueError(
            "this save slot is encrypted and the plugin carries no key for this game"
        )
    if (len(blob) - _BLOCK) % _BLOCK:
        raise ValueError("the encrypted part is not a whole number of AES blocks")

    iv, body = blob[:_BLOCK], blob[_BLOCK:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(body) + decryptor.finalize()
    if not _checksum_holds(plain):
        raise ValueError(
            "the slot could not be read: the key does not fit this game's saves, "
            "or the file is damaged"
        )

    hints["encrypted"] = True
    hints["iv"] = iv
    return plain[_DIGEST:]


def _encode(payload: Any, params: Params, hints: Mapping[str, Any]) -> bytes:
    body = _as_bytes(payload)
    # Recomputed, never carried over: a stale checksum is exactly what makes
    # the game reject or silently "repair" an edited save.
    block = hashlib.md5(body).digest() + body

    if not hints.get("encrypted"):
        return block

    key = _key_from(params)
    iv = bytes(hints.get("iv", b""))
    if key is None or len(iv) != _BLOCK:
        raise ValueError("the key or initialisation vector for this slot was not recorded")
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return iv + encryptor.update(block) + encryptor.finalize()


register(
    Operation(
        name="fromsoft_slot",
        decode=_decode,
        encode=_encode,
        optional_params=("key_hex",),
        summary="Opens a FromSoftware save slot and recalculates its checksum on save",
    )
)
