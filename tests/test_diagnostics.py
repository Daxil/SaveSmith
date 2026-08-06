"""The diagnostics report.

Its whole value is being readable and never blowing up, so that is what these
check: it must survive a machine with nothing on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from savesmith.core.diagnostics import collect, main, render
from savesmith.core.paths import FakeSystem, KnownFolder, RegistryHive
from savesmith.core.platform_ import Platform
from tests.test_steam import write_manifest


@pytest.fixture
def bare_mac(tmp_path: Path) -> FakeSystem:
    home = tmp_path / "Users" / "danil"
    home.mkdir(parents=True)
    return FakeSystem(platform=Platform.MACOS, home_dir=home, user="danil")


def test_a_machine_with_nothing_on_it_still_reports(bare_mac: FakeSystem) -> None:
    """This is exactly when someone runs diagnostics."""
    text = render(collect(bare_mac))
    assert "SaveSmith diagnostics" in text
    assert "Could not find a Steam installation" in text
    assert "none found" in text


def test_every_token_is_listed(bare_mac: FakeSystem) -> None:
    text = render(collect(bare_mac))
    for token in ("APPDATA", "DOCUMENTS", "SAVEDGAMES", "STEAM", "SAVESMITH_DATA"):
        assert token in text


def test_tokens_absent_on_this_platform_are_marked(bare_mac: FakeSystem) -> None:
    report = collect(bare_mac)
    assert report.tokens["SAVEDGAMES"] is None
    assert "not available on this platform" in render(report)


def test_wineuser_is_described_as_contextual_not_broken(bare_mac: FakeSystem) -> None:
    """It has no value outside a bottle; that is not a fault to report."""
    assert "only has a value inside a Wine bottle" in render(collect(bare_mac))


def test_existing_and_missing_folders_are_distinguished(bare_mac: FakeSystem) -> None:
    text = render(collect(bare_mac))
    assert "[ok     ]" in text  # Documents exists
    assert "[missing]" in text  # SaveSmith's own folder does not yet


def test_steam_games_and_libraries_are_summarised(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "danil"
    root = home / "Library" / "Application Support" / "Steam"
    write_manifest(root / "steamapps", 367520, "Hollow Knight")
    system = FakeSystem(platform=Platform.MACOS, home_dir=home, user="danil")

    text = render(collect(system))
    assert str(root) in text
    assert "games: 1" in text


def test_steam_problems_are_shown_not_swallowed(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "danil"
    root = home / "Library" / "Application Support" / "Steam"
    write_manifest(root / "steamapps", 367520, "Hollow Knight")
    (root / "steamapps" / "libraryfolders.vdf").write_text('"libraryfolders"\n{\n\t"0"\n\t{\n')

    system = FakeSystem(platform=Platform.MACOS, home_dir=home, user="danil")
    assert "problem:" in render(collect(system))


def test_bottles_are_listed_with_their_profiles(tmp_path: Path) -> None:
    from tests.test_wine import make_bottle

    home = tmp_path / "Users" / "danil"
    bottles = home / "Library" / "Application Support" / "CrossOver" / "Bottles"
    bottles.mkdir(parents=True)
    make_bottle(bottles, "Steam", users=("crossover",))

    system = FakeSystem(platform=Platform.MACOS, home_dir=home, user="danil")
    text = render(collect(system))
    assert "Steam [crossover]" in text
    assert "profiles: crossover" in text


def test_windows_report_mentions_the_registry_steam_path(tmp_path: Path) -> None:
    root = tmp_path / "Steam"
    write_manifest(root / "steamapps", 367520, "Hollow Knight")
    system = FakeSystem(
        platform=Platform.WINDOWS,
        home_dir=tmp_path / "home",
        known_folders={KnownFolder.PROFILE: tmp_path / "home"},
        registry={(RegistryHive.HKCU, r"Software\Valve\Steam", "SteamPath"): str(root)},
    )
    text = render(collect(system))
    assert str(root) in text
    assert "not applicable here" in text  # no bottles on Windows


def test_an_unsupported_platform_is_reported_not_crashed(tmp_path: Path) -> None:
    system = FakeSystem(platform=Platform.LINUX, home_dir=tmp_path, user="danil")
    report = collect(system)
    assert report.supported is False
    assert "not supported" in render(report)


def test_main_runs_against_the_real_machine(capsys: pytest.CaptureFixture[str]) -> None:
    """The CI smoke step; also proves the native calls work on each runner."""
    assert main() == 0
    assert "SaveSmith diagnostics" in capsys.readouterr().out
