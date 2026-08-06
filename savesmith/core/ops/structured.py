"""Steps that turn bytes into an editable structure.

``json_parse`` is where a save stops being bytes and becomes something with
named fields. Getting back to identical bytes means reproducing the writer's
formatting, not just its data: indentation, whether a space follows the colon,
whether non-ASCII text was escaped, the trailing newline. All of it is measured
from the file rather than assumed, because every engine writes JSON slightly
differently and none of them document it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from savesmith.core.ops._registry import Hints, Operation, Params, register

_BOM = b"\xef\xbb\xbf"
_INDENT_RE = re.compile(r"^[\{\[][^\n]*\n(?P<indent>[ \t]+)", re.MULTILINE)


def _json_decode(payload: Any, params: Params, hints: Hints) -> Any:
    if isinstance(payload, str):
        text, hints["bom"] = payload, False
    else:
        raw = bytes(payload)
        hints["bom"] = raw.startswith(_BOM)
        if hints["bom"]:
            raw = raw[len(_BOM) :]
        encoding = str(params.get("encoding", "utf-8"))
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ValueError(f"the text is not valid {encoding} ({exc.reason})") from exc

    stripped = text.rstrip()
    hints["trailing"] = text[len(stripped) :]
    hints.update(_detect_style(stripped))

    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the JSON is malformed at line {exc.lineno} ({exc.msg})") from exc


def _json_encode(payload: Any, params: Params, hints: Mapping[str, Any]) -> bytes:
    indent = hints.get("indent")
    text = json.dumps(
        payload,
        indent=indent,
        separators=(str(hints.get("item_separator", ",")), str(hints.get("key_separator", ": "))),
        ensure_ascii=bool(hints.get("ensure_ascii", True)),
        allow_nan=False,
    )
    text += str(hints.get("trailing", ""))
    encoded = text.encode(str(params.get("encoding", "utf-8")))
    return _BOM + encoded if hints.get("bom") else encoded


def _detect_style(text: str) -> dict[str, Any]:
    """Work out how the writer formatted this file."""
    match = _INDENT_RE.search(text)
    indent: int | str | None = None
    if match:
        found = match.group("indent")
        indent = found if "\t" in found else len(found)

    if indent is None:
        # Compact form: the only question is whether spaces follow the
        # punctuation. Unity writes {"a":1}, most tools write {"a": 1}.
        item_separator = ", " if re.search(r",\s(?![\s])", text) else ","
        key_separator = ": " if '": ' in text or "': " in text else ":"
    else:
        # Indented form: json.dumps never puts a space after a newline comma,
        # but some writers leave one before the line break.
        item_separator = ", " if ", \n" in text else ","
        key_separator = ": " if '": ' in text else ":"

    return {
        "indent": indent,
        "item_separator": item_separator,
        "key_separator": key_separator,
        # If nothing outside ASCII survived, the writer escaped it (or there
        # was nothing to escape, in which case both settings agree anyway).
        "ensure_ascii": text.isascii(),
    }


register(
    Operation(
        name="json_parse",
        decode=_json_decode,
        encode=_json_encode,
        optional_params=("encoding",),
        summary="Reads JSON, preserving the exact formatting of the original file",
    )
)
