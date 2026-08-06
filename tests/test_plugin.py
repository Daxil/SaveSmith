"""The plugin manifest format and its validation.

Most of these are error cases on purpose. A plugin is written once and read by
strangers; a manifest that is wrong must say which key is wrong, in a sentence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from savesmith.core.errors import FieldValueError, PluginValidationError
from savesmith.core.plugin import (
    Confidence,
    Localized,
    Plugin,
    RiskTier,
)

MANIFEST: dict[str, Any] = {
    "schema": 1,
    "id": "hollow-knight",
    "version": 3,
    "game": "Hollow Knight",
    "engine": "unity-es3",
    "confidence": "verified",
    "steam_appid": 367520,
    "detect": {
        "paths": {
            "windows": ["{LOCALLOW}/Team Cherry/Hollow Knight/user*.dat"],
            "macos": ["{APPDATA}/unity.Team Cherry.Hollow Knight/user*.dat"],
            "wine_prefix": ["drive_c/users/{WINEUSER}/AppData/LocalLow/Team Cherry/*/user*.dat"],
        },
        "magic_hex": None,
        "probe": None,
    },
    "risk": {
        "tier": "safe",
        "reason": {"en": "Single player only, no anti-cheat", "ru": "Чистый сингл"},
        "steam_cloud": True,
    },
    "pipeline": [{"op": "gzip"}, {"op": "json_parse"}],
    "checksum": None,
    "fields": [
        {
            "path": "playerData.geo",
            "label": {"en": "Geo", "ru": "Гео"},
            "type": "int",
            "min": 0,
            "max": 999999,
            "group": {"en": "Resources", "ru": "Ресурсы"},
        },
        {
            "path": "playerData.maxHealth",
            "label": {"en": "Health masks", "ru": "Маски здоровья"},
            "type": "int",
            "min": 1,
            "max": 11,
            "group": {"en": "Character", "ru": "Персонаж"},
            "warn": {"en": "Above 11 breaks the game UI", "ru": "Выше 11 ломает UI игры"},
        },
    ],
}


def manifest(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(json.dumps(MANIFEST))
    data.update(overrides)
    return data


def with_field(**overrides: Any) -> dict[str, Any]:
    base = {"path": "a.b", "label": {"en": "A"}, "type": "int"}
    base.update(overrides)
    return manifest(fields=[base])


class TestLoading:
    def test_a_complete_manifest(self) -> None:
        plugin = Plugin.from_mapping(MANIFEST)
        assert plugin.id == "hollow-knight"
        assert plugin.confidence is Confidence.VERIFIED
        assert plugin.risk.tier is RiskTier.SAFE
        assert plugin.risk.steam_cloud is True
        assert plugin.steam_appid == 367520
        assert [step.op for step in plugin.pipeline.steps] == ["gzip", "json_parse"]

    def test_loading_from_a_folder(self, tmp_path: Path) -> None:
        folder = tmp_path / "hollow-knight"
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        plugin = Plugin.load(folder)
        assert plugin.game == "Hollow Knight"
        assert plugin.source == folder / "manifest.json"

    def test_a_missing_manifest_is_reported_plainly(self, tmp_path: Path) -> None:
        folder = tmp_path / "empty"
        folder.mkdir()
        with pytest.raises(PluginValidationError) as caught:
            Plugin.load(folder)
        assert "manifest.json" in caught.value.user_message

    def test_broken_json_points_at_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text('{\n  "id": "x",\n', encoding="utf-8")
        with pytest.raises(PluginValidationError, match=r"line 3|line 2"):
            Plugin.load(path)


class TestIdentityValidation:
    def test_a_newer_schema_asks_the_user_to_update(self) -> None:
        with pytest.raises(PluginValidationError) as caught:
            Plugin.from_mapping(manifest(schema=2))
        assert "Updating SaveSmith" in caught.value.user_message

    def test_a_missing_id(self) -> None:
        data = manifest()
        del data["id"]
        with pytest.raises(PluginValidationError, match="'id'"):
            Plugin.from_mapping(data)

    def test_an_id_with_capitals_or_spaces(self) -> None:
        with pytest.raises(PluginValidationError, match="lowercase"):
            Plugin.from_mapping(manifest(id="Hollow Knight"))

    @pytest.mark.parametrize("bad", [0, -1, "3", 1.5, True])
    def test_version_must_be_a_counting_number(self, bad: Any) -> None:
        with pytest.raises(PluginValidationError, match="'version'"):
            Plugin.from_mapping(manifest(version=bad))

    def test_an_unknown_confidence_lists_the_allowed_values(self) -> None:
        with pytest.raises(PluginValidationError) as caught:
            Plugin.from_mapping(manifest(confidence="pretty-sure"))
        assert "experimental" in caught.value.user_message


class TestRiskValidation:
    def test_risk_is_mandatory(self) -> None:
        """An unknown game is never assumed safe — including by omission."""
        data = manifest()
        del data["risk"]
        with pytest.raises(PluginValidationError) as caught:
            Plugin.from_mapping(data)
        assert "never assumed to be safe" in caught.value.user_message

    def test_an_unknown_tier(self) -> None:
        with pytest.raises(PluginValidationError, match="blocked"):
            Plugin.from_mapping(manifest(risk={"tier": "fine", "reason": {"en": "x"}}))

    def test_the_reason_must_be_localised(self) -> None:
        with pytest.raises(PluginValidationError, match="language"):
            Plugin.from_mapping(manifest(risk={"tier": "safe", "reason": "single player"}))


class TestChecksums:
    def test_a_checksum_becomes_a_pipeline_step(self) -> None:
        """It goes first, so that writing recalculates it last of all."""
        plugin = Plugin.from_mapping(
            manifest(checksum={"algorithm": "crc32-le", "offset": 8, "covers": "after"})
        )
        assert next(step.op for step in plugin.pipeline.steps) == "checksum"

    def test_a_checksum_the_build_cannot_compute_is_refused(self) -> None:
        """Writing a stale checksum produces a file the game rejects."""
        with pytest.raises(PluginValidationError) as caught:
            Plugin.from_mapping(manifest(checksum={"algorithm": "crc32", "offset": 8}))
        assert "crc32-le" in caught.value.user_message


class TestDetectValidation:
    def test_patterns_are_grouped_by_platform(self) -> None:
        from savesmith.core.platform_ import Platform

        plugin = Plugin.from_mapping(MANIFEST)
        assert plugin.detect.patterns_for(Platform.WINDOWS)
        assert plugin.detect.patterns_for(Platform.MACOS)
        assert plugin.detect.wine_patterns

    def test_an_unknown_platform_key(self) -> None:
        with pytest.raises(PluginValidationError, match="wine_prefix"):
            Plugin.from_mapping(manifest(detect={"paths": {"win": ["x"]}}))

    def test_magic_bytes_must_be_hex(self) -> None:
        with pytest.raises(PluginValidationError, match="hexadecimal"):
            Plugin.from_mapping(manifest(detect={"magic_hex": "GVAS!"}))

    def test_detect_may_be_omitted(self) -> None:
        data = manifest()
        del data["detect"]
        assert Plugin.from_mapping(data).detect.paths == {}


class TestFieldValidation:
    def test_labels_must_be_localised(self) -> None:
        with pytest.raises(PluginValidationError) as caught:
            Plugin.from_mapping(with_field(label="Geo"))
        assert '"en"' in caught.value.user_message

    def test_a_field_without_a_path(self) -> None:
        with pytest.raises(PluginValidationError, match="'path'"):
            Plugin.from_mapping(with_field(path=None))

    def test_an_unknown_field_type(self) -> None:
        with pytest.raises(PluginValidationError, match="'type'"):
            Plugin.from_mapping(with_field(type="integer"))

    def test_enum_fields_need_options(self) -> None:
        with pytest.raises(PluginValidationError, match="options"):
            Plugin.from_mapping(with_field(type="enum"))

    def test_options_on_a_non_enum_field(self) -> None:
        with pytest.raises(PluginValidationError, match="only makes sense"):
            Plugin.from_mapping(with_field(type="int", options=["a", "b"]))

    def test_bounds_on_a_string_field(self) -> None:
        with pytest.raises(PluginValidationError, match="not a number"):
            Plugin.from_mapping(with_field(type="string", min=0))

    def test_min_above_max(self) -> None:
        with pytest.raises(PluginValidationError, match="greater than"):
            Plugin.from_mapping(with_field(min=10, max=1))

    def test_a_flag_that_is_not_a_boolean(self) -> None:
        with pytest.raises(PluginValidationError, match="true or false"):
            Plugin.from_mapping(with_field(online_linked="yes"))

    def test_duplicate_paths_are_refused(self) -> None:
        one = {"path": "a.b", "label": {"en": "A"}, "type": "int"}
        with pytest.raises(PluginValidationError, match="more than once"):
            Plugin.from_mapping(manifest(fields=[one, dict(one)]))

    def test_the_list_path_form_is_accepted(self) -> None:
        plugin = Plugin.from_mapping(with_field(path=["playerData.geo"]))
        assert plugin.fields[0].address == "playerData.geo"


class TestFieldBehaviour:
    def test_localised_labels_fall_back_to_english(self) -> None:
        label = Localized({"en": "Geo"})
        assert label.get("ru") == "Geo"
        assert label.get("en") == "Geo"

    def test_a_label_with_neither_language_still_shows_something(self) -> None:
        assert Localized({"de": "Geo"}).get("ru") == "Geo"

    def test_grouping_keeps_the_authors_order(self) -> None:
        plugin = Plugin.from_mapping(MANIFEST)
        groups = plugin.groups("ru")
        assert list(groups) == ["Ресурсы", "Персонаж"]

    def test_achievement_and_online_fields_are_off_by_default(self) -> None:
        plugin = Plugin.from_mapping(
            manifest(
                fields=[
                    {"path": "a", "label": {"en": "A"}, "type": "int"},
                    {"path": "b", "label": {"en": "B"}, "type": "int", "achievement": True},
                    {"path": "c", "label": {"en": "C"}, "type": "int", "online_linked": True},
                ]
            )
        )
        assert [spec.address for spec in plugin.editable_fields] == ["a"]

    def test_looking_a_field_up_by_address(self) -> None:
        plugin = Plugin.from_mapping(MANIFEST)
        assert plugin.field("playerData.geo") is not None
        assert plugin.field("playerData.nope") is None


class TestValueChecking:
    def spec(self, **overrides: Any) -> Any:
        return Plugin.from_mapping(with_field(**overrides)).fields[0]

    def test_integers_within_range(self) -> None:
        spec = self.spec(min=0, max=100)
        assert spec.coerce(42) == 42
        assert spec.coerce("42") == 42

    def test_a_value_below_the_minimum_says_the_limit(self) -> None:
        with pytest.raises(FieldValueError) as caught:
            self.spec(min=1, max=11).coerce(0)
        assert "1" in caught.value.user_message

    def test_a_value_above_the_maximum_says_the_limit(self) -> None:
        with pytest.raises(FieldValueError, match="11"):
            self.spec(min=1, max=11).coerce(12)

    def test_text_in_a_number_field(self) -> None:
        with pytest.raises(FieldValueError, match="whole number"):
            self.spec().coerce("lots")

    def test_a_boolean_is_not_a_number(self) -> None:
        """True == 1 in Python; accepting it would write nonsense into a save."""
        with pytest.raises(FieldValueError):
            self.spec().coerce(True)

    def test_a_float_that_is_really_an_integer(self) -> None:
        assert self.spec().coerce(42.0) == 42

    def test_a_float_that_is_not(self) -> None:
        with pytest.raises(FieldValueError):
            self.spec().coerce(42.5)

    def test_float_fields(self) -> None:
        assert self.spec(type="float").coerce("1.5") == 1.5

    def test_bool_fields(self) -> None:
        spec = self.spec(type="bool")
        assert spec.coerce(True) is True
        assert spec.coerce("false") is False
        with pytest.raises(FieldValueError, match="on or off"):
            spec.coerce(1)

    def test_enum_fields_list_the_choices(self) -> None:
        spec = self.spec(type="enum", options=["easy", "hard"])
        assert spec.coerce("hard") == "hard"
        with pytest.raises(FieldValueError) as caught:
            spec.coerce("brutal")
        assert "easy, hard" in caught.value.user_message

    def test_the_message_names_the_field_in_the_users_language(self) -> None:
        spec = Plugin.from_mapping(
            with_field(label={"en": "Health masks", "ru": "Маски здоровья"}, min=1, max=11)
        ).fields[0]
        with pytest.raises(FieldValueError) as caught:
            spec.coerce(99)
        assert "Health masks" in caught.value.user_message
