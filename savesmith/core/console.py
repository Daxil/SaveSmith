"""Making sure what SaveSmith prints can actually be printed.

Python picks the encoding for standard output from the locale, and on Windows
that is usually ``cp1252`` or ``cp1251`` — not UTF-8. Anything outside that
codepage then raises ``UnicodeEncodeError`` mid-line and the program dies with
a traceback instead of an answer.

This is not a corner case for this program. Two of its most ordinary outputs
are unprintable under cp1252:

* the arrow in ``gzip → json_parse`` and in every ``before → after``;
* **all Russian text**, which is most of what ``--language ru`` exists to
  print. cp1252 has no Cyrillic at all, so the Russian half of the product
  would crash on its first line.

So both streams are switched to UTF-8 before anything is written, with
``errors="replace"``: a terminal that cannot render an arrow should show a
question mark, never a stack trace. This does not change what a Mac does — it
is already UTF-8 — so there is no platform branch here, and none is wanted.
"""

from __future__ import annotations

from typing import IO, Any


def use_utf8(*streams: IO[str] | Any) -> None:
    """Switch streams to UTF-8, quietly doing nothing where that is impossible.

    Called once at every entry point. Streams that cannot be reconfigured —
    a pytest capture object, a plain ``StringIO``, a stream someone already
    detached — are left exactly as they are: printing something is the goal,
    and failing to set an encoding is not a reason to refuse to run.
    """
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError, AttributeError):
            continue
