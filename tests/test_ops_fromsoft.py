"""FromSoftware save slots: the MD5 header and optional encryption."""

from __future__ import annotations

import hashlib

import pytest
from Crypto.Cipher import AES

from savesmith.core.errors import PipelineError
from savesmith.core.pipeline import Pipeline
from tests.test_ops_bnd4 import REAL_SAVE, build

# Elden Ring's static save key. A per-game constant, which is why it belongs in
# a plugin manifest rather than in the core.
ER_KEY = "18F68356F5C3A1AB6A15E4B7A6ED9A3E"

PAYLOAD = b"character data" + b"\x00" * 2034


def plain_slot(payload: bytes = PAYLOAD) -> bytes:
    return hashlib.md5(payload).digest() + payload


def encrypted_slot(payload: bytes = PAYLOAD, key_hex: str = ER_KEY) -> bytes:
    block = plain_slot(payload)
    iv = bytes(range(16))
    return iv + AES.new(bytes.fromhex(key_hex), AES.MODE_CBC, iv).encrypt(block)


def pipeline(key_hex: str | None = ER_KEY) -> Pipeline:
    step: dict[str, object] = {"op": "fromsoft_slot"}
    if key_hex is not None:
        step["key_hex"] = key_hex
    return Pipeline.from_manifest([step])


class TestPlainSlots:
    """Pirated builds and Seamless Co-op write slots in the clear."""

    def test_the_payload_comes_out(self) -> None:
        decoded = pipeline().decode(plain_slot())
        assert decoded.value == PAYLOAD
        assert decoded.hints[0]["encrypted"] is False

    def test_round_trip(self) -> None:
        assert pipeline().round_trip(plain_slot()).passed

    def test_no_key_is_needed(self) -> None:
        assert pipeline(key_hex=None).decode(plain_slot()).value == PAYLOAD


class TestEncryptedSlots:
    def test_the_payload_comes_out(self) -> None:
        decoded = pipeline().decode(encrypted_slot())
        assert decoded.value == PAYLOAD
        assert decoded.hints[0]["encrypted"] is True

    def test_round_trip(self) -> None:
        assert pipeline().round_trip(encrypted_slot()).passed

    def test_a_wrong_key_is_reported_not_written(self) -> None:
        wrong = "00112233445566778899AABBCCDDEEFF"
        with pytest.raises(PipelineError) as caught:
            pipeline(wrong).decode(encrypted_slot())
        assert "key does not fit" in caught.value.user_message

    def test_an_encrypted_slot_without_a_key(self) -> None:
        with pytest.raises(PipelineError, match="no key"):
            pipeline(key_hex=None).decode(encrypted_slot())

    def test_a_key_of_the_wrong_length(self) -> None:
        with pytest.raises(PipelineError, match="16, 24 or 32"):
            pipeline("0011").decode(encrypted_slot())


class TestChecksum:
    def test_the_checksum_is_recalculated_after_an_edit(self) -> None:
        """A stale checksum is what makes the game reject or 'repair' a save."""
        pipe = pipeline()
        decoded = pipe.decode(plain_slot())
        edited = bytearray(decoded.value)
        edited[0:5] = b"EDITS"

        rebuilt = pipe.encode(bytes(edited), decoded.hints, passthrough=False)
        assert rebuilt[:16] == hashlib.md5(bytes(edited)).digest()
        assert pipe.decode(rebuilt).value == bytes(edited)

    def test_the_checksum_is_recalculated_for_encrypted_slots_too(self) -> None:
        pipe = pipeline()
        decoded = pipe.decode(encrypted_slot())
        edited = bytearray(decoded.value)
        edited[0:5] = b"EDITS"
        rebuilt = pipe.encode(bytes(edited), decoded.hints, passthrough=False)
        assert pipe.decode(rebuilt).value == bytes(edited)

    def test_a_damaged_slot_is_refused(self) -> None:
        broken = bytearray(plain_slot())
        broken[0] ^= 0xFF  # break the checksum without a key in play
        with pytest.raises(PipelineError, match=r"no key|key does not fit"):
            pipeline(key_hex=None).decode(bytes(broken))

    def test_a_slot_that_is_too_short(self) -> None:
        with pytest.raises(PipelineError, match="too short"):
            pipeline().decode(b"tiny")


class TestThroughTheContainer:
    def test_a_whole_archive_round_trips(self) -> None:
        archive = build({"USER_DATA000": plain_slot(), "USER_DATA001": plain_slot(b"\x01" * 512)})
        pipe = Pipeline.from_manifest(
            [{"op": "bnd4", "entry": "USER_DATA000"}, {"op": "fromsoft_slot"}]
        )
        assert pipe.round_trip(archive).passed

    def test_editing_one_slot_leaves_the_archive_intact(self) -> None:
        archive = build({"USER_DATA000": plain_slot(), "USER_DATA001": plain_slot(b"\x02" * 512)})
        pipe = Pipeline.from_manifest(
            [{"op": "bnd4", "entry": "USER_DATA000"}, {"op": "fromsoft_slot"}]
        )
        decoded = pipe.decode(archive)
        edited = bytearray(decoded.value)
        edited[0:5] = b"EDITS"
        rebuilt = pipe.encode(bytes(edited), decoded.hints, passthrough=False)

        assert len(rebuilt) == len(archive)
        other = Pipeline.from_manifest(
            [{"op": "bnd4", "entry": "USER_DATA001"}, {"op": "fromsoft_slot"}]
        )
        assert other.decode(rebuilt).value == other.decode(archive).value


@pytest.mark.skipif(not REAL_SAVE.is_file(), reason="no local Elden Ring save")
class TestRealSave:
    """Against a real ER0000.sl2. Not committed: 28 MB of someone's progress."""

    @pytest.mark.parametrize("slot", ["USER_DATA000", "USER_DATA001", "USER_DATA010"])
    def test_round_trip_is_byte_exact(self, slot: str) -> None:
        pipe = Pipeline.from_manifest(
            [{"op": "bnd4", "entry": slot}, {"op": "fromsoft_slot", "key_hex": ER_KEY}]
        )
        assert pipe.round_trip(REAL_SAVE.read_bytes()).passed

    def test_the_checksum_identifies_an_unencrypted_save(self) -> None:
        """This build writes slots in the clear; the checksum says so on its own."""
        pipe = Pipeline.from_manifest(
            [{"op": "bnd4", "entry": "USER_DATA000"}, {"op": "fromsoft_slot", "key_hex": ER_KEY}]
        )
        decoded = pipe.decode(REAL_SAVE.read_bytes())
        assert decoded.hints[1]["encrypted"] is False
        assert len(decoded.value) == 0x280000
