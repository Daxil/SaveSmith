"""Point at a game folder, get its saves."""

from __future__ import annotations

import gzip
import json
import os
import plistlib
from pathlib import Path

import pytest

from savesmith.core import detect
from savesmith.core.discover import (
    Engine,
    Kind,
    classify,
    examine,
    find_saves,
    save_locations,
)
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


class TestPointingAtAFolderOfSaves:
    """The bug this class exists for.

    Somebody pointed at the folder their Elden Ring saves were sitting in and
    was told "No save files found". The folder was scanned as though it were an
    install folder — thousands of files, of which none is a save — so only
    names containing "save" were read, and ``ER0000.sl2`` is not one.
    """

    def test_saves_are_found_even_when_nothing_is_called_save(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        folder = tmp_path / "76561197960271872"
        folder.mkdir()
        (folder / "ER0000.sl2").write_bytes(SAVE_BYTES)
        (folder / "ER0000.sl2.bak").write_bytes(SAVE_BYTES)

        found = find_saves(examine(folder), windows)

        assert {save.path.name for save in found.saves} == {"ER0000.sl2", "ER0000.sl2.bak"}

    def test_an_install_folder_is_still_read_strictly(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        """The narrow rule has to stay where it earns its keep."""
        game = tmp_path / "Coin Quest"
        game.mkdir()
        (game / "CoinQuest.exe").write_bytes(b"MZ\x90\x00")
        (game / "UnityPlayer.dll").write_bytes(b"\x00" * 64)
        (game / "settings.dat").write_bytes(SAVE_BYTES)
        (game / "savegame.dat").write_bytes(SAVE_BYTES)

        found = find_saves(examine(game), windows)

        names = {save.path.name for save in found.saves}
        assert "savegame.dat" in names
        assert "settings.dat" not in names


class TestNamesThatDoNotMatchAsStrings:
    """A game installed as one spelling and saving under another.

    ``ELDEN RING`` on disk, ``EldenRing`` in AppData, ``eldenring.exe`` in
    between. Joining the install name onto AppData builds a path that is not
    there, so the folders that exist are matched instead.
    """

    def test_appdata_is_matched_ignoring_case_and_spaces(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        game = tmp_path / "ELDEN RING"
        game.mkdir()
        (game / "eldenring.exe").write_bytes(b"MZ\x90\x00")

        appdata = windows.known_folder(KnownFolder.ROAMING_APPDATA)
        assert appdata is not None
        saves = appdata / "EldenRing" / "76561"
        saves.mkdir(parents=True)
        (saves / "ER0000.sl2").write_bytes(SAVE_BYTES)

        found = find_saves(examine(game), windows)

        assert [save.path.name for save in found.saves] == ["ER0000.sl2"]

    def test_the_executable_name_is_used_when_the_folder_lies(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        """Steam's folder can be named anything; the exe rarely is."""
        game = tmp_path / "Game"
        game.mkdir()

        saved_games = windows.known_folder(KnownFolder.SAVED_GAMES)
        assert saved_games is not None
        saves = saved_games / "CoinQuest"
        saves.mkdir(parents=True)
        (saves / "slot1.dat").write_bytes(SAVE_BYTES)

        found = find_saves(examine(game, "CoinQuest"), windows)

        assert [save.path.name for save in found.saves] == ["slot1.dat"]


class TestWhatCountsAsRecognised:
    """Found, and then told the user nothing was found.

    An Elden Ring save is BND4 wrapping an encrypted FromSoftware slot. Every
    layer comes off and goes back on byte for byte — the file is understood —
    but no fields come out of it, and the screen reported that as "format not
    recognised" right under the words "nothing found". Three states, not two.
    """

    def opaque_but_exact(self, tmp_path: Path) -> Path:
        """A file a known step opens, whose payload stays bytes."""
        save = tmp_path / "ER0000.sl2"
        save.write_bytes(gzip.compress(bytes(range(256)) * 8, mtime=0))
        return save

    def test_a_known_wrapper_around_opaque_bytes_is_openable(self, tmp_path: Path) -> None:
        report = detect.identify(self.opaque_but_exact(tmp_path).read_bytes())

        assert report.openable, "the format is understood and rebuilds exactly"
        assert not report.solved, "but no fields came out of it"

    def test_an_unknown_file_is_neither(self, tmp_path: Path) -> None:
        report = detect.identify(b"\x8b\x1d\xf0\x03" * 64)

        assert not report.openable
        assert not report.solved

    def test_the_scan_never_calls_what_it_found_nothing(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        """The invariant the screen depends on."""
        folder = tmp_path / "76561197960271872"
        folder.mkdir()
        self.opaque_but_exact(folder)

        found = find_saves(examine(folder), windows)

        assert found.saves, "the file is there and was read"
        assert found.found_anything
        assert "No save files found" not in " ".join(found.explain())
        assert [save.path.name for save in found.openable] == ["ER0000.sl2"]


class TestOnlyTheSavesAreShown:
    """A folder of saves is mostly not saves.

    The Invincible keeps sixty-two rolling backups, four settings files and one
    save the player made. Handing somebody all sixty-seven and asking them to
    pick is not thoroughness — they opened SaveSmith precisely because they do
    not know what their save file is called.
    """

    def a_folder_like_the_invincible(self, tmp_path: Path) -> Path:
        folder = tmp_path / "SaveGames"
        folder.mkdir()
        (folder / "SaveSlot_0.sav").write_bytes(SAVE_BYTES)
        for index in range(1, 63):
            (folder / f"SaveSlot_0_backup_{index}.sav").write_bytes(SAVE_BYTES)
        (folder / "MenuSettingsSave.sav").write_bytes(SAVE_BYTES)
        (folder / "CondorSettings.sav").write_bytes(SAVE_BYTES)
        (folder / "GameUserSettings.ini").write_bytes(SAVE_BYTES)
        return folder

    def test_the_answer_is_the_one_save(self, tmp_path: Path, windows: FakeSystem) -> None:
        found = find_saves(examine(self.a_folder_like_the_invincible(tmp_path)), windows)

        assert [save.path.name for save in found.player_saves] == ["SaveSlot_0.sav"]
        assert found.best_save is not None
        assert found.best_save.path.name == "SaveSlot_0.sav"

    def test_the_rest_is_counted_not_listed(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        found = find_saves(examine(self.a_folder_like_the_invincible(tmp_path)), windows)

        assert found.aside[Kind.BACKUP] == 62
        # The .ini never even became a candidate — its extension disqualified
        # it during the walk, before anything was read.
        assert found.aside[Kind.SETTINGS] == 2

        printed = "\n".join(found.explain())
        assert "SaveSlot_0.sav" in printed
        assert "backup_7" not in printed, "the game's own copies are not the answer"
        assert "62 backups" in printed

    def test_an_elden_ring_folder_answers_with_one_file(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        folder = tmp_path / "76561197960271872"
        folder.mkdir()
        (folder / "ER0000.sl2").write_bytes(gzip.compress(bytes(range(256)) * 8, mtime=0))
        (folder / "ER0000.sl2.bak").write_bytes(gzip.compress(bytes(range(256)) * 8, mtime=0))

        found = find_saves(examine(folder), windows)

        assert [save.path.name for save in found.player_saves] == ["ER0000.sl2"]


class TestClassifying:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("SaveSlot_0.sav", Kind.SAVE),
            ("ER0000.sl2", Kind.SAVE),
            ("autosave1.dat", Kind.SAVE),
            ("SaveSlot_0_backup_7.sav", Kind.BACKUP),
            ("ER0000.sl2.bak", Kind.BACKUP),
            ("save_old.dat", Kind.BACKUP),
            # A game will happily put "Save" in the name of a settings file.
            ("MenuSettingsSave.sav", Kind.SETTINGS),
            ("GameUserSettings.ini", Kind.SETTINGS),
            ("DeviceProfiles.ini", Kind.SETTINGS),
            ("keybindings.dat", Kind.SETTINGS),
        ],
    )
    def test_names_the_games_actually_use(self, name: str, expected: Kind) -> None:
        assert classify(name, openable=True, size=4096) is expected

    def test_a_file_with_nothing_in_it_is_not_a_save(self) -> None:
        """Two empty bytes will cheerfully "decode" as base64 otherwise."""
        assert classify("mystery.dat", openable=True, size=2) is Kind.OTHER


class TestSlotsStayPut:
    """``--slot 2`` decides which save gets overwritten.

    It must mean the same file tomorrow as today, so the listing is ordered by
    name — which is also how a game numbers its own slots — and never by date.
    """

    def test_listed_by_name_not_by_date(self, tmp_path: Path, windows: FakeSystem) -> None:
        folder = tmp_path / "saves"
        folder.mkdir()
        for name in ("file2.dat", "file1.dat", "file3.dat"):
            (folder / name).write_bytes(SAVE_BYTES)
        # file1 written first, so by date it would come last.
        os.utime(folder / "file1.dat", (1, 1))
        os.utime(folder / "file3.dat", (500, 500))

        found = find_saves(examine(folder), windows)

        assert [save.path.name for save in found.player_saves] == [
            "file1.dat",
            "file2.dat",
            "file3.dat",
        ]

    def test_the_single_best_answer_is_the_newest(
        self, tmp_path: Path, windows: FakeSystem
    ) -> None:
        """For "just open my save", recency is the better guess."""
        folder = tmp_path / "saves"
        folder.mkdir()
        for name in ("file1.dat", "file2.dat"):
            (folder / name).write_bytes(SAVE_BYTES)
        os.utime(folder / "file1.dat", (1, 1))
        os.utime(folder / "file2.dat", (999_000, 999_000))

        best = find_saves(examine(folder), windows).best_save

        assert best is not None and best.path.name == "file2.dat"
