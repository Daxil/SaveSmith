"""Turning ids into names and pictures.

The order matters more than the mechanics: the installed game's own files beat
anything shipped alongside, because they are what the player is actually
running. A catalog that cannot be found is never an error — an inventory full
of bare numbers is still an inventory, and refusing to open it because nobody
wrote down what item 1007 is called would help no one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from savesmith.core import catalog
from savesmith.core.catalog import CatalogError, Entry

PNG = b"\x89PNG\r\n\x1a\n" + b"fake pixels"


def an_rpgmaker_game(root: Path, *, mz: bool = False, with_icons: bool = True) -> Path:
    """MV keeps everything under www/; MZ puts data/ and img/ at the top."""
    base = root if mz else root / "www"
    (base / "data").mkdir(parents=True)
    (base / "data" / "Items.json").write_text(
        json.dumps(
            [
                None,  # the engine's own convention: ids start at one
                {"id": 1, "name": "Potion", "iconIndex": 176, "description": "Heals\na bit."},
                {"id": 2, "name": "Elixir", "iconIndex": 177},
                {"id": 3, "name": ""},  # a hole left by a game in development
            ]
        ),
        encoding="utf-8",
    )
    (base / "data" / "Weapons.json").write_text(
        json.dumps([None, {"id": 1, "name": "Bronze Sword", "iconIndex": 96}]), encoding="utf-8"
    )
    if with_icons:
        (base / "img" / "system").mkdir(parents=True)
        (base / "img" / "system" / "IconSet.png").write_bytes(PNG)
    return root


class TestFromTheGamesOwnFiles:
    def test_names_and_icons_come_out_of_the_game(self, tmp_path: Path) -> None:
        game = an_rpgmaker_game(tmp_path / "game")

        found = catalog.load("rpgmaker:items", game_folder=game)

        assert found.name_of("1") == "Potion"
        assert found.get("1").icon == catalog.Icon(sheet="rpgmaker-iconset", index=176)
        assert found.sheets["rpgmaker-iconset"].png == PNG
        assert "game's own data files" in found.source

    def test_mz_keeps_the_same_files_one_folder_up(self, tmp_path: Path) -> None:
        game = an_rpgmaker_game(tmp_path / "game", mz=True)

        assert catalog.load("rpgmaker:items", game_folder=game).name_of("2") == "Elixir"

    def test_each_kind_of_thing_is_its_own_catalog(self, tmp_path: Path) -> None:
        """Three arrays in the engine, three bags in the party, three catalogs."""
        game = an_rpgmaker_game(tmp_path / "game")

        weapons = catalog.load("rpgmaker:weapons", game_folder=game)

        assert weapons.name_of("1") == "Bronze Sword"

    def test_the_engines_line_breaks_do_not_reach_the_screen(self, tmp_path: Path) -> None:
        game = an_rpgmaker_game(tmp_path / "game")

        assert catalog.load("rpgmaker:items", game_folder=game).get("1").description == (
            "Heals a bit."
        )

    def test_nameless_holes_are_left_out(self, tmp_path: Path) -> None:
        game = an_rpgmaker_game(tmp_path / "game")

        assert catalog.load("rpgmaker:items", game_folder=game).get("3") is None

    def test_a_game_without_its_icon_sheet_still_gives_names(self, tmp_path: Path) -> None:
        game = an_rpgmaker_game(tmp_path / "game", with_icons=False)

        found = catalog.load("rpgmaker:items", game_folder=game)

        assert found.name_of("1") == "Potion"
        assert not found.sheets

    def test_a_folder_that_is_not_that_game_is_not_an_error(self, tmp_path: Path) -> None:
        assert not catalog.load("rpgmaker:items", game_folder=tmp_path)


class TestFromAPackBesideThePlugin:
    def a_pack(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "icons.png").write_bytes(PNG)
        (folder / "items.json").write_text(
            json.dumps(
                {
                    "source": "extracted from my own copy of the game",
                    "sheets": [{"id": "menu", "file": "icons.png", "tile": 40, "columns": 8}],
                    "items": [
                        {"id": "goods:1007", "name": "Rune Arc", "icon": ["menu", 12]},
                        {"id": "goods:8000", "name": "Golden Rune"},
                        {"id": "weapon:110000", "name": "Dagger", "kind": "weapon"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return folder

    def test_a_shipped_list_names_things_the_game_will_not(self, tmp_path: Path) -> None:
        plugin = self.a_pack(tmp_path / "plugin")

        found = catalog.load("eldenring", plugin_folder=plugin)

        assert found.name_of("goods:1007") == "Rune Arc"
        assert found.get("goods:1007").icon == catalog.Icon(sheet="menu", index=12)
        assert found.sheets["menu"].tile == 40

    def test_the_game_wins_when_it_has_the_answer_too(self, tmp_path: Path) -> None:
        """The installed game is what the player is running; a pack may be old."""
        game = an_rpgmaker_game(tmp_path / "game")
        plugin = self.a_pack(tmp_path / "plugin")

        found = catalog.load("rpgmaker:items", plugin_folder=plugin, game_folder=game)

        assert found.name_of("1") == "Potion"

    def test_a_damaged_pack_says_so_instead_of_being_ignored(self, tmp_path: Path) -> None:
        plugin = tmp_path / "plugin"
        plugin.mkdir()
        (plugin / "items.json").write_text("{ this is not json", encoding="utf-8")

        with pytest.raises(CatalogError, match="could not be read"):
            catalog.load("eldenring", plugin_folder=plugin)

    def test_no_pack_and_no_game_is_simply_no_names(self, tmp_path: Path) -> None:
        found = catalog.load("eldenring", plugin_folder=tmp_path)

        assert not found
        assert found.name_of("goods:1007") == "goods:1007"


class TestFindingWhatWasTyped:
    def a_catalog(self) -> catalog.Catalog:
        return catalog.Catalog(
            entries={
                "1": Entry(id="1", name="Potion"),
                "2": Entry(id="2", name="Potion of Strength"),
                "3": Entry(id="3", name="potion"),
                "4": Entry(id="4", name="Elixir"),
            }
        )

    def test_an_id_is_taken_as_an_id(self) -> None:
        assert [entry.id for entry in self.a_catalog().search("2")] == ["2"]

    def test_an_exact_name_wins_over_the_things_it_is_a_prefix_of(self) -> None:
        """Otherwise 'Potion' could never be selected at all."""
        assert [entry.id for entry in self.a_catalog().search("Potion")] == ["1"]

    def test_case_is_only_ignored_when_nothing_matched_exactly(self) -> None:
        assert [entry.id for entry in self.a_catalog().search("POTION")] == ["1", "3"]

    def test_part_of_a_name_finds_everything_it_could_mean(self) -> None:
        assert [entry.id for entry in self.a_catalog().search("strength")] == ["2"]

    def test_nothing_typed_finds_nothing(self) -> None:
        assert self.a_catalog().search("   ") == []
