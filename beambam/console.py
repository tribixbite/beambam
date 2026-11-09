"""beambam.console — make a process's text output safe on any console.

On Windows the default console/pipe encoding is cp1252, which cannot encode the
em dashes, arrows, ≈ and degree signs in beambam's help and status text: `--help`
of any script then dies with UnicodeEncodeError, as soon as output is redirected
(a parent process reading the pipe, e.g. pokefit). Every entry point calls
:func:`utf8_stdio` first.
"""
from __future__ import annotations

import sys

__all__ = ["utf8_stdio"]


def utf8_stdio() -> None:
    """Switch stdout/stderr to UTF-8 (no-op where they already are or can't be)."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
