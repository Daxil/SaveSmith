"""Finding a save's checksum, and keeping it right when writing."""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path
from typing import Any

import pytest

from savesmith.core.checksum import ChecksumSpec, Coverage, find, spec_from_mapping
from savesmith.core.errors import PipelineError, PluginValidationError
from savesmith.core.pipeline import Pipeline
from savesmith.core.plugin import Plugin

BODY = b'{"gold": 100}' + bytes(200)

REAL_SAVE = Path(
    Path.home()
    / "Applications/elden ring.app/Contents/SharedSupport/prefix/drive_c/users"
    / "Wineskin/AppData/Roaming/EldenRing/76561197960271872/ER0000.sl2"
)


def crc_first(body: bytes = BODY) -> bytes:
    return zlib.crc32(body).to_bytes(4, "little") + body


def crc_last(body: bytes = BODY) -> bytes:
    return body + zlib.crc32(body).to_bytes(4, "big")


def md5_zeroed(head: bytes = b"HDR!", tail: bytes = b"payload" * 50) -> bytes:
    return head + hashlib.md5(head + bytes(16) + tail).digest() + tail


class TestFinding:
    def test_a_checksum_at_the_start(self) -> None:
        found = find(crc_first())
        assert found[0].algorithm == "crc32-le"
        assert (found[0].offset, found[0].coverage) == (0, Coverage.AFTER)

    def test_a_checksum_at_the_end(self) -> None:
        found = find(crc_last())
        assert found[0].algorithm == "crc32-be"
        assert found[0].coverage is Coverage.BEFORE

    def test_a_whole_file_checksum_with_the_field_zeroed(self) -> None:
        found = find(md5_zeroed())
        assert (found[0].algorithm, found[0].offset) == ("md5", 4)
        assert found[0].coverage is Coverage.WHOLE_ZEROED

    def test_a_sha256_is_found(self) -> None:
        body = b"progress" * 100
        raw = hashlib.sha256(body).digest() + body
        assert find(raw)[0].algorithm == "sha256"

    def test_an_adler32_is_found(self) -> None:
        body = b"progress" * 100
        raw = zlib.adler32(body).to_bytes(4, "little") + body
        assert any(spec.algorithm == "adler32-le" for spec in find(raw))

    def test_a_simple_additive_sum(self) -> None:
        body = bytes(range(200))
        raw = (sum(body) % 2**32).to_bytes(4, "little") + body
        assert any(spec.algorithm == "sum32-le" for spec in find(raw))

    def test_the_widest_match_is_offered_first(self) -> None:
        """A one-byte match proves nothing; a 32-byte one is certain."""
        found = find(md5_zeroed(), include_weak=True)
        assert found[0].size >= found[-1].size

    def test_weak_checksums_are_hidden_by_default(self) -> None:
        """A one-byte XOR matches by chance in almost any file."""
        assert all(not spec.weak for spec in find(crc_first()))

    def test_a_file_with_no_checksum_yields_nothing(self) -> None:
        assert find(b"plain data with nothing to verify" * 20) == []

    def test_a_tiny_file_is_not_searched(self) -> None:
        assert find(b"abc") == []

    def test_every_result_actually_verifies(self) -> None:
        raw = md5_zeroed()
        assert all(spec.verify(raw) for spec in find(raw))


class TestApplying:
    def test_recalculating_an_untouched_file_changes_nothing(self) -> None:
        raw = crc_first()
        assert find(raw)[0].apply(raw) == raw

    def test_an_edited_file_gets_a_fresh_checksum(self) -> None:
        raw = bytearray(crc_first())
        spec = find(bytes(raw))[0]
        raw[10:14] = b"9999"
        assert not spec.verify(bytes(raw))
        fixed = spec.apply(bytes(raw))
        assert spec.verify(fixed)

    def test_the_old_value_does_not_poison_a_whole_file_checksum(self) -> None:
        """The field is part of what is hashed, so it must be zeroed first."""
        raw = bytearray(md5_zeroed())
        spec = find(bytes(raw))[0]
        raw[-4:] = b"EDIT"
        assert spec.verify(spec.apply(bytes(raw)))

    def test_applying_past_the_end_is_refused(self) -> None:
        spec = ChecksumSpec("crc32-le", offset=10_000, coverage=Coverage.AFTER)
        with pytest.raises(ValueError, match="bytes long"):
            spec.apply(b"short file")


class TestTheOperation:
    def pipeline(self, **params: Any) -> Pipeline:
        settings = {"op": "checksum", "algorithm": "crc32-le", "offset": 0, "covers": "after"}
        settings.update(params)
        return Pipeline.from_manifest([settings, {"op": "strip_prefix", "bytes": 4}])

    def test_round_trip_is_exact(self) -> None:
        assert self.pipeline().round_trip(crc_first()).passed

    def test_reading_records_whether_the_checksum_held(self) -> None:
        decoded = self.pipeline().decode(crc_first())
        assert decoded.hints[0]["held"] is True

    def test_a_wrong_checksum_is_reported_not_refused(self) -> None:
        """Another tool may have edited this file; that is not our business."""
        raw = bytearray(crc_first())
        raw[0] ^= 0xFF
        assert self.pipeline().decode(bytes(raw)).hints[0]["held"] is False

    def test_writing_brings_the_checksum_up_to_date(self) -> None:
        pipeline = Pipeline.from_manifest(
            [
                {"op": "checksum", "algorithm": "crc32-le", "offset": 0, "covers": "after"},
                {"op": "strip_prefix", "bytes": 4},
                {"op": "json_parse"},
            ]
        )
        raw = zlib.crc32(b'{"gold":1}').to_bytes(4, "little") + b'{"gold":1}'
        decoded = pipeline.decode(raw)
        decoded.value["gold"] = 999

        rewritten = pipeline.encode(decoded.value, decoded.hints, passthrough=False)
        assert pipeline.decode(rewritten).hints[0]["held"] is True
        assert pipeline.decode(rewritten).value["gold"] == 999

    def test_a_checksum_past_the_end_of_the_file(self) -> None:
        with pytest.raises(PipelineError, match="past the end"):
            self.pipeline(offset=5000).decode(crc_first())

    def test_an_unknown_algorithm_is_named(self) -> None:
        with pytest.raises(PipelineError, match="unknown algorithm"):
            self.pipeline(algorithm="crc99").decode(crc_first())


class TestManifestSupport:
    def manifest(self, checksum: Any) -> dict[str, Any]:
        return {
            "id": "checksummed",
            "version": 1,
            "game": "Checksummed Game",
            "engine": "test",
            "confidence": "probable",
            "risk": {"tier": "safe", "reason": {"en": "single player"}},
            "checksum": checksum,
            "pipeline": [{"op": "strip_prefix", "bytes": 4}, {"op": "json_parse"}],
            "fields": [],
        }

    def test_a_checksum_becomes_the_first_pipeline_step(self) -> None:
        """First when reading means last when writing, which is when it must run."""
        plugin = Plugin.from_mapping(
            self.manifest({"algorithm": "crc32-le", "offset": 0, "covers": "after"})
        )
        assert next(step.op for step in plugin.pipeline.steps) == "checksum"

    def test_a_plugin_with_a_checksum_round_trips(self) -> None:
        plugin = Plugin.from_mapping(
            self.manifest({"algorithm": "crc32-le", "offset": 0, "covers": "after"})
        )
        body = b'{"gold":1}'
        raw = zlib.crc32(body).to_bytes(4, "little") + body
        assert plugin.pipeline.round_trip(raw).passed

    def test_no_checksum_adds_no_step(self) -> None:
        plugin = Plugin.from_mapping(self.manifest(None))
        assert "checksum" not in [step.op for step in plugin.pipeline.steps]

    def test_an_unknown_algorithm_names_the_known_ones(self) -> None:
        with pytest.raises(PluginValidationError, match="crc32-le"):
            Plugin.from_mapping(self.manifest({"algorithm": "magic", "offset": 0}))

    def test_a_negative_offset(self) -> None:
        with pytest.raises(PluginValidationError, match="offset"):
            Plugin.from_mapping(self.manifest({"algorithm": "crc32-le", "offset": -1}))

    def test_an_unknown_coverage(self) -> None:
        with pytest.raises(PluginValidationError, match="covers"):
            Plugin.from_mapping(
                self.manifest({"algorithm": "crc32-le", "offset": 0, "covers": "sideways"})
            )

    def test_a_checksum_that_is_not_an_object(self) -> None:
        with pytest.raises(PluginValidationError, match="object or null"):
            Plugin.from_mapping(self.manifest("crc32"))


class TestSpecFromMapping:
    def test_defaults_to_covering_what_follows(self) -> None:
        spec = spec_from_mapping({"algorithm": "crc32-le", "offset": 0})
        assert spec.coverage is Coverage.AFTER


@pytest.mark.skipif(not REAL_SAVE.is_file(), reason="no local Elden Ring save")
class TestRealSave:
    def test_the_detector_finds_fromsoftwares_checksum_unaided(self) -> None:
        """Rediscovers, from the bytes alone, what had to be looked up by hand."""
        slot = Pipeline.from_manifest([{"op": "bnd4", "entry": "USER_DATA000"}]).decode(
            REAL_SAVE.read_bytes()
        ).value
        found = find(slot)
        assert found, "the slot should carry a checksum"
        assert (found[0].algorithm, found[0].offset) == ("md5", 0)
        assert found[0].coverage is Coverage.AFTER
