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


def cmd_slice_print(args: argparse.Namespace) -> int:
    """One-shot pipeline: slice an STL with x2d_slice.py + upload + start
    print on the configured X2D. Resolves #99 in IMPROVEMENTS.md.

    Equivalent to:
        x2d_slice.py model.stl --out tmp.gcode.3mf
        x2d_bridge.py print tmp.gcode.3mf

    With --dry-run, slices but doesn't upload — useful for testing.
    """
    import os
    import shutil
    import subprocess
    import tempfile
    import zipfile as _zf
    from beambam.config import Creds
    from beambam.ftps import upload_file
    from beambam.mqtt import X2DClient
    from beambam.print_job import (
        _derive_print_params_from_3mf,
        _validate_ams_slot,
        start_print,
    )
    # X2D_ROOT_PATH stays in x2d_bridge as a single source of truth for
    # the install root; cmd_slice_print subprocesses back into the
    # slicer + (when --3mf input) BambuStudio under it.
    from x2d_bridge import X2D_ROOT_PATH

    stl = Path(args.stl)
    if not stl.exists():
        sys.exit(f"input not found: {stl}")
    if stl.suffix.lower() not in (".stl", ".step", ".stp", ".obj",
                                    ".3mf"):
        sys.exit(f"unsupported input extension: {stl.suffix}")

    # Slice into a temp .gcode.3mf using x2d_slice.py
    slice_bin = X2D_ROOT_PATH / "x2d_slice.py"
    if not slice_bin.exists():
        sys.exit(f"x2d_slice.py not found at {slice_bin}")

    with tempfile.TemporaryDirectory(prefix="x2d_sp_") as td:
        out_3mf = Path(td) / f"{stl.stem}.gcode.3mf"
        if stl.suffix.lower() == ".3mf":
            # Already a 3mf — just re-slice via BS CLI directly to
            # refresh metadata.
            print(f"[slice-print] input already a 3mf, re-slicing in "
                  f"place", file=sys.stderr)
            bs_bin = (X2D_ROOT_PATH / "bs-bionic" / "build" / "src"
                      / "bambu-studio")
            rc = subprocess.call([
                str(bs_bin), "--slice", "0",
                "--outputdir", str(out_3mf.parent),
                "--export-3mf", out_3mf.name,
                str(stl),
            ], env={**os.environ,
                    "DISPLAY": os.environ.get("DISPLAY", ":1")})
        else:
            # STL/OBJ/STEP — graft into template and slice
            cmd = [str(slice_bin), str(stl), "--out", str(out_3mf)]
            if args.template:
                cmd.extend(["--template", str(args.template)])
            if args.scale and args.scale != 1.0:
                cmd.extend(["--scale", str(args.scale)])
            if getattr(args, "scale_pct", None) is not None:
                cmd.extend(["--scale-pct", str(args.scale_pct)])
            if getattr(args, "mm", None) is not None:
                cmd.extend(["--mm", str(args.mm)])
            if getattr(args, "copies", 1) and int(args.copies) != 1:
                cmd.extend(["--copies", str(int(args.copies))])
            if args.color:
                cmd.extend(["--color", args.color])
            rc = subprocess.call(cmd)
        if rc != 0:
            sys.exit(f"slicing failed rc={rc}")
        if not out_3mf.exists():
            sys.exit("slicing reported success but no .gcode.3mf "
                     "produced")

        # Print metrics for confirmation
        try:
            with _zf.ZipFile(out_3mf) as z:
                info = z.read(
                    "Metadata/slice_info.config"
                ).decode("utf-8", errors="replace")
            for key in ("prediction", "weight", "used_m"):
                for line in info.splitlines():
                    if (f'key="{key}"' in line
                            or 'tray_info_idx' in line):
                        print(f"  {line.strip()}", file=sys.stderr)
                        break
        except Exception:
            pass

        if args.dry_run:
            # Save the sliced .gcode.3mf so user can inspect it
            keep = stl.with_suffix(".sliced.gcode.3mf")
            shutil.copy2(out_3mf, keep)
            print(f"[slice-print] DRY RUN — sliced to {keep}; not "
                  f"uploading", file=sys.stderr)
            return 0

        # Upload + print via existing path
        creds = Creds.resolve(args)
        upload_file(creds, out_3mf, remote_name=args.remote)
        cli = X2DClient(creds)
        cli.connect()
        name = args.remote or out_3mf.name
        # "auto" sentinel = let start_print() derive from the 3MF we
        # just produced, which is the slicer's authoritative contract.
        sliced_bed = (args.bed_type
                      if args.bed_type and args.bed_type != "auto"
                      else None)
        # Per code-review #3: parity with cmd_print — refuse to send
        # if the targeted AMS slot is empty or has the wrong filament
        # class. `--force` is not exposed on slice-print; users
        # wanting to bypass should re-slice or load matching material.
        if not args.no_ams:
            derived = _derive_print_params_from_3mf(
                out_3mf, filament_index=0)
            try:
                live = cli.request_state(timeout=15.0)
            except TimeoutError:
                cli.disconnect()
                raise SystemExit(
                    "could not pull live printer state to validate "
                    "AMS slot before sending. Re-run when the "
                    "printer is reachable.")
            _validate_ams_slot(live, args.slot, derived, force=False)
        start_print(cli, name,
                    use_ams=not args.no_ams, ams_slot=args.slot,
                    bed_levelling=not args.no_bed_level,
                    flow_cali=args.flow_cali,
                    timelapse=args.timelapse,
                    vibration_cali=args.vib_cali,
                    bed_type=sliced_bed,
                    local_path=out_3mf)
        print(f"[slice-print] queued: {name} on {creds.ip} "
              f"(ams_slot={args.slot})")
        cli.disconnect()
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
