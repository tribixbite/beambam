"""beambam.cli.lan — LAN-direct file-transfer handlers.

Phase 5d scaffold (`docs/BRIDGE_SPLIT_PLAN.md`). Hosts the LAN-side
file-mover handlers that don't fit the print/control taxonomy in
`beambam.cli.control`:

  cmd_upload   — FTPS-implicit-TLS upload (.gcode.3mf → printer:/sdcard)
  cmd_files    — list SD-card files via the runtime/network_shim
                 FileTunnel (vsFTPd on port 990; #92 details the
                 X2D firmware's surface)

The LAN print verbs (cmd_print + cmd_slice_print) still live in
x2d_bridge.py because they reach deeper into bridge internals (Creds
resolution + safety derivation from 3mf + signing + MQTT publish). A
later Phase 5d batch will move them once the dependency surface is
trimmed.

x2d_bridge.py re-exports each handler so external callers + tests
keep working unchanged.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_upload(args: argparse.Namespace) -> int:
    """FTPS-implicit-TLS upload of a single file to the printer's
    SD card. `--remote <name>` overrides the destination filename;
    by default the source basename is used."""
    from beambam.config import Creds
    from beambam.ftps import upload_file

    creds = Creds.resolve(args)
    upload_file(creds, Path(args.file), remote_name=args.remote)
    print(f"uploaded {args.file} -> {creds.ip}:/"
          f"{args.remote or Path(args.file).name}")
    return 0


def cmd_files(args: argparse.Namespace) -> int:
    """List SD-card files via FTPS — see runtime/network_shim/file_tunnel.py.
    Empirical finding (#92): X2D firmware exposes its SD card via vsFTPd
    on port 990, NOT the BambuTunnel:6000 protocol that older Bambu
    printers + BambuStudio source assume."""
    import json as _json
    from beambam.config import Creds

    creds = Creds.resolve(args)
    try:
        from runtime.network_shim.file_tunnel import (
            FileTunnelClient, FileTunnelError,
        )
    except ImportError as e:
        sys.exit(f"file_tunnel module missing: {e}")

    try:
        with FileTunnelClient(creds.ip, creds.code) as cli:
            files = cli.list_files(args.kind)
    except FileTunnelError as e:
        sys.exit(f"file_tunnel: {e}")
    except OSError as e:
        sys.exit(f"socket error: {e}")

    if args.json:
        print(_json.dumps(
            [{"name": f.name, "path": f.path, "time": f.time,
              "size": f.size, "is_dir": f.is_dir}
             for f in files], indent=2,
        ))
    else:
        if not files:
            print(f"(no {args.kind} files)")
        for f in files:
            print(f)
    return 0
