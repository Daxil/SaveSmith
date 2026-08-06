"""The Valve KeyValues parser.

Golden files live in ``tests/data/steam`` and are copies of what Steam
actually writes, including the three generations of ``libraryfolders.vdf``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from savesmith.core import vdf
from savesmith.core.errors import VdfParseError

DATA = Path(__file__).parent / "data" / "steam"


class TestParsing:
    def test_nested_blocks(self) -> None:
        parsed = vdf.loads('"root" { "a" "1" "sub" { "b" "2" } }')
        assert parsed == {"root": {"a": "1", "sub": {"b": "2"}}}

    def test_escaped_backslashes_in_paths(self) -> None:
        parsed = vdf.loads(r'"root" { "path" "D:\\SteamLibrary" }')
        assert vdf.get_str(parsed, "root", "path") == r"D:\SteamLibrary"

    def test_unknown_escape_keeps_both_characters(self) -> None:
        r"""Hand-edited files contain unescaped paths; "C:\Program" must keep its P."""
        parsed = vdf.loads(r'"root" { "path" "C:\Program Files" }')
        assert vdf.get_str(parsed, "root", "path") == r"C:\Program Files"

    def test_known_escapes(self) -> None:
        parsed = vdf.loads(r'"root" { "text" "line\nnext\ttab\"quoted\"" }')
        assert vdf.get_str(parsed, "root", "text") == 'line\nnext\ttab"quoted"'

    def test_comments_are_ignored(self) -> None:
        parsed = vdf.loads('// leading\n"root"\n{\n"a" "1" // trailing\n}\n')
        assert parsed == {"root": {"a": "1"}}

    def test_platform_conditionals_are_skipped(self) -> None:
        parsed = vdf.load_file(DATA / "conditionals.vdf")
        assert vdf.get_str(parsed, "config", "shared") == "yes"
        # Both conditional lines are read; the last one wins.
        assert vdf.get_str(parsed, "config", "platform") == "macos"

    def test_bare_tokens(self) -> None:
        parsed = vdf.loads("root { key value }")
        assert parsed == {"root": {"key": "value"}}

    def test_crlf_and_bom(self) -> None:
        parsed = vdf.loads('\ufeff"root"\r\n{\r\n\t"a"\t\t"1"\r\n}\r\n')
        assert parsed == {"root": {"a": "1"}}

    def test_duplicate_keys_last_one_wins(self) -> None:
        parsed = vdf.loads('"root" { "a" "1" "a" "2" }')
        assert vdf.get_str(parsed, "root", "a") == "2"

    def test_empty_file_is_an_empty_dict(self) -> None:
        assert vdf.loads("") == {}

    def test_empty_block(self) -> None:
        assert vdf.loads('"root" { }') == {"root": {}}


class TestBrokenFiles:
    """A crash mid-write must give a sentence, not a stack trace."""

    def test_truncated_file(self) -> None:
        with pytest.raises(VdfParseError) as caught:
            vdf.load_file(DATA / "truncated.vdf")
        assert "never closed" in caught.value.user_message
        assert caught.value.line is not None

    def test_unterminated_string(self) -> None:
        with pytest.raises(VdfParseError) as caught:
            vdf.loads('"root" { "a" "unclosed ')
        assert "never closed" in caught.value.user_message

    def test_key_without_value(self) -> None:
        with pytest.raises(VdfParseError) as caught:
            vdf.loads('"root" { "orphan" }')
        assert "no value" in caught.value.user_message

    def test_stray_closing_brace(self) -> None:
        with pytest.raises(VdfParseError):
            vdf.loads('"root" { "a" "1" } }')

    def test_unattached_block(self) -> None:
        with pytest.raises(VdfParseError):
            vdf.loads("{ }")

    def test_unterminated_conditional(self) -> None:
        with pytest.raises(VdfParseError):
            vdf.loads('"root" { "a" "1" [$WIN32 }')

    def test_deep_nesting_is_refused(self) -> None:
        text = '"a" {' * 200 + "}" * 200
        with pytest.raises(VdfParseError) as caught:
            vdf.loads(text)
        assert "nesting" in caught.value.user_message

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(VdfParseError) as caught:
            vdf.load_file(tmp_path / "nope.vdf")
        assert caught.value.user_message

    def test_undecodable_bytes_do_not_stop_the_scan(self, tmp_path: Path) -> None:
        """One mangled game name beats an unreadable library."""
        path = tmp_path / "latin.vdf"
        path.write_bytes(b'"root" { "name" "caf\xe9" }')
        assert vdf.get_str(vdf.load_file(path), "root", "name") is not None


class TestGoldenLibraryFolders:
    def test_generation_1_flat_paths(self) -> None:
        parsed = vdf.load_file(DATA / "libraryfolders_gen1.vdf")
        folders = vdf.get_dict(parsed, "LibraryFolders")
        assert folders["1"] == r"D:\SteamLibrary"

    def test_generation_2_objects_without_apps(self) -> None:
        parsed = vdf.load_file(DATA / "libraryfolders_gen2.vdf")
        assert vdf.get_str(parsed, "libraryfolders", "1", "path") == r"D:\SteamLibrary"

    def test_generation_3_objects_with_apps(self) -> None:
        parsed = vdf.load_file(DATA / "libraryfolders_gen3.vdf")
        apps = vdf.get_dict(parsed, "libraryfolders", "0", "apps")
        assert "367520" in apps

    def test_appmanifest(self) -> None:
        parsed = vdf.load_file(DATA / "appmanifest_367520.acf")
        assert vdf.get_int(parsed, "AppState", "appid") == 367520
        assert vdf.get_str(parsed, "AppState", "installdir") == "Hollow Knight"


class TestLookupHelpers:
    """Steam has shipped the same key as LibraryFolders and libraryfolders."""

    def test_lookup_ignores_case(self) -> None:
        parsed = vdf.loads('"LibraryFolders" { "AppState" { "AppID" "620" } }')
        assert vdf.get_int(parsed, "libraryfolders", "appstate", "appid") == 620

    def test_missing_path_is_none(self) -> None:
        parsed = vdf.loads('"root" { "a" "1" }')
        assert vdf.get(parsed, "root", "nope") is None
        assert vdf.get_str(parsed, "root", "nope") is None
        assert vdf.get_int(parsed, "root", "nope") is None

    def test_get_dict_of_a_missing_block_is_empty(self) -> None:
        assert vdf.get_dict(vdf.loads('"root" { }'), "root", "apps") == {}

    def test_get_str_refuses_a_block(self) -> None:
        parsed = vdf.loads('"root" { "sub" { "a" "1" } }')
        assert vdf.get_str(parsed, "root", "sub") is None

    def test_get_int_refuses_non_numbers(self) -> None:
        parsed = vdf.loads('"root" { "size" "many" "count" " 42 " }')
        assert vdf.get_int(parsed, "root", "size") is None
        assert vdf.get_int(parsed, "root", "count") == 42

    def test_walking_through_a_string_is_none(self) -> None:
        parsed = vdf.loads('"root" { "a" "1" }')
        assert vdf.get(parsed, "root", "a", "deeper") is None
