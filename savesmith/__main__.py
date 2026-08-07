"""Entry point for ``python -m savesmith`` and for the packaged binary."""

from savesmith.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
