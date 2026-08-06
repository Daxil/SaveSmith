"""Decryption steps.

Games encrypt saves to slow down casual editing, not to keep secrets: the key
ships inside the game's own files, which is why a plugin can carry it. Nothing
here is a way around DRM — an encrypted save is still the player's own data on
the player's own disk.

Two operations:

``aes_decrypt``
    A key the plugin already knows, in ECB or CBC.
``es3_decrypt``
    Unity's Easy Save 3, which derives its key from a password with PBKDF2 and
    puts the initialisation vector at the front of the file. Common enough to
    deserve its own step rather than five settings on the general one.

Re-encrypting reproduces the original bytes exactly, because AES with the same
key, mode and IV is deterministic and PKCS#7 padding has only one valid form.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from savesmith.core.ops._registry import Hints, Operation, Params, register

_BLOCK = 16
_ES3_ITERATIONS = 100
_ES3_KEY_LENGTH = 16


def _as_bytes(payload: Any, what: str) -> bytes:
    if isinstance(payload, bytes | bytearray):
        return bytes(payload)
    raise ValueError(f"{what} expects raw bytes but the previous step produced text")


def _key_from(params: Params) -> bytes:
    if "key_b64" in params:
        try:
            key = base64.b64decode(str(params["key_b64"]), validate=True)
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise ValueError(f"the plugin's key is not valid base64 ({exc})") from exc
    elif "key_hex" in params:
        try:
            key = bytes.fromhex(str(params["key_hex"]))
        except ValueError as exc:
            raise ValueError(f"the plugin's key is not valid hex ({exc})") from exc
    else:
        raise ValueError("the plugin gives no key")
    if len(key) not in (16, 24, 32):
        raise ValueError(f"the plugin's key is {len(key)} bytes; AES needs 16, 24 or 32")
    return key


def _unpad(data: bytes, padding: str) -> bytes:
    if padding == "none":
        return data
    if not data:
        raise ValueError("the decrypted data is empty")
    length = data[-1]
    if length < 1 or length > _BLOCK or len(data) < length:
        raise ValueError("the key appears to be wrong: the decrypted padding makes no sense")
    if data[-length:] != bytes([length]) * length:
        raise ValueError("the key appears to be wrong: the decrypted padding makes no sense")
    return data[:-length]


def _pad(data: bytes, padding: str) -> bytes:
    if padding == "none":
        if len(data) % _BLOCK:
            raise ValueError(
                f"the data is {len(data)} bytes, which AES cannot encrypt without padding"
            )
        return data
    fill = _BLOCK - (len(data) % _BLOCK)
    return data + bytes([fill]) * fill


def _cipher(key: bytes, mode: str, iv: bytes | None) -> Cipher[Any]:
    if mode == "ECB":
        # ECB is a poor choice cryptographically, but it is the game's choice
        # and the file has to be read as it was written.
        return Cipher(algorithms.AES(key), modes.ECB())
    if mode == "CBC":
        if iv is None or len(iv) != _BLOCK:
            raise ValueError("CBC needs a 16-byte initialisation vector")
        return Cipher(algorithms.AES(key), modes.CBC(iv))
    raise ValueError(f"unsupported AES mode {mode!r}")


def _aes_decode(payload: Any, params: Params, hints: Hints) -> bytes:
    raw = _as_bytes(payload, "aes_decrypt")
    mode = str(params.get("mode", "CBC")).upper()
    padding = str(params.get("padding", "pkcs7")).lower()

    iv: bytes | None = None
    if mode == "CBC":
        source = str(params.get("iv_source", "prefix"))
        if source == "prefix":
            if len(raw) < _BLOCK:
                raise ValueError("the file is too short to contain an initialisation vector")
            iv, raw = raw[:_BLOCK], raw[_BLOCK:]
            hints["iv_from_prefix"] = True
        else:
            iv = base64.b64decode(str(params["iv_b64"]))
        hints["iv"] = iv

    if len(raw) % _BLOCK:
        raise ValueError(
            f"the encrypted part is {len(raw)} bytes, which is not a whole number of AES blocks"
        )

    decryptor = _cipher(_key_from(params), mode, iv).decryptor()
    return _unpad(decryptor.update(raw) + decryptor.finalize(), padding)


def _aes_encode(payload: Any, params: Params, hints: Mapping[str, Any]) -> bytes:
    raw = _as_bytes(payload, "aes_decrypt")
    mode = str(params.get("mode", "CBC")).upper()
    padding = str(params.get("padding", "pkcs7")).lower()
    iv = hints.get("iv")

    encryptor = _cipher(_key_from(params), mode, iv).encryptor()
    body = encryptor.update(_pad(raw, padding)) + encryptor.finalize()
    # The same IV goes back where it came from; a fresh random one would be
    # cryptographically tidier and would also change bytes for no reason.
    return (bytes(iv) + body) if hints.get("iv_from_prefix") and iv else body


register(
    Operation(
        name="aes_decrypt",
        decode=_aes_decode,
        encode=_aes_encode,
        optional_params=("mode", "padding", "key_b64", "key_hex", "iv_source", "iv_b64"),
        summary="Decrypts with a key the plugin carries",
    )
)


# ---------------------------------------------------------------------------
# Easy Save 3
# ---------------------------------------------------------------------------


def _es3_key(password: str, iv: bytes) -> bytes:
    """Easy Save 3's key derivation: PBKDF2-HMAC-SHA1, 100 rounds, IV as salt."""
    return hashlib.pbkdf2_hmac(
        "sha1", password.encode("utf-8"), iv, _ES3_ITERATIONS, _ES3_KEY_LENGTH
    )


def _es3_decode(payload: Any, params: Params, hints: Hints) -> bytes:
    raw = _as_bytes(payload, "es3_decrypt")
    if len(raw) < _BLOCK * 2:
        raise ValueError("the file is too short to be an encrypted Easy Save 3 file")
    iv, body = raw[:_BLOCK], raw[_BLOCK:]
    if len(body) % _BLOCK:
        raise ValueError("the encrypted part is not a whole number of AES blocks")
    hints["iv"] = iv

    password = str(params.get("password", "")) or None
    if password is None:
        raise ValueError("the plugin gives no Easy Save 3 password")

    decryptor = Cipher(algorithms.AES(_es3_key(password, iv)), modes.CBC(iv)).decryptor()
    return _unpad(decryptor.update(body) + decryptor.finalize(), "pkcs7")


def _es3_encode(payload: Any, params: Params, hints: Mapping[str, Any]) -> bytes:
    raw = _as_bytes(payload, "es3_decrypt")
    iv = bytes(hints.get("iv", b""))
    if len(iv) != _BLOCK:
        raise ValueError("the original initialisation vector was not recorded")
    password = str(params.get("password", ""))
    encryptor = Cipher(algorithms.AES(_es3_key(password, iv)), modes.CBC(iv)).encryptor()
    return iv + encryptor.update(_pad(raw, "pkcs7")) + encryptor.finalize()


register(
    Operation(
        name="es3_decrypt",
        decode=_es3_decode,
        encode=_es3_encode,
        required_params=("password",),
        summary="Decrypts a Unity Easy Save 3 file",
    )
)
