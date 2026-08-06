"""Contract tests for the error hierarchy.

The Definition of Done for milestone 1 says a user must never see a traceback
or a developer sentence. These tests are how that stays true as errors get
added.
"""

from __future__ import annotations

import pytest

from savesmith.core.errors import (
    AmbiguousWineUserError,
    BackupError,
    FieldPathError,
    FieldValueError,
    PathResolutionError,
    PipelineError,
    PluginValidationError,
    SaveInUseError,
    SaveSmithError,
    SteamDataError,
    SteamNotFoundError,
    UnknownOperationError,
    UnknownPathTokenError,
    UnsupportedPlatformError,
    VdfParseError,
    WinePrefixError,
)
from savesmith.core.risk import Acknowledgement
from savesmith.core.session import ConsentRequiredError

# One constructed instance per error class. The registry test below fails if a
# new error class is added without an entry here, so this cannot silently rot.
SAMPLES: list[SaveSmithError] = [
    SaveSmithError("Something went wrong.", detail="internals"),
    UnsupportedPlatformError("Linux"),
    PathResolutionError("{NOPE}/save.dat", "The pattern is malformed."),
    UnknownPathTokenError("NOPE", "{NOPE}/save.dat", known_tokens=("APPDATA", "DOCUMENTS")),
    SteamNotFoundError(searched=("C:\\Program Files (x86)\\Steam",)),
    SteamDataError("libraryfolders.vdf", "the file ends in the middle of a block"),
    VdfParseError("libraryfolders.vdf", "a block that is never closed", line=12),
    PluginValidationError("hollow-knight", "pipeline step 1 (strip_prefix)", "is missing 'bytes'."),
    UnknownOperationError("decrypt_with_magic", known=("gzip", "json_parse")),
    PipelineError(0, "gzip", "the data does not start with a gzip header"),
    BackupError("user1.dat", "Check that there is free disk space."),
    SaveInUseError("/games/user1.dat"),
    FieldPathError("playerData.essence", "the save has no entry called 'essence'."),
    FieldValueError("Health masks", "the largest allowed value is 11."),
    ConsentRequiredError(frozenset({Acknowledgement.BAN_RISK}), steps=(1, 2)),
    WinePrefixError("/bottles/hk", "it has no drive_c folder"),
    AmbiguousWineUserError("/bottles/hk", ("danil", "crossover")),
]


def _all_error_classes() -> set[type[SaveSmithError]]:
    found: set[type[SaveSmithError]] = {SaveSmithError}
    queue = [SaveSmithError]
    while queue:
        for subclass in queue.pop().__subclasses__():
            if subclass not in found:
                found.add(subclass)
                queue.append(subclass)
    return found


def test_every_error_class_has_a_sample() -> None:
    """A new error class without a sample here is an untested user-facing string."""
    covered = {type(sample) for sample in SAMPLES}
    missing = _all_error_classes() - covered
    assert not missing, f"add a sample to SAMPLES for: {sorted(c.__name__ for c in missing)}"


@pytest.mark.parametrize("error", SAMPLES, ids=lambda e: type(e).__name__)
def test_user_message_is_human(error: SaveSmithError) -> None:
    message = error.user_message
    assert message, "user_message must not be empty"
    assert message[0].isupper(), f"should read like a sentence: {message!r}"
    assert message.rstrip().endswith((".", "?", "!")), f"should be a sentence: {message!r}"
    # Symptoms of a developer message leaking into the UI.
    for leak in ("Traceback", "Exception", "0x", "None", "self."):
        assert leak not in message, f"{leak!r} leaked into a user-facing message: {message!r}"


@pytest.mark.parametrize("error", SAMPLES, ids=lambda e: type(e).__name__)
def test_detail_is_separate_from_user_message(error: SaveSmithError) -> None:
    """Technical context belongs in detail, never mixed into the shown text."""
    assert error.detail is None or error.detail != error.user_message


def test_codes_are_unique() -> None:
    codes = [cls.code for cls in _all_error_classes()]
    assert len(codes) == len(set(codes)), f"duplicate error codes: {sorted(codes)}"


def test_str_includes_detail_for_logs() -> None:
    error = SteamDataError("libraryfolders.vdf", "truncated")
    assert "libraryfolders.vdf" in str(error)
    assert "libraryfolders.vdf" not in error.user_message


def test_unknown_token_error_lists_known_tokens() -> None:
    error = UnknownPathTokenError("LOCALOW", "{LOCALOW}/x", known_tokens=("LOCALLOW", "APPDATA"))
    assert "LOCALLOW" in error.user_message
    assert error.token == "LOCALOW"


def test_ambiguous_wine_user_names_the_candidates() -> None:
    """Guessing the wrong profile means editing the wrong save, so we ask."""
    error = AmbiguousWineUserError("/bottles/hk", ("danil", "steamuser"))
    assert "danil" in error.user_message
    assert "steamuser" in error.user_message
    assert isinstance(error, WinePrefixError)


def test_errors_are_catchable_as_the_base_class() -> None:
    with pytest.raises(SaveSmithError):
        raise UnsupportedPlatformError("Linux")
