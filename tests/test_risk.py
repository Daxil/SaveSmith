"""The risk classifier, the Steam Cloud wizard, and the consent they demand."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from savesmith.core.backup import BackupStore
from savesmith.core.cloud import STEPS, CloudStatus, CloudWizard
from savesmith.core.plugin import Plugin, RiskTier
from savesmith.core.repository import bundled
from savesmith.core.risk import Acknowledgement, Consent, RiskDatabase, assess
from savesmith.core.session import ConsentRequiredError, EditSession

DATABASE = RiskDatabase.bundled()

ELDEN_RING = 1245620
HOLLOW_KNIGHT = 367520
UNKNOWN = 999999999

MANIFEST: dict[str, Any] = {
    "id": "test-game",
    "version": 1,
    "game": "Test Game",
    "engine": "test",
    "confidence": "probable",
    "risk": {"tier": "safe", "reason": {"en": "single player"}},
    "pipeline": [{"op": "gzip"}, {"op": "json_parse"}],
    "fields": [
        {"path": "gold", "label": {"en": "Gold"}, "type": "int", "min": 0},
        {"path": "kills", "label": {"en": "Kills"}, "type": "int", "achievement": True},
    ],
}


class TestTiers:
    def test_a_known_safe_game(self) -> None:
        result = assess(database=DATABASE, appid=HOLLOW_KNIGHT)
        assert result.tier is RiskTier.SAFE
        assert result.required == frozenset()

    def test_a_known_dangerous_game(self) -> None:
        result = assess(database=DATABASE, appid=ELDEN_RING)
        assert result.tier is RiskTier.BLOCKED
        assert Acknowledgement.BAN_RISK in result.required

    def test_an_unknown_game_is_never_safe(self) -> None:
        """Silence is not a clean bill of health."""
        result = assess(database=DATABASE, appid=UNKNOWN)
        assert result.tier is RiskTier.CAUTION
        assert Acknowledgement.UNKNOWN_GAME in result.required
        assert not result.known

    def test_no_appid_at_all_is_also_unknown(self) -> None:
        assert assess(database=DATABASE).tier is RiskTier.CAUTION

    def test_the_worst_signal_wins(self) -> None:
        safe_plugin = Plugin.from_mapping(MANIFEST)
        result = assess(database=DATABASE, appid=ELDEN_RING, plugin=safe_plugin)
        assert result.tier is RiskTier.BLOCKED, "a plugin cannot talk a game down"

    def test_a_damaged_database_treats_everything_as_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "risk_db.json"
        path.write_text("{ not json", encoding="utf-8")
        result = assess(database=RiskDatabase.load(path), appid=HOLLOW_KNIGHT)
        assert result.tier is RiskTier.CAUTION


class TestAntiCheat:
    def test_anticheat_on_disk_forces_the_worst_tier(self) -> None:
        result = assess(
            database=DATABASE,
            appid=UNKNOWN,
            anticheat=("EasyAntiCheat.sys",),
            anticheat_scanned=True,
        )
        assert result.tier is RiskTier.BLOCKED
        assert Acknowledgement.ANTICHEAT_PRESENT in result.required

    def test_a_build_without_anticheat_is_reported_accurately(self) -> None:
        """A cracked or older build genuinely carries less risk. Say so."""
        result = assess(database=DATABASE, appid=ELDEN_RING, anticheat=(), anticheat_scanned=True)
        assert Acknowledgement.ANTICHEAT_PRESENT not in result.required
        texts = " ".join(signal.text.get() for signal in result.signals)
        assert "was not found in this installation" in texts

    def test_the_tier_still_reflects_the_other_reasons(self) -> None:
        """No anti-cheat does not make FromSoftware's save detection go away."""
        result = assess(database=DATABASE, appid=ELDEN_RING, anticheat=(), anticheat_scanned=True)
        assert result.tier is RiskTier.BLOCKED

    def test_without_a_scan_the_database_is_quoted_as_is(self) -> None:
        result = assess(database=DATABASE, appid=ELDEN_RING, anticheat_scanned=False)
        texts = " ".join(signal.text.get() for signal in result.signals)
        assert "not found in this installation" not in texts

    def test_a_reason_is_stated_once(self) -> None:
        result = assess(
            database=DATABASE,
            appid=ELDEN_RING,
            anticheat=("EasyAntiCheat.sys", "EasyAntiCheat_EOS.dll"),
            anticheat_scanned=True,
        )
        names = [signal.name for signal in result.signals]
        assert len(names) == len(set(names))


class TestConsent:
    def test_nothing_is_satisfied_by_default(self) -> None:
        assessment = assess(database=DATABASE, appid=ELDEN_RING)
        assert not Consent().satisfies(assessment)

    def test_each_hazard_is_agreed_to_separately(self) -> None:
        """One blanket yes would hide what is being agreed to."""
        assessment = assess(
            database=DATABASE,
            appid=ELDEN_RING,
            anticheat=("EasyAntiCheat",),
            anticheat_scanned=True,
        )
        consent = Consent().give(Acknowledgement.BAN_RISK)
        assert not consent.satisfies(assessment)
        assert consent.missing(assessment) == frozenset({Acknowledgement.ANTICHEAT_PRESENT})

    def test_a_safe_game_needs_nothing(self) -> None:
        assert Consent().satisfies(assess(database=DATABASE, appid=HOLLOW_KNIGHT))


class TestCloudWizard:
    def test_no_cloud_means_no_steps(self) -> None:
        wizard = CloudWizard(status=CloudStatus(enabled=False))
        assert not wizard.needed
        assert wizard.ready_to_write

    def test_the_steps_must_be_confirmed_before_writing(self) -> None:
        wizard = CloudWizard(status=CloudStatus(enabled=True))
        assert not wizard.ready_to_write
        assert len(wizard.remaining) == 3, "the fourth step happens after saving"

    def test_confirming_them_all(self) -> None:
        wizard = CloudWizard(status=CloudStatus(enabled=True)).confirm_all()
        assert wizard.ready_to_write

    def test_a_partial_confirmation_is_not_enough(self) -> None:
        wizard = CloudWizard(status=CloudStatus(enabled=True)).confirm(1)
        assert not wizard.ready_to_write

    def test_the_last_step_is_after_the_edit(self) -> None:
        assert [step.number for step in STEPS if not step.before_editing] == [4]

    def test_an_invented_step_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no step 9"):
            CloudWizard(status=CloudStatus(enabled=True)).confirm(9)

    def test_the_instructions_mention_the_tray(self) -> None:
        """Minimising Steam is the mistake this whole wizard exists to prevent."""
        text = " ".join(step.text.get("en") for step in STEPS)
        assert "tray" in text
        assert "local version" in text


class TestEditSession:
    @pytest.fixture
    def save_path(self, tmp_path: Path) -> Path:
        path = tmp_path / "save.dat"
        path.write_bytes(gzip.compress(json.dumps({"gold": 10, "kills": 3}).encode(), mtime=0))
        return path

    @pytest.fixture
    def store(self, tmp_path: Path) -> BackupStore:
        return BackupStore(tmp_path / "backups")

    def test_a_safe_game_writes_without_ceremony(
        self, save_path: Path, store: BackupStore
    ) -> None:
        plugin = Plugin.from_mapping(dict(MANIFEST, steam_appid=HOLLOW_KNIGHT))
        session = EditSession.open(save_path, plugin, database=DATABASE)
        session.set("gold", 500)
        assert session.may_write
        session.write(store)
        assert json.loads(gzip.decompress(save_path.read_bytes()))["gold"] == 500

    def test_a_dangerous_game_refuses_until_acknowledged(
        self, save_path: Path, store: BackupStore
    ) -> None:
        plugin = Plugin.from_mapping(dict(MANIFEST, steam_appid=ELDEN_RING))
        session = EditSession.open(save_path, plugin, database=DATABASE)
        session.set("gold", 500)

        assert not session.may_write
        with pytest.raises(ConsentRequiredError) as caught:
            session.write(store)
        assert "Nothing has been changed" in caught.value.user_message

        session.acknowledge(Acknowledgement.BAN_RISK)
        assert session.may_write
        session.write(store)

    def test_a_refused_write_leaves_the_file_alone(
        self, save_path: Path, store: BackupStore
    ) -> None:
        original = save_path.read_bytes()
        plugin = Plugin.from_mapping(dict(MANIFEST, steam_appid=ELDEN_RING))
        session = EditSession.open(save_path, plugin, database=DATABASE)
        session.set("gold", 500)
        with pytest.raises(ConsentRequiredError):
            session.write(store)
        assert save_path.read_bytes() == original

    def test_cloud_steps_block_the_write(self, save_path: Path, store: BackupStore) -> None:
        plugin = Plugin.from_mapping(dict(MANIFEST, steam_appid=HOLLOW_KNIGHT))
        session = EditSession.open(
            save_path, plugin, database=DATABASE, cloud_status=CloudStatus(enabled=True)
        )
        session.set("gold", 500)
        assert not session.may_write
        assert any("Steam Cloud" in reason for reason in session.blockers)

        session.confirm_cloud_steps(1, 2, 3)
        assert session.may_write

    def test_editing_an_achievement_field_asks_once(
        self, save_path: Path, store: BackupStore
    ) -> None:
        """The opt-in the save file wants and the acknowledgement are the same thing."""
        plugin = Plugin.from_mapping(dict(MANIFEST, steam_appid=HOLLOW_KNIGHT))
        session = EditSession.open(save_path, plugin, database=DATABASE)

        from savesmith.core.errors import FieldValueError

        with pytest.raises(FieldValueError, match="achievement"):
            session.set("kills", 999)

        session.acknowledge(Acknowledgement.ACHIEVEMENTS)
        session.set("kills", 999)
        assert session.may_write

    def test_the_blockers_are_readable(self, save_path: Path) -> None:
        plugin = Plugin.from_mapping(dict(MANIFEST, steam_appid=ELDEN_RING))
        session = EditSession.open(save_path, plugin, database=DATABASE)
        assert any("ban_risk" in reason for reason in session.blockers)
        assert "Cannot write yet:" in " ".join(session.explain())

    def test_a_real_plugin_reports_its_own_tier(self, tmp_path: Path) -> None:
        plugin = bundled().load().by_id("the-invincible")
        assert plugin is not None
        assert plugin.risk.tier is RiskTier.SAFE
        result = assess(database=DATABASE, appid=plugin.steam_appid, plugin=plugin)
        assert result.tier is RiskTier.SAFE
