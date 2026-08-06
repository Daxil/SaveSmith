"""Smoke test: the package imports and the toolchain is wired up."""

import sys

import savesmith


def test_package_imports() -> None:
    assert savesmith.__version__


def test_running_on_pinned_python() -> None:
    """The project pins 3.12; PyInstaller and the crypto deps are proven there."""
    assert sys.version_info[:2] == (3, 12)
