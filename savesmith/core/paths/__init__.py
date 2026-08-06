"""Path resolution — the only part of the core that knows what OS it is on.

Everything else takes a :class:`~savesmith.core.platform_.Platform` and a
:class:`SystemFacade` as arguments. That is what lets the Windows behaviour be
tested from a Mac, and it is enforced by
``tests/test_no_platform_branching.py``.
"""

from savesmith.core.paths._system import (
    FakeSystem,
    KnownFolder,
    RealSystem,
    RegistryHive,
    SystemFacade,
)

__all__ = [
    "FakeSystem",
    "KnownFolder",
    "RealSystem",
    "RegistryHive",
    "SystemFacade",
]
