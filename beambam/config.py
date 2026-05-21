"""beambam.config — printer credentials + env/file resolution.

Re-exports the existing Creds class from x2d_bridge for stable
import paths going forward. v1.3.0 will move the implementation
inline; the public surface stays this:

    from beambam.config import Creds

    # 1) explicit
    Creds(ip="192.168.1.42", code="XXXXXXXX", serial="20P9...")
    # 2) auto-resolve from $X2D_IP/$X2D_CODE/$X2D_SERIAL or
    #    ~/.x2d/credentials [printer:<name>]
    Creds.resolve_default()
    # 3) named section
    Creds.from_section("studio")
    # 4) list all sections
    Creds.list_names()
"""
from __future__ import annotations

import argparse

from x2d_bridge import Creds as _Creds

__all__ = ["Creds"]


class Creds(_Creds):
    """`x2d_bridge.Creds` with two convenience constructors that don't
    require building an argparse.Namespace by hand."""

    @classmethod
    def resolve_default(cls) -> "Creds":
        """Resolve from env vars or ~/.x2d/credentials [printer] section."""
        return cls.resolve(argparse.Namespace(
            ip=None, code=None, serial=None, printer=None,
        ))

    @classmethod
    def from_section(cls, name: str) -> "Creds":
        """Resolve from a specific [printer:NAME] section."""
        return cls.resolve(argparse.Namespace(
            ip=None, code=None, serial=None, printer=name,
        ))
