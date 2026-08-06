"""Windows-native lookups: known folders and the registry.

Two rules here.

**Known folders, never hand-built paths.** ``%USERPROFILE%\\Documents`` is wrong
on any machine where OneDrive is on — it silently points at the empty local
folder while the real one lives under ``%USERPROFILE%\\OneDrive\\Documents``.
That single mistake is the most common reason a save editor reports "no saves
found". ``SHGetKnownFolderPath`` follows the redirect; nothing else does.

**No pywin32.** Everything needed is three ctypes calls and the stdlib
``winreg``. pywin32 would add tens of megabytes to the PyInstaller sidecar and
a post-install step, for no capability we use.

This module imports cleanly on macOS — the functions raise if actually called
there — so the rest of the package does not need conditional imports.
"""

from __future__ import annotations

import ctypes
import importlib
from enum import StrEnum
from pathlib import Path
from typing import Any

from savesmith.core.errors import UnsupportedPlatformError


class KnownFolder(StrEnum):
    """The Windows known folders SaveSmith asks about.

    Values double as the token names used in plugin manifests where they match.
    """

    ROAMING_APPDATA = "roaming_appdata"
    LOCAL_APPDATA = "local_appdata"
    LOCAL_APPDATA_LOW = "local_appdata_low"
    DOCUMENTS = "documents"
    SAVED_GAMES = "saved_games"
    PROFILE = "profile"
    PUBLIC = "public"


class RegistryHive(StrEnum):
    HKCU = "HKEY_CURRENT_USER"
    HKLM = "HKEY_LOCAL_MACHINE"


# FOLDERID GUIDs from the Windows SDK (KnownFolders.h).
_FOLDER_GUIDS: dict[KnownFolder, str] = {
    KnownFolder.ROAMING_APPDATA: "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",
    KnownFolder.LOCAL_APPDATA: "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    KnownFolder.LOCAL_APPDATA_LOW: "{A520A1A4-1780-4FF6-BD18-167343C5AF16}",
    KnownFolder.DOCUMENTS: "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    KnownFolder.SAVED_GAMES: "{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}",
    KnownFolder.PROFILE: "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
    KnownFolder.PUBLIC: "{DFDF76A2-C82A-4D63-906A-5644AC457385}",
}


class _GUID(ctypes.Structure):
    """Windows GUID. Safe to define off-Windows: no wintypes involved."""

    _fields_ = (
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    )


def _guid_from_string(text: str) -> _GUID:
    raw = text.strip("{}").replace("-", "")
    if len(raw) != 32:
        raise ValueError(f"not a GUID: {text!r}")
    data = bytes.fromhex(raw)
    guid = _GUID()
    guid.Data1 = int.from_bytes(data[0:4], "big")
    guid.Data2 = int.from_bytes(data[4:6], "big")
    guid.Data3 = int.from_bytes(data[6:8], "big")
    guid.Data4 = (ctypes.c_ubyte * 8)(*data[8:16])
    return guid


def _windll(library: str) -> Any:
    """Load a Windows DLL, or fail with a sentence instead of an AttributeError.

    ``getattr`` rather than ``ctypes.windll`` directly: typeshed hides ``windll``
    off-Windows, and writing a ``type: ignore`` here would itself be flagged as
    unused when mypy runs on the Windows CI job.
    """
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise UnsupportedPlatformError(
            "this operating system",
            detail=f"ctypes.windll unavailable; cannot load {library}",
        )
    return getattr(windll, library)


def known_folder_path(folder: KnownFolder) -> Path | None:
    """Ask Windows where a known folder actually is.

    Returns ``None`` when the folder does not exist on this machine — Saved
    Games is genuinely absent on some Windows editions, and that is a normal
    empty result, not an error.
    """
    guid = _guid_from_string(_FOLDER_GUIDS[folder])

    shell32 = _windll("shell32")
    get_path = shell32.SHGetKnownFolderPath
    # Explicit signatures matter: without them ctypes truncates the 64-bit
    # pointer and the call returns garbage on 64-bit Windows.
    get_path.argtypes = (
        ctypes.POINTER(_GUID),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    )
    get_path.restype = ctypes.c_long  # HRESULT

    buffer = ctypes.c_wchar_p()
    hresult = get_path(ctypes.byref(guid), 0, None, ctypes.byref(buffer))
    if hresult != 0:
        return None
    try:
        value = buffer.value
    finally:
        free = _windll("ole32").CoTaskMemFree
        free.argtypes = (ctypes.c_void_p,)
        free.restype = None
        free(buffer)
    return Path(value) if value else None


def registry_read(hive: RegistryHive, key: str, value_name: str) -> str | None:
    """Read a single string value. Returns ``None`` if key or value is absent.

    Read-only by design: milestone 1 never writes to the registry, and the two
    hives in :class:`RegistryHive` are the only ones we touch.
    """
    # importlib rather than a plain import: winreg does not exist off-Windows,
    # and this keeps the module importable there without conditional imports.
    try:
        winreg = importlib.import_module("winreg")
    except ImportError as exc:
        raise UnsupportedPlatformError(
            "this operating system",
            detail=f"winreg unavailable: {exc}",
        ) from exc

    # RegistryHive values are the winreg constant names, so no mapping table.
    hive_handle = getattr(winreg, hive.value)
    try:
        with winreg.OpenKey(hive_handle, key, 0, winreg.KEY_READ) as handle:
            value, _value_type = winreg.QueryValueEx(handle, value_name)
    except FileNotFoundError:
        return None
    except OSError:
        # Permission denied, corrupted hive, redirected view — all mean
        # "no answer", and none of them should crash a save scan.
        return None
    if value is None:
        return None
    return str(value)
