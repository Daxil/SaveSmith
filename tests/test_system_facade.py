"""The seam between SaveSmith and the host machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from savesmith.core.errors import UnsupportedPlatformError
from savesmith.core.paths import (
    FakeSystem,
    KnownFolder,
    RealSystem,
    RegistryHive,
    SystemFacade,
)
from savesmith.core.platform_ import Platform, current_platform


def test_both_implementations_satisfy_the_protocol(windows_system: FakeSystem) -> None:
    assert isinstance(windows_system, SystemFacade)
    assert isinstance(RealSystem(), SystemFacade)


class TestFakeSystem:
    def test_known_folders_come_back(self, windows_system: FakeSystem) -> None:
        documents = windows_system.known_folder(KnownFolder.DOCUMENTS)
        assert documents is not None
        assert documents.is_dir()

    def test_missing_known_folder_is_none_not_an_error(self, tmp_path: Path) -> None:
        """Saved Games genuinely does not exist on some Windows editions."""
        system = FakeSystem(platform=Platform.WINDOWS, home_dir=tmp_path)
        assert system.known_folder(KnownFolder.SAVED_GAMES) is None

    def test_registry_read(self, windows_system: FakeSystem) -> None:
        steam = windows_system.registry_read(
            RegistryHive.HKCU, r"Software\Valve\Steam", "SteamPath"
        )
        assert steam is not None
        assert steam.endswith("Steam")

    def test_absent_registry_value_is_none(self, windows_system: FakeSystem) -> None:
        assert windows_system.registry_read(RegistryHive.HKCU, r"Software\Nope", "X") is None

    def test_empty_env_var_counts_as_unset(self, tmp_path: Path) -> None:
        """An empty %APPDATA% must not yield paths rooted at nothing."""
        system = FakeSystem(platform=Platform.WINDOWS, home_dir=tmp_path, env_vars={"APPDATA": ""})
        assert system.env("APPDATA") is None

    def test_windows_only_calls_fail_loudly_on_macos(self, macos_system: FakeSystem) -> None:
        """Silence here would hide a resolver bug until a user hit it."""
        with pytest.raises(UnsupportedPlatformError) as caught:
            macos_system.known_folder(KnownFolder.DOCUMENTS)
        assert "macOS" in caught.value.user_message

        with pytest.raises(UnsupportedPlatformError):
            macos_system.registry_read(RegistryHive.HKCU, "Software", "X")


class TestRealSystem:
    def test_home_and_username_work_everywhere(self) -> None:
        system = RealSystem()
        assert system.home().is_dir()
        assert system.username()

    def test_platform_matches_the_host(self) -> None:
        assert RealSystem().platform is current_platform()

    def test_unset_env_var_is_none(self) -> None:
        assert RealSystem().env("SAVESMITH_DEFINITELY_NOT_SET_12345") is None

    def test_windows_calls_refuse_to_run_on_the_wrong_os(self) -> None:
        """A RealSystem told it is on Windows still guards the actual call."""
        if current_platform() is Platform.WINDOWS:
            pytest.skip("this asserts the off-Windows guard")
        with pytest.raises(UnsupportedPlatformError):
            RealSystem().known_folder(KnownFolder.DOCUMENTS)


@pytest.mark.native_windows
class TestNativeWindows:
    """Runs only on the windows-latest CI job. Proves the ctypes calls work."""

    @pytest.mark.parametrize(
        "folder",
        [
            KnownFolder.PROFILE,
            KnownFolder.ROAMING_APPDATA,
            KnownFolder.LOCAL_APPDATA,
            KnownFolder.LOCAL_APPDATA_LOW,
            KnownFolder.DOCUMENTS,
        ],
    )
    def test_known_folder_returns_an_absolute_path(self, folder: KnownFolder) -> None:
        path = RealSystem().known_folder(folder)
        assert path is not None, f"{folder} should exist on a normal Windows install"
        assert path.is_absolute()
        # A truncated 64-bit pointer shows up as a nonsense short string.
        assert len(str(path)) > 3

    def test_saved_games_may_be_absent_without_crashing(self) -> None:
        RealSystem().known_folder(KnownFolder.SAVED_GAMES)

    def test_registry_read_of_a_missing_key_is_none(self) -> None:
        value = RealSystem().registry_read(
            RegistryHive.HKCU, r"Software\SaveSmithNotHere", "Nope"
        )
        assert value is None

    def test_registry_read_of_a_key_windows_always_has(self) -> None:
        value = RealSystem().registry_read(
            RegistryHive.HKCU, r"Software\Microsoft\Windows\CurrentVersion\Explorer", "Logging"
        )
        # Either a string or absent; the point is that it does not raise.
        assert value is None or isinstance(value, str)
