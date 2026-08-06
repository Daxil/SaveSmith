"""Finding and reading Wine bottles on a Mac."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from savesmith.core.errors import AmbiguousWineUserError, WinePrefixError
from savesmith.core.paths import FakeSystem, KnownFolder, RegistryHive
from savesmith.core.platform_ import Platform
from savesmith.core.wine import (
    BottleKind,
    WinePrefix,
    describe_prefixes,
    is_prefix,
    scan_prefixes,
)

USER_REG = r"""WINE REGISTRY Version 2

[Software\\Valve\\Steam] 1700000000
#time=1dabcdef
"SteamPath"="C:\\Program Files (x86)\\Steam"
"Language"="english"
"Rate"=dword:0000001e

[Software\\Wine\\Fonts] 1700000000
"Codepages"="1252,1252"
"""


def make_bottle(
    parent: Path,
    folder: str,
    *,
    users: tuple[str, ...] = ("crossover",),
    with_registry: bool = True,
    display_name: str | None = None,
) -> Path:
    bottle = parent / folder
    for user in users:
        for leaf in ("Roaming", "Local", "LocalLow"):
            (bottle / "drive_c" / "users" / user / "AppData" / leaf).mkdir(parents=True)
        (bottle / "drive_c" / "users" / user / "Documents").mkdir(parents=True)
    (bottle / "drive_c" / "users" / "Public").mkdir(parents=True, exist_ok=True)
    (bottle / "drive_c" / "users" / "Default").mkdir(parents=True, exist_ok=True)

    if with_registry:
        (bottle / "user.reg").write_text(USER_REG, encoding="utf-8")
        (bottle / "system.reg").write_text("WINE REGISTRY Version 2\n", encoding="utf-8")

    dosdevices = bottle / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    (dosdevices / "c:").symlink_to("../drive_c")

    if display_name is not None:
        with (bottle / "Metadata.plist").open("wb") as handle:
            plistlib.dump({"Name": display_name}, handle)
    return bottle


@pytest.fixture
def mac_home(tmp_path: Path) -> Path:
    home = tmp_path / "Users" / "danil"
    home.mkdir(parents=True)
    return home


@pytest.fixture
def mac_system(mac_home: Path) -> FakeSystem:
    return FakeSystem(platform=Platform.MACOS, home_dir=mac_home, user="danil")


class TestDetection:
    def test_drive_c_without_a_registry_is_not_a_bottle(self, tmp_path: Path) -> None:
        """Half-deleted bottles and unrelated folders both exist."""
        (tmp_path / "not-a-bottle" / "drive_c").mkdir(parents=True)
        assert is_prefix(tmp_path / "not-a-bottle") is False

    def test_a_registry_without_drive_c_is_not_a_bottle(self, tmp_path: Path) -> None:
        folder = tmp_path / "odd"
        folder.mkdir()
        (folder / "user.reg").write_text("", encoding="utf-8")
        assert is_prefix(folder) is False

    def test_a_real_bottle_is_recognised(self, tmp_path: Path) -> None:
        assert is_prefix(make_bottle(tmp_path, "bottle"))


class TestScanning:
    def test_finds_whisky_and_crossover_bottles(
        self, mac_home: Path, mac_system: FakeSystem
    ) -> None:
        whisky = mac_home / "Library" / "Containers" / "com.isaacmarovitz.Whisky" / "Bottles"
        crossover = mac_home / "Library" / "Application Support" / "CrossOver" / "Bottles"
        whisky.mkdir(parents=True)
        crossover.mkdir(parents=True)
        make_bottle(whisky, "5F2C-UUID", display_name="Hollow Knight")
        make_bottle(crossover, "Steam", users=("danil",))

        found = scan_prefixes(mac_system)
        assert {prefix.name for prefix in found} == {"Hollow Knight", "Steam"}
        kinds = {prefix.name: prefix.kind for prefix in found}
        assert kinds["Hollow Knight"] is BottleKind.WHISKY
        assert kinds["Steam"] is BottleKind.CROSSOVER

    def test_whisky_uuid_folders_show_their_real_name(
        self, mac_home: Path, mac_system: FakeSystem
    ) -> None:
        """A row of hex digits tells the player nothing."""
        bottles = mac_home / "Library" / "Containers" / "com.isaacmarovitz.Whisky" / "Bottles"
        bottles.mkdir(parents=True)
        make_bottle(bottles, "A1B2C3D4-0000", display_name="My Games")
        assert scan_prefixes(mac_system)[0].name == "My Games"

    def test_bottles_hidden_inside_app_wrappers_are_found(
        self, mac_home: Path, mac_system: FakeSystem
    ) -> None:
        """Wineskin and Porting Kit bury the whole prefix inside a .app."""
        applications = mac_home / "Applications"
        bundle = applications / "Some Game.app" / "Contents" / "SharedSupport"
        bundle.mkdir(parents=True)
        make_bottle(bundle, "prefix", users=("Wineskin",))

        found = scan_prefixes(mac_system)
        assert [prefix.kind for prefix in found] == [BottleKind.WINESKIN]

    def test_an_app_wrapper_is_named_after_its_bundle(
        self, mac_home: Path, mac_system: FakeSystem
    ) -> None:
        """Every Wineskin bottle is called "prefix"; that tells nobody anything."""
        bundle = mac_home / "Applications" / "Elden Ring.app" / "Contents" / "SharedSupport"
        bundle.mkdir(parents=True)
        make_bottle(bundle, "prefix")
        assert scan_prefixes(mac_system)[0].name == "Elden Ring"

    def test_an_app_without_a_bottle_is_ignored(
        self, mac_home: Path, mac_system: FakeSystem
    ) -> None:
        (mac_home / "Applications" / "Calculator.app" / "Contents").mkdir(parents=True)
        assert scan_prefixes(mac_system) == []

    def test_app_wrappers_are_not_looked_for_on_windows(self, tmp_path: Path) -> None:
        system = FakeSystem(platform=Platform.WINDOWS, home_dir=tmp_path)
        assert scan_prefixes(system) == []

    def test_a_folder_merely_named_after_a_tool_is_not_that_tool(
        self, tmp_path: Path, mac_system: FakeSystem
    ) -> None:
        """Kind comes from whole path components, not substrings."""
        notes = tmp_path / "My Whisky Notes"
        notes.mkdir()
        make_bottle(notes, "bottle")
        assert scan_prefixes(mac_system, [notes])[0].kind is BottleKind.WINE

    def test_dot_wine_in_the_home_folder(self, mac_home: Path, mac_system: FakeSystem) -> None:
        make_bottle(mac_home, ".wine")
        found = scan_prefixes(mac_system)
        assert [prefix.kind for prefix in found] == [BottleKind.WINE]

    def test_a_folder_the_user_picks_by_hand(self, tmp_path: Path, mac_system: FakeSystem) -> None:
        elsewhere = tmp_path / "Volumes" / "Games" / "bottles"
        elsewhere.mkdir(parents=True)
        make_bottle(elsewhere, "Manual")
        assert [prefix.name for prefix in scan_prefixes(mac_system, [elsewhere])] == ["Manual"]

    def test_no_bottles_on_windows(self, tmp_path: Path) -> None:
        system = FakeSystem(platform=Platform.WINDOWS, home_dir=tmp_path)
        assert scan_prefixes(system) == []

    def test_bottles_do_not_nest(self, tmp_path: Path, mac_system: FakeSystem) -> None:
        """A bottle inside a bottle is one bottle, not two."""
        outer = make_bottle(tmp_path, "outer")
        make_bottle(outer / "drive_c", "inner")
        assert [prefix.path for prefix in scan_prefixes(mac_system, [tmp_path])] == [outer]

    def test_a_symlink_loop_does_not_hang_the_scan(
        self, tmp_path: Path, mac_system: FakeSystem
    ) -> None:
        root = tmp_path / "loop"
        root.mkdir()
        (root / "back").symlink_to(root, target_is_directory=True)
        make_bottle(root, "bottle")
        assert [prefix.name for prefix in scan_prefixes(mac_system, [root])] == ["bottle"]

    def test_the_visit_budget_stops_a_runaway_scan(
        self, tmp_path: Path, mac_system: FakeSystem
    ) -> None:
        deep = tmp_path
        for index in range(20):
            deep = deep / f"level{index}"
        deep.mkdir(parents=True)
        assert scan_prefixes(mac_system, [tmp_path], max_depth=30, visit_budget=5) == []

    def test_the_same_bottle_reached_twice_is_listed_once(
        self, mac_home: Path, mac_system: FakeSystem
    ) -> None:
        bottles = mac_home / "Library" / "Application Support" / "CrossOver" / "Bottles"
        bottles.mkdir(parents=True)
        bottle = make_bottle(bottles, "Steam")
        assert len(scan_prefixes(mac_system, [bottle.parent])) == 1


class TestProfiles:
    def test_windows_own_profiles_are_ignored(self, tmp_path: Path) -> None:
        bottle = make_bottle(tmp_path, "bottle", users=("crossover",))
        prefix = scan_prefixes(
            FakeSystem(platform=Platform.MACOS, home_dir=tmp_path), [tmp_path]
        )[0]
        assert prefix.users == ("crossover",)
        assert (bottle / "drive_c" / "users" / "Public").is_dir(), "fixture sanity"

    def test_a_single_profile_is_chosen_without_asking(
        self, tmp_path: Path, mac_system: FakeSystem
    ) -> None:
        make_bottle(tmp_path, "bottle", users=("crossover",))
        prefix = scan_prefixes(mac_system, [tmp_path])[0]
        assert prefix.preferred_user(mac_system) == "crossover"

    def test_the_host_username_breaks_a_tie(self, tmp_path: Path, mac_system: FakeSystem) -> None:
        make_bottle(tmp_path, "bottle", users=("danil", "steamuser"))
        prefix = scan_prefixes(mac_system, [tmp_path])[0]
        assert prefix.preferred_user(mac_system) == "danil"

    def test_several_profiles_and_no_match_asks_instead_of_guessing(
        self, tmp_path: Path, mac_system: FakeSystem
    ) -> None:
        """Guessing wrong means editing a save the player never made."""
        make_bottle(tmp_path, "bottle", users=("crossover", "steamuser"))
        prefix = scan_prefixes(mac_system, [tmp_path])[0]
        with pytest.raises(AmbiguousWineUserError) as caught:
            prefix.preferred_user(mac_system)
        assert "crossover" in caught.value.user_message
        assert "steamuser" in caught.value.user_message

    def test_a_bottle_with_no_profiles_says_so(self, mac_system: FakeSystem) -> None:
        prefix = WinePrefix(Path("/nowhere"), BottleKind.WINE, "empty", users=())
        with pytest.raises(WinePrefixError):
            prefix.preferred_user(mac_system)


class TestBottleAsAMachine:
    @pytest.fixture
    def bottle(self, tmp_path: Path, mac_system: FakeSystem) -> WinePrefix:
        make_bottle(tmp_path, "bottle", users=("crossover",))
        return scan_prefixes(mac_system, [tmp_path])[0]

    def test_windows_folders_land_inside_the_bottle(self, bottle: WinePrefix) -> None:
        system = bottle.system("crossover")
        low = system.known_folder(KnownFolder.LOCAL_APPDATA_LOW)
        assert low is not None
        assert low == bottle.drive_c / "users" / "crossover" / "AppData" / "LocalLow"

    def test_the_bottle_reports_itself_as_windows(self, bottle: WinePrefix) -> None:
        assert bottle.system("crossover").platform is Platform.WINDOWS

    def test_the_ordinary_windows_token_table_works_inside(self, bottle: WinePrefix) -> None:
        """No Wine-specific path logic outside this module — that is the point."""
        saves = (
            bottle.drive_c
            / "users"
            / "crossover"
            / "AppData"
            / "LocalLow"
            / "Team Cherry"
            / "Hollow Knight"
        )
        saves.mkdir(parents=True)
        (saves / "user1.dat").write_bytes(b"save")
        (saves / "user2.dat").write_bytes(b"save")

        resolver = bottle.resolver("crossover")
        found = resolver.resolve("{LOCALLOW}/Team Cherry/Hollow Knight/user*.dat")
        assert [path.name for path in found] == ["user1.dat", "user2.dat"]

    def test_the_wineuser_token_is_filled_in(self, bottle: WinePrefix) -> None:
        resolver = bottle.resolver("crossover")
        expanded = resolver.expand("{USERPROFILE}/../{WINEUSER}/Documents")
        assert expanded is not None
        assert "crossover" in str(expanded)

    def test_saved_games_absent_from_the_bottle_is_none(self, bottle: WinePrefix) -> None:
        assert bottle.system("crossover").known_folder(KnownFolder.SAVED_GAMES) is None


class TestBottleRegistry:
    @pytest.fixture
    def bottle(self, tmp_path: Path, mac_system: FakeSystem) -> WinePrefix:
        make_bottle(tmp_path, "bottle", users=("crossover",))
        return scan_prefixes(mac_system, [tmp_path])[0]

    def test_a_windows_path_comes_back_as_a_host_path(self, bottle: WinePrefix) -> None:
        r"""C:\Program Files (x86)\Steam is useless to anything on the Mac."""
        steam = bottle.drive_c / "Program Files (x86)" / "Steam"
        steam.mkdir(parents=True)
        value = bottle.system("crossover").registry_read(
            RegistryHive.HKCU, r"Software\Valve\Steam", "SteamPath"
        )
        assert value == str(steam)

    def test_steam_token_resolves_inside_the_bottle(self, bottle: WinePrefix) -> None:
        steam = bottle.drive_c / "Program Files (x86)" / "Steam"
        steam.mkdir(parents=True)
        assert bottle.resolver("crossover").token("STEAM") == steam

    def test_non_path_values_are_returned_as_they_are(self, bottle: WinePrefix) -> None:
        value = bottle.system("crossover").registry_read(
            RegistryHive.HKCU, r"Software\Valve\Steam", "Language"
        )
        assert value == "english"

    def test_dword_values_become_decimal(self, bottle: WinePrefix) -> None:
        value = bottle.system("crossover").registry_read(
            RegistryHive.HKCU, r"Software\Valve\Steam", "Rate"
        )
        assert value == "30"

    def test_lookups_ignore_case(self, bottle: WinePrefix) -> None:
        value = bottle.system("crossover").registry_read(
            RegistryHive.HKCU, r"software\valve\steam", "steampath"
        )
        assert value is not None

    def test_missing_keys_and_values_are_none(self, bottle: WinePrefix) -> None:
        system = bottle.system("crossover")
        assert system.registry_read(RegistryHive.HKCU, r"Software\Nope", "X") is None
        assert system.registry_read(RegistryHive.HKCU, r"Software\Valve\Steam", "Nope") is None

    def test_a_missing_hive_file_is_empty_not_fatal(self, tmp_path: Path) -> None:
        bottle_path = make_bottle(tmp_path, "b", with_registry=False)
        (bottle_path / "user.reg").write_text("", encoding="utf-8")
        prefix = WinePrefix(bottle_path, BottleKind.WINE, "b", ("crossover",))
        assert prefix.system("crossover").registry_read(RegistryHive.HKLM, "Software", "X") is None


class TestDriveTranslation:
    def test_other_drive_letters_follow_dosdevices(
        self, tmp_path: Path, mac_system: FakeSystem
    ) -> None:
        """A bottle can point d: anywhere; Wine resolves it through dosdevices."""
        bottle_path = make_bottle(tmp_path, "bottle")
        games = tmp_path / "ExternalGames"
        (games / "Saves").mkdir(parents=True)
        (bottle_path / "dosdevices" / "d:").symlink_to(games)

        prefix = scan_prefixes(mac_system, [tmp_path])[0]
        assert prefix.system("crossover").translate(r"D:\Saves") == games / "Saves"

    def test_an_unmapped_drive_letter_is_none(self, tmp_path: Path, mac_system: FakeSystem) -> None:
        make_bottle(tmp_path, "bottle")
        prefix = scan_prefixes(mac_system, [tmp_path])[0]
        assert prefix.system("crossover").translate(r"Z:\Nothing") is None

    def test_text_that_is_not_a_path_is_none(self, tmp_path: Path, mac_system: FakeSystem) -> None:
        make_bottle(tmp_path, "bottle")
        prefix = scan_prefixes(mac_system, [tmp_path])[0]
        assert prefix.system("crossover").translate("english") is None


def test_describe_prefixes_is_readable() -> None:
    prefix = WinePrefix(Path("/b"), BottleKind.WHISKY, "My Games", ("crossover", "danil"))
    line = describe_prefixes([prefix])[0]
    assert "My Games" in line
    assert "crossover, danil" in line
