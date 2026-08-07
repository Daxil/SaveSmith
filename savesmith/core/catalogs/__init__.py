"""Readers that get item names and pictures out of an installed game.

Importing this package registers them. Each one is keyed by the name a
manifest's container puts in its ``catalog`` field.
"""

from __future__ import annotations

from savesmith.core.catalogs import rpgmaker

__all__ = ["rpgmaker"]
