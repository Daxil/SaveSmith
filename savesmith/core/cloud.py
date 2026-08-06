"""Steam Cloud, and the four steps that stop it eating an edit.

Not a ban risk — a far more common one. Steam can upload its own older copy
over the file that was just edited, or throw a conflict dialog the user
resolves the wrong way. Either way hours of work vanish and the tool gets
blamed, correctly, because it knew the game used the cloud and said nothing.

So when the cloud is in play SaveSmith asks for four things in order, and will
not write until they are confirmed:

1. Close the game completely.
2. Quit Steam entirely — not to the tray — or turn Steam Cloud off for this
   game in Properties → General.
3. Edit the save.
4. Start Steam. If a conflict appears, choose the local version.

Yes, it is friction. It is repaid by people not losing progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from savesmith.core.plugin import Localized
from savesmith.core.steam import SteamInstall


@dataclass(frozen=True)
class CloudStatus:
    """Whether Steam Cloud has anything to do with this game."""

    enabled: bool
    caches: tuple[Path, ...] = ()
    """The remotecache.vdf files found, one per local account."""
    accounts: tuple[str, ...] = ()

    @property
    def evidence(self) -> str:
        if not self.enabled:
            return "no sign that Steam Cloud is used for this game"
        if not self.caches:
            # Declared by the plugin rather than seen on disk. Saying "in use
            # for 0 accounts" would read like a contradiction.
            return "this game uses Steam Cloud"
        return f"Steam has synced this game for {len(self.caches)} local account(s)"


def cloud_status(install: SteamInstall, appid: int) -> CloudStatus:
    """Look for Steam's own record of syncing this game.

    ``remotecache.vdf`` is written when Steam actually syncs a game's files, so
    its presence is evidence rather than a setting someone might have changed
    since. Absence is not proof of the opposite, which is why the wizard can be
    demanded by a plugin as well.
    """
    caches: list[Path] = []
    accounts: list[str] = []
    for user in install.scan().users:
        cache = user.cloud_cache(appid)
        if cache is not None:
            caches.append(cache)
            accounts.append(user.account_id)
    return CloudStatus(enabled=bool(caches), caches=tuple(caches), accounts=tuple(accounts))


@dataclass(frozen=True)
class Step:
    number: int
    text: Localized
    before_editing: bool
    """False for the steps that only make sense once the edit is written."""


STEPS: tuple[Step, ...] = (
    Step(
        number=1,
        before_editing=True,
        text=Localized(
            {
                "en": "Close the game completely.",
                "ru": "Полностью закройте игру.",
            }
        ),
    ),
    Step(
        number=2,
        before_editing=True,
        text=Localized(
            {
                "en": (
                    "Quit Steam entirely — not just to the tray — or turn Steam Cloud "
                    "off for this game in Properties → General."
                ),
                "ru": (
                    "Выйдите из Steam целиком — не сворачивая в трей — либо отключите "
                    "Steam Cloud для этой игры в Свойства → Основные."
                ),
            }
        ),
    ),
    Step(
        number=3,
        before_editing=True,
        text=Localized(
            {
                "en": "Edit the save.",
                "ru": "Отредактируйте сохранение.",
            }
        ),
    ),
    Step(
        number=4,
        before_editing=False,
        text=Localized(
            {
                "en": "Start Steam. If a conflict dialog appears, choose the local version.",
                "ru": (
                    "Запустите Steam. Если появится диалог конфликта, выберите "
                    "локальную версию."
                ),
            }
        ),
    ),
)


@dataclass
class CloudWizard:
    """Tracks which steps the user says they have done.

    The state lives here rather than in a checkbox so that the rule survives
    whatever the interface turns out to be.
    """

    status: CloudStatus
    confirmed: set[int] = field(default_factory=set)

    @property
    def needed(self) -> bool:
        return self.status.enabled

    @property
    def required_before_writing(self) -> tuple[Step, ...]:
        return tuple(step for step in STEPS if step.before_editing)

    @property
    def remaining(self) -> tuple[Step, ...]:
        if not self.needed:
            return ()
        return tuple(
            step for step in self.required_before_writing if step.number not in self.confirmed
        )

    @property
    def ready_to_write(self) -> bool:
        return not self.remaining

    def confirm(self, *numbers: int) -> CloudWizard:
        known = {step.number for step in STEPS}
        for number in numbers:
            if number not in known:
                raise ValueError(f"there is no step {number}")
            self.confirmed.add(number)
        return self

    def confirm_all(self) -> CloudWizard:
        return self.confirm(*(step.number for step in self.required_before_writing))

    def explain(self, language: str = "en") -> list[str]:
        if not self.needed:
            return ["Steam Cloud is not involved for this game."]
        lines = [f"Steam Cloud: {self.status.evidence}"]
        for step in STEPS:
            mark = "✓" if step.number in self.confirmed else " "
            when = "" if step.before_editing else "  (after saving)"
            lines.append(f"  [{mark}] {step.number}. {step.text.get(language)}{when}")
        return lines
