"""Addressing one value inside a parsed save."""

from __future__ import annotations

from typing import Any

import pytest

from savesmith.core.errors import FieldPathError
from savesmith.core.fields import exists, normalise, parse_path, read, render, write


@pytest.fixture
def save() -> dict[str, Any]:
    return {
        "playerData": {"geo": 42, "maxHealth": 5, "unlocked": True},
        "items": [{"id": "nail", "count": 1}, {"id": "charm", "count": 3}],
        "playerData.geo": "a flat key that contains a dot",
    }


class TestParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("geo", ("geo",)),
            ("playerData.geo", ("playerData", "geo")),
            ("items[0]", ("items", 0)),
            ("items[1].count", ("items", 1, "count")),
            ("a.b[2].c[10]", ("a", "b", 2, "c", 10)),
        ],
    )
    def test_dotted_paths(self, text: str, expected: tuple[Any, ...]) -> None:
        assert parse_path(text) == expected

    def test_an_explicit_list_avoids_ambiguity(self) -> None:
        """Easy Save 3 keys contain dots; the list form says which is which."""
        assert normalise(["playerData.geo"]) == ("playerData.geo",)
        assert normalise(["items", 0, "count"]) == ("items", 0, "count")

    def test_an_empty_path_is_refused(self) -> None:
        with pytest.raises(FieldPathError):
            parse_path("")
        with pytest.raises(FieldPathError):
            normalise([])

    def test_a_boolean_step_is_refused(self) -> None:
        """True is an int in Python; silently reading item 1 would be absurd."""
        with pytest.raises(FieldPathError):
            normalise([True])

    @pytest.mark.parametrize(
        "steps", [("playerData", "geo"), ("items", 0, "count"), ("a",)]
    )
    def test_rendering_is_the_inverse_of_parsing(self, steps: tuple[Any, ...]) -> None:
        assert parse_path(render(steps)) == steps


class TestReading:
    def test_nested_values(self, save: dict[str, Any]) -> None:
        assert read(save, "playerData.geo") == 42
        assert read(save, "items[1].count") == 3

    def test_the_flat_key_form(self, save: dict[str, Any]) -> None:
        assert read(save, ["playerData.geo"]) == "a flat key that contains a dot"

    def test_negative_indexes_work(self, save: dict[str, Any]) -> None:
        assert read(save, "items[-1].id") == "charm"

    def test_a_missing_key_says_where_it_looked(self, save: dict[str, Any]) -> None:
        with pytest.raises(FieldPathError) as caught:
            read(save, "playerData.essence")
        assert "playerData" in caught.value.user_message
        assert "essence" in caught.value.user_message

    def test_indexing_past_the_end(self, save: dict[str, Any]) -> None:
        with pytest.raises(FieldPathError, match="only 2 entries"):
            read(save, "items[7]")

    def test_indexing_something_that_is_not_a_list(self, save: dict[str, Any]) -> None:
        with pytest.raises(FieldPathError, match="not a list"):
            read(save, "playerData[0]")

    def test_naming_a_key_inside_a_number(self, save: dict[str, Any]) -> None:
        with pytest.raises(FieldPathError, match="not a group"):
            read(save, "playerData.geo.deeper")

    def test_exists_answers_instead_of_raising(self, save: dict[str, Any]) -> None:
        """Fields missing from one save are ordinary, not exceptional."""
        assert exists(save, "playerData.geo")
        assert not exists(save, "playerData.essence")


class TestWriting:
    def test_writing_changes_only_the_target(self, save: dict[str, Any]) -> None:
        write(save, "playerData.geo", 99999)
        assert save["playerData"] == {"geo": 99999, "maxHealth": 5, "unlocked": True}
        assert save["items"][0] == {"id": "nail", "count": 1}

    def test_writing_into_a_list(self, save: dict[str, Any]) -> None:
        write(save, "items[0].count", 7)
        assert save["items"][0]["count"] == 7

    def test_a_missing_key_is_never_created(self, save: dict[str, Any]) -> None:
        """Inventing a key produces a file the game has never seen."""
        with pytest.raises(FieldPathError, match="does not contain"):
            write(save, "playerData.essence", 10)
        assert "essence" not in save["playerData"]

    def test_a_missing_parent_is_never_created(self, save: dict[str, Any]) -> None:
        with pytest.raises(FieldPathError):
            write(save, "questData.progress", 1)
        assert "questData" not in save

    def test_writing_past_the_end_of_a_list(self, save: dict[str, Any]) -> None:
        with pytest.raises(FieldPathError, match="only 2 entries"):
            write(save, "items[9]", {})
        assert len(save["items"]) == 2

    def test_types_are_not_policed_here(self, save: dict[str, Any]) -> None:
        """Value rules live in the field spec; this layer only places values."""
        write(save, "playerData.geo", "anything")
        assert save["playerData"]["geo"] == "anything"
