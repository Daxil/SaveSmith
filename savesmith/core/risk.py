"""How risky is editing this game's save, and what must the user acknowledge.

Three sources, combined worst-first:

* ``risk_db.json`` — what is known about this game by Steam AppID.
* The game folder — anti-cheat components sitting right next to the executable.
* The plugin — what its author declared.

Two rules shape everything here.

**An unknown game is never safe.** With no evidence at all the answer is
``caution``, not ``safe``. Silence is not a clean bill of health.

**A blocked tier is a wall the user can choose to walk through, not a refusal.**
The owner of this project decided that: the audience plays games like Elden
Ring, and refusing outright would only send them to a tool that says nothing at
all about the risk. So SaveSmith states plainly what will happen and requires a
deliberate acknowledgement — one that cannot be given by accident, and is asked
for again for each separate hazard.

What SaveSmith still will not do, at any tier, is touch anything other than the
save file. No patching executables, no disabling anti-cheat, no writing to a
running game's memory. That boundary is what keeps this a save editor.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from savesmith.core.plugin import Localized, Plugin, RiskTier

_TIER_ORDER: dict[RiskTier, int] = {
    RiskTier.SAFE: 0,
    RiskTier.CAUTION: 1,
    RiskTier.BLOCKED: 2,
}

# Anti-cheat found on disk maps to the signal name the database uses.
_ANTICHEAT_SIGNALS: tuple[tuple[str, str], ...] = (
    ("easyanticheat", "easy-anti-cheat"),
    ("_eac", "easy-anti-cheat"),
    ("battleye", "battleye"),
    ("nprotect", "nprotect"),
    ("gameguard", "gameguard"),
)


class Acknowledgement(StrEnum):
    """Something the user must confirm before SaveSmith will write.

    Separate items on purpose: agreeing that a game may ban you is not the same
    as agreeing to touch achievements, and one blanket "yes" would hide both.
    """

    BAN_RISK = "ban_risk"
    """This game is known to detect edited saves or to share progression with
    an online service."""
    ANTICHEAT_PRESENT = "anticheat_present"
    """Anti-cheat software is installed alongside this game."""
    ACHIEVEMENTS = "achievements"
    """Values feeding achievements or statistics are being changed."""
    STEAM_CLOUD = "steam_cloud"
    """The Steam Cloud steps have been carried out."""

    # There is deliberately no acknowledgement for "we know nothing about this
    # game". Almost no game is in the database, so demanding one would put a
    # wall in front of every ordinary edit and teach people to click past it —
    # which would then also get them past the warnings that matter. An unknown
    # game is `caution`: shown plainly, and allowed.


@dataclass(frozen=True)
class Signal:
    """One reason the tier is what it is."""

    name: str
    text: Localized

    def __str__(self) -> str:
        return f"{self.name}: {self.text.get()}"


@dataclass(frozen=True)
class Assessment:
    tier: RiskTier
    signals: tuple[Signal, ...]
    required: frozenset[Acknowledgement]
    title: str | None = None
    known: bool = False
    """Whether the game was found in the database at all."""

    def explain(self, language: str = "en") -> list[str]:
        lines = [f"Risk: {self.tier.value}" + ("" if self.known else " (game not in the database)")]
        lines += [f"  {signal.text.get(language)}" for signal in self.signals]
        if self.required:
            lines.append("  Must be acknowledged: " + ", ".join(sorted(self.required)))
        return lines


class RiskDatabase:
    """``risk_db.json``, kept apart from the app so it can be updated on its own."""

    def __init__(self, data: Mapping[str, object]) -> None:
        self._games: Mapping[str, Mapping[str, object]] = _mapping(data.get("games"))
        self._signals: Mapping[str, Mapping[str, object]] = _mapping(data.get("anticheat_signals"))

    @classmethod
    def load(cls, path: Path) -> RiskDatabase:
        try:
            return cls(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            # A missing or damaged database must not stop the app; it only
            # means every game is treated as unknown, which is the safe way to
            # be wrong.
            return cls({})

    @classmethod
    def bundled(cls) -> RiskDatabase:
        return cls.load(Path(__file__).resolve().parent.parent.parent / "plugins" / "risk_db.json")

    def entry(self, appid: int | None) -> Mapping[str, object] | None:
        if appid is None:
            return None
        found = self._games.get(str(appid))
        return found if isinstance(found, dict) else None

    def signal(self, name: str) -> Signal:
        text = self._signals.get(name)
        if isinstance(text, dict):
            return Signal(name=name, text=Localized({str(k): str(v) for k, v in text.items()}))
        return Signal(name=name, text=Localized({"en": name.replace("-", " ")}))


def assess(
    *,
    database: RiskDatabase,
    appid: int | None = None,
    anticheat: Iterable[str] = (),
    anticheat_scanned: bool = False,
    plugin: Plugin | None = None,
    editing_achievements: bool = False,
    steam_cloud: bool = False,
) -> Assessment:
    """Combine everything known about a game into one answer.

    ``anticheat_scanned`` says whether the install folder was actually looked
    at. It matters: the database records what a game *normally ships with*, and
    a build that does not have it — cracked, or simply a different release — is
    a different situation the user deserves to be told about accurately.
    """
    signals: list[Signal] = []
    required: set[Acknowledgement] = set()
    tiers: list[RiskTier] = []
    title: str | None = None

    found_anticheat = _anticheat_signals(anticheat)

    entry = database.entry(appid)
    if entry is not None:
        title = str(entry.get("title") or "") or None
        tiers.append(_tier_of(entry.get("tier")))
        for name in _strings(entry.get("signals")):
            if name in _ANTICHEAT_NAMES and anticheat_scanned and name not in found_anticheat:
                signals.append(_absent_signal(name))
            else:
                signals.append(database.signal(name))
    else:
        # No entry means no knowledge, and no knowledge is not reassurance —
        # but it is also not a reason to stop someone. Caution, stated.
        tiers.append(RiskTier.CAUTION)
        signals.append(
            Signal(
                name="unknown-game",
                text=Localized(
                    {
                        "en": (
                            "This game is not in the risk database, so nothing is known "
                            "about how it treats edited saves."
                        ),
                        "ru": (
                            "Этой игры нет в базе рисков, поэтому про её отношение к "
                            "правленым сейвам ничего не известно."
                        ),
                    }
                ),
            )
        )

    for name in found_anticheat:
        signals.append(database.signal(name))
    if found_anticheat:
        tiers.append(RiskTier.BLOCKED)
        required.add(Acknowledgement.ANTICHEAT_PRESENT)

    if plugin is not None:
        tiers.append(plugin.risk.tier)
        signals.append(Signal(name=f"plugin:{plugin.id}", text=plugin.risk.reason))
        if plugin.risk.steam_cloud:
            steam_cloud = True

    tier = max(tiers, key=lambda item: _TIER_ORDER[item])
    if tier is RiskTier.BLOCKED:
        required.add(Acknowledgement.BAN_RISK)
    if editing_achievements:
        required.add(Acknowledgement.ACHIEVEMENTS)
    if steam_cloud:
        required.add(Acknowledgement.STEAM_CLOUD)

    return Assessment(
        tier=tier,
        signals=_deduplicate(signals),
        required=frozenset(required),
        title=title,
        known=entry is not None,
    )


def _deduplicate(signals: Iterable[Signal]) -> tuple[Signal, ...]:
    """One reason stated once, however many sources agree on it."""
    seen: dict[str, Signal] = {}
    for signal in signals:
        seen.setdefault(signal.name, signal)
    return tuple(seen.values())


_ANTICHEAT_NAMES = frozenset({"easy-anti-cheat", "battleye", "nprotect", "gameguard"})

_PRETTY = {
    "easy-anti-cheat": "Easy Anti-Cheat",
    "battleye": "BattlEye",
    "nprotect": "nProtect",
    "gameguard": "GameGuard",
}


def _absent_signal(name: str) -> Signal:
    """The database says this game ships with anti-cheat; this copy does not.

    Worth saying out loud rather than repeating the database: a build without
    it — cracked, or an older release — genuinely carries less risk, and
    telling the user otherwise would be wrong.
    """
    pretty = _PRETTY.get(name, name)
    return Signal(
        name=f"{name}:absent",
        text=Localized(
            {
                "en": (
                    f"This game normally ships with {pretty}, but it was not found "
                    f"in this installation."
                ),
                "ru": (
                    f"Игра обычно поставляется с {pretty}, но в этой установке "
                    f"он не найден."
                ),
            }
        ),
    )


@dataclass
class Consent:
    """What the user has actually confirmed.

    Kept as data rather than a flag on a button so that the rule lives in the
    core: whatever the interface looks like, it cannot write without filling
    this in.
    """

    given: set[Acknowledgement] = field(default_factory=set)

    def give(self, *items: Acknowledgement) -> Consent:
        self.given.update(items)
        return self

    def missing(self, assessment: Assessment) -> frozenset[Acknowledgement]:
        return frozenset(assessment.required - self.given)

    def satisfies(self, assessment: Assessment) -> bool:
        return not self.missing(assessment)


def _tier_of(value: object) -> RiskTier:
    try:
        return RiskTier(str(value))
    except ValueError:
        # An unreadable tier in the database is treated as unknown, not safe.
        return RiskTier.CAUTION


def _anticheat_signals(names: Iterable[str]) -> list[str]:
    found: list[str] = []
    for name in names:
        lowered = name.lower()
        for marker, signal in _ANTICHEAT_SIGNALS:
            if marker in lowered and signal not in found:
                found.append(signal)
    return found


def _mapping(value: object) -> Mapping[str, Mapping[str, object]]:
    return value if isinstance(value, dict) else {}


def _strings(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []
