from __future__ import annotations

import json

import pytest

from savesmith.core.errors import UnsupportedPlatformError
from savesmith.core.platform_ import (
    Platform,
    current_platform,
    platform_from_sys,
    require_supported,
)


@pytest.mark.parametrize(
    ("sys_platform", "expected"),
    [
        ("win32", Platform.WINDOWS),
        ("win64", Platform.WINDOWS),
        ("cygwin", Platform.OTHER),
        ("darwin", Platform.MACOS),
        ("linux", Platform.LINUX),
        ("linux2", Platform.LINUX),
        ("freebsd14", Platform.OTHER),
        ("emscripten", Platform.OTHER),
    ],
)
def test_platform_from_sys(sys_platform: str, expected: Platform) -> None:
    assert platform_from_sys(sys_platform) is expected


@pytest.mark.parametrize(
    ("platform", "supported"),
    [
        (Platform.WINDOWS, True),
        (Platform.MACOS, True),
        (Platform.LINUX, False),
        (Platform.OTHER, False),
    ],
)
def test_is_supported(platform: Platform, supported: bool) -> None:
    assert platform.is_supported is supported


def test_current_platform_never_raises() -> None:
    """Detection must work everywhere; only *using* an unsupported OS fails."""
    assert isinstance(current_platform(), Platform)


def test_require_supported_passes_through_supported_platforms() -> None:
    assert require_supported(Platform.WINDOWS) is Platform.WINDOWS
    assert require_supported(Platform.MACOS) is Platform.MACOS


def test_require_supported_rejects_linux_with_a_readable_message() -> None:
    with pytest.raises(UnsupportedPlatformError) as caught:
        require_supported(Platform.LINUX)
    assert "Linux" in caught.value.user_message
    assert "Windows" in caught.value.user_message  # tells the user what *does* work


def test_require_supported_defaults_to_the_current_platform() -> None:
    """CI runs this on Windows and macOS; both must be accepted."""
    assert require_supported() is current_platform()


def test_platform_serialises_as_a_plain_string() -> None:
    """Plugin manifests key their path lists by these names, so the wire format matters."""
    assert Platform.WINDOWS.value == "windows"
    assert Platform.MACOS.value == "macos"
    assert json.dumps({"detect": Platform.WINDOWS}) == '{"detect": "windows"}'
    assert Platform("macos") is Platform.MACOS
