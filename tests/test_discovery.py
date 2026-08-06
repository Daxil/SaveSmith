"""The discovery state machine.

The point of the ordering is that the expensive step runs last and least often,
so most of these check that it is never reached.
"""

from __future__ import annotations

import gzip
import json
import struct
import zlib
from pathlib import Path

import pytest

from savesmith.agent.discovery import (
    CodecProposal,
    CodecRequest,
    Discovery,
    NoCodecWriter,
    Stage,
    discover,
    hexdump,
)
from savesmith.core.backup import BackupStore

CORPUS = Path(__file__).parent / "corpus" / "unreal-gvas" / "the-invincible"


@pytest.fixture
def store(tmp_path: Path) -> BackupStore:
    return BackupStore(tmp_path / "backups")


def known_save(tmp_path: Path, value: int = 100) -> Path:
    path = tmp_path / f"save{value}.dat"
    path.write_bytes(gzip.compress(json.dumps({"gold": value}).encode(), mtime=0))
    return path


def opaque_save(tmp_path: Path, name: str = "mystery.sav", value: int = 12400) -> Path:
    """A binary blob nothing known can open, with a number hidden in it."""
    body = bytearray(b"\x11\x22\x33\x44" * 512)
    struct.pack_into("<i", body, 0x100, value)
    path = tmp_path / name
    path.write_bytes(bytes(body))
    return path


def stage_of(result: Discovery, stage: Stage) -> object:
    return next((item for item in result.stages if item.stage is stage), None)


class TestOrdering:
    def test_the_backup_happens_before_anything_else(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        result = discover(known_save(tmp_path), backups=store)
        assert result.stages[0].stage is Stage.BACKUP
        assert result.backup is not None
        assert result.backup.file.exists()

    def test_nothing_runs_if_the_backup_fails(self, tmp_path: Path, store: BackupStore) -> None:
        """No backup, no work. The rule has no exceptions."""
        result = discover(tmp_path / "not-there.dat", backups=store)
        assert len(result.stages) == 1
        assert not result.stages[0].ok

    def test_every_step_is_reported(self, tmp_path: Path, store: BackupStore) -> None:
        result = discover(known_save(tmp_path), backups=store)
        reached = {item.stage for item in result.stages}
        assert {Stage.BACKUP, Stage.CLASSIFY, Stage.LADDER, Stage.CHECKSUM} <= reached
        assert Stage.ROUND_TRIP in reached


class TestTheCheapPath:
    def test_a_known_format_never_reaches_the_model(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        """The whole design: most games cost nothing."""
        result = discover(known_save(tmp_path), backups=store)
        assert result.solved
        assert stage_of(result, Stage.MODEL) is None
        assert result.cost_usd == 0

    def test_the_pipeline_it_found_actually_works(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        path = known_save(tmp_path)
        result = discover(path, backups=store)
        assert result.pipeline is not None
        assert result.pipeline.decode(path.read_bytes()).value == {"gold": 100}

    def test_the_round_trip_is_proved_not_assumed(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        result = discover(known_save(tmp_path), backups=store)
        assert result.round_trip is not None and result.round_trip.exact_bytes

    def test_a_checksum_is_looked_for_even_on_the_cheap_path(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        body = json.dumps({"gold": 1}).encode()
        path = tmp_path / "checksummed.sav"
        path.write_bytes(zlib.crc32(body).to_bytes(4, "little") + body)

        result = discover(path, backups=store)
        assert result.checksums
        assert result.checksums[0].algorithm == "crc32-le"


class TestTheDiffStep:
    def test_two_decodable_saves_are_compared_by_field(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        first = known_save(tmp_path, 100)
        second = known_save(tmp_path, 70)
        result = discover(first, backups=store, second_save=second)
        assert any("gold" in candidate for candidate in result.field_candidates)

    def test_two_opaque_saves_are_compared_by_bytes(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        first = opaque_save(tmp_path, "a.sav", 12400)
        second = opaque_save(tmp_path, "b.sav", 7300)
        result = discover(
            first, backups=store, second_save=second, known_before=12400, known_after=7300
        )
        assert result.field_candidates
        assert "0x100" in " ".join(result.field_candidates)

    def test_saves_of_different_sizes_are_reported_not_crashed(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        first = opaque_save(tmp_path, "a.sav")
        second = tmp_path / "b.sav"
        second.write_bytes(b"shorter")
        result = discover(first, backups=store, second_save=second)
        diff = stage_of(result, Stage.DIFF)
        assert diff is not None and not diff.ok  # type: ignore[attr-defined]

    def test_a_missing_second_save_is_survivable(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        result = discover(
            opaque_save(tmp_path), backups=store, second_save=tmp_path / "gone.sav"
        )
        assert stage_of(result, Stage.DIFF) is not None


class TestTheModelStep:
    def test_without_a_model_it_says_so_plainly(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        result = discover(opaque_save(tmp_path), backups=store, writer=NoCodecWriter())
        model = stage_of(result, Stage.MODEL)
        assert model is not None
        assert "no model configured" in model.summary  # type: ignore[attr-defined]
        assert not result.solved

    def test_a_proposed_codec_runs_in_the_sandbox(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        class Writer:
            def propose(self, request: CodecRequest) -> CodecProposal:
                return CodecProposal(
                    source="def decode(data):\n    return data[:4]\n",
                    explanation="takes the header",
                    cost_usd=0.03,
                )

        result = discover(opaque_save(tmp_path), backups=store, writer=Writer())
        model = stage_of(result, Stage.MODEL)
        assert model is not None and model.ok  # type: ignore[attr-defined]
        assert result.codec_result is not None and result.codec_result.ok
        assert result.cost_usd == pytest.approx(0.03)

    def test_a_codec_that_misbehaves_is_contained(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        class Writer:
            def propose(self, request: CodecRequest) -> CodecProposal:
                return CodecProposal(source="import socket\ndef decode(d):\n    return d\n")

        result = discover(opaque_save(tmp_path), backups=store, writer=Writer())
        model = stage_of(result, Stage.MODEL)
        assert model is not None and not model.ok  # type: ignore[attr-defined]

    def test_the_model_is_given_a_slice_not_the_file(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        """A save can be hundreds of megabytes; none of it needs to leave."""
        captured: list[CodecRequest] = []

        class Writer:
            def propose(self, request: CodecRequest) -> CodecProposal | None:
                captured.append(request)
                return None

        big = tmp_path / "big.sav"
        big.write_bytes(bytes(range(256)) * 4096)
        discover(big, backups=store, writer=Writer())

        assert captured
        prompt = captured[0].as_prompt()
        assert len(prompt) < 20_000
        assert "Decoders already tried" in prompt

    def test_the_prompt_carries_what_the_earlier_steps_learned(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        captured: list[CodecRequest] = []

        class Writer:
            def propose(self, request: CodecRequest) -> CodecProposal | None:
                captured.append(request)
                return None

        first = opaque_save(tmp_path, "a.sav", 12400)
        second = opaque_save(tmp_path, "b.sav", 7300)
        discover(
            first,
            backups=store,
            second_save=second,
            known_before=12400,
            known_after=7300,
            writer=Writer(),
        )
        prompt = captured[0].as_prompt()
        assert "changed between two saves" in prompt
        assert "Candidate fields" in prompt


class TestLiveCheck:
    def test_it_is_never_granted_automatically(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        """Only a person who started the game can confirm the save loads."""
        result = discover(known_save(tmp_path), backups=store)
        live = stage_of(result, Stage.LIVE_CHECK)
        assert live is not None and not live.ok  # type: ignore[attr-defined]
        assert result.needs_a_person


class TestDraftManifest:
    def test_it_produces_something_installable(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        from savesmith.core.plugin import Plugin

        result = discover(known_save(tmp_path), backups=store)
        draft = result.draft_manifest("mystery-game", "Mystery Game")
        plugin = Plugin.from_mapping(draft)
        assert plugin.id == "mystery-game"
        assert [step.op for step in plugin.pipeline.steps] == ["gzip", "json_parse"]

    def test_the_draft_never_claims_more_than_experimental(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        result = discover(known_save(tmp_path), backups=store)
        assert result.draft_manifest("x", "X")["confidence"] == "experimental"

    def test_an_unknown_game_is_never_marked_safe(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        result = discover(known_save(tmp_path), backups=store)
        assert result.draft_manifest("x", "X")["risk"]["tier"] == "caution"

    def test_a_found_checksum_ends_up_in_the_draft(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        body = json.dumps({"gold": 1}).encode()
        path = tmp_path / "checksummed.sav"
        path.write_bytes(zlib.crc32(body).to_bytes(4, "little") + body)

        draft = discover(path, backups=store).draft_manifest("x", "X")
        assert draft["checksum"] is not None
        assert draft["checksum"]["algorithm"] == "crc32-le"


class TestHexdump:
    def test_it_shows_offsets_and_text(self) -> None:
        dump = hexdump(b"GVAS\x02\x00\x00\x00")
        assert dump.startswith("00000000  47 56 41 53")
        assert "GVAS" in dump

    def test_unprintable_bytes_become_dots(self) -> None:
        assert "." in hexdump(b"\x00\x01\x02")


@pytest.mark.skipif(not CORPUS.is_dir(), reason="corpus not present")
class TestOnARealSave:
    def test_an_unreal_save_is_solved_without_a_model(
        self, tmp_path: Path, store: BackupStore
    ) -> None:
        import shutil

        copy = tmp_path / "ComicsSave.sav"
        shutil.copy2(CORPUS / "ComicsSave.sav", copy)

        result = discover(copy, backups=store)
        assert result.solved
        assert stage_of(result, Stage.MODEL) is None
        assert result.pipeline is not None
        assert [step.op for step in result.pipeline.steps] == ["gvas"]
