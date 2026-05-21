"""beambam.ftps — FTPS-implicit-TLS helpers for the printer's SD card.

Stable import path for the file transfer functions:

    from beambam.ftps import upload_file, download_file, list_files

    upload_file(creds, Path("model.gcode.3mf"))            # local → /
    download_file(creds, "/cache/x.3mf", Path("./x.3mf"))  # → bytes written
    list_files(creds, "")                                  # root listing
    list_files(creds, "cache")                             # /cache listing

The actual implementations live in x2d_bridge.py (will move inline in
v1.3.0). We lazy-import to avoid a circular import: x2d_bridge imports
beambam.config (for Creds), and beambam.__init__ imports this module
during package load — eager top-level import here would close the
loop too early.

For most callers `beambam.Printer.{upload,download,list_files}` is
the better entry point — it's a thin wrapper that doesn't drop down
to raw FTPS.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beambam.config import Creds

__all__ = ["download_file", "list_files", "upload_file"]


def upload_file(creds: "Creds", local_path: Path,
                remote_name: str | None = None) -> None:
    """Upload a file to the printer via implicit-TLS FTPS on port 990."""
    from x2d_bridge import upload_file as _impl
    return _impl(creds, local_path, remote_name=remote_name)


def download_file(creds: "Creds", remote_name: str,
                  local_path: Path) -> int:
    """Download a file from the printer's SD card. Returns bytes written."""
    from x2d_bridge import download_file as _impl
    return _impl(creds, remote_name, local_path)


def list_files(creds: "Creds", path: str = "") -> list[str]:
    """List the printer's SD card. Empty path = root."""
    from x2d_bridge import list_files as _impl
    return _impl(creds, path)
