"""Unreal Engine saves.

Half of these run against real files from The Invincible (UE 4.27) in
``tests/corpus``. Synthetic files prove the reader handles each construct; real
files prove we understood what an actual engine writes, which is the only claim
that matters.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

from savesmith.core import ops
from savesmith.core.pipeline import Pipeline

CORPUS = Path(__file__).parent / "corpus" / "unreal-gvas" / "the-invincible"


def text(value: str) -> bytes:
    if value == "":
        return struct.pack("<i", 0)
    encoded = value.encode("ascii") + b"\x00"
    return struct.pack("<i", len(encoded)) + encoded


def header(class_name: str = "/Game/SaveGame.SaveGame_C") -> bytes:
    return (
        b"GVAS"
        + struct.pack("<i", 2)  # save game version
        + struct.pack("<i", 522)  # package version
        + struct.pack("<HHH", 4, 27, 2)  # engine version
        + struct.pack("<I", 48954)  # changelist
        + text("++UE427+ue427_release")
        + struct.pack("<i", 3)  # custom version format
        + struct.pack("<i", 0)  # no custom versions
        + text(class_name)
    )


def prop(name: str, type_name: str, meta: bytes, body: bytes) -> bytes:
    return text(name) + text(type_name) + struct.pack("<q", len(body)) + meta + body


def save(*properties: bytes, trailer: bytes = b"\x00\x00\x00\x00") -> bytes:
    return header() + b"".join(properties) + text("None") + trailer


def gvas() -> Pipeline:
    return Pipeline.from_manifest([{"op": "gvas"}])


def decode(raw: bytes) -> dict[str, Any]:
    properties: dict[str, Any] = gvas().decode(raw).value["properties"]
    return properties


class TestScalars:
    def test_an_integer(self) -> None:
        raw = save(prop("Money", "IntProperty", b"\x00", struct.pack("<i", 5000)))
        assert decode(raw)["Money"] == 5000
        assert gvas().round_trip(raw).passed

    def test_a_float(self) -> None:
        raw = save(prop("Health", "FloatProperty", b"\x00", struct.pack("<f", 87.5)))
        assert decode(raw)["Health"] == pytest.approx(87.5)
        assert gvas().round_trip(raw).passed

    def test_a_64_bit_integer(self) -> None:
        raw = save(prop("Score", "Int64Property", b"\x00", struct.pack("<q", 2**40)))
        assert decode(raw)["Score"] == 2**40

    def test_a_boolean(self) -> None:
        """A bool has no body: its value sits in the tag and its size is zero."""
        raw = save(text("Finished") + text("BoolProperty") + struct.pack("<q", 0) + b"\x01\x00")
        assert decode(raw)["Finished"] is True
        assert gvas().round_trip(raw).passed

    def test_text(self) -> None:
        raw = save(prop("PlayerName", "StrProperty", b"\x00", text("Astrogator")))
        assert decode(raw)["PlayerName"] == "Astrogator"
        assert gvas().round_trip(raw).passed

    def test_non_ascii_text_survives(self) -> None:
        body = "Ярослав".encode("utf-16-le") + b"\x00\x00"
        raw = save(
            prop(
                "PlayerName",
                "StrProperty",
                b"\x00",
                struct.pack("<i", -(len(body) // 2)) + body,
            )
        )
        assert decode(raw)["PlayerName"] == "Ярослав"
        assert gvas().round_trip(raw).passed


class TestArrays:
    def test_an_array_of_integers(self) -> None:
        body = struct.pack("<i", 3) + struct.pack("<iii", 10, 20, 30)
        raw = save(prop("Unlocked", "ArrayProperty", text("IntProperty") + b"\x00", body))
        assert decode(raw)["Unlocked"] == [10, 20, 30]
        assert gvas().round_trip(raw).passed

    def test_an_empty_array(self) -> None:
        body = struct.pack("<i", 0)
        raw = save(prop("Unlocked", "ArrayProperty", text("IntProperty") + b"\x00", body))
        assert decode(raw)["Unlocked"] == []

    def test_an_array_of_structs_is_preserved_but_not_offered(self) -> None:
        """Not understood, so not editable — and not mangled either."""
        body = struct.pack("<i", 1) + b"\xde\xad\xbe\xef" * 4
        raw = save(prop("Items", "ArrayProperty", text("StructProperty") + b"\x00", body))
        assert "Items" not in decode(raw)
        assert gvas().round_trip(raw).passed


class TestStructs:
    def test_a_plain_struct_is_walked(self) -> None:
        inner = prop("Volume", "FloatProperty", b"\x00", struct.pack("<f", 0.5)) + text("None")
        meta = text("AudioSettings") + b"\x00" * 16 + b"\x00"
        raw = save(prop("Settings", "StructProperty", meta, inner))
        assert decode(raw)["Settings"]["Volume"] == pytest.approx(0.5)
        assert gvas().round_trip(raw).passed

    def test_an_engine_struct_is_preserved_whole(self) -> None:
        """Vector and friends are serialised natively, not as properties."""
        meta = text("Vector") + b"\x00" * 16 + b"\x00"
        raw = save(prop("Position", "StructProperty", meta, struct.pack("<fff", 1.0, 2.0, 3.0)))
        assert "Position" not in decode(raw)
        assert gvas().round_trip(raw).passed


class TestOpaqueTypes:
    def test_a_map_is_preserved(self) -> None:
        """MapProperty carries two type names before its body."""
        meta = text("NameProperty") + text("IntProperty") + b"\x00"
        body = struct.pack("<ii", 0, 1) + text("Key") + struct.pack("<i", 7)
        raw = save(prop("Counters", "MapProperty", meta, body))
        assert "Counters" not in decode(raw)
        assert gvas().round_trip(raw).passed

    def test_an_unknown_property_type_does_not_stop_the_file(self) -> None:
        raw = save(
            prop("Weird", "SomeFutureProperty", b"\x00", b"\x01\x02\x03\x04"),
            prop("Money", "IntProperty", b"\x00", struct.pack("<i", 42)),
        )
        assert decode(raw)["Money"] == 42
        assert gvas().round_trip(raw).passed


class TestEditing:
    def test_changing_a_number_changes_only_that_number(self) -> None:
        raw = save(
            prop("Money", "IntProperty", b"\x00", struct.pack("<i", 100)),
            prop("Health", "FloatProperty", b"\x00", struct.pack("<f", 50.0)),
        )
        pipeline = gvas()
        decoded = pipeline.decode(raw)
        decoded.value["properties"]["Money"] = 999999

        rewritten = pipeline.encode(decoded.value, decoded.hints)
        reread = pipeline.decode(rewritten).value["properties"]
        assert reread["Money"] == 999999
        assert reread["Health"] == pytest.approx(50.0)
        assert len(rewritten) == len(raw), "an int stays four bytes"

    def test_a_longer_string_resizes_its_property(self) -> None:
        raw = save(prop("PlayerName", "StrProperty", b"\x00", text("Ann")))
        pipeline = gvas()
        decoded = pipeline.decode(raw)
        decoded.value["properties"]["PlayerName"] = "Anastasia"
        rewritten = pipeline.encode(decoded.value, decoded.hints)
        assert pipeline.decode(rewritten).value["properties"]["PlayerName"] == "Anastasia"

    def test_a_value_that_does_not_fit_is_refused(self) -> None:
        from savesmith.core.errors import PipelineError

        raw = save(prop("Money", "IntProperty", b"\x00", struct.pack("<i", 100)))
        pipeline = gvas()
        decoded = pipeline.decode(raw)
        decoded.value["properties"]["Money"] = 2**40
        with pytest.raises(PipelineError, match="cannot hold"):
            pipeline.encode(decoded.value, decoded.hints)


class TestBrokenFiles:
    def test_a_file_that_is_not_gvas(self) -> None:
        with pytest.raises(ValueError, match="GVAS"):
            ops.get("gvas").decode(b"BND4 not this one", {}, {})

    def test_a_truncated_file(self) -> None:
        with pytest.raises(ValueError, match="ends unexpectedly"):
            ops.get("gvas").decode(header()[:20], {}, {})

    def test_a_property_cut_in_half(self) -> None:
        raw = save(prop("Money", "IntProperty", b"\x00", struct.pack("<i", 1)))[:-6]
        with pytest.raises(ValueError):
            ops.get("gvas").decode(raw, {}, {})


@pytest.mark.skipif(not CORPUS.is_dir(), reason="corpus not present")
class TestRealSaves:
    """The Invincible, Unreal Engine 4.27. Single player, no anti-cheat."""

    @pytest.mark.parametrize(
        "name",
        [
            "GameSlotSave.sav",
            "ComicsSave.sav",
            "CondorSettings.sav",
            "MenuSettingsSave.sav",
            "SaveSlot_0_backup_1.sav",
        ],
    )
    def test_round_trip_is_byte_exact(self, name: str) -> None:
        result = gvas().round_trip((CORPUS / name).read_bytes())
        assert result.passed, result.detail

    def test_real_values_are_readable(self) -> None:
        values = decode((CORPUS / "ComicsSave.sav").read_bytes())
        assert isinstance(values["VisibleComicsID"], list)
        assert all(isinstance(item, int) for item in values["VisibleComicsID"])

    def test_a_nested_setting_is_reachable(self) -> None:
        from savesmith.core import fields

        values = decode((CORPUS / "CondorSettings.sav").read_bytes())
        settings = values["CurrentUserSettings"]
        graphics = next(iter(settings.values()))
        assert any("FrameRateLimit" in key for key in graphics)
        assert fields.exists({"properties": values}, ["properties", "CurrentUserSettings"])

    def test_editing_a_real_save_keeps_everything_else_identical(self) -> None:
        """The strongest claim: change one number, nothing else moves."""
        raw = (CORPUS / "MenuSettingsSave.sav").read_bytes()
        pipeline = gvas()
        decoded = pipeline.decode(raw)
        original = decoded.value["properties"]["SaveVersion"]
        decoded.value["properties"]["SaveVersion"] = original + 1

        rewritten = pipeline.encode(decoded.value, decoded.hints)
        assert len(rewritten) == len(raw)
        pairs = enumerate(zip(raw, rewritten, strict=True))
        differing = [index for index, (before, after) in pairs if before != after]
        assert len(differing) <= 4, "only the four bytes of that integer may change"
