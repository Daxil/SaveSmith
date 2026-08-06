"""Test package.

An ``__init__.py`` so test modules can share builders (``write_manifest``,
``make_bottle``) by importing each other, and so mypy sees one package rather
than a pile of same-named top-level modules.
"""
