"""Running a format forwards and backwards."""

from __future__ import annotations

import gzip
import json
import zlib
from typing import Any

import pytest

from savesmith.core import ops
from savesmith.core.errors import (
    PipelineError,
    PluginValidationError,
    UnknownOperationError,
)
from savesmith.core.pipeline import Pipeline

SAVE = {"playerData": {"geo": 42, "maxHealth": 5}}


def gzipped_json(indent: int | None = None, level: int = 6) -> bytes:
    return gzip.compress(json.dumps(SAVE, indent=indent).encode(), compresslevel=level, mtime=0)


def json_pipeline() -> Pipeline:
    return Pipeline.from_manifest([{"op": "gzip"}, {"op": "json_parse"}], plugin_id="test")


class TestConstruction:
    def test_steps_come_from_the_manifest(self) -> None:
        pipeline = Pipeline.from_manifest(
            [{"op": "strip_prefix", "bytes": 22}, {"op": "json_parse"}]
        )
        assert [step.op for step in pipeline.steps] == ["strip_prefix", "json_parse"]
        assert pipeline.steps[0].params == {"bytes": 22}

    def test_an_unknown_operation_is_refused_at_load_time(self) -> None:
        """Better now than halfway through writing someone's save."""
        with pytest.raises(UnknownOperationError):
            Pipeline.from_manifest([{"op": "decrypt_with_magic"}])

    def test_a_missing_required_setting_names_it(self) -> None:
        with pytest.raises(PluginValidationError) as caught:
            Pipeline.from_manifest([{"op": "strip_prefix"}], plugin_id="hollow-knight")
        assert "bytes" in caught.value.user_message
        assert "hollow-knight" in caught.value.user_message

    def test_an_unrecognised_setting_is_refused(self) -> None:
        """A typo in a plugin must not be silently ignored."""
        with pytest.raises(PluginValidationError) as caught:
            Pipeline.from_manifest([{"op": "strip_prefix", "bytes": 4, "byte": 5}])
        assert "byte" in caught.value.user_message

    def test_a_step_without_an_op_name(self) -> None:
        with pytest.raises(PluginValidationError, match="no 'op' name"):
            Pipeline.from_manifest([{"bytes": 4}])


class TestDecodeEncode:
    def test_reading_gives_an_editable_structure(self) -> None:
        decoded = json_pipeline().decode(gzipped_json())
        assert decoded.value == SAVE
        assert len(decoded.hints) == 2

    def test_writing_an_unchanged_file_reproduces_it(self) -> None:
        pipeline = json_pipeline()
        raw = gzipped_json(indent=2)
        decoded = pipeline.decode(raw)
        assert pipeline.encode(decoded.value, decoded.hints) == raw

    def test_an_edit_changes_only_what_it_should(self) -> None:
        pipeline = json_pipeline()
        raw = gzipped_json(indent=2)
        decoded = pipeline.decode(raw)
        decoded.value["playerData"]["geo"] = 99999

        rewritten = pipeline.encode(decoded.value, decoded.hints)
        reread = pipeline.decode(rewritten).value
        assert reread["playerData"]["geo"] == 99999
        assert reread["playerData"]["maxHealth"] == 5

    def test_a_broken_file_says_which_step_failed(self) -> None:
        with pytest.raises(PipelineError) as caught:
            json_pipeline().decode(b"this is not gzip")
        assert "step 1" in caught.value.user_message
        assert "gzip" in caught.value.user_message

    def test_a_failure_in_a_later_step_is_numbered_correctly(self) -> None:
        raw = gzip.compress(b"{not json", mtime=0)
        with pytest.raises(PipelineError) as caught:
            json_pipeline().decode(raw)
        assert "step 2" in caught.value.user_message

    def test_hints_that_do_not_match_the_plugin_are_refused(self) -> None:
        pipeline = json_pipeline()
        with pytest.raises(PipelineError, match="does not match the plugin"):
            pipeline.encode(SAVE, [{}])

    def test_an_empty_pipeline_passes_bytes_through(self) -> None:
        pipeline = Pipeline.from_manifest([])
        decoded = pipeline.decode(b"raw bytes")
        assert pipeline.encode(decoded.value, decoded.hints) == b"raw bytes"


class TestPassthrough:
    """The shortcut that keeps untouched bytes untouched."""

    def test_a_stream_we_cannot_recompress_still_survives_an_edit_elsewhere(self) -> None:
        """Java and .NET deflate differently; their bytes must not be mangled."""
        payload = json.dumps(SAVE).encode()
        # A deflate stream no zlib level produces: maximum compression with a
        # different strategy.
        compressor = zlib.compressobj(9, zlib.DEFLATED, 15, 9, zlib.Z_FILTERED)
        odd = compressor.compress(payload) + compressor.flush()

        pipeline = Pipeline.from_manifest([{"op": "zlib"}, {"op": "json_parse"}])
        decoded = pipeline.decode(odd)
        assert decoded.hints[0]["level"] is None, "no standard level should match"
        assert pipeline.encode(decoded.value, decoded.hints) == odd

    def test_the_shortcut_can_be_turned_off(self) -> None:
        compressor = zlib.compressobj(9, zlib.DEFLATED, 15, 9, zlib.Z_FILTERED)
        odd = compressor.compress(json.dumps(SAVE).encode()) + compressor.flush()
        pipeline = Pipeline.from_manifest([{"op": "zlib"}, {"op": "json_parse"}])
        decoded = pipeline.decode(odd)
        assert pipeline.encode(decoded.value, decoded.hints, passthrough=False) != odd

    def test_a_changed_value_bypasses_the_shortcut(self) -> None:
        pipeline = json_pipeline()
        raw = gzipped_json()
        decoded = pipeline.decode(raw)
        decoded.value["playerData"]["geo"] = 1
        assert pipeline.encode(decoded.value, decoded.hints) != raw

    def test_a_mutated_structure_is_never_mistaken_for_unchanged(self) -> None:
        """Editing mutates in place; comparing an object with itself would lie."""
        pipeline = json_pipeline()
        decoded = pipeline.decode(gzipped_json())
        for hint in decoded.hints:
            assert not isinstance(hint.get("_output"), dict)


class TestRoundTripGate:
    def test_a_faithful_pipeline_passes(self) -> None:
        result = json_pipeline().round_trip(gzipped_json(indent=4))
        assert result.passed
        assert result.exact_bytes and result.rebuild_readable

    @pytest.mark.parametrize("level", [1, 6, 9])
    def test_every_compression_level_passes(self, level: int) -> None:
        assert json_pipeline().round_trip(gzipped_json(level=level)).passed

    def test_a_foreign_compressor_fails_the_gate_but_is_reported_honestly(self) -> None:
        """The data is understood; the container cannot be reproduced. Say so."""
        compressor = zlib.compressobj(9, zlib.DEFLATED, 15, 9, zlib.Z_FILTERED)
        odd = compressor.compress(json.dumps(SAVE).encode()) + compressor.flush()
        result = Pipeline.from_manifest([{"op": "zlib"}, {"op": "json_parse"}]).round_trip(odd)
        assert result.passed is False
        assert result.rebuild_readable is True
        assert "container differs" in result.detail

    def test_a_pipeline_that_writes_an_unreadable_file_is_caught(self) -> None:
        """The dangerous case: not just different bytes, but a broken save."""

        def decode(payload: Any, _params: Any, _hints: Any) -> Any:
            raw = bytes(payload)
            if not raw.startswith(b"OK"):
                raise ValueError("the marker at the start of the file is missing")
            return raw[2:]

        def encode(payload: Any, _params: Any, _hints: Any) -> Any:
            return bytes(payload)  # forgets to put the marker back

        ops.register(
            ops.Operation(
                name="_test_forgetful",
                decode=decode,
                encode=encode,
                summary="test double that fails to restore its own marker",
            )
        )
        result = Pipeline.from_manifest([{"op": "_test_forgetful"}]).round_trip(b"OKdata")
        assert result.passed is False
        assert result.rebuild_readable is False
        assert "cannot be read back" in result.detail
