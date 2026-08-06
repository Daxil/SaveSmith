"""Error hierarchy.

Every error that can reach a user carries two separate texts:

``user_message``
    One or two sentences a non-technical person can act on. No jargon, no
    tracebacks, no file offsets. This is what the GUI shows.
``detail``
    Everything an engineer needs: paths, offsets, the underlying exception.
    Goes to the log, never to the main UI surface.

``code`` is a stable machine-readable identifier. The UI layer will use it to
look up a localized string later; until then ``user_message`` is the English
fallback and the only text we ship.
"""

from __future__ import annotations

from typing import Any, ClassVar

_TERMINATORS = (".", "?", "!")


def _as_sentence(text: str) -> str:
    """Make sure a message reads as a finished sentence.

    Reasons are handed in by callers as fragments ("it has no drive_c folder"),
    and requiring every call site to remember the full stop guarantees that
    some of them will not.
    """
    stripped = text.rstrip()
    if stripped and not stripped.endswith(_TERMINATORS):
        return stripped + "."
    return stripped


class SaveSmithError(Exception):
    """Base class for everything SaveSmith raises on purpose.

    Anything escaping the core that is *not* a subclass of this is a bug: it
    means a raw OSError or ValueError reached the UI with a message written for
    a developer.
    """

    code: ClassVar[str] = "error"

    def __init__(
        self,
        user_message: str,
        *,
        detail: str | None = None,
        **context: Any,
    ) -> None:
        user_message = _as_sentence(user_message)
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail
        self.context: dict[str, Any] = context

    def __str__(self) -> str:
        if self.detail:
            return f"{self.user_message} [{self.detail}]"
        return self.user_message

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, user_message={self.user_message!r})"


class UnsupportedPlatformError(SaveSmithError):
    """Raised when running somewhere we deliberately do not support."""

    code: ClassVar[str] = "unsupported_platform"

    def __init__(self, platform_name: str, *, detail: str | None = None) -> None:
        super().__init__(
            f"SaveSmith does not support {platform_name}. "
            f"Supported systems are Windows 10 or later and macOS 13 or later.",
            detail=detail,
            platform=platform_name,
        )


class PathResolutionError(SaveSmithError):
    """A path pattern could not be turned into a real location.

    Not for "the folder does not exist" — that is an ordinary empty result.
    This means the pattern itself is wrong, e.g. an unknown ``{TOKEN}``.
    """

    code: ClassVar[str] = "path_resolution_failed"

    def __init__(self, pattern: str, reason: str, *, detail: str | None = None) -> None:
        super().__init__(
            f"Could not work out where this location points: {pattern}. {reason}",
            detail=detail,
            pattern=pattern,
        )


class UnknownPathTokenError(PathResolutionError):
    """A plugin used a ``{TOKEN}`` the resolver has never heard of."""

    code: ClassVar[str] = "unknown_path_token"

    def __init__(self, token: str, pattern: str, known_tokens: tuple[str, ...] = ()) -> None:
        hint = ""
        if known_tokens:
            hint = " Known tokens: " + ", ".join(sorted(known_tokens)) + "."
        super().__init__(
            pattern,
            f"The plugin refers to {{{token}}}, which this version of SaveSmith "
            f"does not know.{hint} The plugin probably needs a newer version of the app.",
            detail=f"unknown token {token!r} in pattern {pattern!r}",
        )
        self.token = token


class VdfParseError(SaveSmithError):
    """A Valve KeyValues file could not be parsed.

    Callers that know which file it was should catch this and re-raise
    something more specific — :class:`SteamDataError` for Steam's own files.
    """

    code: ClassVar[str] = "vdf_parse_failed"

    def __init__(self, source: str, reason: str, *, line: int | None = None) -> None:
        where = f"{source}:{line}" if line is not None else source
        super().__init__(
            f"A Valve configuration file is damaged and could not be read: {reason}",
            detail=f"{where}: {reason}",
            source=source,
            line=line,
        )
        self.line = line


class PluginValidationError(SaveSmithError):
    """A plugin is malformed and was not loaded.

    Aimed at whoever wrote the plugin, but a user may see it after installing
    one, so it still has to say which plugin and what is wrong with it.
    """

    code: ClassVar[str] = "plugin_invalid"

    def __init__(self, plugin: str, where: str, reason: str) -> None:
        super().__init__(
            f"The plugin '{plugin}' is not valid and was not loaded: {where} {reason}",
            detail=f"{plugin}: {where} {reason}",
            plugin=plugin,
            where=where,
        )
        self.plugin = plugin
        self.where = where


class BackupError(SaveSmithError):
    """A backup could not be made, so the edit did not happen.

    Deliberately fatal to the write: an edit without a backup is the one thing
    SaveSmith must never do.
    """

    code: ClassVar[str] = "backup_failed"

    def __init__(self, source: str, reason: str, *, detail: str | None = None) -> None:
        super().__init__(
            f"A backup of the save file could not be made, so nothing was changed. {reason}",
            detail=detail or f"{source}: {reason}",
            source=source,
        )


class SaveInUseError(SaveSmithError):
    """The game is running, or something else holds the file open."""

    code: ClassVar[str] = "save_file_in_use"

    def __init__(self, path: str, *, detail: str | None = None) -> None:
        super().__init__(
            "The save file is in use. Close the game completely and try again — "
            "editing a save while the game is running loses the change as soon "
            "as the game saves over it.",
            detail=detail or path,
            path=path,
        )


class FieldPathError(SaveSmithError):
    """A field the plugin describes is not present in this particular save.

    Normal and expected: saves differ by game version and by how far the player
    has got. The editor hides such fields instead of failing.
    """

    code: ClassVar[str] = "field_not_in_save"

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(
            f"This save has no value at '{path}': {reason} "
            f"The game version may differ from the one the plugin was made for.",
            detail=f"{path}: {reason}",
            path=path,
        )
        self.path = path


class FieldValueError(SaveSmithError):
    """A value the user typed is not acceptable for this field."""

    code: ClassVar[str] = "field_value_rejected"

    def __init__(self, label: str, reason: str) -> None:
        # No separate detail: there is no technical context here beyond the
        # sentence the user is already reading.
        super().__init__(f"{label}: {reason}", label=label)
        self.label = label


class UnknownOperationError(SaveSmithError):
    """A plugin asks for a pipeline step this build does not implement."""

    code: ClassVar[str] = "unknown_operation"

    def __init__(self, operation: str, known: tuple[str, ...] = ()) -> None:
        hint = f" This build knows: {', '.join(sorted(known))}." if known else ""
        super().__init__(
            f"This game's plugin needs a decoding step called '{operation}' that this "
            f"version of SaveSmith does not have.{hint} Updating SaveSmith should fix it.",
            detail=f"unknown op {operation!r}",
            operation=operation,
        )
        self.operation = operation


class PipelineError(SaveSmithError):
    """A decoding or re-encoding step failed on this particular file."""

    code: ClassVar[str] = "pipeline_step_failed"

    def __init__(
        self,
        step_index: int,
        operation: str,
        reason: str,
        *,
        detail: str | None = None,
    ) -> None:
        super().__init__(
            f"This save file could not be read: step {step_index + 1} "
            f"({operation}) failed because {reason}",
            detail=detail or f"step {step_index} ({operation}): {reason}",
            step_index=step_index,
            operation=operation,
        )
        self.step_index = step_index
        self.operation = operation


class SteamNotFoundError(SaveSmithError):
    """No Steam installation on this machine, or it is somewhere unusual."""

    code: ClassVar[str] = "steam_not_found"

    def __init__(self, *, searched: tuple[str, ...] = (), detail: str | None = None) -> None:
        super().__init__(
            "Could not find a Steam installation. If Steam is installed in an unusual "
            "place, point SaveSmith at its folder manually; otherwise games outside "
            "Steam can still be added by hand.",
            detail=detail or ("searched: " + ", ".join(searched) if searched else None),
            searched=searched,
        )


class SteamDataError(SaveSmithError):
    """Steam is installed but one of its data files could not be read."""

    code: ClassVar[str] = "steam_data_unreadable"

    def __init__(self, path: str, reason: str, *, detail: str | None = None) -> None:
        super().__init__(
            f"Steam's own data file could not be read, so some games may be missing "
            f"from the list. Reason: {reason}",
            detail=detail or f"{path}: {reason}",
            path=path,
        )


class WinePrefixError(SaveSmithError):
    """A Wine/CrossOver/Whisky bottle could not be used."""

    code: ClassVar[str] = "wine_prefix_unusable"

    def __init__(self, path: str, reason: str, *, detail: str | None = None) -> None:
        super().__init__(
            f"This Windows bottle could not be read: {reason}",
            detail=detail or f"{path}: {reason}",
            path=path,
        )


class AmbiguousWineUserError(WinePrefixError):
    """Several Windows users inside one bottle and no way to pick one safely.

    We refuse to guess: picking the wrong user means silently editing the wrong
    save, which is worse than asking.
    """

    code: ClassVar[str] = "wine_user_ambiguous"

    def __init__(self, path: str, candidates: tuple[str, ...]) -> None:
        super().__init__(
            path,
            f"it contains several Windows user profiles ({', '.join(candidates)}) "
            f"and SaveSmith will not guess which one is yours. Pick one in the settings.",
            detail=f"{path}: candidates={candidates!r}",
        )
        self.candidates = candidates
