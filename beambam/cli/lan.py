"""beambam.cli.lan — LAN-direct file-transfer handlers.

Phase 5d scaffold (`docs/BRIDGE_SPLIT_PLAN.md`). Hosts the LAN-side
file-mover handlers that don't fit the print/control taxonomy in
`beambam.cli.control`:

  cmd_upload   — FTPS-implicit-TLS upload (.gcode.3mf → printer:/sdcard)
  cmd_files    — list SD-card files via the runtime/network_shim
                 FileTunnel (vsFTPd on port 990; #92 details the
                 X2D firmware's surface)

  cmd_print       — upload + signed project_file, then wait for the
                    printer's verdict (exit 0 started / 1 refused /
                    3 unconfirmed)
  cmd_slice_print — slice an STL, then cmd_print
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


from beambam.signing import SIGNATURE_REJECTED, signing_status  # noqa: E402


def cmd_print(args: argparse.Namespace) -> int:
    """LAN-direct print: derive bed/temp from 3MF, validate AMS slot
    against live state, upload .gcode.3mf (unless --no-upload), publish
    the signed project_file MQTT.

    `--dry-run` runs `beambam.analyze` against the 3MF before any
    network access and refuses on excessive purge waste (controlled by
    `--max-flush-g`)."""
    from beambam.config import Creds
    from beambam.ftps import upload_file
    from beambam.mqtt import X2DClient
    from beambam.print_job import (
        PrintRefusal,
        _derive_print_params_from_3mf,
        _validate_ams_slot,
        parse_slots,
        start_print,
        used_filament_ids,
    )

    local = Path(args.file)
    # One AMS slot per filament of the file, in order (`--slot 4,0,12`); a plain int still works.
    slots = parse_slots(args.slot) if not isinstance(args.slot, list) else list(args.slot)
    use_ams = not args.no_ams
    force = bool(getattr(args, "force", False))

    # Step 1: derive authoritative bed_type / bed_temp / filament
    # expectations from the 3MF before we touch the network. If the
    # 3MF is unreadable we fail loud here, well before publish.
    try:
        derived = _derive_print_params_from_3mf(local,
                                                 filament_index=0)
    except PrintRefusal as e:
        raise SystemExit(str(e))

    # Step 2: reconcile with user-supplied --bed-type / --bed-temp.
    # Defaults are sentinels (None) so we can tell "user didn't pass"
    # from "user explicitly passed a contradicting value".
    user_bed_type = getattr(args, "bed_type", None)
    user_bed_temp = getattr(args, "bed_temp", None)
    bed_type = derived["bed_type"]
    bed_temp = derived["bed_temp"]
    if user_bed_type and user_bed_type != bed_type:
        if not force:
            raise SystemExit(
                f"--bed-type {user_bed_type!r} contradicts 3MF "
                f"curr_bed_type ({bed_type!r}). Re-slice with the "
                f"right plate, OR pass --force if you genuinely want "
                f"to override (NOT recommended — heat profile WILL "
                f"be wrong).")
        sys.stderr.write(
            f"[print] WARN: bed_type override {bed_type!r} -> "
            f"{user_bed_type!r} under --force\n")
        bed_type = user_bed_type
    if user_bed_temp is not None and user_bed_temp != bed_temp:
        if not force:
            raise SystemExit(
                f"--bed-temp {user_bed_temp} contradicts 3MF-derived "
                f"value ({bed_temp}°C for {bed_type}). Re-slice or "
                f"pass --force.")
        sys.stderr.write(
            f"[print] WARN: bed_temp override {bed_temp} -> "
            f"{user_bed_temp} under --force\n")
        bed_temp = user_bed_temp

    sys.stderr.write(
        f"[print] derived from 3MF: bed_type={bed_type!r} "
        f"bed_temp={bed_temp}°C "
        f"filament_type={derived['expected_filament_type']!r} "
        f"filament_id={derived['expected_filament_id']!r} "
        f"colour={derived['expected_filament_colour']!r}\n")
    # What each mapped filament must be, from the same 3MF (filament i -> slots[i]).
    try:
        expected = [derived] + [_derive_print_params_from_3mf(local, filament_index=i)
                                for i in range(1, len(slots))]
    except PrintRefusal as e:
        raise SystemExit(f"--slot lists {len(slots)} slots but the file does not have that many "
                         f"filaments: {e}")
    # One slot per filament the plate prints with: ams_mapping is indexed by
    # the project's filament number, so a short list leaves a used filament
    # unmapped (the firmware then stalls at the AMS load or uses the wrong spool).
    if use_ams:
        used = used_filament_ids(local)
        if used and len(slots) < max(used):
            raise SystemExit(
                f"this plate prints with filament(s) {', '.join(map(str, used))}: give one "
                f"--slot per filament up to #{max(used)} in order (got {len(slots)}: "
                f"{','.join(map(str, slots))})")
    if len(slots) > 1:
        sys.stderr.write("[print] filament -> AMS slot: " + ", ".join(
            f"{i + 1} ({want['expected_filament_type']} {want['expected_filament_colour']}) -> {slot}"
            for i, (slot, want) in enumerate(zip(slots, expected))) + "\n")

    # --dry-run stops here: every offline check above has passed; also
    # report the purge and refuse when it is over --max-flush-g. No
    # credentials, network or FTPS needed.
    if getattr(args, "dry_run", False):
        return _dry_run_report(local, float(getattr(args, "max_flush_g", 10.0)))

    creds = Creds.resolve(args)
    signing = signing_status()
    if not signing.ready:
        # Not fatal (older firmware takes the legacy signature), but on an
        # X2D/H2D this is why a start would be refused: say so up front.
        sys.stderr.write(f"[print] WARNING: LAN print signing not ready: {signing.summary()}\n")

    # Step 3: upload (if requested) BEFORE we open the MQTT client so a
    # transient FTPS failure doesn't leave a stale subscription.
    if not args.no_upload:
        # cache/ — the firmware-accepted LAN print URL is ftp:///cache/<file>
        upload_file(creds, local, remote_name=args.remote, remote_dir="cache")

    # Step 4: open the MQTT client, read the live state, validate AMS slots.
    cli = X2DClient(creds)
    cli.connect()
    name = args.remote or local.name
    try:
        try:
            live = cli.request_state(timeout=15.0)
        except TimeoutError:
            raise SystemExit(
                "could not read the printer's state before sending. Either the "
                "printer dropped, or credentials are wrong. Re-run when "
                "`beambam status` works.")
        busy = (live.get("print") or {}).get("gcode_state", "")
        if busy in BUSY_STATES and not force:
            raise SystemExit(
                f"printer is busy (gcode_state={busy}); refusing to start another "
                f"print. Wait for it to finish, or pass --force.")
        if use_ams:
            try:
                for slot, want in zip(slots, expected):
                    _validate_ams_slot(live, slot, want, force=force)
            except PrintRefusal as e:
                raise SystemExit(str(e))
            sys.stderr.write(
                "[print] AMS state validated; do not change AMS slots "
                "until the printer transitions to PREPARE/RUNNING.\n")

        since = cli.message_seq
        try:
            start_print(cli, name,
                        use_ams=use_ams,
                        ams_slot=slots if len(slots) > 1 else slots[0],
                        bed_levelling=not args.no_bed_level,
                        flow_cali=args.flow_cali,
                        timelapse=args.timelapse,
                        vibration_cali=args.vib_cali,
                        bed_type=bed_type,
                        bed_temp=bed_temp,
                        local_path=local)
        except PrintRefusal as e:
            raise SystemExit(str(e))
        summary = (f"{name} (slot={','.join(map(str, slots))}, ams={use_ams}, "
                   f"bed={bed_type}@{bed_temp}°C)")
        wait_s = float(getattr(args, "confirm_timeout", 45.0))
        if wait_s <= 0:
            print(f"start_print sent (not confirmed): {summary}")
            return 0
        verdict, detail = await_print_start(cli, since, wait_s)
        if verdict == "started":
            print(f"print started: {summary} — {detail}")
            return 0
        if verdict == "refused":
            sys.stderr.write(f"[print] printer REFUSED the job: {detail}\n")
            if any(str(code) in detail for code in SIGNATURE_REJECTED):
                sys.stderr.write(f"[print] signature rejected: {signing_status().summary()}\n")
            return 1
        sys.stderr.write(
            f"[print] sent, but the printer did not confirm within {wait_s:g} s "
            f"({detail}). Check the printer screen before retrying.\n")
        return 3
    finally:
        cli.disconnect()


# Re-exported for callers of the CLI module; the canonical home is print_job.
from beambam.print_job import BUSY_STATES, STARTED_STATES, await_print_start  # noqa: E402,F401


def _dry_run_report(local: Path, max_g: float) -> int:
    """`print --dry-run` tail: show beambam's analysis, gate on purge grams."""
    from beambam.analyze import analyze_3mf, format_report
    report = analyze_3mf(local)
    print(format_report(report))
    flush_g = float(report.totals.get("flush_volume_g", 0.0))
    if flush_g > max_g:
        sys.stderr.write(
            f"\n[print --dry-run] REFUSED: predicted purge "
            f"{flush_g:.1f} g exceeds --max-flush-g {max_g:.1f} g. "
            f"Re-slice with fewer color swaps OR raise the "
            f"threshold.\n")
        return 2
    sys.stderr.write(
        f"\n[print --dry-run] OK: offline checks passed; predicted purge "
        f"{flush_g:.1f} g <= --max-flush-g {max_g:.1f} g\n")
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
    from beambam import X2D_ROOT_PATH

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
            # Already a 3mf — re-slice via BS CLI. For project .3mfs
            # authored for another printer (A1, etc.) or with multi-color
            # AMS-swap that needs per-nozzle distribution, the caller can
            # pass `--load-filaments` / `--load-settings` /
            # `--load-filament-ids` / `--allow-newer-3mf` / `--uptodate`
            # / `--load-defaultfila` to feed bambu-studio the right
            # context. These mirror BS CLI's own flag names verbatim so
            # existing BS docs apply.
            print(f"[slice-print] input already a 3mf, re-slicing in "
                  f"place", file=sys.stderr)
            from beambam.slicer_paths import bambu_studio_bin, run_bambu_cli
            bs_bin = bambu_studio_bin()
            if bs_bin is None:
                sys.exit("bambu-studio not found: install Bambu Studio or set BAMBU_STUDIO")
            cmd = [
                str(bs_bin), "--slice", "0",
                "--outputdir", str(out_3mf.parent),
                "--export-3mf", out_3mf.name,
            ]
            if getattr(args, "load_filaments", None):
                cmd.extend(["--load-filaments", args.load_filaments])
            if getattr(args, "load_settings", None):
                cmd.extend(["--load-settings", args.load_settings])
            if getattr(args, "load_filament_ids", None):
                cmd.extend(["--load-filament-ids", args.load_filament_ids])
            if getattr(args, "load_defaultfila", False):
                cmd.append("--load-defaultfila")
            if getattr(args, "uptodate", False):
                cmd.append("--uptodate")
            if getattr(args, "allow_newer_3mf", False):
                # BS v02.06.00.51 names this --allow-newer-file (not -3mf
                # as older docs say); takes `=1` to enable (space-form
                # gets parsed as positional). Keep our CLI arg friendly.
                cmd.append("--allow-newer-file=1")
            if getattr(args, "allow_multicolor_oneplate", False):
                cmd.append("--allow-multicolor-oneplate")
            if getattr(args, "repetitions", None) and int(args.repetitions) > 1:
                # BS CLI: repeat the whole plate N times. Different from
                # x2d_slice.py's --copies (which tiles one model).
                cmd.extend(["--repetitions", str(int(args.repetitions))])
            cmd.append(str(stl))
            env = dict(os.environ)
            if os.name != "nt":
                env.setdefault("DISPLAY", ":1")
            rc = run_bambu_cli(cmd, out_3mf.parent, env=env)
        else:
            # STL/OBJ/STEP — graft into template and slice
            # Through this interpreter: running the .py directly fails on
            # Windows (WinError 193) and wherever the file isn't executable.
            cmd = [sys.executable, str(slice_bin), str(stl), "--out", str(out_3mf)]
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
            if getattr(args, "colors", None):
                cmd.extend(["--colors", args.colors])
            if getattr(args, "color_by_region", None):
                cmd.extend(["--color-by-region", args.color_by_region])
            if getattr(args, "orient", "original") != "original":
                cmd.extend(["--orient", args.orient])
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
        upload_file(creds, out_3mf, remote_name=args.remote, remote_dir="cache")
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
