"""beambam.ftps — FTPS-implicit-TLS helpers for the printer's SD card.

Re-exports the file transfer functions from x2d_bridge. Stable import
path going forward:

    from beambam.ftps import upload_file, download_file, list_files

    upload_file(creds, Path("model.gcode.3mf"))            # local → /
    download_file(creds, "/cache/x.3mf", Path("./x.3mf"))  # → bytes written
    list_files(creds, "")                                  # root listing
    list_files(creds, "cache")                             # /cache listing

All three speak Bambu's vsFTPd implicit-TLS dialect on port 990 with
session-reuse on PASV (the firmware requires this since 2024). The
download_file path uses the TLSv1.2 + create_default_context()
workaround for the INVALID_ALERT bug that fires mid-print
(see x2d_bridge:591 docstring).

For most callers `beambam.Printer.{upload,download,list_files}` is
the better entry point — it's a thin wrapper that doesn't drop down
to raw FTPS.
"""
from __future__ import annotations

from x2d_bridge import (
    download_file,
    list_files,
    upload_file,
)

__all__ = ["download_file", "list_files", "upload_file"]
