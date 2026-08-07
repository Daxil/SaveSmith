"""Printing on a console that cannot spell what we are printing.

The failure this exists for is not hypothetical: the packaged binary died on
Windows CI with UnicodeEncodeError on the arrow in 'gzip → json_parse', before
printing anything at all. Under --language ru it would have died on the first
Cyrillic character instead, which is most of what that option is for.
"""

from __future__ import annotations

import gzip
import io
import json
import sys
from pathlib import Path

import pytest

from savesmith import cli, rpc
from savesmith.core.console import use_utf8
from savesmith.core.paths import FakeSystem
from savesmith.core.store import PluginStore


@pytest.fixture
def save(tmp_path: Path) -> Path:
    path = tmp_path / "save.dat"
    path.write_bytes(gzip.compress(json.dumps({"gold": 100}).encode(), mtime=0))
    return path


def _windows_console() -> io.TextIOWrapper:
    """What Python hands a program on Windows when the locale is Western."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")


class TestOutputSurvivesTheConsole:
    def test_an_arrow_does_not_kill_the_program(
        self, monkeypatch: pytest.MonkeyPatch, save: Path
    ) -> None:
        console = _windows_console()
        monkeypatch.setattr(sys, "stdout", console)

        assert cli.main(["identify", str(save)]) == 0

        console.flush()
        printed = console.buffer.getvalue().decode("utf-8")  # type: ignore[attr-defined]
        assert "gzip → json_parse" in printed

    def test_russian_survives_it_too(
        self, monkeypatch: pytest.MonkeyPatch, fake_machine: FakeSystem, save: Path
    ) -> None:
        """cp1252 has no Cyrillic at all, so this is the whole Russian half of
        the product: every label under --language ru would raise on its way to
        the screen."""
        plugin = PluginStore.for_system(fake_machine).root / "console-test"
        plugin.mkdir(parents=True)
        (plugin / "manifest.json").write_text(
            json.dumps(
                {
                    "id": "console-test",
                    "version": 1,
                    "game": "Console Test",
                    "engine": "test",
                    "confidence": "probable",
                    "risk": {"tier": "safe", "reason": {"en": "single player"}},
                    "pipeline": [{"op": "gzip"}, {"op": "json_parse"}],
                    "fields": [
                        {"path": "gold", "label": {"en": "Gold", "ru": "Золото"}, "type": "int"}
                    ],
                }
            ),
            encoding="utf-8",
        )

        console = _windows_console()
        monkeypatch.setattr(sys, "stdout", console)

        assert cli.main(["--language", "ru", "show", str(save)], system=fake_machine) == 0

        console.flush()
        printed = console.buffer.getvalue().decode("utf-8")  # type: ignore[attr-defined]
        assert "Золото" in printed

    def test_the_rpc_stream_is_utf8_whatever_the_console_says(self) -> None:
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        sink = _windows_console()
        code = rpc.Server().serve(io.StringIO(request + "\n"), sink)

        assert code == 0
        sink.flush()
        answer = json.loads(sink.buffer.getvalue().decode("utf-8"))  # type: ignore[attr-defined]
        assert answer["result"]["ok"]


class TestTheHelperItself:
    def test_it_switches_a_stream_over(self) -> None:
        stream = _windows_console()
        use_utf8(stream)
        assert stream.encoding.lower().replace("-", "") == "utf8"

    def test_a_stream_that_cannot_be_reconfigured_is_left_alone(self) -> None:
        """A pytest capture object or a plain StringIO must not raise here."""
        use_utf8(io.StringIO(), object(), None)

    def test_errors_are_replaced_rather_than_raised(self) -> None:
        """A terminal that cannot render a character should show a question
        mark, never a stack trace."""
        stream = _windows_console()
        use_utf8(stream)
        stream.write("→ ✓ Золото\n")
        stream.flush()
        assert stream.buffer.getvalue().decode("utf-8") == "→ ✓ Золото\n"  # type: ignore[attr-defined]
