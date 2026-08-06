"""Reading a Steam installation.

Every test builds a real (temporary) Steam tree and points a FakeSystem at it,
so the Windows registry lookup and the macOS default path are both exercised
from whichever host runs the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from savesmith.core.errors import SteamNotFoundError
from savesmith.core.paths import FakeSystem, RegistryHive
from savesmith.core.platform_ import Platform
from savesmith.core.steam import SteamInstall

APPMANIFEST = """\
"AppState"
{{
	"appid"		"{appid}"
	"name"		"{name}"
	"installdir"		"{installdir}"
	"SizeOnDisk"		"{size}"
	"LastUpdated"		"1754300000"
}}
"""


def write_manifest(steamapps: Path, appid: int, name: str, size: int = 1000) -> Path:
    steamapps.mkdir(parents=True, exist_ok=True)
    path = steamapps / f"appmanifest_{appid}.acf"
    path.write_text(
        APPMANIFEST.format(appid=appid, name=name, installdir=name, size=size),
        encoding="utf-8",
    )
    (steamapps / "common" / name).mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def steam_root(tmp_path: Path) -> Path:
    """A Steam install with two reachable libraries and one offline one."""
    root = tmp_path / "C" / "Program Files (x86)" / "Steam"
    second = tmp_path / "D" / "SteamLibrary"
    offline = tmp_path / "X" / "OfflineDrive"  # deliberately never created

    write_manifest(root / "steamapps", 367520, "Hollow Knight")
    write_manifest(second / "steamapps", 413150, "Stardew Valley")

    index = root / "steamapps" / "libraryfolders.vdf"
    index.write_text(
        f"""\
"libraryfolders"
{{
	"0"
	{{
		"path"		"{root.as_posix()}"
		"label"		""
		"apps"
		{{
			"367520"		"9114661188"
		}}
	}}
	"1"
	{{
		"path"		"{second.as_posix()}"
		"label"		"Games drive"
		"apps"
		{{
			"413150"		"1004553652"
		}}
	}}
	"2"
	{{
		"path"		"{offline.as_posix()}"
		"label"		"External"
	}}
}}
""",
        encoding="utf-8",
    )

    (root / "userdata" / "76561198000000000" / "367520").mkdir(parents=True)
    (root / "userdata" / "76561198000000000" / "367520" / "remotecache.vdf").write_text(
        '"367520" { }', encoding="utf-8"
    )
    (root / "userdata" / "0").mkdir(parents=True)
    (root / "userdata" / "anonymous").mkdir(parents=True)
    return root


def windows_pointing_at(root: Path, tmp_path: Path) -> FakeSystem:
    return FakeSystem(
        platform=Platform.WINDOWS,
        home_dir=tmp_path / "home",
        registry={(RegistryHive.HKCU, r"Software\Valve\Steam", "SteamPath"): str(root)},
    )


class TestDiscovery:
    def test_finds_steam_through_the_windows_registry(
        self, steam_root: Path, tmp_path: Path
    ) -> None:
        install = SteamInstall.discover(windows_pointing_at(steam_root, tmp_path))
        assert install.root == steam_root

    def test_finds_steam_in_the_default_macos_location(self, tmp_path: Path) -> None:
        home = tmp_path / "Users" / "danil"
        root = home / "Library" / "Application Support" / "Steam"
        write_manifest(root / "steamapps", 367520, "Hollow Knight")
        system = FakeSystem(platform=Platform.MACOS, home_dir=home)
        assert SteamInstall.discover(system).root == root

    def test_missing_steam_says_so_in_plain_words(self, tmp_path: Path) -> None:
        system = FakeSystem(platform=Platform.MACOS, home_dir=tmp_path / "empty")
        with pytest.raises(SteamNotFoundError) as caught:
            SteamInstall.discover(system)
        assert "Steam" in caught.value.user_message
        assert "manually" in caught.value.user_message

    def test_an_empty_leftover_folder_is_not_steam(self, tmp_path: Path) -> None:
        """Uninstalling Steam leaves the folder behind; it must not count."""
        home = tmp_path / "Users" / "danil"
        (home / "Library" / "Application Support" / "Steam").mkdir(parents=True)
        system = FakeSystem(platform=Platform.MACOS, home_dir=home)
        with pytest.raises(SteamNotFoundError):
            SteamInstall.discover(system)


class TestLibraries:
    def test_all_libraries_including_the_root(self, steam_root: Path) -> None:
        scan = SteamInstall(steam_root).scan()
        labels = {library.label for library in scan.libraries}
        assert {"Steam", "Games drive", "External"} <= labels

    def test_offline_drive_is_listed_but_marked_unavailable(self, steam_root: Path) -> None:
        """An unplugged external drive must not hide the games on the others."""
        scan = SteamInstall(steam_root).scan()
        external = next(lib for lib in scan.libraries if lib.label == "External")
        assert external.available is False
        assert {game.name for game in scan.games} == {"Hollow Knight", "Stardew Valley"}

    def test_the_root_library_is_not_duplicated(self, steam_root: Path) -> None:
        scan = SteamInstall(steam_root).scan()
        roots = [lib for lib in scan.libraries if lib.path == steam_root]
        assert len(roots) == 1

    def test_missing_index_still_yields_the_root_library(self, tmp_path: Path) -> None:
        """Normal state of a fresh install with one library."""
        root = tmp_path / "Steam"
        write_manifest(root / "steamapps", 367520, "Hollow Knight")
        scan = SteamInstall(root).scan()
        assert [library.path for library in scan.libraries] == [root]
        assert len(scan.games) == 1

    def test_generation_1_flat_format(self, tmp_path: Path) -> None:
        root = tmp_path / "Steam"
        second = tmp_path / "D" / "SteamLibrary"
        write_manifest(root / "steamapps", 367520, "Hollow Knight")
        write_manifest(second / "steamapps", 413150, "Stardew Valley")
        (root / "steamapps" / "libraryfolders.vdf").write_text(
            '"LibraryFolders"\n{\n'
            '\t"TimeNextStatsReport"\t\t"1546330000"\n'
            f'\t"1"\t\t"{second.as_posix()}"\n'
            "}\n",
            encoding="utf-8",
        )
        scan = SteamInstall(root).scan()
        assert {game.name for game in scan.games} == {"Hollow Knight", "Stardew Valley"}

    def test_index_may_live_in_the_config_folder(self, tmp_path: Path) -> None:
        root = tmp_path / "Steam"
        second = tmp_path / "D" / "SteamLibrary"
        write_manifest(root / "steamapps", 367520, "Hollow Knight")
        write_manifest(second / "steamapps", 413150, "Stardew Valley")
        (root / "config").mkdir(parents=True)
        (root / "config" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"1"\n\t{\n'
            f'\t\t"path"\t\t"{second.as_posix()}"\n'
            "\t}\n}\n",
            encoding="utf-8",
        )
        assert len(SteamInstall(root).scan().games) == 2

    def test_damaged_index_is_a_warning_not_a_crash(self, tmp_path: Path) -> None:
        root = tmp_path / "Steam"
        write_manifest(root / "steamapps", 367520, "Hollow Knight")
        (root / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n', encoding="utf-8"
        )
        scan = SteamInstall(root).scan()
        assert scan.problems, "the damage should be reported"
        assert [game.name for game in scan.games] == ["Hollow Knight"]
        assert "damaged" in scan.problems[0].user_message

    def test_library_entry_without_a_path_is_reported(self, tmp_path: Path) -> None:
        root = tmp_path / "Steam"
        write_manifest(root / "steamapps", 367520, "Hollow Knight")
        (root / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"1"\n\t{\n\t\t"label"\t\t"broken"\n\t}\n}\n',
            encoding="utf-8",
        )
        scan = SteamInstall(root).scan()
        assert any("no path" in (problem.detail or "") for problem in scan.problems)


class TestGames:
    def test_manifest_fields(self, steam_root: Path) -> None:
        scan = SteamInstall(steam_root).scan()
        game = scan.game_by_appid(367520)
        assert game is not None
        assert game.name == "Hollow Knight"
        assert game.size_on_disk == 1000
        assert game.last_updated == 1754300000
        assert game.install_dir.name == "Hollow Knight"
        assert game.is_installed

    def test_games_are_sorted_by_name(self, steam_root: Path) -> None:
        names = [game.name for game in SteamInstall(steam_root).scan().games]
        assert names == sorted(names, key=str.lower)

    def test_a_damaged_manifest_does_not_hide_the_others(self, steam_root: Path) -> None:
        (steam_root / "steamapps" / "appmanifest_9999.acf").write_text(
            '"AppState"\n{\n\t"name"\t\t"Broken', encoding="utf-8"
        )
        scan = SteamInstall(steam_root).scan()
        assert {game.name for game in scan.games} == {"Hollow Knight", "Stardew Valley"}
        assert any("missing from the list" in problem.user_message for problem in scan.problems)

    def test_appid_falls_back_to_the_filename(self, tmp_path: Path) -> None:
        root = tmp_path / "Steam"
        steamapps = root / "steamapps"
        steamapps.mkdir(parents=True)
        (steamapps / "appmanifest_620.acf").write_text(
            '"AppState"\n{\n\t"name"\t\t"Portal 2"\n\t"installdir"\t\t"Portal 2"\n}\n',
            encoding="utf-8",
        )
        game = SteamInstall(root).scan().games[0]
        assert game.appid == 620

    def test_a_game_being_installed_is_listed_but_not_marked_installed(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "Steam"
        steamapps = root / "steamapps"
        steamapps.mkdir(parents=True)
        (steamapps / "appmanifest_620.acf").write_text(
            '"AppState"\n{\n\t"appid"\t\t"620"\n\t"name"\t\t"Portal 2"\n'
            '\t"installdir"\t\t"Portal 2"\n}\n',
            encoding="utf-8",
        )
        game = SteamInstall(root).scan().games[0]
        assert game.is_installed is False


class TestUsers:
    def test_only_real_account_folders(self, steam_root: Path) -> None:
        users = SteamInstall(steam_root).scan().users
        assert [user.account_id for user in users] == ["76561198000000000"]

    def test_cloud_cache_is_found_when_present(self, steam_root: Path) -> None:
        """Milestone 3 uses this to decide whether to show the Cloud wizard."""
        user = SteamInstall(steam_root).scan().users[0]
        assert user.cloud_cache(367520) is not None
        assert user.cloud_cache(413150) is None

    def test_no_userdata_folder_is_an_empty_list(self, tmp_path: Path) -> None:
        root = tmp_path / "Steam"
        write_manifest(root / "steamapps", 367520, "Hollow Knight")
        assert SteamInstall(root).scan().users == []


class TestAwkwardPaths:
    def test_library_path_with_glob_characters(self, tmp_path: Path) -> None:
        """A library at "D:\\Games [SSD]" must not be treated as a pattern."""
        root = tmp_path / "Steam"
        second = tmp_path / "Games [SSD]" / "SteamLibrary"
        write_manifest(root / "steamapps", 367520, "Hollow Knight")
        write_manifest(second / "steamapps", 413150, "Stardew Valley")
        (root / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"1"\n\t{\n'
            f'\t\t"path"\t\t"{second.as_posix()}"\n'
            "\t}\n}\n",
            encoding="utf-8",
        )
        scan = SteamInstall(root).scan()
        assert {game.name for game in scan.games} == {"Hollow Knight", "Stardew Valley"}

    def test_windows_style_backslash_paths_are_understood(self, tmp_path: Path) -> None:
        root = tmp_path / "Steam"
        write_manifest(root / "steamapps", 367520, "Hollow Knight")
        (root / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"1"\n\t{\n'
            '\t\t"path"\t\t"D:\\\\NotHere\\\\SteamLibrary"\n'
            "\t}\n}\n",
            encoding="utf-8",
        )
        scan = SteamInstall(root).scan()
        listed = [str(library.path) for library in scan.libraries]
        assert any("NotHere/SteamLibrary" in path for path in listed)
