"""Point at a game folder, get its saves."""

from __future__ import annotations

import gzip
import json
import plistlib
from pathlib import Path

import pytest

from savesmith.core.discover import Engine, examine, find_saves, save_locations
from savesmith.core.paths import FakeSystem, KnownFolder, RegistryHive
from savesmith.core.platform_ import Platform
from savesmith.core.playerprefs import REG_DWORD

SAVE_BYTES = gzip.compress(json.dumps({"gold": 100}).encode(), mtime=0)


@pytest.fixture
def windows(tmp_path: Path) -> FakeSystem:
    profile = tmp_path / "Users" / "player"
    known = {
        KnownFolder.PROFILE: profile,
        KnownFolder.LOCAL_APPDATA: profile / "AppData" / "Local",
        KnownFolder.ROAMING_APPDATA: profile / "AppData" / "Roaming",
        KnownFolder.LOCAL_APPDATA_LOW: profile / "AppData" / "LocalLow",
        KnownFolder.DOCUMENTS: profile / "Documents",
        KnownFolder.SAVED_GAMES: profile / "Saved Games",
    }
    for path in known.values():
        path.mkdir(parents=True, exist_ok=True)
    return FakeSystem(
        platform=Platform.WINDOWS, home_dir=profile, known_folders=known, user="player"
    )


class TestEngineDetection:
    def test_unreal_by_its_engine_folder(self, tmp_path: Path) -> None:
        game = tmp_path / "The Invincible"
        (game / "Engine" / "Binaries").mkdir(parents=True)
        (game / "TheInvincible" / "Content").mkdir(parents=True)
        (game / "TheInvincible.exe").write_bytes(b"")

        found = examine(game)
        assert found.engine is Engine.UNREAL
        assert found.project == "TheInvincible", "the project names the save folder"

    def test_unity_reads_its_own_app_info(self, tmp_path: Path) -> None:
        """Unity writes publisher and product where we can just read them."""
        game = tmp_path / "Hollow Knight"
        data = game / "hollow_knight_Data"
        data.mkdir(parents=True)
        (data / "app.info").write_text("Team Cherry\nHollow Knight\n", encoding="utf-8")

        found = examine(game)
        assert found.engine is Engine.UNITY
        assert (found.company, found.project) == ("Team Cherry", "Hollow Knight")

    def test_unity_without_app_info_falls_back_to_the_folder_name(self, tmp_path: Path) -> None:
        game = tmp_path / "Some Game"
        (game / "somegame_Data").mkdir(parents=True)
        found = examine(game)
        assert found.engine is Engine.UNITY
        assert found.project == "somegame"

    def test_rpg_maker(self, tmp_path: Path) -> None:
        game = tmp_path / "My RPG"
        (game / "www" / "save").mkdir(parents=True)
        assert examine(game).engine is Engine.RPGMAKER

    def test_godot(self, tmp_path: Path) -> None:
        game = tmp_path / "Indie"
        game.mkdir()
        (game / "indie.pck").write_bytes(b"GDPC")
        (game / "indie.exe").write_bytes(b"")
        assert examine(game).engine is Engine.GODOT

    def test_gamemaker(self, tmp_path: Path) -> None:
        game = tmp_path / "Undertale"
        game.mkdir()
        (game / "data.win").write_bytes(b"FORM")
        (game / "Undertale.exe").write_bytes(b"")
        assert examine(game).engine is Engine.GAMEMAKER

    def test_an_unfamiliar_game_still_yields_a_name(self, tmp_path: Path) -> None:
        game = tmp_path / "Isaac"
        game.mkdir()
        (game / "isaac-ng.exe").write_bytes(b"")
        found = examine(game)
        assert found.engine is Engine.UNKNOWN
        assert found.project == "isaac-ng"

    def test_installer_leftovers_are_not_mistaken_for_the_game(self, tmp_path: Path) -> None:
        game = tmp_path / "Game"
        game.mkdir()
        (game / "unins000.exe").write_bytes(b"")
        (game / "vcredist_x64.exe").write_bytes(b"")
        (game / "RealGame.exe").write_bytes(b"")
        assert examine(game).project == "RealGame"

    def test_an_unreadable_folder_does_not_raise(self, tmp_path: Path) -> None:
        assert examine(tmp_path / "not here").engine is Engine.UNKNOWN


class TestSteamAndAntiCheat:
    def test_the_steam_appid_is_read(self, tmp_path: Path) -> None:
        game = tmp_path / "Game"
        game.mkdir()
        (game / "steam_appid.txt").write_text("367520\n", encoding="utf-8")
        assert examine(game).steam_appid == 367520

    def test_a_damaged_appid_file_is_ignored(self, tmp_path: Path) -> None:
        game = tmp_path / "Game"
        game.mkdir()
        (game / "steam_appid.txt").write_text("not a number", encoding="utf-8")
        assert examine(game).steam_appid is None

    def test_anticheat_components_are_noticed(self, tmp_path: Path) -> None:
        """Presence sets the risk tier the user sees; it stops nothing by itself."""
        game = tmp_path / "Game"
        (game / "EasyAntiCheat").mkdir(parents=True)
        (game / "EasyAntiCheat" / "EasyAntiCheat.sys").write_bytes(b"")
        found = examine(game)
        assert found.has_anticheat
        assert any("EasyAntiCheat" in name for name in found.anticheat)

    def test_a_clean_game_reports_no_anticheat(self, tmp_path: Path) -> None:
        """A build without it — pirated, cracked, or simply never shipped with it."""
        game = tmp_path / "Game"
        game.mkdir()
        (game / "Game.exe").write_bytes(b"")
        assert not examine(game).has_anticheat


class TestSaveLocations:
    def test_unreal_saves_land_under_local_appdata(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        game = tmp_path / "The Invincible"
        (game / "Engine").mkdir(parents=True)
        (game / "TheInvincible" / "Content").mkdir(parents=True)
        saves = windows.home_dir / "AppData" / "Local" / "TheInvincible" / "Saved" / "SaveGames"
        saves.mkdir(parents=True)

        assert saves in save_locations(examine(game), windows)

    def test_unity_saves_land_in_locallow(self, tmp_path: Path, windows: FakeSystem) -> None:
        game = tmp_path / "Hollow Knight"
        data = game / "hk_Data"
        data.mkdir(parents=True)
        (data / "app.info").write_text("Team Cherry\nHollow Knight\n", encoding="utf-8")
        saves = windows.home_dir / "AppData" / "LocalLow" / "Team Cherry" / "Hollow Knight"
        saves.mkdir(parents=True)

        assert saves in save_locations(examine(game), windows)

    def test_rpg_maker_saves_sit_inside_the_game(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        game = tmp_path / "My RPG"
        saves = game / "www" / "save"
        saves.mkdir(parents=True)
        assert saves in save_locations(examine(game), windows)

    def test_generic_places_are_tried_for_any_game(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        game = tmp_path / "Mystery"
        game.mkdir()
        (game / "Mystery.exe").write_bytes(b"")
        expected = windows.home_dir / "Saved Games" / "Mystery"
        expected.mkdir(parents=True)
        assert expected in save_locations(examine(game), windows)

    def test_places_that_do_not_exist_are_not_offered(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        game = tmp_path / "Mystery"
        game.mkdir()
        assert all(path.is_dir() for path in save_locations(examine(game), windows))


class TestFindingSaves:
    def test_the_whole_chain(self, tmp_path: Path, windows: FakeSystem) -> None:
        """Folder in, identified saves out — the thing the user actually does."""
        game = tmp_path / "Hollow Knight"
        data = game / "hk_Data"
        data.mkdir(parents=True)
        (data / "app.info").write_text("Team Cherry\nHollow Knight\n", encoding="utf-8")

        saves = windows.home_dir / "AppData" / "LocalLow" / "Team Cherry" / "Hollow Knight"
        saves.mkdir(parents=True)
        (saves / "user1.dat").write_bytes(SAVE_BYTES)
        (saves / "user2.dat").write_bytes(SAVE_BYTES)

        found = find_saves(examine(game), windows)
        assert len(found.recognised) == 2
        assert found.recognised[0].format == "gzip → json_parse"

    def test_a_file_found_through_two_places_is_listed_once(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        game = tmp_path / "The Invincible"
        (game / "Engine").mkdir(parents=True)
        (game / "TheInvincible" / "Content").mkdir(parents=True)

        base = windows.home_dir / "AppData" / "Local" / "TheInvincible"
        saves = base / "Saved" / "SaveGames"
        saves.mkdir(parents=True)
        (saves / "SaveSlot_0.sav").write_bytes(SAVE_BYTES)

        found = find_saves(examine(game), windows)
        assert len(found.saves) == 1, "the parent folder must not list it again"

    def test_the_install_folder_is_searched_only_for_save_like_names(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        """An install folder is full of everything except saves."""
        game = tmp_path / "Mystery"
        game.mkdir()
        (game / "Mystery.exe").write_bytes(b"")
        (game / "content.bin").write_bytes(b"\x00" * 4096)
        (game / "savedata.bin").write_bytes(SAVE_BYTES)

        names = {save.path.name for save in find_saves(examine(game), windows).saves}
        assert "savedata.bin" in names
        assert "content.bin" not in names

    def test_unrecognised_files_are_still_reported(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        """The honest case: found something, cannot read it. That is the agent's cue."""
        game = tmp_path / "Mystery"
        game.mkdir()
        (game / "Mystery.exe").write_bytes(b"")
        saves = windows.home_dir / "Saved Games" / "Mystery"
        saves.mkdir(parents=True)
        (saves / "profile.sav").write_bytes(bytes(range(256)) * 8)

        found = find_saves(examine(game), windows)
        assert len(found.saves) == 1
        assert not found.saves[0].recognised

    def test_recognised_saves_come_first(self, tmp_path: Path, windows: FakeSystem) -> None:
        game = tmp_path / "Mystery"
        game.mkdir()
        (game / "Mystery.exe").write_bytes(b"")
        saves = windows.home_dir / "Saved Games" / "Mystery"
        saves.mkdir(parents=True)
        (saves / "aaa_unreadable.sav").write_bytes(bytes(range(256)) * 8)
        (saves / "zzz_good.sav").write_bytes(SAVE_BYTES)

        found = find_saves(examine(game), windows)
        assert found.saves[0].path.name == "zzz_good.sav"

    def test_the_log_names_the_places_it_looked(self, tmp_path: Path, windows: FakeSystem) -> None:
        game = tmp_path / "Mystery"
        game.mkdir()
        lines = " ".join(find_saves(examine(game), windows).explain())
        assert "Looked in" in lines or "No save files" in lines

    def test_huge_files_are_not_read(self, tmp_path: Path, windows: FakeSystem) -> None:
        """A 300 MB archive is game data, whatever it is called."""
        game = tmp_path / "Mystery"
        game.mkdir()
        saves = windows.home_dir / "Saved Games" / "Mystery"
        saves.mkdir(parents=True)
        big = saves / "savedata.pak"
        with big.open("wb") as handle:
            handle.truncate(70 * 1024 * 1024)
        assert find_saves(examine(game), windows).saves == []


class TestUnityPlayerPrefs:
    """A Unity game keeping progress in PlayerPrefs is not a game with no saves."""

    def _unity_game(self, tmp_path: Path) -> Path:
        game = tmp_path / "Coin Quest"
        (game / "CoinQuest_Data").mkdir(parents=True)
        (game / "CoinQuest_Data" / "app.info").write_text(
            "Tiny Studio\nCoin Quest\n", encoding="utf-8"
        )
        return game

    def test_registry_prefs_are_reported_beside_the_files(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        game = self._unity_game(tmp_path)
        key = "Software\\Tiny Studio\\Coin Quest"
        windows.registry[(RegistryHive.HKCU, key, "coins_h1234567890")] = 250
        windows.registry_types[(RegistryHive.HKCU, key, "coins_h1234567890")] = REG_DWORD

        found = find_saves(examine(game), windows)

        assert found.saves == [], "this game keeps nothing in a file"
        assert found.found_anything, "but it does have progress worth showing"
        assert found.prefs is not None
        assert [entry.name for entry in found.prefs.entries] == ["coins"]
        assert found.prefs.numbers[0].value == 250

    def test_the_report_says_where_they_are_and_how_to_change_one(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        game = self._unity_game(tmp_path)
        key = "Software\\Tiny Studio\\Coin Quest"
        windows.registry[(RegistryHive.HKCU, key, "coins_h1")] = 250
        windows.registry_types[(RegistryHive.HKCU, key, "coins_h1")] = REG_DWORD

        lines = " ".join(find_saves(examine(game), windows).explain())
        assert "Unity settings" in lines
        assert "coins = 250" in lines
        assert "savesmith prefs" in lines

    def test_a_mac_reads_them_from_the_property_list(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / "Library" / "Preferences").mkdir(parents=True)
        plist = home / "Library" / "Preferences" / "unity.Tiny Studio.Coin Quest.plist"
        with plist.open("wb") as handle:
            plistlib.dump({"coins": 250, "muted": True}, handle, fmt=plistlib.FMT_BINARY)
        system = FakeSystem(platform=Platform.MACOS, home_dir=home)

        found = find_saves(examine(self._unity_game(tmp_path)), system)

        assert found.prefs is not None
        assert {entry.name for entry in found.prefs.entries} == {"coins", "muted"}
        # A flag is not a number a player wants to edit first.
        assert [entry.name for entry in found.prefs.numbers] == ["coins"]

    def test_nothing_stored_means_nothing_reported(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        assert find_saves(examine(self._unity_game(tmp_path)), windows).prefs is None

    def test_a_non_unity_game_is_not_asked(self, tmp_path: Path, windows: FakeSystem) -> None:
        game = tmp_path / "Mystery"
        game.mkdir()
        assert find_saves(examine(game), windows).prefs is None

    def test_an_unreadable_property_list_is_not_fatal(self, tmp_path: Path) -> None:
        """A damaged settings file must not take the whole folder scan down."""
        home = tmp_path / "home"
        (home / "Library" / "Preferences").mkdir(parents=True)
        plist = home / "Library" / "Preferences" / "unity.Tiny Studio.Coin Quest.plist"
        plist.write_bytes(b"not a property list")
        system = FakeSystem(platform=Platform.MACOS, home_dir=home)

        found = find_saves(examine(self._unity_game(tmp_path)), system)
        assert found.prefs is None
        assert "No save files found" in " ".join(found.explain())
