"""beambam.download — `beambam download <remote> [local]` CLI.

Pulls a file from the printer's SD card via FTPS. Complements:

  beambam upload  <local>          # send local file to printer
  beambam fetch   <url>            # download from MakerWorld / Printables / direct URL
  beambam files   [kind]           # list printer's SD card
  beambam analyze <local.3mf>      # dissect a local 3mf

Until this, getting a file off the printer required dropping into the
Python API (`Printer.download(...)`) or running the bridge `serve`
endpoint. The FTPS path is now exposed as a first-class verb.

Examples:

    beambam download '/cache/0.2mm layer, 6 walls, 15% infill.gcode.3mf'
    beambam download /rumi_gold.gcode.3mf rumi.3mf
    beambam download /timelapse/2026-05-20_eevee.mp4 ~/Videos/

If `local` is omitted, writes to ./<basename(remote)>. If `local` is
an existing directory, writes <local>/<basename(remote)>.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    p = sub.add_parser(
        "pull",
        aliases=["download"],
        help="Pull a file off the printer's SD card via FTPS. "
             "Inverse of `push`. (was: download)",
    )
    p.add_argument("remote",
                   help="Remote path on the printer (e.g. /cache/x.3mf, "
                        "/timelapse/2026-05-20.mp4)")
    p.add_argument("local", nargs="?",
                   help="Local path; defaults to ./<basename(remote)>. "
                        "If a directory, writes inside it.")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Don't print the success line")
    p.set_defaults(fn=cmd_download)
    return p


def cmd_download(args: argparse.Namespace) -> int:
    from beambam.config import Creds
    from beambam.ftps import download_file

    try:
        creds = Creds.resolve(argparse.Namespace(
            ip=args.ip, code=args.code, serial=args.serial,
            printer=args.printer,
        ))
    except SystemExit:
        raise
    except Exception as e:                                  # noqa: BLE001
        print(f"can't resolve creds: {e}", file=sys.stderr)
        return 2

    remote = args.remote
    basename = Path(remote).name
    if args.local is None:
        local = Path.cwd() / basename
    else:
        local = Path(args.local).expanduser()
        if local.is_dir() or str(local).endswith("/"):
            local = local / basename

    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        n = download_file(creds, remote, local)
    except Exception as e:                                  # noqa: BLE001
        print(f"download failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if not args.quiet:
        size_str = f"{n:,} B" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MiB"
        print(f"wrote {size_str} → {local}")
    return 0
