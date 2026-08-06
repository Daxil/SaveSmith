"""The operation registry.

An operation is one reversible step of a save format: strip a header, un-gzip,
parse JSON. A plugin's ``pipeline`` is a list of these, and the same list run
backwards writes the file again.

Every operation must satisfy one contract:

    encode(decode(payload, params, hints), params, hints) == payload

byte for byte, for any file the operation accepts. That is what makes the
round-trip gate in milestone 2 meaningful, and it is why ``decode`` is handed a
``hints`` dictionary to fill in.

Hints exist because decoding throws information away. Un-gzipping a file
discards the compression level, the timestamp and the stored filename; without
recording them, re-compressing produces a different byte stream even when not a
single value changed. Anything a step needs in order to rebuild its input goes
into hints.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from savesmith.core.errors import UnknownOperationError

type Params = Mapping[str, Any]
type Hints = MutableMapping[str, Any]
type DecodeFn = Callable[[Any, Params, Hints], Any]
type EncodeFn = Callable[[Any, Params, Mapping[str, Any]], Any]


@dataclass(frozen=True)
class Operation:
    name: str
    decode: DecodeFn
    encode: EncodeFn
    required_params: tuple[str, ...] = ()
    optional_params: tuple[str, ...] = ()
    summary: str = ""
    """One line, shown in the GUI's advanced view so a curious user can see
    what the plugin is doing to their file."""

    def known_params(self) -> frozenset[str]:
        return frozenset(self.required_params) | frozenset(self.optional_params)


_REGISTRY: dict[str, Operation] = {}


def register(operation: Operation) -> Operation:
    if operation.name in _REGISTRY:
        raise ValueError(f"operation {operation.name!r} is registered twice")
    _REGISTRY[operation.name] = operation
    return operation


def get(name: str) -> Operation:
    operation = _REGISTRY.get(name)
    if operation is None:
        raise UnknownOperationError(name, known=tuple(_REGISTRY))
    return operation


def names() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def all_operations() -> tuple[Operation, ...]:
    return tuple(_REGISTRY[name] for name in names())
