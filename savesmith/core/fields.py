"""Addressing one value inside a parsed save.

A field path names a place in the decoded structure::

    playerData.geo
    playerData.equipment[2].count

Dots and brackets cover almost everything, but not quite: Easy Save 3 stores
flat keys that themselves contain dots, so ``"playerData.geo"`` can be a single
key rather than two levels. For those, a plugin writes the path as a list —
``["playerData.geo"]`` — which is unambiguous. Both forms are accepted.

Writing never creates structure. If a save has no ``playerData.geo``, that is
reported; inventing the key would produce a file the game has never seen, and
guessing what shape it should be is exactly the kind of cleverness that
corrupts saves.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from savesmith.core.errors import FieldPathError

type PathStep = str | int

# Negative indexes are allowed in both forms of a path, so that ["items", -1]
# and "items[-1]" cannot disagree.
_SEGMENT_RE = re.compile(r"([^.\[\]]+)|\[(-?\d+)\]")


def parse_path(text: str) -> tuple[PathStep, ...]:
    """Turn ``a.b[0].c`` into ``("a", "b", 0, "c")``."""
    if not text:
        raise FieldPathError(text, "the plugin gives an empty path.")
    steps: list[PathStep] = []
    position = 0
    while position < len(text):
        if text[position] == ".":
            position += 1
            continue
        match = _SEGMENT_RE.match(text, position)
        if match is None:
            raise FieldPathError(text, f"the path cannot be read from character {position + 1}.")
        name, index = match.group(1), match.group(2)
        steps.append(name if name is not None else int(index))
        position = match.end()
    if not steps:
        raise FieldPathError(text, "the plugin gives an empty path.")
    return tuple(steps)


def normalise(path: str | Sequence[str | int]) -> tuple[PathStep, ...]:
    """Accept either the dotted string form or an explicit list of steps."""
    if isinstance(path, str):
        return parse_path(path)
    steps: list[PathStep] = []
    for step in path:
        if isinstance(step, bool) or not isinstance(step, str | int):
            raise FieldPathError(str(path), "path steps must be names or numbers.")
        steps.append(step)
    if not steps:
        raise FieldPathError(str(path), "the plugin gives an empty path.")
    return tuple(steps)


def render(steps: Sequence[PathStep]) -> str:
    """The readable form, for error messages and the GUI."""
    out = ""
    for step in steps:
        if isinstance(step, int):
            out += f"[{step}]"
        else:
            out += step if not out else f".{step}"
    return out


def read(root: Any, path: str | Sequence[str | int]) -> Any:
    steps = normalise(path)
    current = root
    for depth, step in enumerate(steps):
        current = _descend(current, step, steps, depth)
    return current


def exists(root: Any, path: str | Sequence[str | int]) -> bool:
    """Whether the save actually has this value.

    Fields missing from a particular save are ordinary, so this is a question,
    not an exception.
    """
    try:
        read(root, path)
    except FieldPathError:
        return False
    return True


def write(root: Any, path: str | Sequence[str | int], value: Any) -> None:
    """Replace one value, touching nothing else."""
    steps = normalise(path)
    container = root
    for depth, step in enumerate(steps[:-1]):
        container = _descend(container, step, steps, depth)

    last = steps[-1]
    where = render(steps)
    if isinstance(last, int):
        if not isinstance(container, list):
            raise FieldPathError(where, f"{render(steps[:-1])} is not a list.")
        if not -len(container) <= last < len(container):
            raise FieldPathError(where, f"the list has only {len(container)} entries.")
        container[last] = value
        return
    if not isinstance(container, dict):
        raise FieldPathError(where, f"{render(steps[:-1])} is not a group of named values.")
    if last not in container:
        raise FieldPathError(where, "this save does not contain that value.")
    container[last] = value


def _descend(current: Any, step: PathStep, steps: Sequence[PathStep], depth: int) -> Any:
    where = render(steps)
    reached = render(steps[:depth]) or "the save"
    if isinstance(step, int):
        if not isinstance(current, list):
            raise FieldPathError(where, f"{reached} is not a list.")
        if not -len(current) <= step < len(current):
            raise FieldPathError(where, f"{reached} has only {len(current)} entries.")
        return current[step]
    if not isinstance(current, dict):
        raise FieldPathError(where, f"{reached} is not a group of named values.")
    if step not in current:
        raise FieldPathError(where, f"{reached} has no entry called '{step}'.")
    return current[step]
