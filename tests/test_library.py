"""Every game on this machine.

The list the first screen offers. What matters is that it reaches the places a
player's games actually are — including a Steam library living inside a Wine
bottle inside a Mac application, which the host filesystem cannot see — and
that it does not pad itself out with things that are not games.

The assertions ask whether a particular game is in the list rather than what
the whole list is: ``application_folders`` really does look at ``/Applications``,
and whatever the developer has installed there is none of these tests' business.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from savesmith.core.library import Game, Library, scan
from savesmith.core.paths import FakeSystem
from savesmith.core.platform_ import Platform

from .test_steam import write_manifest


def make_steam(root: Path, *games: tuple[int, str]) -> Path:
    """A minimal but real Steam install, with itself as its only library."""
    (root / "steamapps").mkdir(parents=True, exist_ok=True)
    for appid, name in games:
        write_manifest(root / "steamapps", appid, name)
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n'
        f'\t\t"path"\t\t"{root.as_posix()}"\n'
        '\t\t"label"\t\t""\n\t}\n}\n',
        encoding="utf-8",
    )
    return root


def make_bottle(bundle: Path) -> Path:
    """A Wineskin wrapper: a double-clickable .app with a prefix inside."""
    prefix = bundle / "Contents" / "SharedSupport" / "prefix"
    (prefix / "drive_c" / "users" / "Wineskin").mkdir(parents=True)
    (prefix / "system.reg").write_text("WINE REGISTRY Version 2\n", encoding="utf-8")
    return prefix


def named(games: list[Game], name: str) -> Game | None:
    return next((game for game in games if game.name == name), None)


@pytest.fixture
def mac(tmp_path: Path) -> FakeSystem:
    """A Mac with an empty home, and no Steam unless a test installs one."""
    home = tmp_path / "home"
    (home / "Applications").mkdir(parents=True)
    return FakeSystem(platform=Platform.MACOS, home_dir=home, user="player")


def steam_at_home(system: FakeSystem, *games: tuple[int, str]) -> Path:
    """Steam where macOS actually keeps it, so the resolver finds it itself."""
    return make_steam(
        system.home() / "Library" / "Application Support" / "Steam", *games
    )


class TestSteamOnThisMachine:
    def test_installed_games_are_listed(self, mac: FakeSystem) -> None:
        steam_at_home(mac, (367520, "Hollow Knight"))

        game = named(scan(mac).games, "Hollow Knight")

        assert game is not None
        assert game.source == "Steam"
        assert game.steam_appid == 367520

    def test_steams_own_plumbing_is_not_a_game(self, mac: FakeSystem) -> None:
        """These carry appids and manifests, and no saves whatsoever."""
        steam_at_home(
            mac,
            (228980, "Steamworks Common Redistributables"),
            (367520, "Hollow Knight"),
        )

        games = scan(mac).games

        assert named(games, "Hollow Knight") is not None
        assert named(games, "Steamworks Common Redistributables") is None

    def test_no_steam_is_not_a_problem_to_report(self, mac: FakeSystem) -> None:
        """A machine without Steam is ordinary, not broken."""
        assert scan(mac).problems == []


class TestGamesInsideABottle:
    def test_a_windows_steam_library_inside_a_wrapper_is_reached(
        self, mac: FakeSystem
    ) -> None:
        """Where a Mac player's Windows games really live."""
        prefix = make_bottle(mac.home() / "Applications" / "Steambuild.app")
        make_steam(
            prefix / "drive_c" / "Program Files (x86)" / "Steam",
            (731040, "The Invincible"),
        )

        game = named(scan(mac).games, "The Invincible")

        assert game is not None
        assert game.bottle == "Steambuild"
        assert game.path.is_dir()

    def test_the_wrapper_itself_is_offered_as_a_game(self, mac: FakeSystem) -> None:
        """A wrapper with no Steam in it is still one game somebody installed."""
        make_bottle(mac.home() / "Applications" / "elden ring.app")

        game = named(scan(mac).games, "elden ring")

        assert game is not None
        assert game.path.suffix == ".app"


class TestMacApplications:
    def test_an_engine_fingerprint_makes_it_a_game(self, mac: FakeSystem) -> None:
        bundle = mac.home() / "Applications" / "Coin Quest.app"
        (bundle / "Contents" / "Frameworks").mkdir(parents=True)
        (bundle / "Contents" / "Frameworks" / "UnityPlayer.dylib").write_bytes(b"\x00")

        assert named(scan(mac).games, "Coin Quest") is not None

    def test_an_ordinary_application_is_left_out(self, mac: FakeSystem) -> None:
        """Listing every .app would bury four games under sixty utilities."""
        bundle = mac.home() / "Applications" / "Totally Not A Game.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True)

        assert named(scan(mac).games, "Totally Not A Game") is None


class TestTheListItself:
    def test_the_same_game_reached_twice_is_listed_once(self, tmp_path: Path) -> None:
        """A bottle's Steam library can also be the host's, on the same disk."""
        library = Library()
        library.add(Game(name="Portal 2", path=tmp_path / "Portal 2", source="Steam"))
        library.add(
            Game(name="Portal 2", path=tmp_path / "Portal 2", source="Steam в бутылке X")
        )

        assert [game.source for game in library.games] == ["Steam"]

    def test_games_are_grouped_by_where_they_came_from(self, tmp_path: Path) -> None:
        library = Library()
        library.add(Game(name="Zebra", path=tmp_path / "z", source="Steam"))
        library.add(Game(name="Apple", path=tmp_path / "a", source="Программы"))
        library.add(Game(name="Bear", path=tmp_path / "b", source="Steam"))

        assert [game.name for game in library.sorted()] == ["Bear", "Zebra", "Apple"]


class TestGamesFoundByTheirSaves:
    """A game can be uninstalled, or installed in one bottle and saved in another.

    This is not a corner case: the same game in two bottles gives two saves that
    both look right, and a list showing only one of them silently hands over the
    wrong playthrough — which happened, twice, with a level 575 character shown
    where a level 713 one was meant.
    """

    def elden_ring_save(self, prefix: Path, steam_id: str = "76561") -> Path:
        folder = (
            prefix / "drive_c" / "users" / "Wineskin" / "AppData" / "Roaming"
            / "EldenRing" / steam_id
        )
        folder.mkdir(parents=True)
        (folder / "ER0000.sl2").write_bytes(b"BND4" + bytes(64))
        return folder

    def test_a_game_with_only_saves_left_is_still_offered(self, mac: FakeSystem) -> None:
        prefix = make_bottle(mac.home() / "Applications" / "elden ring.app")
        self.elden_ring_save(prefix)

        game = named(scan(mac).games, "ELDEN RING")

        assert game is not None
        assert game.bottle == "elden ring"

    def test_the_same_game_in_two_bottles_appears_twice(self, mac: FakeSystem) -> None:
        """Both, with the bottle named, so the player picks rather than guesses."""
        for bundle in ("elden ring.app", "Steambuild.app"):
            self.elden_ring_save(make_bottle(mac.home() / "Applications" / bundle))

        found = [game for game in scan(mac).games if game.name == "ELDEN RING"]

        assert {game.bottle for game in found} == {"elden ring", "Steambuild"}

    def test_a_folder_of_saves_is_one_entry_not_seventy(self, mac: FakeSystem) -> None:
        """The Invincible keeps sixty-two rolling backups beside its save."""
        prefix = make_bottle(mac.home() / "Applications" / "Steambuild.app")
        folder = (
            prefix / "drive_c" / "users" / "Wineskin" / "AppData" / "Local"
            / "TheInvincible" / "Saved" / "SaveGames"
        )
        folder.mkdir(parents=True)
        (folder / "SaveSlot_0.sav").write_bytes(b"GVAS" + bytes(64))
        for index in range(62):
            (folder / f"SaveSlot_0_backup_{index}.sav").write_bytes(b"GVAS" + bytes(64))

        found = [game for game in scan(mac).games if game.name == "The Invincible"]

        assert len(found) == 1
        assert found[0].path == folder
