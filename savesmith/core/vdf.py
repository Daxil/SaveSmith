"""Valve KeyValues (VDF) — the text format Steam keeps its own data in.

``libraryfolders.vdf``, ``appmanifest_*.acf``, ``localconfig.vdf`` and
``remotecache.vdf`` are all this format::

    "libraryfolders"
    {
        "0"
        {
            "path"      "C:\\\\Program Files (x86)\\\\Steam"
            "apps"
            {
                "367520"    "1234567"
            }
        }
    }

Written by hand rather than taken from PyPI because the published parsers
reject files this one has to read: Steam has shipped three different shapes of
``libraryfolders.vdf``, some files carry platform conditionals (``[$WIN32]``),
and a half-written file left behind by a crash must degrade to a clear error
rather than an exception from a dependency.

Binary VDF (``appinfo.vdf``) is a different format and is not needed —
everything SaveSmith reads is text.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

from savesmith.core.errors import VdfParseError

type VdfValue = str | VdfDict
type VdfDict = dict[str, VdfValue]

# Deeply nested files are always corruption or malice; real ones are ~5 deep.
_MAX_DEPTH = 64

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
}


class _Token:
    __slots__ = ("kind", "line", "value")

    def __init__(self, kind: str, value: str, line: int) -> None:
        self.kind = kind  # "string" | "{" | "}"
        self.value = value
        self.line = line

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Token({self.kind!r}, {self.value!r}, line={self.line})"


def _tokenize(text: str, source: str) -> Iterator[_Token]:
    index = 0
    line = 1
    length = len(text)

    while index < length:
        char = text[index]

        if char == "\n":
            line += 1
            index += 1
            continue
        if char.isspace():
            index += 1
            continue

        # Comments run to end of line. Steam writes "//"; a stray "/" has been
        # seen in hand-edited files, so treat a single slash the same way.
        if char == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue

        if char in "{}":
            yield _Token(char, char, line)
            index += 1
            continue

        # Platform conditionals such as [$WIN32] gate the preceding entry. We
        # read every platform's data, so they are simply skipped.
        if char == "[":
            end = text.find("]", index)
            if end == -1:
                raise VdfParseError(source, "an unterminated [condition] block", line=line)
            index = end + 1
            continue

        if char == '"':
            index += 1
            chunks: list[str] = []
            start_line = line
            while True:
                if index >= length:
                    raise VdfParseError(
                        source, "a quoted value that is never closed", line=start_line
                    )
                current = text[index]
                if current == "\\":
                    if index + 1 >= length:
                        raise VdfParseError(
                            source, "a backslash at the end of the file", line=line
                        )
                    following = text[index + 1]
                    # Unknown escapes keep both characters: Windows paths in
                    # older files are written unescaped, and "C:\Program" must
                    # not silently lose its P.
                    chunks.append(_ESCAPES.get(following, "\\" + following))
                    index += 2
                    continue
                if current == '"':
                    index += 1
                    break
                if current == "\n":
                    line += 1
                chunks.append(current)
                index += 1
            yield _Token("string", "".join(chunks), start_line)
            continue

        # Bare token: `key value` without quotes. Steam writes these rarely,
        # third-party tools that rewrite these files write them often.
        start = index
        while index < length and not text[index].isspace() and text[index] not in '{}"/':
            index += 1
        yield _Token("string", text[start:index], line)


def loads(text: str, *, source: str = "<string>") -> VdfDict:
    """Parse VDF text into nested dictionaries.

    Duplicate keys: the last one wins, matching how Steam itself reads these
    files when a client rewrites an entry without removing the old one.
    """
    if text.startswith("\ufeff"):
        text = text[1:]

    tokens = list(_tokenize(text, source))
    position = 0

    def parse_block(depth: int, opened_at: int | None) -> VdfDict:
        nonlocal position
        if depth > _MAX_DEPTH:
            raise VdfParseError(source, f"more than {_MAX_DEPTH} levels of nesting")
        block: VdfDict = {}
        while position < len(tokens):
            token = tokens[position]
            if token.kind == "}":
                if opened_at is None:
                    raise VdfParseError(
                        source, "a closing brace with nothing open", line=token.line
                    )
                position += 1
                return block
            if token.kind == "{":
                raise VdfParseError(
                    source, "a block that is not attached to a key", line=token.line
                )

            key = token.value
            position += 1
            if position >= len(tokens):
                raise VdfParseError(source, f"the key {key!r} has no value", line=token.line)

            following = tokens[position]
            if following.kind == "{":
                position += 1
                block[key] = parse_block(depth + 1, following.line)
            elif following.kind == "string":
                block[key] = following.value
                position += 1
            else:
                raise VdfParseError(source, f"the key {key!r} has no value", line=following.line)

        if opened_at is not None:
            raise VdfParseError(source, "a block that is never closed", line=opened_at)
        return block

    result = parse_block(0, None)
    return result


def load_file(path: Path) -> VdfDict:
    """Read and parse a VDF file.

    Steam's files are UTF-8, but a few very old ones are not. Undecodable
    bytes are replaced rather than raising: a mangled game name is a far better
    outcome than an unreadable library.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise VdfParseError(str(path), f"the file could not be read ({exc.strerror})") from exc
    return loads(raw.decode("utf-8", errors="replace"), source=str(path))


# ---------------------------------------------------------------------------
# Lookup helpers
#
# Steam is inconsistent about capitalisation across client versions: the same
# file has shipped as "LibraryFolders" and "libraryfolders", "AppState" and
# "appstate". Every read goes through these.
# ---------------------------------------------------------------------------


def get(data: Mapping[str, VdfValue], *keys: str) -> VdfValue | None:
    """Walk nested keys, ignoring case. ``None`` if any step is missing."""
    current: VdfValue = dict(data)
    for key in keys:
        if not isinstance(current, dict):
            return None
        folded = key.lower()
        for candidate, value in current.items():
            if candidate.lower() == folded:
                current = value
                break
        else:
            return None
    return current


def get_str(data: Mapping[str, VdfValue], *keys: str) -> str | None:
    value = get(data, *keys)
    return value if isinstance(value, str) else None


def get_dict(data: Mapping[str, VdfValue], *keys: str) -> VdfDict:
    """A nested block, or an empty dict — callers iterate, so absence is empty."""
    value = get(data, *keys)
    return value if isinstance(value, dict) else {}


def get_int(data: Mapping[str, VdfValue], *keys: str) -> int | None:
    """VDF has no types; everything is a string. Non-numbers give ``None``."""
    value = get_str(data, *keys)
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None
