"""Containers: reading a bag of things, and putting something into it.

The rule under test throughout is that a new record is a *clone* of one that is
already there. Games keep fields per item that nobody outside the game knows
about — a uuid, a durability, a flag saying the thing was bought rather than
found — and a record built from nothing is missing all of them. What the game
then does with such a record is its business, and none of the outcomes are good.
"""

from __future__ import annotations

from typing import Any

import pytest

from savesmith.core import inventory
from savesmith.core.inventory import ContainerError
from savesmith.core.plugin import ContainerShape, ContainerSpec, Localized


def spec(**overrides: Any) -> ContainerSpec:
    settings: dict[str, Any] = {
        "id": "bag",
        "label": Localized({"en": "inventory"}),
        "path": ("party", "items"),
        "shape": ContainerShape.LIST,
        "key": "id",
        "count": "count",
    }
    settings.update(overrides)
    return ContainerSpec(**settings)


def save(items: Any) -> dict[str, Any]:
    return {"party": {"items": items}}


class TestReading:
    def test_a_list_comes_out_as_stacks(self) -> None:
        root = save([{"id": "potion", "count": 3}, {"id": "sword", "count": 1}])

        assert inventory.stacks(root, spec()) == [
            inventory.Stack(item="potion", count=3, position=0),
            inventory.Stack(item="sword", count=1, position=1),
        ]

    def test_a_map_comes_out_as_stacks_too(self) -> None:
        """RPG Maker's own shape: the id is the key."""
        root = save({"1": 5, "7": 2})

        assert inventory.stacks(root, spec(shape=ContainerShape.MAP)) == [
            inventory.Stack(item="1", count=5, position="1"),
            inventory.Stack(item="7", count=2, position="7"),
        ]

    def test_empty_places_in_a_slots_container_are_not_things(self) -> None:
        root = save([{"id": "potion", "count": 1}, {"id": None, "count": 0}])

        found = inventory.stacks(root, spec(shape=ContainerShape.SLOTS))

        assert [stack.item for stack in found] == ["potion"]

    def test_the_same_thing_twice_is_two_stacks(self) -> None:
        """Three copies of one weapon, each with its own reinforcement."""
        root = save([{"id": "sword", "count": 1}, {"id": "sword", "count": 1}])

        assert [stack.position for stack in inventory.find(root, spec(), "sword")] == [0, 1]

    def test_a_container_the_save_does_not_have_says_so(self) -> None:
        with pytest.raises(ContainerError, match="no inventory in it"):
            inventory.stacks({"party": {}}, spec())

    def test_a_container_of_the_wrong_shape_is_refused(self) -> None:
        with pytest.raises(ContainerError, match="not shaped the way"):
            inventory.stacks(save({"1": 5}), spec(shape=ContainerShape.LIST))


class TestChangingWhatIsThere:
    def test_a_count_changes_by_place_not_by_name(self) -> None:
        root = save([{"id": "sword", "count": 1}, {"id": "sword", "count": 1}])

        inventory.set_count(root, spec(), 1, 5)

        assert [item["count"] for item in root["party"]["items"]] == [1, 5]

    def test_a_count_above_what_the_game_expects_is_refused(self) -> None:
        root = save([{"id": "potion", "count": 1}])

        with pytest.raises(ContainerError, match="at most 99"):
            inventory.set_count(root, spec(), 0, 100)

    def test_setting_a_count_never_creates_anything(self) -> None:
        root = save([{"id": "potion", "count": 1}])

        with pytest.raises(ContainerError, match="no place 4"):
            inventory.set_count(root, spec(), 4, 1)

    def test_removing_from_a_list_takes_the_record_out(self) -> None:
        root = save([{"id": "potion", "count": 1}, {"id": "sword", "count": 1}])

        inventory.remove(root, spec(), 0)

        assert [item["id"] for item in root["party"]["items"]] == ["sword"]

    def test_removing_from_slots_empties_the_place_and_keeps_it(self) -> None:
        """The array's length is part of the format; it must not shrink."""
        root = save([{"id": "potion", "count": 1}, {"id": "sword", "count": 1}])

        inventory.remove(root, spec(shape=ContainerShape.SLOTS), 0)

        assert len(root["party"]["items"]) == 2
        assert root["party"]["items"][0] == {"id": None, "count": 0}


class TestAddingSomething:
    def test_a_new_record_is_a_copy_of_one_that_was_there(self) -> None:
        """The uuid field is the point: nothing here knows what it is for."""
        root = save([{"id": "potion", "count": 1, "uuid": "abc", "bought": False}])

        inventory.give(root, spec(), "elixir", 2)

        assert root["party"]["items"][1] == {
            "id": "elixir",
            "count": 2,
            "uuid": "abc",
            "bought": False,
        }

    def test_an_empty_container_refuses_and_says_what_to_do(self) -> None:
        root = save([])

        with pytest.raises(ContainerError, match="Pick up any one item"):
            inventory.give(root, spec(), "elixir")

    def test_a_map_needs_nothing_to_clone(self) -> None:
        """There a new key *is* the native form, so there is nothing to keep."""
        root = save({})

        inventory.give(root, spec(shape=ContainerShape.MAP), "1", 5)

        assert root["party"]["items"] == {"1": 5}

    def test_giving_something_already_there_adds_to_the_stack(self) -> None:
        root = save([{"id": "potion", "count": 3}])

        inventory.give(root, spec(), "potion", 4)

        assert root["party"]["items"] == [{"id": "potion", "count": 7}]

    def test_a_stack_never_grows_past_what_the_game_expects(self) -> None:
        root = save([{"id": "potion", "count": 98}])

        inventory.give(root, spec(), "potion", 50)

        assert root["party"]["items"][0]["count"] == 99

    def test_a_numbered_container_numbers_the_new_record(self) -> None:
        root = save([{"id": "potion", "count": 1, "order": 7}])

        inventory.give(root, spec(sequence="order"), "elixir")

        assert root["party"]["items"][1]["order"] == 8

    def test_slots_fill_the_first_empty_place(self) -> None:
        root = save(
            [{"id": "potion", "count": 1}, {"id": None, "count": 0}, {"id": "sword", "count": 1}]
        )

        placed = inventory.give(root, spec(shape=ContainerShape.SLOTS), "elixir")

        assert placed.position == 1
        assert len(root["party"]["items"]) == 3

    def test_a_full_slots_container_is_refused(self) -> None:
        root = save([{"id": "potion", "count": 1}])

        with pytest.raises(ContainerError, match="full"):
            inventory.give(root, spec(shape=ContainerShape.SLOTS), "elixir")

    def test_a_full_list_is_refused_at_its_capacity(self) -> None:
        root = save([{"id": "potion", "count": 1}, {"id": "sword", "count": 1}])

        with pytest.raises(ContainerError, match="holds 2 different things"):
            inventory.give(root, spec(capacity=2), "elixir")

    def test_the_copy_is_deep_enough_to_be_a_copy(self) -> None:
        root = save([{"id": "potion", "count": 1, "tags": ["found"]}])

        inventory.give(root, spec(), "elixir")
        root["party"]["items"][1]["tags"].append("cheated")

        assert root["party"]["items"][0]["tags"] == ["found"]
