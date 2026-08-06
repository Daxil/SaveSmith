"""LZString — the compression RPG Maker wraps its saves in."""

from __future__ import annotations

import json

import pytest

from savesmith.core import ops
from savesmith.core.ops.lzstring import compress_to_base64, decompress_from_base64
from savesmith.core.pipeline import Pipeline

# From an independent implementation (go-lz-string). A compressor that merely
# produces *valid* LZString would rewrite every save it touches, so being
# byte-identical to the reference is the whole requirement.
REFERENCE_INPUT = "🍎🍇🍌"
REFERENCE_OUTPUT = "jwbjl96cX3kGX2g="

SAVE = {"party": {"_gold": 12400, "_actors": [1, 2]}, "switches": {"_data": [None, True]}}


class TestAgainstTheReference:
    def test_compressing_matches_the_reference_byte_for_byte(self) -> None:
        assert compress_to_base64(REFERENCE_INPUT) == REFERENCE_OUTPUT

    def test_decompressing_the_reference(self) -> None:
        assert decompress_from_base64(REFERENCE_OUTPUT) == REFERENCE_INPUT

    def test_astral_characters_count_as_two(self) -> None:
        """JavaScript strings are UTF-16; an emoji is two characters there."""
        assert compress_to_base64("🍎") != compress_to_base64("\ud83c")


class TestRoundTrip:
    @pytest.mark.parametrize(
        "text",
        [
            "hello",
            "a" * 500,
            json.dumps(SAVE),
            "Привет мир",
            "mixed Кириллица and 🍎 emoji",
            "x",
            '{"nested":{"deep":{"deeper":[1,2,3]}}}',
        ],
    )
    def test_text_survives(self, text: str) -> None:
        assert decompress_from_base64(compress_to_base64(text)) == text

    def test_an_empty_string(self) -> None:
        assert compress_to_base64("") == ""
        assert decompress_from_base64("") == ""

    def test_repetition_actually_compresses(self) -> None:
        """If it did not, the port would be wrong in a way tests could miss."""
        assert len(compress_to_base64("ab" * 500)) < 200


class TestOperation:
    def test_the_pipeline_reads_an_rpg_maker_save(self) -> None:
        raw = compress_to_base64(json.dumps(SAVE)).encode()
        pipeline = Pipeline.from_manifest([{"op": "lzstring"}, {"op": "json_parse"}])
        assert pipeline.decode(raw).value == SAVE

    def test_round_trip_through_the_pipeline(self) -> None:
        raw = compress_to_base64(json.dumps(SAVE)).encode()
        assert Pipeline.from_manifest([{"op": "lzstring"}, {"op": "json_parse"}]).round_trip(
            raw
        ).passed

    def test_an_edit_survives(self) -> None:
        raw = compress_to_base64(json.dumps(SAVE)).encode()
        pipeline = Pipeline.from_manifest([{"op": "lzstring"}, {"op": "json_parse"}])
        decoded = pipeline.decode(raw)
        decoded.value["party"]["_gold"] = 999999

        rewritten = pipeline.encode(decoded.value, decoded.hints)
        assert pipeline.decode(rewritten).value["party"]["_gold"] == 999999

    def test_surrounding_whitespace_is_preserved(self) -> None:
        raw = ("\n" + compress_to_base64(json.dumps(SAVE)) + "\n").encode()
        assert Pipeline.from_manifest([{"op": "lzstring"}]).round_trip(raw).passed

    def test_data_that_is_not_lzstring(self) -> None:
        with pytest.raises(ValueError, match=r"not LZString|not text"):
            ops.get("lzstring").decode(b"just plain text, not compressed", {}, {})

    def test_an_empty_file(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            ops.get("lzstring").decode(b"   ", {}, {})

    def test_binary_input_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not text"):
            ops.get("lzstring").decode(b"\xff\xfe\x00\x01", {}, {})
