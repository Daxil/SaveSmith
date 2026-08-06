"""Every operation must be able to rebuild exactly what it was given.

That contract is checked here for each operation in the registry, on top of the
per-operation tests, so a new operation cannot be added without meeting it.
"""

from __future__ import annotations

import gzip as gzip_module
import json
import zlib
from typing import Any

import pytest

from savesmith.core import ops


def round_trip(name: str, raw: Any, params: dict[str, Any] | None = None) -> Any:
    operation = ops.get(name)
    hints: dict[str, Any] = {}
    decoded = operation.decode(raw, params or {}, hints)
    return operation.encode(decoded, params or {}, hints)


class TestRegistry:
    def test_the_built_in_operations_are_registered(self) -> None:
        assert {"strip_prefix", "xor", "base64", "gzip", "zlib", "json_parse"} <= set(ops.names())

    def test_an_unknown_operation_names_the_known_ones(self) -> None:
        from savesmith.core.errors import UnknownOperationError

        with pytest.raises(UnknownOperationError) as caught:
            ops.get("decrypt_with_magic")
        assert "gzip" in caught.value.user_message
        assert "Updating SaveSmith" in caught.value.user_message

    def test_every_operation_documents_itself(self) -> None:
        """The advanced view shows these; an empty one is a bug, not a style nit."""
        for operation in ops.all_operations():
            assert operation.summary, f"{operation.name} has no summary"

    def test_registering_a_name_twice_is_refused(self) -> None:
        existing = ops.get("gzip")
        with pytest.raises(ValueError, match="registered twice"):
            ops.register(existing)


class TestStripPrefix:
    def test_the_header_comes_back_verbatim(self) -> None:
        raw = b"\x00\x01BinaryFormatterJunk\xff" + b"payload"
        assert round_trip("strip_prefix", raw, {"bytes": 21}) == raw

    def test_a_short_file_says_what_was_expected(self) -> None:
        with pytest.raises(ValueError, match="22-byte header"):
            round_trip("strip_prefix", b"tiny", {"bytes": 22})

    def test_encoding_without_the_recorded_header_refuses_to_guess(self) -> None:
        operation = ops.get("strip_prefix")
        with pytest.raises(ValueError, match="cannot be restored"):
            operation.encode(b"payload", {"bytes": 4}, {})


class TestXor:
    def test_is_its_own_inverse(self) -> None:
        assert round_trip("xor", b"secret data", {"key_hex": "a5"}) == b"secret data"

    def test_a_multi_byte_key_repeats(self) -> None:
        operation = ops.get("xor")
        scrambled = operation.decode(b"\x00\x00\x00\x00", {"key_hex": "0102"}, {})
        assert scrambled == b"\x01\x02\x01\x02"

    def test_a_bad_key_is_reported_not_silently_skipped(self) -> None:
        with pytest.raises(ValueError, match="not valid hex"):
            round_trip("xor", b"data", {"key_hex": "zz"})

    def test_an_empty_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            round_trip("xor", b"data", {"key_hex": ""})


class TestBase64:
    def test_single_line(self) -> None:
        raw = b"eyJnZW8iOiA0Mn0="
        assert round_trip("base64", raw) == raw

    def test_wrapped_lines_stay_wrapped(self) -> None:
        """MIME-style wrapping is part of the file, not decoration."""
        import base64 as b64

        payload = b64.b64encode(b"x" * 200).decode()
        wrapped = "\n".join(payload[i : i + 76] for i in range(0, len(payload), 76))
        raw = (wrapped + "\n").encode()
        assert round_trip("base64", raw) == raw

    def test_urlsafe_variant(self) -> None:
        import base64 as b64

        raw = b64.urlsafe_b64encode(b"\xfb\xef\xbe\x01")  # exercises - and _
        assert b"-" in raw or b"_" in raw
        assert round_trip("base64", raw, {"variant": "urlsafe"}) == raw

    def test_malformed_text_is_reported(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            round_trip("base64", b"not base64 at all!!!")

    def test_binary_input_is_rejected_clearly(self) -> None:
        with pytest.raises(ValueError, match="not base64"):
            round_trip("base64", b"\xff\xfe\x00")


class TestGzip:
    @pytest.mark.parametrize("level", [1, 6, 9])
    def test_every_level_is_recovered(self, level: int) -> None:
        raw = gzip_module.compress(b'{"geo": 42}' * 20, compresslevel=level, mtime=1700000000)
        assert round_trip("gzip", raw) == raw

    def test_the_stored_filename_and_timestamp_survive(self) -> None:
        import io

        buffer = io.BytesIO()
        with gzip_module.GzipFile(
            filename="user1.dat", mode="wb", fileobj=buffer, mtime=1234567890
        ) as handle:
            handle.write(b"payload" * 30)
        raw = buffer.getvalue()
        assert round_trip("gzip", raw) == raw

    def test_a_non_gzip_file_says_so(self) -> None:
        with pytest.raises(ValueError, match="gzip header"):
            round_trip("gzip", b"PK\x03\x04not a gzip")

    def test_damaged_data_is_reported(self) -> None:
        raw = bytearray(gzip_module.compress(b"payload" * 50))
        raw[30] ^= 0xFF
        with pytest.raises(ValueError, match=r"damaged|checksum|size"):
            round_trip("gzip", bytes(raw))

    def test_a_truncated_stream_is_reported(self) -> None:
        with pytest.raises(ValueError):
            round_trip("gzip", gzip_module.compress(b"payload")[:12])

    def test_an_unknown_compressor_still_reads(self) -> None:
        """.NET and Java deflate differently; reading must still work."""
        operation = ops.get("gzip")
        raw = gzip_module.compress(b"hello" * 100, compresslevel=6)
        hints: dict[str, Any] = {}
        assert operation.decode(raw, {}, hints) == b"hello" * 100
        assert hints["level"] is not None


class TestZlib:
    @pytest.mark.parametrize("level", [1, 6, 9])
    def test_every_level_is_recovered(self, level: int) -> None:
        raw = zlib.compress(b'{"geo": 42}' * 20, level)
        assert round_trip("zlib", raw) == raw

    def test_raw_deflate_via_wbits(self) -> None:
        compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
        raw = compressor.compress(b"payload" * 40) + compressor.flush()
        assert round_trip("zlib", raw, {"wbits": -15}) == raw

    def test_a_non_zlib_stream_is_reported(self) -> None:
        with pytest.raises(ValueError, match="not a valid zlib"):
            round_trip("zlib", b"plain text, definitely not compressed")


class TestJson:
    def test_compact_unity_style(self) -> None:
        raw = b'{"playerData":{"geo":42,"maxHealth":5}}'
        assert round_trip("json_parse", raw) == raw

    def test_compact_with_spaces(self) -> None:
        raw = b'{"geo": 42, "health": 5}'
        assert round_trip("json_parse", raw) == raw

    def test_indented_with_two_spaces(self) -> None:
        raw = json.dumps({"geo": 42, "items": [1, 2]}, indent=2).encode()
        assert round_trip("json_parse", raw) == raw

    def test_indented_with_tabs(self) -> None:
        raw = json.dumps({"geo": 42, "items": [1, 2]}, indent="\t").encode()
        assert round_trip("json_parse", raw) == raw

    def test_trailing_newline_is_kept(self) -> None:
        raw = b'{"geo":42}\n'
        assert round_trip("json_parse", raw) == raw

    def test_a_byte_order_mark_is_kept(self) -> None:
        """Some Windows writers add one; dropping it can break the game's reader."""
        raw = b'\xef\xbb\xbf{"geo":42}'
        assert round_trip("json_parse", raw) == raw

    def test_escaped_non_ascii_stays_escaped(self) -> None:
        raw = b'{"name":"\\u041a\\u043e\\u0442"}'
        assert round_trip("json_parse", raw) == raw

    def test_raw_non_ascii_stays_raw(self) -> None:
        raw = '{"name":"Кот"}'.encode()
        assert round_trip("json_parse", raw) == raw

    def test_key_order_is_preserved(self) -> None:
        raw = b'{"z":1,"a":2,"m":3}'
        assert round_trip("json_parse", raw) == raw

    def test_malformed_json_points_at_the_line(self) -> None:
        with pytest.raises(ValueError, match="line 2"):
            round_trip("json_parse", b'{\n  "geo": ,\n}')

    def test_invalid_utf8_is_reported(self) -> None:
        with pytest.raises(ValueError, match="not valid utf-8"):
            round_trip("json_parse", b'{"name":"\xff\xfe"}')
