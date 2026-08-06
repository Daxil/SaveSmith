"""Shared fixtures: fake machines built on a real temporary filesystem.

The Windows fixture is deliberately hostile. Its ``Documents`` known folder is
OneDrive-redirected, and a decoy ``C:\\Users\\danil\\Documents`` exists next to
it, empty. Code that builds the path from ``%USERPROFILE%`` instead of asking
Windows gets a directory that exists and is wrong — exactly the failure mode
users report as "SaveSmith cannot find my saves".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from savesmith.core.paths import FakeSystem, KnownFolder, RegistryHive
from savesmith.core.platform_ import Platform, current_platform


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip host-specific tests instead of failing them on the wrong OS."""
    platform = current_platform()
    skip_windows = pytest.mark.skip(reason="requires a real Windows host")
    skip_macos = pytest.mark.skip(reason="requires a real macOS host")
    for item in items:
        if "native_windows" in item.keywords and platform is not Platform.WINDOWS:
            item.add_marker(skip_windows)
        if "native_macos" in item.keywords and platform is not Platform.MACOS:
            item.add_marker(skip_macos)


def _make(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def windows_system(tmp_path: Path) -> FakeSystem:
    """A Windows machine with OneDrive turned on."""
    drive_c = tmp_path / "C"
    profile = drive_c / "Users" / "danil"
    appdata = profile / "AppData"
    onedrive_documents = profile / "OneDrive" / "Documents"
    decoy_documents = profile / "Documents"

    known_folders = {
        KnownFolder.PROFILE: profile,
        KnownFolder.ROAMING_APPDATA: appdata / "Roaming",
        KnownFolder.LOCAL_APPDATA: appdata / "Local",
        KnownFolder.LOCAL_APPDATA_LOW: appdata / "LocalLow",
        KnownFolder.DOCUMENTS: onedrive_documents,
        KnownFolder.SAVED_GAMES: profile / "Saved Games",
        KnownFolder.PUBLIC: drive_c / "Users" / "Public",
    }
    _make(*known_folders.values(), decoy_documents)

    return FakeSystem(
        platform=Platform.WINDOWS,
        home_dir=profile,
        env_vars={
            "USERPROFILE": str(profile),
            "APPDATA": str(appdata / "Roaming"),
            "LOCALAPPDATA": str(appdata / "Local"),
            "ProgramFiles(x86)": str(drive_c / "Program Files (x86)"),
        },
        known_folders=known_folders,
        registry={
            (RegistryHive.HKCU, r"Software\Valve\Steam", "SteamPath"): str(
                drive_c / "Program Files (x86)" / "Steam"
            ),
        },
        user="danil",
    )


@pytest.fixture
def macos_system(tmp_path: Path) -> FakeSystem:
    """A macOS machine with the usual Library layout."""
    home = tmp_path / "Users" / "danil"
    library = home / "Library"
    _make(
        library / "Application Support",
        library / "Preferences",
        library / "Containers",
        home / "Documents",
    )

    return FakeSystem(
        platform=Platform.MACOS,
        home_dir=home,
        env_vars={"HOME": str(home)},
        user="danil",
    )
