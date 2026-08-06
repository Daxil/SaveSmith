"""LZString — the compression RPG Maker MV and MZ wrap their saves in.

A ``.rpgsave`` file is ``LZString.compressToBase64(JSON.stringify(save))``.
The algorithm is LZW with a variable-width code and its own base64 alphabet,
and it is not compatible with anything else, which is why it lives here rather
than coming from a library.

The compressor is a faithful port rather than a lookalike, because the
round-trip gate compares bytes: a compressor that merely produces *valid*
LZString would rewrite every save it touches. Where the original JavaScript
does something odd — the dictionary starting at size 3, the trailing marker of
two — this does the same odd thing on purpose.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from savesmith.core.ops._registry import Hints, Operation, Params, register

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
_BITS_PER_CHAR = 6


def _to_char(index: int) -> str:
    return _ALPHABET[index]


def _from_char(character: str) -> int:
    return _ALPHABET.index(character)


def _to_units(text: str) -> str:
    """Split a string into UTF-16 code units, one per character.

    LZString is JavaScript, and JavaScript strings are UTF-16. An emoji is one
    character in Python and two in JavaScript, and compressing it as one
    produces output the game cannot read. Verified against the reference
    implementation: '🍎🍇🍌' must compress to 'jwbjl96cX3kGX2g='.
    """
    raw = text.encode("utf-16-le", errors="surrogatepass")
    return "".join(
        chr(int.from_bytes(raw[index : index + 2], "little"))
        for index in range(0, len(raw), 2)
    )


def _from_units(units: str) -> str:
    raw = b"".join(ord(unit).to_bytes(2, "little") for unit in units)
    return raw.decode("utf-16-le", errors="surrogatepass")


def compress_to_base64(text: str) -> str:
    compressed = _compress(_to_units(text), _BITS_PER_CHAR, _to_char)
    # The original pads with '=' to a multiple of four.
    remainder = len(compressed) % 4
    return compressed + "=" * (0 if remainder == 0 else 4 - remainder)


def decompress_from_base64(text: str) -> str:
    if text == "":
        return ""
    stripped = text.rstrip("=")
    units = _decompress(len(stripped), 32, lambda index: _from_char(stripped[index]))
    return _from_units(units)


def _compress(uncompressed: str, bits_per_char: int, to_char: Callable[[int], str]) -> str:
    if not uncompressed:
        return ""

    dictionary: dict[str, int] = {}
    pending: dict[str, bool] = {}
    word = ""
    enlarge_in = 2
    dict_size = 3
    num_bits = 2
    output: list[str] = []
    value = 0
    position = 0

    def emit_bit(bit: int) -> None:
        nonlocal value, position
        value = (value << 1) | bit
        if position == bits_per_char - 1:
            position = 0
            output.append(to_char(value))
            value = 0
        else:
            position += 1

    def emit(number: int, width: int) -> None:
        for _ in range(width):
            emit_bit(number & 1)
            number >>= 1

    def emit_reversed(number: int, width: int) -> None:
        """The original shifts the value left while reading bits low-first."""
        for _ in range(width):
            emit_bit(number & 1)
            number >>= 1

    def widen() -> None:
        nonlocal enlarge_in, num_bits
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 2**num_bits
            num_bits += 1

    def flush_word() -> None:
        if word in pending:
            if ord(word[0]) < 256:
                emit(0, num_bits)
                emit_reversed(ord(word[0]), 8)
            else:
                emit(1, num_bits)
                emit_reversed(ord(word[0]), 16)
            # Counted twice for a character being introduced: once here and
            # once below. The original does exactly this, and the decoder's
            # widths only line up if we do too.
            widen()
            del pending[word]
        else:
            emit_reversed(dictionary[word], num_bits)
        widen()

    for character in uncompressed:
        if character not in dictionary:
            dictionary[character] = dict_size
            dict_size += 1
            pending[character] = True

        candidate = word + character
        if candidate in dictionary:
            word = candidate
            continue
        if word:
            flush_word()
        dictionary[candidate] = dict_size
        dict_size += 1
        word = character

    if word:
        flush_word()

    # The end marker, then whatever bits are needed to finish the last character.
    emit_reversed(2, num_bits)
    while True:
        value = value << 1
        if position == bits_per_char - 1:
            output.append(to_char(value))
            break
        position += 1
    return "".join(output)


def _decompress(length: int, reset_value: int, read: Callable[[int], int]) -> str:
    dictionary: dict[int, str] = {index: str(index) for index in range(3)}
    enlarge_in = 4
    dict_size = 4
    num_bits = 3
    result: list[str] = []
    position = reset_value
    index = 0
    current = read(0) if length else 0

    def next_bits(width: int) -> int:
        nonlocal position, index, current
        number = 0
        power = 1
        for _ in range(width):
            resb = current & position
            position >>= 1
            if position == 0:
                position = reset_value
                index += 1
                current = read(index) if index < length else 0
            number |= (1 if resb > 0 else 0) * power
            power <<= 1
        return number

    # Reading starts at the top bit of the first character.
    position = 1 << (_BITS_PER_CHAR - 1)

    first = next_bits(2)
    if first == 0:
        entry = chr(next_bits(8))
    elif first == 1:
        entry = chr(next_bits(16))
    else:
        return ""

    dictionary[3] = entry
    word = entry
    result.append(entry)

    while True:
        if index > length:
            return ""
        code = next_bits(num_bits)
        if code == 0:
            dictionary[dict_size] = chr(next_bits(8))
            dict_size += 1
            code = dict_size - 1
            enlarge_in -= 1
        elif code == 1:
            dictionary[dict_size] = chr(next_bits(16))
            dict_size += 1
            code = dict_size - 1
            enlarge_in -= 1
        elif code == 2:
            return "".join(result)

        if enlarge_in == 0:
            enlarge_in = 2**num_bits
            num_bits += 1

        if code in dictionary:
            entry = dictionary[code]
        elif code == dict_size:
            entry = word + word[0]
        else:
            return ""

        result.append(entry)
        dictionary[dict_size] = word + entry[0]
        dict_size += 1
        enlarge_in -= 1
        word = entry

        if enlarge_in == 0:
            enlarge_in = 2**num_bits
            num_bits += 1


def _decode(payload: Any, _params: Params, hints: Hints) -> bytes:
    if isinstance(payload, bytes | bytearray):
        try:
            text = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"the data is not text ({exc.reason})") from exc
    else:
        text = str(payload)

    stripped = text.strip()
    if not stripped:
        raise ValueError("the file is empty")
    # Whatever surrounded the payload is part of the file and comes back with it.
    leading = text[: len(text) - len(text.lstrip())]
    hints["surrounding"] = (leading, text[len(leading) + len(stripped) :])

    result = decompress_from_base64(stripped)
    if not result:
        raise ValueError("the data is not LZString-compressed, or it is damaged")
    return result.encode("utf-8")


def _encode(payload: Any, _params: Params, hints: Mapping[str, Any]) -> bytes:
    if isinstance(payload, bytes | bytearray):
        text = bytes(payload).decode("utf-8")
    else:
        text = str(payload)
    before, after = hints.get("surrounding", ("", ""))
    return (str(before) + compress_to_base64(text) + str(after)).encode("utf-8")


register(
    Operation(
        name="lzstring",
        decode=_decode,
        encode=_encode,
        summary="Unpacks the LZString compression RPG Maker uses",
    )
)
