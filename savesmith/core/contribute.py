"""Sending a plugin back, so the next person does not have to work it out.

The whole product rests on somebody, once, figuring out where a game keeps its
numbers. That work is worth exactly as much as the number of people it reaches,
and today it reaches one: whoever ran ``discover``. This is the road back.

**The save file never travels. Not once, not optionally, not "just this one to
help us debug".** A manifest describes a *format* — offsets, operations, which
byte is the gold. A save file describes a *person*: their character's name,
their Steam id, how many hours they have played, sometimes the friends they
played with. Those are different things, and only the first is anybody else's
business. So this sends the manifest, and refuses to be talked into more.

**What it does send is checked twice.**

*That it works.* The plugin is run against the sender's own saves and every one
of them must rebuild byte for byte. A plugin that cannot do that is not a
contribution, it is a way to corrupt somebody else's save, and it is stopped
here rather than in review.

*That it carries nothing personal.* A manifest is written by hand as often as
it is generated, and a hand-written one picks up absolute paths — the author's
home folder, their user name, the Steam id in the middle of a save path. Those
are found and shown before anything leaves the machine, because a person who
pastes their user name into a public issue tracker cannot take it back.

**There is no server, and that is a decision.** The manifest goes into a
prefilled issue on the project's tracker, opened in the sender's own browser,
under their own account, where they can read every word before pressing the
button. Accepting uploads would mean running a service, moderating it, and
being trusted with whatever people upload — all to avoid one click.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from savesmith.core.errors import SaveSmithError
from savesmith.core.paths import SystemFacade
from savesmith.core.plugin import MANIFEST_NAME, Plugin
from savesmith.core.repository import Verification, verify

PROJECT = "https://github.com/Daxil/SaveSmith"

# A GitHub issue can be prefilled through the query string, and browsers and
# servers both give up on very long URLs. Beyond this the manifest travels as a
# file the sender attaches instead.
MAX_URL = 6000


class ContributionError(SaveSmithError):
    """This plugin is not in a state to be sent anywhere."""

    code = "contribute"


@dataclass(frozen=True)
class Leak:
    """Something in the manifest that belongs to the sender, not the format."""

    what: str
    where: str
    text: str

    def describe(self) -> str:
        return f"{self.what} in '{self.where}': {self.text}"


@dataclass
class Submission:
    """A plugin, checked and ready for a person to look at and send."""

    plugin: Plugin
    verification: Verification
    leaks: list[Leak] = field(default_factory=list)
    manifest: str = ""

    @property
    def proved(self) -> bool:
        return self.verification.passed

    @property
    def clean(self) -> bool:
        return not self.leaks

    def title(self) -> str:
        return f"Плагин: {self.plugin.game}"

    def body(self, *, with_manifest: bool = True) -> str:
        """What the issue says. Written to be read by a person, not parsed."""
        files = len(self.verification.results)
        lines = [
            f"**Игра:** {self.plugin.game}",
            f"**Движок:** {self.plugin.engine}",
            f"**Плагин:** `{self.plugin.id}` версия {self.plugin.version}",
        ]
        if self.plugin.steam_appid is not None:
            lines.append(f"**Steam AppID:** {self.plugin.steam_appid}")
        lines += [
            "",
            f"Проверено на {files} сохранени{'и' if files == 1 else 'ях'}: каждое "
            f"разбирается и собирается обратно байт в байт.",
            "",
            "Сами сохранения не прикладываются и никуда не отправлялись — ниже только "
            "описание формата.",
            "",
        ]
        if with_manifest:
            lines += ["```json", self.manifest, "```"]
        else:
            lines += [
                f"Манифест великоват для ссылки, поэтому приложи файл "
                f"`{MANIFEST_NAME}` к этому сообщению — перетащи его сюда мышкой.",
            ]
        return "\n".join(lines)

    def url(self) -> str:
        """The tracker, with the form already filled in.

        The length that matters is the *encoded* one, and encoding Russian
        text triples it: a manifest well under the limit turned into a link far
        over it, which browsers silently truncate. So the link is built, then
        measured, and only then falls back to asking for an attachment.
        """
        full = self._link(self.body())
        return full if len(full) <= MAX_URL else self._link(self.body(with_manifest=False))

    @property
    def needs_attachment(self) -> bool:
        """Whether the manifest has to travel as a file rather than in the link."""
        return len(self._link(self.body())) > MAX_URL

    def _link(self, body: str) -> str:
        return (
            f"{PROJECT}/issues/new?labels=plugin"
            f"&title={quote(self.title())}"
            f"&body={quote(body)}"
        )

    def explain(self) -> list[str]:
        lines = list(self.verification.explain())
        if self.leaks:
            lines.append("")
            lines.append("В манифесте есть личное — уедет в публичный трекер, если не убрать:")
            lines += [f"  {leak.describe()}" for leak in self.leaks]
        return lines


def prepare(
    plugin: Plugin,
    saves: list[Path],
    system: SystemFacade | None = None,
) -> Submission:
    """Check a plugin against real saves and look it over for personal data."""
    if plugin.source is None:
        raise ContributionError(
            f"'{plugin.id}' has no manifest file on this machine, so there is "
            f"nothing to send. Install it first."
        )
    if not saves:
        raise ContributionError(
            "Nothing to check the plugin against. Point at a save file or at the "
            "game, so the plugin can be proved on real data before it is offered "
            "to anybody else.",
            detail=f"plugin {plugin.id}",
        )

    manifest = plugin.source.read_text(encoding="utf-8")
    return Submission(
        plugin=plugin,
        verification=verify(plugin, saves),
        leaks=look_for_personal_data(manifest, system),
        manifest=manifest.strip(),
    )


# A Steam id is seventeen digits and starts with the same four; it turns up in
# save paths, and it names an account.
_STEAM_ID = re.compile(r"\b7656\d{13}\b")
# Windows and macOS spell an absolute home path differently and both name their
# owner in it. The manifest is JSON, where a Windows path arrives with its
# backslashes doubled — matching only single ones would miss the most common
# case there is: somebody's hand-written manifest with their own folder in it.
_HOME_PATH = re.compile(r"(?:[A-Za-z]:\\{1,2}Users\\{1,2}|/Users/|/home/)([^\\/\"\s]+)")


def look_for_personal_data(manifest: str, system: SystemFacade | None = None) -> list[Leak]:
    """Everything in this text that identifies whoever wrote it.

    Deliberately noisy rather than clever: a false alarm costs a glance, and a
    miss costs somebody their user name on a public issue tracker forever.
    """
    found: list[Leak] = []

    for match in _STEAM_ID.finditer(manifest):
        found.append(Leak(what="Steam ID", where=_line_of(manifest, match.start()), text=match[0]))

    for match in _HOME_PATH.finditer(manifest):
        found.append(
            Leak(
                what="путь к домашней папке",
                where=_line_of(manifest, match.start()),
                text=match[0],
            )
        )

    if system is not None:
        # The user's own name, wherever it appears and however it got there.
        name = (system.username() or "").strip()
        if len(name) > 2:
            for match in re.finditer(re.escape(name), manifest, re.IGNORECASE):
                place = _line_of(manifest, match.start())
                if not any(leak.where == place for leak in found):
                    found.append(Leak(what="имя пользователя", where=place, text=name))

    return found


def _line_of(text: str, index: int) -> str:
    """Which line of the manifest something sits on, for pointing at it."""
    line = text.count("\n", 0, index) + 1
    return f"строка {line}"


def as_json(submission: Submission) -> str:
    """The submission as data, for a window rather than a terminal."""
    return json.dumps(
        {
            "plugin": submission.plugin.id,
            "game": submission.plugin.game,
            "proved": submission.proved,
            "files": len(submission.verification.results),
            "leaks": [leak.describe() for leak in submission.leaks],
            "url": submission.url(),
        },
        ensure_ascii=False,
        indent=2,
    )
