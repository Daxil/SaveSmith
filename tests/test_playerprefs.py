"""Unity PlayerPrefs on both platforms."""

from __future__ import annotations

import plistlib
import struct
from pathlib import Path

import pytest

from savesmith.core.paths import FakeSystem, KnownFolder, RegistryHive
from savesmith.core.platform_ import Platform
from savesmith.core.playerprefs import (
    REG_BINARY,
    REG_DWORD,
    PlayerPrefsError,
    PlistPrefs,
    RegistryPrefs,
    open_prefs,
)

KEY = "Software\\Team Cherry\\Hollow Knight"


@pytest.fixture
def windows(tmp_path: Path) -> FakeSystem:
    """A Windows machine with a few PlayerPrefs already stored."""
    system = FakeSystem(platform=Platform.WINDOWS, home_dir=tmp_path)
    # Unity mangles every name with a hash it has never documented.
    system.registry[(RegistryHive.HKCU, KEY, "coins_h1234567890")] = 250
    system.registry_types[(RegistryHive.HKCU, KEY, "coins_h1234567890")] = REG_DWORD
    system.registry[(RegistryHive.HKCU, KEY, "playerName_h987654321")] = b"Ann\x00"
    system.registry_types[(RegistryHive.HKCU, KEY, "playerName_h987654321")] = REG_BINARY
    system.registry[(RegistryHive.HKCU, KEY, "unmangled")] = 7
    system.registry_types[(RegistryHive.HKCU, KEY, "unmangled")] = REG_DWORD
    return system


@pytest.fixture
def macos(tmp_path: Path) -> FakeSystem:
    home = tmp_path / "home"
    (home / "Library" / "Preferences").mkdir(parents=True)
    path = home / "Library" / "Preferences" / "unity.Team Cherry.Hollow Knight.plist"
    with path.open("wb") as handle:
        plistlib.dump({"coins": 250, "playerName": "Ann", "volume": 0.5}, handle,
                      fmt=plistlib.FMT_BINARY)
    return FakeSystem(platform=Platform.MACOS, home_dir=home)


class TestWindows:
    def test_names_are_recovered_without_the_hash(self, windows: FakeSystem) -> None:
        """Listing what is there beats reimplementing an undocumented hash."""
        prefs = RegistryPrefs(system=windows, company="Team Cherry", product="Hollow Knight")
        entries = prefs.read()
        assert set(entries) == {"coins", "playerName", "unmangled"}
        assert entries["coins"].raw_name == "coins_h1234567890"

    def test_values_come_back_typed(self, windows: FakeSystem) -> None:
        entries = RegistryPrefs(windows, "Team Cherry", "Hollow Knight").read()
        assert entries["coins"].value == 250
        assert entries["playerName"].value == "Ann"
        assert entries["playerName"].kind == "string"

    def test_writing_uses_the_stored_name(self, windows: FakeSystem) -> None:
        prefs = RegistryPrefs(windows, "Team Cherry", "Hollow Knight")
        prefs.write("coins", 9999)
        assert windows.registry[(RegistryHive.HKCU, KEY, "coins_h1234567890")] == 9999

    def test_a_string_is_stored_the_way_unity_stores_it(self, windows: FakeSystem) -> None:
        prefs = RegistryPrefs(windows, "Team Cherry", "Hollow Knight")
        prefs.write("playerName", "Yaroslav")
        stored = windows.registry[(RegistryHive.HKCU, KEY, "playerName_h987654321")]
        assert stored == b"Yaroslav" + b"\x00"

    def test_a_float_occupies_the_same_four_bytes_an_int_would(
        self, windows: FakeSystem
    ) -> None:
        prefs = RegistryPrefs(windows, "Team Cherry", "Hollow Knight")
        prefs.write("coins", 0.5)
        stored = windows.registry[(RegistryHive.HKCU, KEY, "coins_h1234567890")]
        assert stored == struct.unpack("<I", struct.pack("<f", 0.5))[0]

    def test_a_setting_that_does_not_exist_is_refused(self, windows: FakeSystem) -> None:
        """Creating one needs Unity's hash, and the game would not read it anyway."""
        prefs = RegistryPrefs(windows, "Team Cherry", "Hollow Knight")
        with pytest.raises(PlayerPrefsError, match="no setting called"):
            prefs.write("neverSeen", 1)

    def test_a_game_with_nothing_stored(self, tmp_path: Path) -> None:
        system = FakeSystem(platform=Platform.WINDOWS, home_dir=tmp_path)
        assert RegistryPrefs(system, "Nobody", "Nothing").read() == {}

    def test_export_is_readable_and_restorable_by_hand(self, windows: FakeSystem) -> None:
        exported = RegistryPrefs(windows, "Team Cherry", "Hollow Knight").export()
        assert b"coins_h1234567890" in exported
        assert b"HKEY_CURRENT_USER" in exported


class TestMacos:
    def test_reading_a_binary_plist(self, macos: FakeSystem) -> None:
        prefs = open_prefs(macos, "Team Cherry", "Hollow Knight")
        entries = prefs.read()
        assert entries["coins"].value == 250
        assert entries["volume"].value == pytest.approx(0.5)

    def test_names_are_not_mangled_here(self, macos: FakeSystem) -> None:
        entries = open_prefs(macos, "Team Cherry", "Hollow Knight").read()
        assert entries["coins"].raw_name == "coins"

    def test_writing_keeps_the_other_values(self, macos: FakeSystem) -> None:
        prefs = open_prefs(macos, "Team Cherry", "Hollow Knight")
        prefs.write("coins", 9999)
        entries = prefs.read()
        assert entries["coins"].value == 9999
        assert entries["playerName"].value == "Ann"

    def test_it_stays_a_binary_plist(self, macos: FakeSystem) -> None:
        """A game that wrote binary will not thank us for handing back XML."""
        prefs = open_prefs(macos, "Team Cherry", "Hollow Knight")
        prefs.write("coins", 1)
        assert isinstance(prefs, PlistPrefs)
        assert prefs.path.read_bytes().startswith(b"bplist")

    def test_a_missing_file_is_empty_not_an_error(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / "Library" / "Preferences").mkdir(parents=True)
        system = FakeSystem(platform=Platform.MACOS, home_dir=home)
        assert open_prefs(system, "Nobody", "Nothing").read() == {}

    def test_a_damaged_file_says_so(self, macos: FakeSystem, tmp_path: Path) -> None:
        prefs = open_prefs(macos, "Team Cherry", "Hollow Knight")
        assert isinstance(prefs, PlistPrefs)
        prefs.path.write_bytes(b"bplist00 but not really")
        with pytest.raises(PlayerPrefsError, match="damaged or unreadable"):
            prefs.read()

    def test_a_setting_that_does_not_exist_is_refused(self, macos: FakeSystem) -> None:
        with pytest.raises(PlayerPrefsError, match="no setting called"):
            open_prefs(macos, "Team Cherry", "Hollow Knight").write("neverSeen", 1)

    def test_export_returns_the_file_itself(self, macos: FakeSystem) -> None:
        assert open_prefs(macos, "Team Cherry", "Hollow Knight").export().startswith(b"bplist")


class TestChoosingTheBackend:
    def test_windows_gets_the_registry(self, windows: FakeSystem) -> None:
        prefs = open_prefs(windows, "Team Cherry", "Hollow Knight")
        assert "HKEY_CURRENT_USER" in prefs.location

    def test_macos_gets_the_plist(self, macos: FakeSystem) -> None:
        assert open_prefs(macos, "Team Cherry", "Hollow Knight").location.endswith(".plist")

    def test_the_windows_backend_refuses_to_run_elsewhere(self, macos: FakeSystem) -> None:
        from savesmith.core.errors import UnsupportedPlatformError

        with pytest.raises(UnsupportedPlatformError):
            RegistryPrefs(macos, "Team Cherry", "Hollow Knight").read()

    def test_a_windows_known_folder_is_not_needed_for_prefs(self, tmp_path: Path) -> None:
        """The registry path is built from names, not from a folder lookup."""
        system = FakeSystem(
            platform=Platform.WINDOWS,
            home_dir=tmp_path,
            known_folders={KnownFolder.PROFILE: tmp_path},
        )
        assert open_prefs(system, "A", "B").read() == {}
