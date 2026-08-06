"""Guard: platform branching stays inside the path layer.

The spec's rule is "not a single ``if platform == 'win32'`` outside
``core/paths``". This test is what makes that rule real — without it the rule
survives about two weeks of hurried commits.

If you are here because this test failed: take the OS-specific value you need
and expose it from ``savesmith.core.paths`` (or ``platform_.py``) instead, then
pass it in as an argument. That is also what makes the code testable on the
other OS.
"""

from __future__ import annotations

import re
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


def _is_allowed(path: Path) -> bool:
    return path in ALLOWED or any(parent in ALLOWED_DIRS for parent in path.parents)


def test_no_platform_branching_outside_the_path_layer() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if _is_allowed(path):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#", 1)[0]
            if FORBIDDEN.search(code):
                rel = path.relative_to(PROJECT_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "OS detection outside the path layer:\n  "
        + "\n  ".join(offenders)
        + "\nExpose the value from savesmith.core.paths and pass it in instead."
    )


def test_the_guard_can_actually_fail() -> None:
    """A guard that cannot fail guards nothing."""
    assert FORBIDDEN.search("if sys.platform == 'win32':")
    assert FORBIDDEN.search("import os; os.name")
    assert not FORBIDDEN.search("resolver = get_resolver(platform)")


def test_allowed_paths_exist() -> None:
    """Catches the allowlist drifting after a rename."""
    for path in ALLOWED:
        assert path.exists(), f"allowlisted file is gone: {path}"
