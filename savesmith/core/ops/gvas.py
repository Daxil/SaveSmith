"""Unreal Engine save games (GVAS).

The format is Unreal's tagged property stream: a header, then a flat list of
properties, each carrying its own name, type and length, terminated by a
property called ``None``. Every value in the file is preceded by its size,
which is what makes a partial reader safe — anything this module does not
understand is kept as raw bytes and written back untouched.

That is the central decision here. A save from a large game contains property
types nobody has documented, and a parser that guesses at them corrupts saves.
Instead:

* scalar properties (numbers, flags, text) are parsed and become editable;
* plain structs and arrays of scalars are parsed and become editable;
* everything else — maps, arrays of structs, engine-native structs like
  Vector or DateTime — is preserved byte for byte and simply not offered for
  editing.

The result round-trips exactly on files it has never seen, and the fields a
player actually wants (money, health, counts) are the ones that parse.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from savesmith.core.ops._registry import Hints, Operation, Params, register

_MAGIC = b"GVAS"
_TERMINATOR = "None"

# Types whose body is a single fixed value we can read and write safely.
_SCALARS: dict[str, tuple[str, int]] = {
    "IntProperty": ("<i", 4),
    "UInt32Property": ("<I", 4),
    "Int64Property": ("<q", 8),
    "UInt64Property": ("<Q", 8),
    "Int16Property": ("<h", 2),
    "UInt16Property": ("<H", 2),
    "Int8Property": ("<b", 1),
    "FloatProperty": ("<f", 4),
    "DoubleProperty": ("<d", 8),
}
_TEXTUAL = ("StrProperty", "NameProperty")


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError(f"the file ends unexpectedly at byte {self.pos}")
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def unpack(self, layout: str, size: int) -> Any:
        return struct.unpack(layout, self.take(size))[0]

    def i32(self) -> int:
        return int(self.unpack("<i", 4))

    def i64(self) -> int:
        return int(self.unpack("<q", 8))

    def u8(self) -> int:
        return self.take(1)[0]

    def text(self) -> str:
        """An Unreal FString: a length, the characters, and a null terminator.

        A negative length means the string is UTF-16, which Unreal uses only
        when the text does not fit in ASCII.
        """
        length = self.i32()
        if length == 0:
            return ""
        if length > 0:
            return self.take(length)[:-1].decode("utf-8", errors="replace")
        return self.take(-length * 2)[:-2].decode("utf-16-le", errors="replace")


def _write_text(value: str) -> bytes:
    if value == "":
        return struct.pack("<i", 0)
    if value.isascii():
        encoded = value.encode("ascii") + b"\x00"
        return struct.pack("<i", len(encoded)) + encoded
    # Unreal's own rule: UTF-16 only when ASCII will not do.
    encoded = value.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<i", -(len(encoded) // 2)) + encoded


@dataclass
class _Property:
    """One entry in the tagged property stream.

    ``meta`` is the type-specific preamble (an array's element type, a struct's
    name and GUID) kept verbatim, so nothing has to be reconstructed from
    assumptions.
    """

    name: str
    type: str
    meta: bytes
    kind: str  # scalar | text | bool | struct | array | opaque
    body: bytes = b""
    children: list[_Property] = field(default_factory=list)
    element_type: str = ""


def _parse_properties(reader: _Reader) -> list[_Property]:
    properties: list[_Property] = []
    while True:
        name = reader.text()
        if name == _TERMINATOR:
            return properties
        if not name:
            raise ValueError(f"a property with no name at byte {reader.pos}")
        properties.append(_parse_property(reader, name))


# How many type names sit between the size field and the terminator byte.
# Getting this wrong does not fail loudly — it silently shifts every following
# property, which is how a whole save ends up rewritten as noise.
_META_NAMES: dict[str, int] = {
    "ArrayProperty": 1,  # element type
    "SetProperty": 1,  # element type
    "MapProperty": 2,  # key type, then value type
    "EnumProperty": 1,  # enum type
    "ByteProperty": 1,  # enum name
    "StructProperty": 1,  # struct type, followed by a GUID
}


def _parse_property(reader: _Reader, name: str) -> _Property:
    type_name = reader.text()
    size = reader.i64()
    meta_start = reader.pos

    names = [reader.text() for _ in range(_META_NAMES.get(type_name, 0))]
    if type_name == "StructProperty":
        reader.take(16)  # struct GUID

    if type_name == "BoolProperty":
        # A bool has no body: the value sits in the tag where other types keep
        # their terminator, and the size field is zero.
        value = reader.u8()
        reader.u8()
        meta = reader.data[meta_start : reader.pos]
        return _Property(name=name, type=type_name, meta=meta, kind="bool", body=bytes([value]))

    reader.u8()  # terminator
    meta = reader.data[meta_start : reader.pos]

    if type_name == "ArrayProperty":
        return _parse_array(reader, name, meta, names[0], size)
    if type_name == "StructProperty":
        return _parse_struct(reader, name, meta, names[0], size)
    if type_name in _SCALARS or type_name in _TEXTUAL:
        kind = "scalar" if type_name in _SCALARS else "text"
        return _Property(name=name, type=type_name, meta=meta, kind=kind, body=reader.take(size))

    # Everything else keeps its bytes. Guessing at an undocumented type is how
    # a save editor corrupts a save.
    return _Property(name=name, type=type_name, meta=meta, kind="opaque", body=reader.take(size))


def _parse_array(
    reader: _Reader, name: str, meta: bytes, element_type: str, size: int
) -> _Property:
    body = reader.take(size)
    if element_type in _SCALARS:
        _layout, width = _SCALARS[element_type]
        count = struct.unpack_from("<i", body, 0)[0]
        # The length must add up exactly; if it does not, this is some variant
        # we have not seen and it stays untouched.
        if 4 + count * width == len(body):
            return _Property(
                name=name,
                type="ArrayProperty",
                meta=meta,
                kind="array",
                body=body,
                element_type=element_type,
            )
    # Arrays of structs carry a second header; not parsed, only preserved.
    return _Property(
        name=name, type="ArrayProperty", meta=meta, kind="opaque", body=body,
        element_type=element_type,
    )


# Struct types Unreal serialises natively rather than as a property list.
_NATIVE_STRUCTS = frozenset(
    {
        "Vector", "Vector2D", "Vector4", "Rotator", "Quat", "Transform",
        "LinearColor", "Color", "Guid", "DateTime", "Timespan", "IntPoint",
        "IntVector", "Box", "Box2D", "FloatRange", "FloatRangeBound",
    }
)


def _parse_struct(
    reader: _Reader, name: str, meta: bytes, struct_type: str, size: int
) -> _Property:
    body = reader.take(size)
    if struct_type in _NATIVE_STRUCTS:
        return _Property(name=name, type="StructProperty", meta=meta, kind="opaque", body=body)
    try:
        inner = _Reader(body)
        children = _parse_properties(inner)
        if inner.pos != len(body):
            raise ValueError("trailing bytes")
    except ValueError:
        # A struct we cannot walk is kept whole rather than half-understood.
        return _Property(name=name, type="StructProperty", meta=meta, kind="opaque", body=body)
    return _Property(
        name=name, type="StructProperty", meta=meta, kind="struct", children=children
    )


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def _value_of(prop: _Property) -> Any:
    if prop.kind == "scalar":
        layout, _width = _SCALARS[prop.type]
        return struct.unpack(layout, prop.body)[0]
    if prop.kind == "bool":
        return prop.body[0] != 0
    if prop.kind == "text":
        return _Reader(prop.body).text()
    if prop.kind == "array":
        layout, width = _SCALARS[prop.element_type]
        count = struct.unpack_from("<i", prop.body, 0)[0]
        return [
            struct.unpack_from(layout, prop.body, 4 + index * width)[0] for index in range(count)
        ]
    if prop.kind == "struct":
        return _values_of(prop.children)
    raise AssertionError(f"no value for {prop.kind}")


def _values_of(properties: list[_Property]) -> dict[str, Any]:
    """Editable values only. Opaque properties are preserved, not shown."""
    return {prop.name: _value_of(prop) for prop in properties if prop.kind != "opaque"}


def _body_for(prop: _Property, value: Any) -> bytes:
    if prop.kind == "scalar":
        layout, _width = _SCALARS[prop.type]
        try:
            return struct.pack(layout, value)
        except struct.error as exc:
            raise ValueError(f"{prop.name} cannot hold {value!r} ({exc})") from exc
    if prop.kind == "bool":
        return bytes([1 if value else 0])
    if prop.kind == "text":
        return _write_text(str(value))
    if prop.kind == "array":
        layout, _element_width = _SCALARS[prop.element_type]
        items = list(value)
        return struct.pack("<i", len(items)) + b"".join(
            struct.pack(layout, item) for item in items
        )
    raise AssertionError(f"no body for {prop.kind}")


def _write_properties(properties: list[_Property], values: Mapping[str, Any]) -> bytes:
    out = bytearray()
    for prop in properties:
        out += _write_text(prop.name)
        out += _write_text(prop.type)

        if prop.kind == "struct":
            child_values = values.get(prop.name, {})
            if not isinstance(child_values, Mapping):
                raise ValueError(f"{prop.name} must stay a group of values")
            body = _write_properties(prop.children, child_values)
        elif prop.kind == "opaque":
            body = prop.body
        else:
            body = _body_for(prop, values.get(prop.name, _value_of(prop)))

        if prop.kind == "bool":
            # A bool has no body; its size field is zero and the value sits in
            # the tag, where the terminator byte would otherwise be.
            out += struct.pack("<q", 0) + body + prop.meta[len(prop.meta) - 1 :]
            continue

        out += struct.pack("<q", len(body))
        out += prop.meta
        out += body
    out += _write_text(_TERMINATOR)
    return bytes(out)


# ---------------------------------------------------------------------------
# The operation
# ---------------------------------------------------------------------------


def _decode(payload: Any, _params: Params, hints: Hints) -> Any:
    if not isinstance(payload, bytes | bytearray):
        raise ValueError("gvas expects raw bytes but the previous step produced text")
    raw = bytes(payload)
    if not raw.startswith(_MAGIC):
        raise ValueError("the file does not start with the Unreal save marker 'GVAS'")

    reader = _Reader(raw)
    reader.take(4)
    save_version = reader.i32()
    reader.i32()  # package version (UE4)
    if save_version >= 3:
        reader.i32()  # package version (UE5)
    reader.take(6)  # engine major, minor, patch
    reader.take(4)  # changelist
    reader.text()  # engine branch
    reader.i32()  # custom version format
    custom_count = reader.i32()
    reader.take(custom_count * 20)  # GUID plus version, each
    class_name = reader.text()

    hints["header"] = raw[: reader.pos]
    hints["class_name"] = class_name

    properties = _parse_properties(reader)
    hints["shape"] = properties
    # Unreal writes a trailing word after the final "None"; kept as found.
    hints["trailer"] = raw[reader.pos :]

    return {"class": class_name, "properties": _values_of(properties)}


def _encode(payload: Any, _params: Params, hints: Mapping[str, Any]) -> bytes:
    shape = hints.get("shape")
    header = hints.get("header")
    if shape is None or header is None:
        raise ValueError("the structure of the original file was not recorded")
    if not isinstance(payload, Mapping):
        raise ValueError("an Unreal save must stay a group of values")
    values = payload.get("properties", {})
    if not isinstance(values, Mapping):
        raise ValueError("'properties' must stay a group of values")
    return bytes(header) + _write_properties(list(shape), values) + bytes(hints.get("trailer", b""))


register(
    Operation(
        name="gvas",
        decode=_decode,
        encode=_encode,
        summary="Reads an Unreal Engine save, preserving anything it does not understand",
    )
)
