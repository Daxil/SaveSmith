"""The plugin format: what one game's save layout looks like on disk.

A plugin is a folder with ``manifest.json`` in it (and, when the declarative
pipeline is not enough, a ``codec.py`` — that arrives in a later milestone).
The manifest is data, not code: it names the steps to decode the file and the
fields worth editing.

Everything user-visible is localised. ``label``, ``group``, ``warn`` and the
risk ``reason`` are objects like ``{"ru": "Гео", "en": "Geo"}`` rather than
plain strings, because plugins are a shared, long-lived resource and
retrofitting translations into published manifests is far more expensive than
requiring them from the start.

Validation is deliberately strict and written by hand rather than delegated to
a schema library. A plugin that is wrong should say which key is wrong, in a
sentence, on the day it is written — not fail obscurely on a stranger's save
file six months later.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from savesmith.core.errors import FieldValueError, PluginValidationError
from savesmith.core.fields import PathStep, normalise, render
from savesmith.core.pipeline import Pipeline
from savesmith.core.platform_ import Platform

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"

_ID_RE = "abcdefghijklmnopqrstuvwxyz0123456789-"
_FALLBACK_LANGUAGE = "en"


class Confidence(StrEnum):
    """How much the plugin has been proven, shown to the user as-is.

    Set by the round-trip gate and by live verification, never by the author's
    optimism.
    """

    VERIFIED = "verified"
    """Round-trip clean and confirmed loading in the actual game."""
    PROBABLE = "probable"
    """Round-trip clean, nobody has confirmed it in-game yet."""
    EXPERIMENTAL = "experimental"
    """Does not round-trip byte for byte. Editing may break the save."""


class RiskTier(StrEnum):
    SAFE = "safe"
    CAUTION = "caution"
    BLOCKED = "blocked"


class FieldType(StrEnum):
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STRING = "string"
    ENUM = "enum"


@dataclass(frozen=True)
class Localized:
    """A user-visible string in every language the plugin provides."""

    values: Mapping[str, str]

    def get(self, language: str = _FALLBACK_LANGUAGE) -> str:
        if language in self.values:
            return self.values[language]
        if _FALLBACK_LANGUAGE in self.values:
            return self.values[_FALLBACK_LANGUAGE]
        return next(iter(self.values.values()))

    def __str__(self) -> str:
        return self.get()


@dataclass(frozen=True)
class FieldSpec:
    """One editable value."""

    path: tuple[PathStep, ...]
    label: Localized
    type: FieldType
    group: Localized | None = None
    minimum: float | None = None
    maximum: float | None = None
    warn: Localized | None = None
    options: tuple[str, ...] = ()
    advanced: bool = False
    online_linked: bool = False
    """Shared with an online service. Never editable, at any risk tier."""
    achievement: bool = False
    """Feeds achievements or statistics. Off unless the user turns it on."""

    @property
    def address(self) -> str:
        return render(self.path)

    @property
    def editable_by_default(self) -> bool:
        """Achievement and online-linked fields stay off until asked for."""
        return not (self.online_linked or self.achievement)

    def coerce(self, value: Any) -> Any:
        """Check and convert what the user typed, or say why it will not do."""
        label = self.label.get()
        converted: float
        match self.type:
            case FieldType.INT:
                converted = self._to_int(value, label)
            case FieldType.FLOAT:
                converted = self._to_float(value, label)
            case FieldType.BOOL:
                return self._to_bool(value, label)
            case FieldType.STRING:
                if not isinstance(value, str):
                    raise FieldValueError(label, "this field holds text.")
                return value
            case FieldType.ENUM:
                if value not in self.options:
                    raise FieldValueError(
                        label, f"choose one of: {', '.join(self.options)}."
                    )
                return value
        if self.minimum is not None and converted < self.minimum:
            raise FieldValueError(label, f"the smallest allowed value is {_plain(self.minimum)}.")
        if self.maximum is not None and converted > self.maximum:
            raise FieldValueError(label, f"the largest allowed value is {_plain(self.maximum)}.")
        return converted

    @staticmethod
    def _to_int(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise FieldValueError(label, "this field holds a whole number.")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
        raise FieldValueError(label, "this field holds a whole number.")

    @staticmethod
    def _to_float(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise FieldValueError(label, "this field holds a number.")
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise FieldValueError(label, "this field holds a number.")

    @staticmethod
    def _to_bool(value: Any, label: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise FieldValueError(label, "this field is either on or off.")


@dataclass(frozen=True)
class DetectSpec:
    paths: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    magic_hex: str | None = None
    probe: str | None = None

    def patterns_for(self, platform: Platform) -> tuple[str, ...]:
        """Where to look on this platform, bottles included.

        Wine patterns are returned alongside the macOS ones because a Mac may
        well be running the Windows build of a game.
        """
        return self.paths.get(platform.value, ())

    @property
    def wine_patterns(self) -> tuple[str, ...]:
        return self.paths.get("wine_prefix", ())


@dataclass(frozen=True)
class RiskSpec:
    tier: RiskTier
    reason: Localized
    steam_cloud: bool = False


@dataclass(frozen=True)
class Plugin:
    id: str
    version: int
    game: str
    engine: str
    confidence: Confidence
    detect: DetectSpec
    risk: RiskSpec
    pipeline: Pipeline
    fields: tuple[FieldSpec, ...]
    steam_appid: int | None = None
    source: Path | None = None

    @property
    def editable_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(spec for spec in self.fields if spec.editable_by_default)

    def field(self, address: str) -> FieldSpec | None:
        return next((spec for spec in self.fields if spec.address == address), None)

    def groups(self, language: str = _FALLBACK_LANGUAGE) -> dict[str, list[FieldSpec]]:
        """Fields in the order the manifest lists them, bucketed by group.

        Order is the plugin author's decision — they know which numbers a
        player looks for first.
        """
        grouped: dict[str, list[FieldSpec]] = {}
        for spec in self.fields:
            key = spec.group.get(language) if spec.group else ""
            grouped.setdefault(key, []).append(spec)
        return grouped

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, folder: Path) -> Plugin:
        manifest = folder / MANIFEST_NAME if folder.is_dir() else folder
        try:
            raw = manifest.read_text(encoding="utf-8")
        except OSError as exc:
            raise PluginValidationError(
                folder.name, MANIFEST_NAME, f"could not be read ({exc.strerror})."
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PluginValidationError(
                folder.name, MANIFEST_NAME, f"is not valid JSON (line {exc.lineno}: {exc.msg})."
            ) from exc
        if not isinstance(data, dict):
            raise PluginValidationError(folder.name, MANIFEST_NAME, "must contain a JSON object.")
        return cls.from_mapping(data, source=manifest)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source: Path | None = None) -> Plugin:
        plugin_id = data.get("id")
        name = plugin_id if isinstance(plugin_id, str) and plugin_id else "<unnamed>"

        schema = data.get("schema", SCHEMA_VERSION)
        if schema != SCHEMA_VERSION:
            raise PluginValidationError(
                name,
                "'schema'",
                f"is {schema!r}, but this build of SaveSmith understands version "
                f"{SCHEMA_VERSION}. Updating SaveSmith should fix it.",
            )

        if not isinstance(plugin_id, str) or not plugin_id:
            raise PluginValidationError(name, "'id'", "is missing.")
        if any(character not in _ID_RE for character in plugin_id):
            raise PluginValidationError(
                name, "'id'", "may only contain lowercase letters, digits and hyphens."
            )

        # 'checksum' is milestone 7. Accepting one now would mean writing files
        # with a stale checksum, which the game rejects — refuse instead.
        if data.get("checksum") is not None:
            raise PluginValidationError(
                name,
                "'checksum'",
                "describes a checksum, which this version of SaveSmith cannot "
                "recalculate yet. Updating SaveSmith should fix it.",
            )

        fields = tuple(
            _parse_field(entry, name, index)
            for index, entry in enumerate(_list(data, "fields", name, required=False))
        )
        seen: set[str] = set()
        for spec in fields:
            if spec.address in seen:
                raise PluginValidationError(
                    name, f"field '{spec.address}'", "is listed more than once."
                )
            seen.add(spec.address)

        return cls(
            id=plugin_id,
            version=_positive_int(data, "version", name),
            game=_text(data, "game", name),
            engine=_text(data, "engine", name),
            confidence=_enum(data, "confidence", Confidence, name),
            detect=_parse_detect(data.get("detect"), name),
            risk=_parse_risk(data.get("risk"), name),
            pipeline=Pipeline.from_manifest(
                _list(data, "pipeline", name, required=False), plugin_id=plugin_id
            ),
            fields=fields,
            steam_appid=_optional_int(data, "steam_appid", name),
            source=source,
        )


# ---------------------------------------------------------------------------
# Validation helpers. Each one names the offending key.
# ---------------------------------------------------------------------------


def _text(data: Mapping[str, Any], key: str, plugin: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PluginValidationError(plugin, f"'{key}'", "is missing or empty.")
    return value


def _positive_int(data: Mapping[str, Any], key: str, plugin: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PluginValidationError(plugin, f"'{key}'", "must be a whole number of 1 or more.")
    return value


def _optional_int(data: Mapping[str, Any], key: str, plugin: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PluginValidationError(plugin, f"'{key}'", "must be a whole number.")
    return value


def _enum[E: StrEnum](data: Mapping[str, Any], key: str, kind: type[E], plugin: str) -> E:
    value = data.get(key)
    if isinstance(value, str):
        try:
            return kind(value)
        except ValueError:
            pass
    allowed = ", ".join(member.value for member in kind)
    raise PluginValidationError(plugin, f"'{key}'", f"is {value!r}; it must be one of: {allowed}.")


def _list(
    data: Mapping[str, Any], key: str, plugin: str, *, required: bool = True
) -> Sequence[Any]:
    value = data.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list):
        raise PluginValidationError(plugin, f"'{key}'", "must be a list.")
    return value


def _localized(value: Any, plugin: str, where: str, *, required: bool = True) -> Localized | None:
    if value is None:
        if required:
            raise PluginValidationError(plugin, where, "is missing.")
        return None
    if isinstance(value, str):
        raise PluginValidationError(
            plugin,
            where,
            'must give a language for each translation, for example {"en": "Geo", "ru": "Гео"}.',
        )
    if not isinstance(value, dict) or not value:
        raise PluginValidationError(plugin, where, "must be an object of translations.")
    for language, text in value.items():
        if not isinstance(language, str) or not isinstance(text, str) or not text:
            raise PluginValidationError(
                plugin, where, "has a translation that is not text, or an empty one."
            )
    return Localized(dict(value))


def _parse_field(entry: Any, plugin: str, index: int) -> FieldSpec:
    where = f"field {index + 1}"
    if not isinstance(entry, dict):
        raise PluginValidationError(plugin, where, "must be an object.")

    raw_path = entry.get("path")
    if raw_path is None:
        raise PluginValidationError(plugin, where, "has no 'path'.")
    if not isinstance(raw_path, str | list):
        raise PluginValidationError(
            plugin, f"{where} 'path'", "must be text or a list of steps."
        )
    steps = normalise(raw_path)
    where = f"field '{render(steps)}'"

    kind = _enum(entry, "type", FieldType, plugin)
    options = tuple(str(option) for option in entry.get("options", ()))
    if kind is FieldType.ENUM and not options:
        raise PluginValidationError(plugin, f"{where} 'options'", "is required for enum fields.")
    if kind is not FieldType.ENUM and options:
        raise PluginValidationError(
            plugin, f"{where} 'options'", "only makes sense for enum fields."
        )

    minimum = _bound(entry, "min", plugin, where)
    maximum = _bound(entry, "max", plugin, where)
    if minimum is not None and maximum is not None and minimum > maximum:
        raise PluginValidationError(plugin, where, "has a 'min' greater than its 'max'.")
    if (minimum is not None or maximum is not None) and kind not in (
        FieldType.INT,
        FieldType.FLOAT,
    ):
        raise PluginValidationError(
            plugin, where, "sets 'min' or 'max' on a field that is not a number."
        )

    label = _localized(entry.get("label"), plugin, f"{where} 'label'")
    assert label is not None  # _localized only returns None when not required

    return FieldSpec(
        path=steps,
        label=label,
        type=kind,
        group=_localized(entry.get("group"), plugin, f"{where} 'group'", required=False),
        minimum=minimum,
        maximum=maximum,
        warn=_localized(entry.get("warn"), plugin, f"{where} 'warn'", required=False),
        options=options,
        advanced=_flag(entry, "advanced", plugin, where),
        online_linked=_flag(entry, "online_linked", plugin, where),
        achievement=_flag(entry, "achievement", plugin, where),
    )


def _bound(entry: Mapping[str, Any], key: str, plugin: str, where: str) -> float | None:
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PluginValidationError(plugin, f"{where} '{key}'", "must be a number.")
    return float(value)


def _flag(entry: Mapping[str, Any], key: str, plugin: str, where: str) -> bool:
    value = entry.get(key, False)
    if not isinstance(value, bool):
        raise PluginValidationError(plugin, f"{where} '{key}'", "must be true or false.")
    return value


_DETECT_PLATFORMS = frozenset({"windows", "macos", "linux", "wine_prefix"})


def _parse_detect(value: Any, plugin: str) -> DetectSpec:
    if value is None:
        return DetectSpec()
    if not isinstance(value, dict):
        raise PluginValidationError(plugin, "'detect'", "must be an object.")

    raw_paths = value.get("paths", {})
    if not isinstance(raw_paths, dict):
        raise PluginValidationError(plugin, "'detect.paths'", "must be an object.")
    paths: dict[str, tuple[str, ...]] = {}
    for key, patterns in raw_paths.items():
        if key not in _DETECT_PLATFORMS:
            raise PluginValidationError(
                plugin,
                f"'detect.paths.{key}'",
                f"is not a platform SaveSmith knows: {', '.join(sorted(_DETECT_PLATFORMS))}.",
            )
        if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
            raise PluginValidationError(
                plugin, f"'detect.paths.{key}'", "must be a list of path patterns."
            )
        paths[key] = tuple(patterns)

    magic = value.get("magic_hex")
    if magic is not None:
        if not isinstance(magic, str):
            raise PluginValidationError(plugin, "'detect.magic_hex'", "must be text.")
        try:
            bytes.fromhex(magic)
        except ValueError:
            raise PluginValidationError(
                plugin, "'detect.magic_hex'", "is not valid hexadecimal."
            ) from None

    probe = value.get("probe")
    if probe is not None and not isinstance(probe, str):
        raise PluginValidationError(plugin, "'detect.probe'", "must be text.")

    return DetectSpec(paths=paths, magic_hex=magic, probe=probe)


def _parse_risk(value: Any, plugin: str) -> RiskSpec:
    if not isinstance(value, dict):
        raise PluginValidationError(
            plugin,
            "'risk'",
            "is missing. Every plugin must state a risk tier; an unknown game is "
            "never assumed to be safe.",
        )
    reason = _localized(value.get("reason"), plugin, "'risk.reason'")
    assert reason is not None
    steam_cloud = value.get("steam_cloud", False)
    if not isinstance(steam_cloud, bool):
        raise PluginValidationError(plugin, "'risk.steam_cloud'", "must be true or false.")
    return RiskSpec(
        tier=_enum(value, "tier", RiskTier, plugin),
        reason=reason,
        steam_cloud=steam_cloud,
    )


def _plain(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)
