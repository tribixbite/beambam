#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lan_upload.py (`beambam-upload`) — FTPS-only upload of a .gcode.3mf to a
Bambu Lab printer on the local network. Sends ZERO MQTT messages, so the
printer does NOT start anything: the file appears in the printer's Files
browser and is started from the touchscreen, or with
`beambam print FILE --no-upload` after an upload with `--cache`.

The transfer itself is `beambam.ftps.upload_file`, the single FTPS path
beambam uses everywhere: implicit TLS on 990, user `bblp` + the access
code, the session-reusing data channel the X2D firmware requires, and the
TLS settings that avoid its INVALID_ALERT failure. This script used to
carry its own FTP_TLS subclass with a bare `SSLContext(PROTOCOL_TLS_CLIENT)`,
exactly the setup `beambam/ftps.py` documents as failing on the X2D.

Usage:
    beambam-upload --ip 192.168.1.42 --access-code 12345678 \\
        --serial 0123456789ABCDEF --file model.gcode.3mf [--cache]

Credentials may also come from the usual sources (`--printer NAME`, X2D_*
env vars or .env, ~/.x2d/credentials) when --ip/--access-code are omitted.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("lan_upload")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="beambam-upload",
        description="FTPS-upload a .gcode.3mf to a Bambu printer (no print start).",
    )
    parser.add_argument("--ip", default=None, help="Printer IP (default: beambam credentials)")
    parser.add_argument("--access-code", dest="code", default=None,
                        help="8-digit LAN access code (default: beambam credentials)")
    parser.add_argument("--serial", default=None,
                        help="Printer serial (only used to pick credentials)")
    parser.add_argument("--printer", default=None,
                        help="Named [printer:NAME] section of the credentials file")
    parser.add_argument("--file", required=True, type=Path, help=".gcode.3mf to upload")
    parser.add_argument("--remote-name", default=None,
                        help="Filename on the printer (default: the local basename)")
    parser.add_argument("--cache", action="store_true",
                        help="Upload into cache/, where `beambam print --no-upload` "
                             "(and the X2D's LAN print start) expects it")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    from beambam.console import utf8_stdio
    utf8_stdio()
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    from beambam.config import Creds
    from beambam.ftps import upload_file

    if not args.file.is_file():
        log.error("file not found: %s", args.file)
        return 2
    # Explicit --ip + --access-code are enough for FTPS (the serial only
    # matters for MQTT); otherwise resolve from env / .env / the INI.
    if args.ip and args.code:
        creds = Creds(ip=args.ip, code=args.code, serial=args.serial or "")
    else:
        creds = Creds.resolve(argparse.Namespace(ip=args.ip, code=args.code,
                                                 serial=args.serial, printer=args.printer))
    remote_dir = "cache" if args.cache else None
    try:
        upload_file(creds, args.file, remote_name=args.remote_name, remote_dir=remote_dir)
    except SystemExit as e:          # upload_file reports user errors via SystemExit
        log.error("Upload failed: %s", e)
        return 1
    except Exception as e:  # noqa: BLE001 — top-level CLI handler
        log.error("Upload failed: %s", e)
        return 1
    where = f"{remote_dir}/" if remote_dir else ""
    log.info("Uploaded %s -> %s:/%s%s. The printer will NOT auto-start.",
             args.file.name, creds.ip, where, args.remote_name or args.file.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
