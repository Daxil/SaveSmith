"""Atomic, reversible steps a save format is built from.

Importing this package registers every built-in operation. Plugins name them in
their ``pipeline``; nothing else in the codebase should special-case a format.
"""

from savesmith.core.ops._registry import (
    Hints,
    Operation,
    Params,
    all_operations,
    get,
    names,
    register,
)

# Imported for the side effect of registering their operations.
from savesmith.core.ops import (  # noqa: F401  isort:skip
    binary,
    compress,
    crypto,
    gvas,
    packed,
    structured,
)

__all__ = [
    "Hints",
    "Operation",
    "Params",
    "all_operations",
    "get",
    "names",
    "register",
]
