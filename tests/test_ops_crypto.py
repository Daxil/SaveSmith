"""Decryption and MessagePack steps."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import msgpack
import pytest
from Crypto.Cipher import AES

from savesmith.core import ops
from savesmith.core.pipeline import Pipeline

KEY = bytes(range(16))
KEY_B64 = base64.b64encode(KEY).decode()
PAYLOAD = json.dumps({"playerData": {"geo": 42}}).encode()


def pad(data: bytes) -> bytes:
    fill = 16 - len(data) % 16
    return data + bytes([fill]) * fill


def encrypt_ecb(data: bytes) -> bytes:
    return AES.new(KEY, AES.MODE_ECB).encrypt(pad(data))


def encrypt_cbc(data: bytes, iv: bytes) -> bytes:
    return iv + AES.new(KEY, AES.MODE_CBC, iv).encrypt(pad(data))


def round_trip(name: str, raw: Any, params: dict[str, Any] | None = None) -> Any:
    operation = ops.get(name)
    hints: dict[str, Any] = {}
    decoded = operation.decode(raw, params or {}, hints)
    return operation.encode(decoded, params or {}, hints)


class TestAes:
    def test_ecb_with_a_static_key(self) -> None:
        """The Hollow Knight arrangement: header, then AES-ECB, then JSON."""
        raw = encrypt_ecb(PAYLOAD)
        assert round_trip("aes_decrypt", raw, {"mode": "ECB", "key_b64": KEY_B64}) == raw

    def test_cbc_with_the_vector_at_the_front(self) -> None:
        raw = encrypt_cbc(PAYLOAD, bytes(range(16, 32)))
        assert round_trip("aes_decrypt", raw, {"mode": "CBC", "key_b64": KEY_B64}) == raw

    def test_the_decrypted_content_is_what_was_encrypted(self) -> None:
        decoded = ops.get("aes_decrypt").decode(
            encrypt_ecb(PAYLOAD), {"mode": "ECB", "key_b64": KEY_B64}, {}
        )
        assert decoded == PAYLOAD

    def test_a_hex_key_works_too(self) -> None:
        raw = encrypt_ecb(PAYLOAD)
        assert round_trip("aes_decrypt", raw, {"mode": "ECB", "key_hex": KEY.hex()}) == raw

    def test_a_wrong_key_says_so_instead_of_producing_rubbish(self) -> None:
        wrong = base64.b64encode(bytes(range(100, 116))).decode()
        with pytest.raises(ValueError, match="key appears to be wrong"):
            round_trip("aes_decrypt", encrypt_ecb(PAYLOAD), {"mode": "ECB", "key_b64": wrong})

    def test_a_key_of_the_wrong_length(self) -> None:
        with pytest.raises(ValueError, match="16, 24 or 32"):
            round_trip(
                "aes_decrypt",
                encrypt_ecb(PAYLOAD),
                {"mode": "ECB", "key_b64": base64.b64encode(b"short").decode()},
            )

    def test_no_key_at_all(self) -> None:
        with pytest.raises(ValueError, match="no key"):
            round_trip("aes_decrypt", encrypt_ecb(PAYLOAD), {"mode": "ECB"})

    def test_truncated_ciphertext(self) -> None:
        with pytest.raises(ValueError, match="whole number of AES blocks"):
            round_trip(
                "aes_decrypt", encrypt_ecb(PAYLOAD)[:-3], {"mode": "ECB", "key_b64": KEY_B64}
            )

    def test_an_unsupported_mode(self) -> None:
        with pytest.raises(ValueError, match="unsupported AES mode"):
            round_trip("aes_decrypt", encrypt_ecb(PAYLOAD), {"mode": "GCM", "key_b64": KEY_B64})

    def test_a_full_pipeline_round_trips(self) -> None:
        raw = encrypt_ecb(PAYLOAD)
        pipeline = Pipeline.from_manifest(
            [
                {"op": "aes_decrypt", "mode": "ECB", "key_b64": KEY_B64},
                {"op": "json_parse"},
            ]
        )
        assert pipeline.round_trip(raw).passed


class TestEasySave3:
    def build(self, password: str, data: bytes, iv: bytes) -> bytes:
        key = hashlib.pbkdf2_hmac("sha1", password.encode(), iv, 100, 16)
        return iv + AES.new(key, AES.MODE_CBC, iv).encrypt(pad(data))

    def test_reading_an_easy_save_3_file(self) -> None:
        raw = self.build("hunter2", PAYLOAD, bytes(range(16)))
        decoded = ops.get("es3_decrypt").decode(raw, {"password": "hunter2"}, {})
        assert decoded == PAYLOAD

    def test_round_trip(self) -> None:
        raw = self.build("hunter2", PAYLOAD, bytes(range(16)))
        assert round_trip("es3_decrypt", raw, {"password": "hunter2"}) == raw

    def test_a_wrong_password(self) -> None:
        raw = self.build("hunter2", PAYLOAD, bytes(range(16)))
        with pytest.raises(ValueError, match="padding makes no sense"):
            round_trip("es3_decrypt", raw, {"password": "wrong"})

    def test_a_file_that_is_too_short(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            round_trip("es3_decrypt", b"tiny", {"password": "x"})

    def test_the_password_is_required_by_the_registry(self) -> None:
        from savesmith.core.errors import PluginValidationError

        with pytest.raises(PluginValidationError, match="password"):
            Pipeline.from_manifest([{"op": "es3_decrypt"}])


class TestMessagePack:
    def test_round_trip(self) -> None:
        raw = msgpack.packb({"geo": 42, "items": ["nail"]}, use_bin_type=True)
        assert round_trip("msgpack", raw) == raw

    def test_the_structure_is_editable(self) -> None:
        raw = msgpack.packb({"geo": 42}, use_bin_type=True)
        assert ops.get("msgpack").decode(raw, {}, {}) == {"geo": 42}

    def test_non_string_map_keys_are_accepted(self) -> None:
        """Refusing them would make some saves unreadable for no benefit."""
        raw = msgpack.packb({1: "one", 2: "two"}, use_bin_type=True)
        assert ops.get("msgpack").decode(raw, {}, {}) == {1: "one", 2: "two"}

    def test_data_that_is_not_msgpack(self) -> None:
        with pytest.raises(ValueError, match="not valid MessagePack"):
            round_trip("msgpack", b"just some text here, honestly")

    def test_a_differently_packed_file_is_caught_by_the_gate(self) -> None:
        """Packers disagree about integer widths; the gate must notice."""
        wide = msgpack.packb({"geo": 42}, use_bin_type=True)
        # Force the same value into a wider integer encoding.
        odd = b"\x81\xa3geo\xd2\x00\x00\x00\x2a"
        assert odd != wide
        result = Pipeline.from_manifest([{"op": "msgpack"}]).round_trip(odd)
        assert result.passed is False
        assert result.rebuild_readable is True
