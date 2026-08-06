"""Running a plugin's pipeline forwards and backwards.

A pipeline is the whole of a save format expressed as an ordered list of
reversible steps. Reading runs it forwards; writing runs the identical list
backwards. There is no separate writer to fall out of sync with the reader,
which is the usual way save editors corrupt files.

**Hints.** Decoding discards things — a compression level, the spacing in a
JSON file, a header we skipped. Each step gets a dictionary to record whatever
it needs to rebuild its own input, and that dictionary comes back to it on the
way out.

**Passthrough.** The pipeline also remembers each step's input and output while
decoding. On the way back, a step whose value has not changed is skipped
entirely and its recorded input is reused. This matters for formats compressed
by something other than zlib — .NET and Java produce different deflate output
for the same data — where re-compressing would change bytes that had no reason
to change. Editing one number then re-writes only what that number affects.

The round-trip gate turns passthrough off, so it measures real understanding of
the format rather than the shortcut.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from savesmith.core import ops
from savesmith.core.errors import PipelineError, PluginValidationError

# Above this, remembering a step's input costs more memory than the shortcut is
# worth. Ordinary saves are kilobytes; this only guards against a pathological
# file.
_PASSTHROUGH_LIMIT = 32 * 1024 * 1024


@dataclass(frozen=True)
class Step:
    op: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.op


@dataclass
class Decoded:
    """What came out of a file, plus everything needed to put it back."""

    value: Any
    hints: list[dict[str, Any]]


@dataclass(frozen=True)
class RoundTrip:
    """How faithfully a pipeline can rebuild one particular file."""

    exact_bytes: bool
    """The rebuilt file is identical, byte for byte. What the gate requires."""
    rebuild_readable: bool
    """The rebuilt file can be read back and yields the same values. True with
    ``exact_bytes`` false means the container differs — almost always a
    compressor that is not zlib — and the game would still load the file.
    False means the pipeline produced something that is not a valid save, which
    is the dangerous case."""
    detail: str

    @property
    def passed(self) -> bool:
        return self.exact_bytes


class Pipeline:
    def __init__(self, steps: Sequence[Step], *, plugin_id: str = "<unnamed>") -> None:
        self.steps = tuple(steps)
        self.plugin_id = plugin_id
        self._validate()

    def _validate(self) -> None:
        for index, step in enumerate(self.steps):
            operation = ops.get(step.op)  # raises UnknownOperationError
            missing = [name for name in operation.required_params if name not in step.params]
            if missing:
                raise PluginValidationError(
                    self.plugin_id,
                    f"pipeline step {index + 1} ({step.op})",
                    f"is missing the setting(s): {', '.join(missing)}.",
                )
            unknown = set(step.params) - operation.known_params()
            if unknown:
                raise PluginValidationError(
                    self.plugin_id,
                    f"pipeline step {index + 1} ({step.op})",
                    f"has settings it does not understand: {', '.join(sorted(unknown))}.",
                )

    @classmethod
    def from_manifest(
        cls, entries: Sequence[Mapping[str, Any]], *, plugin_id: str = "<unnamed>"
    ) -> Pipeline:
        steps = []
        for index, entry in enumerate(entries):
            name = entry.get("op")
            if not isinstance(name, str) or not name:
                raise PluginValidationError(
                    plugin_id, f"pipeline step {index + 1}", "has no 'op' name."
                )
            steps.append(Step(op=name, params={k: v for k, v in entry.items() if k != "op"}))
        return cls(steps, plugin_id=plugin_id)

    # -- reading ---------------------------------------------------------

    def decode(self, raw: bytes) -> Decoded:
        current: Any = raw
        hints: list[dict[str, Any]] = []
        for index, step in enumerate(self.steps):
            hint: dict[str, Any] = {}
            operation = ops.get(step.op)
            recorded_input = _recordable(current)
            try:
                current = operation.decode(current, step.params, hint)
            except PipelineError:
                raise
            except Exception as exc:
                raise PipelineError(index, step.op, _reason(exc), detail=repr(exc)) from exc
            if recorded_input is not None:
                recorded_output = _recordable(current)
                if recorded_output is not None:
                    hint["_input"] = recorded_input
                    hint["_output"] = recorded_output
            hints.append(hint)
        return Decoded(value=current, hints=hints)

    # -- writing ---------------------------------------------------------

    def encode(
        self,
        value: Any,
        hints: Sequence[Mapping[str, Any]],
        *,
        passthrough: bool = True,
    ) -> bytes:
        if len(hints) != len(self.steps):
            raise PipelineError(
                0,
                self.steps[0].op if self.steps else "<empty>",
                "the information recorded while reading this file does not match the plugin.",
                detail=f"{len(hints)} hint sets for {len(self.steps)} steps",
            )

        current: Any = value
        for index in reversed(range(len(self.steps))):
            step = self.steps[index]
            hint = hints[index]
            if passthrough and _unchanged(current, hint):
                current = hint["_input"]
                continue
            operation = ops.get(step.op)
            try:
                current = operation.encode(current, step.params, hint)
            except PipelineError:
                raise
            except Exception as exc:
                raise PipelineError(index, step.op, _reason(exc), detail=repr(exc)) from exc

        if isinstance(current, str):
            current = current.encode("utf-8")
        if not isinstance(current, bytes | bytearray):
            raise PipelineError(
                0,
                self.steps[0].op if self.steps else "<empty>",
                f"the pipeline produced {type(current).__name__} instead of file contents.",
            )
        return bytes(current)

    # -- the gate --------------------------------------------------------

    def round_trip(self, raw: bytes) -> RoundTrip:
        """Rebuild this file without changing anything and compare.

        A plugin that fails this has not understood the format, whatever its
        author believes.
        """
        decoded = self.decode(raw)
        rebuilt = self.encode(decoded.value, decoded.hints, passthrough=False)
        if rebuilt == raw:
            return RoundTrip(True, True, "identical")

        try:
            readable = self.decode(rebuilt).value == decoded.value
        except PipelineError:
            readable = False

        if readable:
            detail = (
                "the data survives but the container differs, usually a compressor "
                "other than zlib"
            )
        else:
            detail = (
                f"the rebuilt file cannot be read back ({len(rebuilt)} bytes vs {len(raw)})"
            )
        return RoundTrip(False, readable, detail)


def _recordable(value: Any) -> bytes | str | None:
    """Values safe to remember for the passthrough shortcut.

    Only immutable ones. A parsed structure is exactly what editing mutates, so
    remembering it would compare an object with itself and wrongly conclude
    nothing changed.
    """
    if isinstance(value, bytes | bytearray):
        return bytes(value) if len(value) <= _PASSTHROUGH_LIMIT else None
    if isinstance(value, str):
        return value if len(value) <= _PASSTHROUGH_LIMIT else None
    return None


def _unchanged(current: Any, hint: Mapping[str, Any]) -> bool:
    if "_input" not in hint or "_output" not in hint:
        return False
    if not isinstance(current, bytes | bytearray | str):
        return False
    return bool(current == hint["_output"])


def _reason(exc: Exception) -> str:
    text = str(exc).strip()
    return text or f"of an unexpected {type(exc).__name__}"
