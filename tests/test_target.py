"""Whatever the user pointed at, turned into a game to look into.

The cases here are the ones people actually type: the thing they double-click,
which is almost never the folder the old interface asked for.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from savesmith.core.errors import SaveSmithError
from savesmith.core.target import resolve


def make_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ\x90\x00")
    return path


class TestExecutables:
    def test_an_exe_names_the_game_and_gives_its_folder(self, tmp_path: Path) -> None:
        exe = make_exe(tmp_path / "Coin Quest" / "CoinQuest.exe")
        target = resolve(exe)
        assert target.folder == tmp_path / "Coin Quest"
        assert target.name == "CoinQuest"

    def test_climbs_out_of_unreal_packaging(self, tmp_path: Path) -> None:
        """``<install>/Project/Binaries/Win64/Game.exe`` is four levels deep."""
        exe = make_exe(
            tmp_path / "The Invincible" / "Invincible" / "Binaries" / "Win64" / "Invincible.exe"
        )
        target = resolve(exe)
        assert target.folder == tmp_path / "The Invincible" / "Invincible"
        assert target.name == "Invincible"

    def test_climbs_out_of_a_game_subfolder(self, tmp_path: Path) -> None:
        """How Steam ships Elden Ring: the exe sits in ``Game/``."""
        exe = make_exe(tmp_path / "ELDEN RING" / "Game" / "eldenring.exe")
        target = resolve(exe)
        assert target.folder == tmp_path / "ELDEN RING"
        assert target.name == "eldenring"
        assert target.note is not None  # the climb is explained, not silent

    def test_an_uninstaller_gives_the_folder_but_not_the_name(self, tmp_path: Path) -> None:
        """It says where the game is and nothing about what it is called."""
        exe = make_exe(tmp_path / "Coin Quest" / "unins000.exe")
        target = resolve(exe)
        assert target.folder == tmp_path / "Coin Quest"
        assert target.name is None


class TestFolders:
    def test_a_folder_is_taken_as_it_is(self, tmp_path: Path) -> None:
        folder = tmp_path / "Coin Quest"
        folder.mkdir()
        assert resolve(folder).folder == folder

    def test_a_missing_path_says_so_in_a_sentence(self, tmp_path: Path) -> None:
        with pytest.raises(SaveSmithError) as problem:
            resolve(tmp_path / "nowhere")
        assert "nothing at this path" in problem.value.user_message


class TestSaveFiles:
    def test_a_plain_file_is_the_save_itself(self, tmp_path: Path) -> None:
        save = tmp_path / "ER0000.sl2"
        save.write_bytes(b"BND4")
        target = resolve(save)
        assert target.save_file == save
        assert target.folder == tmp_path


class TestMacApplications:
    def test_a_native_application_is_named_by_its_plist(self, tmp_path: Path) -> None:
        bundle = tmp_path / "Coin Quest.app"
        contents = bundle / "Contents"
        contents.mkdir(parents=True)
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleName": "Coin Quest"}, stream)
        target = resolve(bundle)
        assert target.folder == bundle
        assert target.name == "Coin Quest"
        assert target.bottle is None

    def test_a_wineskin_wrapper_points_inside_the_bottle(self, tmp_path: Path) -> None:
        """The double-clickable ``.app`` is a whole Windows machine in a box."""
        bundle = tmp_path / "elden ring.app"
        prefix = bundle / "Contents" / "SharedSupport" / "prefix"
        (prefix / "drive_c").mkdir(parents=True)
        (prefix / "system.reg").write_text("WINE REGISTRY Version 2\n")

        target = resolve(bundle)
        assert target.folder == prefix
        assert target.bottle == prefix
        assert target.note is not None and "bottle" in target.note
        # Named the way Finder names it, so the game list and this agree.
        assert target.name == "elden ring"

    def test_a_bundle_without_a_plist_falls_back_to_its_name(self, tmp_path: Path) -> None:
        bundle = tmp_path / "Coin Quest.app"
        (bundle / "Contents").mkdir(parents=True)
        assert resolve(bundle).name == "Coin Quest"
