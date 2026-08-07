"""The decoder ladder: opening an unknown save without asking a model.

This is the branch that has to close most games on its own, so the tests are
about breadth — a file wrapped three deep must still come apart — and about
knowing when to stop.
"""

from __future__ import annotations

import base64
import gzip
import json
import zlib
from pathlib import Path

import msgpack
import pytest

from savesmith.core.detect import identify, inspect, known_operations

SAVE = {"playerData": {"geo": 42, "maxHealth": 5}}
JSON_BYTES = json.dumps(SAVE).encode()
CORPUS = Path(__file__).parent / "corpus" / "unreal-gvas" / "the-invincible"


class TestFirstLook:
    def test_text_is_recognised_as_text(self) -> None:
        look = inspect(JSON_BYTES)
        assert look.looks_textual
        assert not look.looks_encrypted
        assert look.magic == "JSON object"

    def test_compressed_data_is_not_mistaken_for_text(self) -> None:
        look = inspect(gzip.compress(JSON_BYTES * 50, mtime=0))
        assert look.magic == "gzip"
        assert not look.looks_textual

    def test_random_bytes_read_as_encrypted(self) -> None:
        """The honest signal that no amount of trying will help."""
        import os

        look = inspect(os.urandom(65536))
        assert look.looks_encrypted

    def test_an_empty_file_does_not_divide_by_zero(self) -> None:
        look = inspect(b"")
        assert look.size == 0
        assert look.entropy == 0.0

    def test_the_summary_is_readable(self) -> None:
        assert "bytes" in inspect(JSON_BYTES).summary()


class TestLadder:
    @pytest.mark.parametrize(
        ("label", "raw", "expected"),
        [
            ("plain json", JSON_BYTES, "json_parse"),
            ("gzip", gzip.compress(JSON_BYTES, mtime=0), "gzip → json_parse"),
            ("zlib", zlib.compress(JSON_BYTES), "zlib → json_parse"),
            (
                "base64 over gzip",
                base64.b64encode(gzip.compress(JSON_BYTES, mtime=0)),
                "base64 → gzip → json_parse",
            ),
            ("msgpack", msgpack.packb(SAVE, use_bin_type=True), "msgpack"),
        ],
    )
    def test_known_wrappings_come_apart(self, label: str, raw: bytes, expected: str) -> None:
        report = identify(raw)
        assert report.best is not None, label
        assert report.best.description == expected
        assert report.solved, label

    def test_the_shortest_answer_wins(self) -> None:
        """Longer pipelines that also work must not outrank the obvious one."""
        report = identify(JSON_BYTES)
        assert report.best is not None
        assert len(report.best.steps) == 1

    def test_structure_outranks_more_bytes(self) -> None:
        """Turning a file into named fields beats turning it into other bytes."""
        report = identify(gzip.compress(JSON_BYTES, mtime=0))
        assert report.best is not None
        assert report.best.structured
        assert any(not candidate.structured for candidate in report.candidates)

    def test_nothing_fits_random_data(self) -> None:
        import os

        report = identify(os.urandom(4096))
        assert report.candidates == []
        assert not report.solved
        assert "encrypted" in " ".join(report.explain())

    def test_an_unrecognised_but_orderly_file_is_reported_honestly(self) -> None:
        report = identify(b"\x00\x01\x02\x03" * 256)
        assert not report.solved

    def test_the_search_is_bounded(self) -> None:
        report = identify(gzip.compress(JSON_BYTES, mtime=0), max_attempts=5)
        assert report.attempted <= 5 + len(known_operations())

    def test_depth_can_be_limited(self) -> None:
        raw = base64.b64encode(gzip.compress(JSON_BYTES, mtime=0))
        assert not identify(raw, max_depth=2).solved
        assert identify(raw, max_depth=3).solved

    def test_the_result_is_a_usable_pipeline(self) -> None:
        """The point of identifying a file is being able to write it back."""
        raw = gzip.compress(JSON_BYTES, mtime=0)
        best = identify(raw).best
        assert best is not None
        decoded = best.pipeline.decode(raw)
        assert decoded.value == SAVE
        assert best.pipeline.encode(decoded.value, decoded.hints) == raw

    def test_the_log_says_what_was_tried(self) -> None:
        lines = identify(JSON_BYTES).explain()
        assert any("Combinations tried" in line for line in lines)
        assert any("json_parse" in line for line in lines)


@pytest.mark.skipif(not CORPUS.is_dir(), reason="corpus not present")
class TestRealFiles:
    def test_an_unreal_save_is_recognised_unaided(self) -> None:
        report = identify((CORPUS / "ComicsSave.sav").read_bytes())
        assert report.best is not None
        assert report.best.description == "gvas"
        assert report.solved

    def test_every_corpus_file_is_recognised(self) -> None:
        for path in sorted(CORPUS.glob("*.sav")):
            report = identify(path.read_bytes(), max_depth=2)
            assert report.solved, f"{path.name}: {report.explain()}"


def test_an_rpg_maker_save_is_recognised_without_a_plugin() -> None:
    """RPG Maker MV and MZ are most of the indie games with a save worth
    editing, and 'find' calling one of them "unknown" while the bundled plugin
    opens it perfectly well is the kind of gap a user reads as "not supported"."""
    import json

    from savesmith.core.ops.lzstring import compress_to_base64

    raw = compress_to_base64(json.dumps({"party": {"_gold": 1250}})).encode("utf-8")

    report = identify(raw)

    assert report.solved
    assert report.best is not None
    assert report.best.description == "lzstring → json_parse"
    assert report.best.round_trip.exact_bytes
