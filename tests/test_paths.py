"""Token resolution and pattern matching, on both platforms, from one host."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from savesmith.core.errors import PathResolutionError, UnknownPathTokenError
from savesmith.core.paths import (
    FakeSystem,
    KnownFolder,
    PathResolver,
    PathToken,
    RegistryHive,
    extended_length_path,
)
from savesmith.core.paths import _resolver as resolver_module
from savesmith.core.platform_ import Platform


class TestWindowsTokens:
    def test_appdata_family(self, windows_system: FakeSystem) -> None:
        resolver = PathResolver(windows_system)
        assert resolver.token("APPDATA") == windows_system.known_folders[
            KnownFolder.ROAMING_APPDATA
        ]
        assert resolver.token("LOCALAPPDATA") == windows_system.known_folders[
            KnownFolder.LOCAL_APPDATA
        ]
        assert resolver.token("LOCALLOW") == windows_system.known_folders[
            KnownFolder.LOCAL_APPDATA_LOW
        ]

    def test_locallow_falls_back_to_a_sibling_of_local(self, tmp_path: Path) -> None:
        """Older Windows builds can fail the LocalAppDataLow lookup."""
        system = FakeSystem(
            platform=Platform.WINDOWS,
            home_dir=tmp_path,
            env_vars={"LOCALAPPDATA": str(tmp_path / "AppData" / "Local")},
        )
        assert PathResolver(system).token("LOCALLOW") == tmp_path / "AppData" / "LocalLow"

    def test_saved_games(self, windows_system: FakeSystem) -> None:
        assert PathResolver(windows_system).token("SAVEDGAMES") is not None

    def test_macos_only_tokens_are_unavailable(self, windows_system: FakeSystem) -> None:
        resolver = PathResolver(windows_system)
        assert resolver.token("CONTAINERS") is None
        assert resolver.token("PREFS") is None

    def test_savesmith_data_sits_under_local_appdata(self, windows_system: FakeSystem) -> None:
        data = PathResolver(windows_system).token("SAVESMITH_DATA")
        assert data == windows_system.known_folders[KnownFolder.LOCAL_APPDATA] / "SaveSmith"

    def test_steam_comes_from_the_registry(self, windows_system: FakeSystem) -> None:
        steam = PathResolver(windows_system).token("STEAM")
        assert steam is not None and steam.name == "Steam"

    def test_steam_falls_back_to_program_files(self, tmp_path: Path) -> None:
        system = FakeSystem(
            platform=Platform.WINDOWS,
            home_dir=tmp_path,
            env_vars={"ProgramFiles(x86)": r"C:\Program Files (x86)"},
        )
        assert PathResolver(system).token("STEAM") == Path("C:/Program Files (x86)/Steam")

    def test_steam_absent_everywhere_is_none(self, tmp_path: Path) -> None:
        system = FakeSystem(platform=Platform.WINDOWS, home_dir=tmp_path)
        assert PathResolver(system).token("STEAM") is None


class TestOneDriveRedirect:
    """The single most common cause of "no saves found" in editors like this."""

    def test_documents_follows_the_onedrive_redirect(self, windows_system: FakeSystem) -> None:
        documents = PathResolver(windows_system).token("DOCUMENTS")
        assert documents == windows_system.known_folders[KnownFolder.DOCUMENTS]
        assert "OneDrive" in str(documents)

    def test_the_decoy_folder_is_never_returned(self, windows_system: FakeSystem) -> None:
        """C:\\Users\\danil\\Documents exists and is empty. Returning it is the bug."""
        decoy = windows_system.home_dir / "Documents"
        assert decoy.is_dir(), "fixture should provide the decoy"
        assert PathResolver(windows_system).token("DOCUMENTS") != decoy

    def test_documents_has_no_environment_fallback(self, tmp_path: Path) -> None:
        """No known folder means no answer — guessing would give the wrong one."""
        system = FakeSystem(
            platform=Platform.WINDOWS,
            home_dir=tmp_path,
            env_vars={"USERPROFILE": str(tmp_path)},
        )
        assert PathResolver(system).token("DOCUMENTS") is None

    def test_windows_branch_never_hand_builds_a_documents_path(self) -> None:
        """A source-level guard: the string itself must not appear there."""
        source = inspect.getsource(resolver_module._windows_token)
        code = "\n".join(
            line.split("#", 1)[0]
            for line in source.splitlines()
            if not line.strip().startswith("#")
        )
        # The docstring explains why; strip it before checking for real code.
        code_after_docstring = code.split('"""')[-1]
        assert '"Documents"' not in code_after_docstring
        assert "'Documents'" not in code_after_docstring


class TestMacosTokens:
    def test_appdata_tokens_collapse_to_application_support(
        self, macos_system: FakeSystem
    ) -> None:
        resolver = PathResolver(macos_system)
        expected = macos_system.home_dir / "Library" / "Application Support"
        assert resolver.token("APPDATA") == expected
        assert resolver.token("LOCALAPPDATA") == expected
        assert resolver.token("LOCALLOW") == expected

    def test_saved_games_does_not_exist_here(self, macos_system: FakeSystem) -> None:
        assert PathResolver(macos_system).token("SAVEDGAMES") is None

    def test_library_tokens(self, macos_system: FakeSystem) -> None:
        resolver = PathResolver(macos_system)
        library = macos_system.home_dir / "Library"
        assert resolver.token("PREFS") == library / "Preferences"
        assert resolver.token("CONTAINERS") == library / "Containers"
        assert resolver.token("STEAM") == library / "Application Support" / "Steam"
        assert resolver.token("SAVESMITH_DATA") == library / "Application Support" / "SaveSmith"


class TestExpand:
    def test_unknown_token_names_the_known_ones(self, macos_system: FakeSystem) -> None:
        with pytest.raises(UnknownPathTokenError) as caught:
            PathResolver(macos_system).expand("{LOCALOW}/save.dat")
        assert "LOCALLOW" in caught.value.user_message

    def test_unavailable_token_makes_the_pattern_unavailable(
        self, macos_system: FakeSystem
    ) -> None:
        assert PathResolver(macos_system).expand("{SAVEDGAMES}/Game/save.dat") is None

    def test_backslashes_are_accepted(self, macos_system: FakeSystem) -> None:
        expanded = PathResolver(macos_system).expand(r"{DOCUMENTS}\My Games\save.dat")
        assert expanded == macos_system.home_dir / "Documents" / "My Games" / "save.dat"

    def test_wineuser_outside_a_bottle_is_an_error_not_a_guess(
        self, macos_system: FakeSystem
    ) -> None:
        with pytest.raises(PathResolutionError) as caught:
            PathResolver(macos_system).expand("{HOME}/users/{WINEUSER}/save.dat")
        assert "bottle" in caught.value.user_message

    def test_extra_tokens_win(self, macos_system: FakeSystem) -> None:
        resolver = PathResolver(macos_system, extra_tokens={"WINEUSER": "danil"})
        expanded = resolver.expand("{HOME}/users/{WINEUSER}/save.dat")
        assert expanded == macos_system.home_dir / "users" / "danil" / "save.dat"

    def test_expand_does_not_touch_the_filesystem(self, macos_system: FakeSystem) -> None:
        expanded = PathResolver(macos_system).expand("{DOCUMENTS}/Nothing Here/save.dat")
        assert expanded is not None and not expanded.exists()


class TestResolve:
    @pytest.fixture
    def saves(self, windows_system: FakeSystem) -> Path:
        folder = (
            windows_system.known_folders[KnownFolder.LOCAL_APPDATA_LOW]
            / "Team Cherry"
            / "Hollow Knight"
        )
        folder.mkdir(parents=True)
        (folder / "user1.dat").write_bytes(b"a")
        (folder / "user2.dat").write_bytes(b"b")
        (folder / "user1.dat.bak").write_bytes(b"c")
        return folder

    def test_glob_matches_and_sorts(self, windows_system: FakeSystem, saves: Path) -> None:
        found = PathResolver(windows_system).resolve(
            "{LOCALLOW}/Team Cherry/Hollow Knight/user*.dat"
        )
        assert [path.name for path in found] == ["user1.dat", "user2.dat"]

    def test_matching_ignores_case(self, windows_system: FakeSystem, saves: Path) -> None:
        """A Wine-written save may be User1.DAT on a case-sensitive volume."""
        (saves / "User3.DAT").write_bytes(b"d")
        found = PathResolver(windows_system).resolve(
            "{LOCALLOW}/team cherry/HOLLOW KNIGHT/user*.dat"
        )
        # Sorted case-insensitively: which of "User3.DAT" and "user1.dat" comes
        # first is the filesystem's business, and it differs between a
        # case-sensitive volume and Windows. What matters is that all three
        # were found through a differently-cased pattern.
        assert sorted(path.name.lower() for path in found) == [
            "user1.dat",
            "user2.dat",
            "user3.dat",
        ]

    def test_missing_folder_is_an_empty_list_not_an_error(
        self, windows_system: FakeSystem
    ) -> None:
        assert PathResolver(windows_system).resolve("{LOCALLOW}/Nobody/save.dat") == []

    def test_unavailable_token_yields_nothing(self, macos_system: FakeSystem) -> None:
        assert PathResolver(macos_system).resolve("{SAVEDGAMES}/Game/*.sav") == []

    def test_recursive_globs_are_rejected_clearly(self, windows_system: FakeSystem) -> None:
        with pytest.raises(PathResolutionError) as caught:
            PathResolver(windows_system).resolve("{LOCALLOW}/**/save.dat")
        assert "**" in str(caught.value)

    def test_a_file_in_the_middle_of_a_pattern_does_not_crash(
        self, windows_system: FakeSystem, saves: Path
    ) -> None:
        found = PathResolver(windows_system).resolve(
            "{LOCALLOW}/Team Cherry/Hollow Knight/user1.dat/deeper/*.dat"
        )
        assert found == []

    def test_results_are_unique(self, windows_system: FakeSystem, saves: Path) -> None:
        found = PathResolver(windows_system).resolve(
            "{LOCALLOW}/Team Cherry/Hollow Knight/user?.dat"
        )
        assert len(found) == len(set(found))


class TestAllTokens:
    def test_diagnostics_view_covers_every_token(self, macos_system: FakeSystem) -> None:
        table = PathResolver(macos_system).all_tokens()
        assert set(table) == {token.value for token in PathToken}

    def test_wineuser_is_listed_but_empty(self, macos_system: FakeSystem) -> None:
        assert PathResolver(macos_system).all_tokens()["WINEUSER"] is None


class TestExtendedLengthPath:
    def test_short_paths_are_untouched(self) -> None:
        assert extended_length_path(Path("C:/Games/save.dat")) == str(Path("C:/Games/save.dat"))

    def test_long_windows_path_gets_the_prefix(self) -> None:
        long_path = Path("C:/" + "/".join(["directory"] * 30) + "/save.dat")
        result = extended_length_path(long_path)
        assert result.startswith("\\\\?\\")
        assert "/" not in result

    def test_long_posix_path_is_untouched(self) -> None:
        long_path = Path("/" + "/".join(["directory"] * 30) + "/save.dat")
        assert extended_length_path(long_path) == str(long_path)

    def test_already_prefixed_path_is_not_doubled(self) -> None:
        raw = "\\\\?\\C:\\" + "\\".join(["directory"] * 30)
        assert extended_length_path(Path(raw)) == raw


class TestRegistryHiveNames:
    def test_values_match_winreg_constants(self) -> None:
        """registry_read does getattr(winreg, hive.value); a typo here is silent."""
        assert RegistryHive.HKCU.value == "HKEY_CURRENT_USER"
        assert RegistryHive.HKLM.value == "HKEY_LOCAL_MACHINE"
