"""One editing session: a save, a plugin, the risks, and what was agreed to.

Everything the interface needs, in one object, so that the rules live in the
core rather than being re-implemented by each front end. A command line and a
window should not each get to decide what counts as consent.

The write path refuses until three separate things hold:

* a backup can be made (enforced by :class:`SaveFile` itself),
* every acknowledgement the risk assessment demands has been given,
* the Steam Cloud steps, if the cloud is involved, have been carried out.

Nothing here relaxes the rules that live further down: online-linked fields
stay uneditable, achievement fields still need their own opt-in, and the file
is still re-read before writing in case the game saved over it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from savesmith.core.backup import Backup, BackupStore
from savesmith.core.cloud import CloudStatus, CloudWizard
from savesmith.core.errors import SaveSmithError
from savesmith.core.inventory import Stack
from savesmith.core.plugin import Plugin, RiskTier
from savesmith.core.risk import Acknowledgement, Assessment, Consent, RiskDatabase, assess
from savesmith.core.savefile import Change, FieldView, SaveFile


class ConsentRequiredError(SaveSmithError):
    """The user has not yet agreed to something this edit requires."""

    code = "consent_required"

    def __init__(self, missing: frozenset[Acknowledgement], steps: tuple[int, ...] = ()) -> None:
        parts = []
        if missing:
            parts.append("confirm: " + ", ".join(sorted(item.value for item in missing)))
        if steps:
            parts.append("Steam Cloud steps " + ", ".join(str(step) for step in steps))
        super().__init__(
            "This save was not written because something still needs your confirmation. "
            "Nothing has been changed on disk.",
            detail="; ".join(parts),
            missing=sorted(item.value for item in missing),
        )
        self.missing = missing
        self.steps = steps


@dataclass
class EditSession:
    """A save file open for editing, with its risk picture attached."""

    save: SaveFile
    assessment: Assessment
    consent: Consent = field(default_factory=Consent)
    cloud: CloudWizard | None = None

    @classmethod
    def open(
        cls,
        path: Path,
        plugin: Plugin,
        *,
        database: RiskDatabase | None = None,
        anticheat: tuple[str, ...] = (),
        anticheat_scanned: bool = False,
        cloud_status: CloudStatus | None = None,
    ) -> EditSession:
        save = SaveFile.open(path, plugin)
        status = cloud_status or CloudStatus(enabled=plugin.risk.steam_cloud)
        assessment = assess(
            database=database or RiskDatabase.bundled(),
            appid=plugin.steam_appid,
            anticheat=anticheat,
            anticheat_scanned=anticheat_scanned,
            plugin=plugin,
            steam_cloud=status.enabled,
        )
        return cls(
            save=save,
            assessment=assessment,
            cloud=CloudWizard(status=status) if status.enabled else None,
        )

    # -- reading ---------------------------------------------------------

    @property
    def plugin(self) -> Plugin:
        return self.save.plugin

    @property
    def tier(self) -> RiskTier:
        return self.assessment.tier

    def view(self) -> list[FieldView]:
        return self.save.view()

    # -- editing ---------------------------------------------------------

    def set(self, address: str, value: Any) -> Change:
        """Stage a change. Achievement fields need their own acknowledgement.

        The consent given for achievements doubles as the opt-in the save file
        itself demands, so the user is asked once rather than twice for the
        same thing.
        """
        spec = self.plugin.field(address)
        allow = Acknowledgement.ACHIEVEMENTS in self.consent.given
        change = self.save.set(address, value, allow_achievements=allow)
        if spec is not None and spec.achievement:
            # Recorded so the write path knows achievements were touched even
            # if the assessment was made before the change.
            self.consent.give(Acknowledgement.ACHIEVEMENTS)
        return change

    def revert(self, address: str) -> None:
        self.save.revert(address)

    # -- containers -------------------------------------------------------

    def stacks(self, container: str) -> list[Stack]:
        return self.save.stacks(container)

    def set_stack_count(self, container: str, position: str | int, count: int) -> Stack:
        return self.save.set_stack_count(container, position, count)

    def give_item(self, container: str, item: str, count: int = 1) -> Stack:
        """Put something into a container. Same rules, same backup, same risk.

        Items go through the session rather than around it precisely because
        the ban risk and the Steam Cloud dance apply to a granted item exactly
        as they apply to a changed number.
        """
        return self.save.give_item(container, item, count)

    def remove_stack(self, container: str, position: str | int) -> Stack:
        return self.save.remove_stack(container, position)

    @property
    def pending(self) -> tuple[Change, ...]:
        return self.save.pending

    # -- consent ---------------------------------------------------------

    def acknowledge(self, *items: Acknowledgement) -> EditSession:
        self.consent.give(*items)
        return self

    def confirm_cloud_steps(self, *numbers: int) -> EditSession:
        if self.cloud is None:
            return self
        self.cloud.confirm(*numbers)
        if self.cloud.ready_to_write:
            self.consent.give(Acknowledgement.STEAM_CLOUD)
        return self

    @property
    def blockers(self) -> tuple[str, ...]:
        """Everything standing between the user and a write, in plain words."""
        missing = self.consent.missing(self.assessment)
        reasons = [f"needs confirmation: {item.value}" for item in missing]
        if self.cloud is not None and not self.cloud.ready_to_write:
            steps = ", ".join(str(step.number) for step in self.cloud.remaining)
            reasons.append(f"Steam Cloud steps not done: {steps}")
        return tuple(sorted(reasons))

    @property
    def may_write(self) -> bool:
        return not self.blockers

    # -- writing ---------------------------------------------------------

    def write(self, backups: BackupStore, *, force: bool = False) -> Backup:
        """Write, if everything has been agreed to. Refuses rather than warns."""
        missing = self.consent.missing(self.assessment)
        steps = tuple(
            step.number
            for step in (self.cloud.remaining if self.cloud is not None else ())
        )
        if missing or steps:
            raise ConsentRequiredError(missing, steps)
        return self.save.write(backups, force=force)

    def explain(self, language: str = "en") -> list[str]:
        lines = list(self.assessment.explain(language))
        if self.cloud is not None:
            lines += self.cloud.explain(language)
        if self.blockers:
            lines.append("Cannot write yet:")
            lines += [f"  {reason}" for reason in self.blockers]
        else:
            lines.append("Ready to write.")
        return lines
