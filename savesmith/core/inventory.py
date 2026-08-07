"""Containers: the second kind of edit.

A field is a number that already exists and gets a different value. An item is
a *record that has to appear*, and :func:`savesmith.core.fields.write` refuses
to create structure on purpose — a path that does not exist is almost always a
typo, and inventing dictionaries along the way is how an editor silently writes
nonsense into somebody's save. So containers are their own surface rather than
a clever reading of the field one.

Three shapes cover nearly every game seen so far:

``map``
    ``{"1": 5}`` — the id is the key, the count is the value. RPG Maker's
    ``party._items`` is exactly this, and adding an item is adding a key, which
    is the format's own idea of how it works.
``list``
    a list of records of any length, each carrying an id and a count. Elden
    Ring's inventory tables are this.
``slots``
    a fixed-length array where an empty place is ``null`` or a record whose id
    is zero. Length never changes; what changes is what sits in a place.

**The rule that makes adding safe: a new record is always a clone.** Records
carry fields nobody here understands — a uuid, durability, a flag the game sets
when an item was bought rather than found. Cloning an existing record keeps
them; building one from scratch throws them away and the game meets a record
unlike any it wrote. When there is nothing to clone, this refuses and says to
pick the thing up in the game once, save, and come back. ``map`` is the
exception: there a new key *is* the native form and there is nothing to keep.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, MutableMapping, MutableSequence
from dataclasses import dataclass
from typing import Any

from savesmith.core import fields as field_access
from savesmith.core.errors import FieldPathError, SaveSmithError
from savesmith.core.plugin import ContainerShape, ContainerSpec


class ContainerError(SaveSmithError):
    """Something about this container makes the asked-for change impossible."""

    code = "container"


@dataclass(frozen=True)
class Stack:
    """One kind of thing in a container, and how many of it there are.

    ``position`` is where it sits in the container's own storage: the key for a
    ``map``, the index for a ``list`` or a ``slots``. Every change goes through
    it rather than through the item's id, because a container may perfectly
    well hold the same thing twice — three copies of one weapon, each with its
    own reinforcement — and "set the count of that one" has to mean one of them.
    """

    item: str
    count: int
    position: str | int


def container(root: Any, spec: ContainerSpec) -> Any:
    """The raw container out of the decoded save, with a readable refusal."""
    try:
        found = field_access.read(root, spec.path)
    except FieldPathError as exc:
        raise ContainerError(
            f"This save has no {spec.label.get()} in it. The plugin expects one at "
            f"'{field_access.render(spec.path)}', and this file does not have that.",
            detail=str(exc),
        ) from exc
    return found


def stacks(root: Any, spec: ContainerSpec) -> list[Stack]:
    """Everything in the container, gaps and unidentifiable rows left out."""
    return stacks_of(container(root, spec), spec)


def stacks_of(found: Any, spec: ContainerSpec) -> list[Stack]:
    """The same, for a container already in hand rather than a whole save."""
    if spec.shape is ContainerShape.MAP:
        if not isinstance(found, Mapping):
            raise _wrong_shape(spec, found)
        return [
            Stack(item=str(key), count=_as_count(value), position=str(key))
            for key, value in found.items()
            if _is_count(value)
        ]

    if not isinstance(found, list):
        raise _wrong_shape(spec, found)
    out = []
    for index, record in enumerate(found):
        item = _item_of(record, spec)
        if item is None:
            continue
        out.append(Stack(item=item, count=_count_of(record, spec), position=index))
    return out


def find(root: Any, spec: ContainerSpec, item: str) -> list[Stack]:
    return [stack for stack in stacks(root, spec) if stack.item == item]


def set_count(root: Any, spec: ContainerSpec, position: str | int, count: int) -> Stack:
    """Change how many of one stack there are. Never creates anything."""
    _check_count(spec, count)
    found = container(root, spec)

    if spec.shape is ContainerShape.MAP:
        if not isinstance(found, MutableMapping) or str(position) not in found:
            raise ContainerError(
                f"There is no such thing in the {spec.label.get()} to change."
            )
        found[str(position)] = count
        return Stack(item=str(position), count=count, position=str(position))

    record = _at(found, spec, position)
    record[spec.count] = count
    item = _item_of(record, spec)
    return Stack(item=item or "", count=count, position=position)


def remove(root: Any, spec: ContainerSpec, position: str | int) -> Stack:
    """Take a stack out. In ``slots`` the place stays and is emptied."""
    found = container(root, spec)

    if spec.shape is ContainerShape.MAP:
        if not isinstance(found, MutableMapping) or str(position) not in found:
            raise ContainerError(f"There is no such thing in the {spec.label.get()} to remove.")
        count = _as_count(found.pop(str(position)))
        return Stack(item=str(position), count=count, position=str(position))

    record = _at(found, spec, position)
    gone = Stack(
        item=_item_of(record, spec) or "",
        count=_count_of(record, spec),
        position=position,
    )
    if spec.shape is ContainerShape.SLOTS:
        # The place has to stay: the array's length is part of the format.
        record[spec.key] = spec.empty
        record[spec.count] = 0
    else:
        assert isinstance(found, MutableSequence)
        del found[int(position)]
    return gone


def give(root: Any, spec: ContainerSpec, item: str, count: int = 1) -> Stack:
    """Put a thing that is not there into the container.

    Already present? Then this adds to the stack rather than making a second
    one, which is what a player means by "give me more of these".
    """
    _check_count(spec, count)
    existing = find(root, spec, item)
    if existing:
        first = existing[0]
        return set_count(root, spec, first.position, min(first.count + count, spec.max_count))

    found = container(root, spec)

    if spec.shape is ContainerShape.MAP:
        if not isinstance(found, MutableMapping):
            raise _wrong_shape(spec, found)
        _check_capacity(spec, len(found))
        found[item] = count
        return Stack(item=item, count=count, position=item)

    if not isinstance(found, MutableSequence):
        raise _wrong_shape(spec, found)

    if spec.shape is ContainerShape.SLOTS:
        position = _first_empty(found, spec)
        if position is None:
            raise ContainerError(
                f"The {spec.label.get()} is full: every one of its {len(found)} places "
                f"is taken. Remove something first."
            )
        record = _clone(found, spec, at=position)
        found[position] = record
    else:
        _check_capacity(spec, len(found))
        record = _clone(found, spec)
        position = len(found)
        found.append(record)

    record[spec.key] = item
    record[spec.count] = count
    if spec.sequence:
        record[spec.sequence] = _next_in_sequence(found, spec)
    return Stack(item=item, count=count, position=position)


def totals(found: Any, spec: ContainerSpec) -> dict[str, int]:
    """How many of each thing there are, the same thing in two places summed.

    What a player wants to see about a change is "Rune Arc: 10 → 99", not which
    row of a table moved. Summing per item says that, and it stays true however
    the rows underneath were shuffled.
    """
    counted: dict[str, int] = {}
    for stack in stacks_of(found, spec):
        counted[stack.item] = counted.get(stack.item, 0) + stack.count
    return counted


def differences(before: Any, after: Any, spec: ContainerSpec) -> list[tuple[str, int, int]]:
    """What changed between two versions of the same container."""
    was, now = totals(before, spec), totals(after, spec)
    changed = []
    for item in sorted(set(was) | set(now)):
        if was.get(item, 0) != now.get(item, 0):
            changed.append((item, was.get(item, 0), now.get(item, 0)))
    return changed


# -- the pieces the operations above are made of --------------------------


def _clone(found: MutableSequence[Any], spec: ContainerSpec, *, at: int | None = None) -> Any:
    """A copy of a record that is already there, or a refusal.

    Which record is copied barely matters — the fields worth keeping are the
    ones every record of this container has. What matters is that one exists.
    """
    for record in found:
        if isinstance(record, MutableMapping) and _item_of(record, spec) is not None:
            return copy.deepcopy(record)
    if at is not None and isinstance(found[at], MutableMapping):
        return copy.deepcopy(found[at])
    raise ContainerError(
        f"The {spec.label.get()} is empty, and SaveSmith adds things by copying "
        f"something already in there. A record it builds from nothing would be "
        f"missing whatever else the game keeps per item. Pick up any one item in "
        f"the game, save, and open this again."
    )


def _first_empty(found: MutableSequence[Any], spec: ContainerSpec) -> int | None:
    for index, record in enumerate(found):
        if _item_of(record, spec) is None:
            return index
    return None


def _next_in_sequence(found: MutableSequence[Any], spec: ContainerSpec) -> int:
    """One past the highest, for containers whose records are numbered.

    Elden Ring numbers every inventory row and shows the list in that order;
    two rows sharing a number is not something the game ever writes.
    """
    highest = 0
    for record in found:
        if isinstance(record, Mapping):
            value = record.get(spec.sequence)
            if isinstance(value, int) and not isinstance(value, bool):
                highest = max(highest, value)
    return highest + 1


def _at(found: Any, spec: ContainerSpec, position: str | int) -> MutableMapping[str, Any]:
    if not isinstance(found, MutableSequence):
        raise _wrong_shape(spec, found)
    try:
        index = int(position)
    except (TypeError, ValueError):
        raise ContainerError(f"'{position}' is not a place in the {spec.label.get()}.") from None
    if not 0 <= index < len(found):
        raise ContainerError(
            f"The {spec.label.get()} has {len(found)} places and there is no place {index}."
        )
    record = found[index]
    if not isinstance(record, MutableMapping):
        raise ContainerError(f"Place {index} of the {spec.label.get()} is not an item record.")
    return record


def _item_of(record: Any, spec: ContainerSpec) -> str | None:
    if not isinstance(record, Mapping):
        return None
    value = record.get(spec.key)
    if value is None or value == spec.empty:
        return None
    return str(value)


def _count_of(record: Mapping[str, Any], spec: ContainerSpec) -> int:
    value = record.get(spec.count)
    return _as_count(value) if _is_count(value) else 0


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_count(value: Any) -> int:
    return int(value) if _is_count(value) else 0


def _check_count(spec: ContainerSpec, count: int) -> None:
    if isinstance(count, bool) or not isinstance(count, int):
        raise ContainerError("A number of items is a whole number.")
    if count < 0:
        raise ContainerError("A number of items cannot be negative.")
    if count > spec.max_count:
        raise ContainerError(
            f"{spec.label.get()} holds at most {spec.max_count} of one thing, and "
            f"{count} was asked for. A count the game does not expect is a good way "
            f"to have it rewrite the save its own way."
        )


def _check_capacity(spec: ContainerSpec, used: int) -> None:
    if spec.capacity is not None and used >= spec.capacity:
        raise ContainerError(
            f"The {spec.label.get()} holds {spec.capacity} different things and is full. "
            f"Remove something first."
        )


def _wrong_shape(spec: ContainerSpec, found: Any) -> ContainerError:
    return ContainerError(
        f"The {spec.label.get()} in this save is not shaped the way the plugin says "
        f"it is, so SaveSmith will not touch it.",
        detail=(
            f"{field_access.render(spec.path)} is {type(found).__name__}, "
            f"expected {spec.shape.value}"
        ),
    )
