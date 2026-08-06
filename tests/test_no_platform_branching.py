"""Guard: platform branching stays inside the path layer.

The spec's rule is "not a single ``if platform == 'win32'`` outside
``core/paths``". This test is what makes that rule real — without it the rule
survives about two weeks of hurried commits.

If you are here because this test failed: take the OS-specific value you need
and expose it from ``savesmith.core.paths`` (or ``platform_.py``) instead, then
pass it in as an argument. That is also what makes the code testable on the
other OS.

Only executable code is inspected. Docstrings talking *about* Windows are the
whole point of the documentation, and an early version of this guard flagged
the sentence explaining ``[$WIN32]`` conditionals in the VDF parser.
"""

from __future__ import annotations

import re
import tokenize
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = PROJECT_ROOT / "savesmith"

# Files allowed to inspect the host OS directly.
ALLOWED = {
    PACKAGE_ROOT / "core" / "platform_.py",
}
ALLOWED_DIRS = {
    PACKAGE_ROOT / "core" / "paths",
}

FORBIDDEN = re.compile(
    r"""
    sys\.platform          # the classic
    | os\.name
    | platform\.system     # stdlib platform module
    | platform\.machine
    | \bwin32\b
    | \bdarwin\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

_IGNORED_TOKENS = {
    tokenize.COMMENT,
    tokenize.STRING,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
    # f-string parts, tokenised separately since 3.12
    getattr(tokenize, "FSTRING_START", -1),
    getattr(tokenize, "FSTRING_MIDDLE", -2),
    getattr(tokenize, "FSTRING_END", -3),
}


def _code_by_line(path: Path) -> dict[int, str]:
    """Source lines with comments and string literals stripped out."""
    collected: dict[int, list[str]] = {}
    with path.open(encoding="utf-8") as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type in _IGNORED_TOKENS:
                continue
            collected.setdefault(token.start[0], []).append(token.string)
    return {number: "".join(parts) for number, parts in collected.items()}


def _offenders_in(path: Path) -> list[str]:
    return [
        f"{path.name}:{number}: {code}"
        for number, code in sorted(_code_by_line(path).items())
        if FORBIDDEN.search(code)
    ]


def _is_allowed(path: Path) -> bool:
    return path in ALLOWED or any(parent in ALLOWED_DIRS for parent in path.parents)


def test_no_platform_branching_outside_the_path_layer() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if _is_allowed(path):
            continue
        offenders += [f"{path.relative_to(PROJECT_ROOT)}: {hit}" for hit in _offenders_in(path)]

    assert not offenders, (
        "OS detection outside the path layer:\n  "
        + "\n  ".join(offenders)
        + "\nExpose the value from savesmith.core.paths and pass it in instead."
    )


def test_the_guard_catches_real_branching(tmp_path: Path) -> None:
    """A guard that cannot fail guards nothing."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        "import sys\n"
        "def where():\n"
        "    if sys.platform == 'win32':\n"
        "        return 1\n"
        "    return 2\n",
        encoding="utf-8",
    )
    assert _offenders_in(offender)


def test_the_guard_ignores_prose(tmp_path: Path) -> None:
    """Documentation mentioning Windows must not trip it."""
    innocent = tmp_path / "innocent.py"
    innocent.write_text(
        '"""Handles [$WIN32] conditionals and darwin-specific layouts."""\n'
        "# sys.platform is deliberately not used here\n"
        "def where(platform):\n"
        "    return platform\n",
        encoding="utf-8",
    )
    assert _offenders_in(innocent) == []


def test_allowed_paths_exist() -> None:
    """Catches the allowlist drifting after a rename."""
    for path in ALLOWED:
        assert path.exists(), f"allowlisted file is gone: {path}"
    for directory in ALLOWED_DIRS:
        assert directory.is_dir(), f"allowlisted directory is gone: {directory}"
