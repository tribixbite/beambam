"""beambam.cli.lan — LAN-direct file-transfer handlers.

Phase 5d scaffold (`docs/BRIDGE_SPLIT_PLAN.md`). Hosts the LAN-side
file-mover handlers that don't fit the print/control taxonomy in
`beambam.cli.control`:

  cmd_upload   — FTPS-implicit-TLS upload (.gcode.3mf → printer:/sdcard)
  cmd_files    — list SD-card files via the runtime/network_shim
                 FileTunnel (vsFTPd on port 990; #92 details the
                 X2D firmware's surface)

  cmd_print       — plan the filament→tray mapping, confirm, then
                    upload + signed project_file and wait for the start
                    ack (exit 0 sent/started, 1 refused via SystemExit)
  cmd_slice_print — slice an STL, then cmd_print
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


# Seconds to wait for the printer's reply to project_file before reporting.
_ACK_WAIT_S = 15.0


def _report_start_ack(ack: dict | None, what: str, plan) -> None:
    """Print the outcome of a start-print publish; exit non-zero on rejection.

    No reply is reported but not treated as failure: older firmware doesn't
    always echo project_file, and the print may still start."""
    from beambam.mqtt_sign import explain_ack
    v0, _ = plan.mapping_arrays()
    mapping = v0[:plan.project.total_filaments]
    if ack is None:
        print(f"start_print sent: {what} (ams_mapping={mapping}); the printer did not "
              f"confirm within {_ACK_WAIT_S:g}s — check `beambam status`")
        return
    problem = explain_ack(ack)
    if problem:
        raise SystemExit(f"[print] the printer REJECTED the print: {problem}")
    print(f"print accepted by the printer: {what} (ams_mapping={mapping})")


def _plan_and_confirm(args: argparse.Namespace, live: dict, local: Path, *,
                      use_ams: bool, force: bool, plate: int = 1,
                      extra_explicit: dict[int, int] | None = None):
    """Build the filament → tray plan for `local` against live printer state,
    show it, and get consent before the print starts.

    * Hard errors (empty tray, wrong material, tray that can't reach the
      filament's nozzle, …) always refuse.
    * On a terminal the table is followed by a y/N prompt; answering yes
      accepts any soft warnings (brand / colour / exact-type differences).
      `--yes` answers the prompt for you.
    * Without a terminal there is nobody to ask: soft warnings refuse unless
      `--force` (or `--yes`) is given, as the old slot check did.
    `extra_explicit` ({filament_index: tray}) pins filaments that came from an
    AMS colour sync (`slice-print --colors A1,B3`); `--map` entries win over it.
    Returns the PrintPlan to pass to start_print(plan=...)."""
    from beambam.filament_mapping import (
        MappingError, format_plan, parse_map, plan_print, read_project,
    )
    from beambam.print_job import parse_slots
    # `--slot` arrives parsed (int, or a list for `4,0,12`) from the CLI, but
    # programmatic callers may pass the raw text; normalise both.
    slot = getattr(args, "slot", None)
    if isinstance(slot, str):
        try:
            parsed = parse_slots(slot)
        except ValueError as e:
            raise SystemExit(f"[print] refused: {e}")
        slot = parsed[0] if len(parsed) == 1 else parsed
    try:
        project = read_project(local, plate)
        explicit = dict(extra_explicit or {})
        if getattr(args, "map", None):
            explicit.update(parse_map(args.map))
        plan = plan_print(project, live, use_ams=use_ams,
                          slot=slot, explicit=explicit or None)
    except MappingError as e:
        raise SystemExit(f"[print] refused: {e}")
    tty = sys.stdin.isatty() and sys.stderr.isatty()
    sys.stderr.write(format_plan(plan, color=sys.stderr.isatty()))
    if plan.errors:
        raise SystemExit("[print] refused — fix the error(s) above (not bypassable)")
    assume_yes = bool(getattr(args, "yes", False))
    if assume_yes:
        return plan
    if tty:
        try:
            answer = input("Start the print with this mapping? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            raise SystemExit("[print] cancelled")
        return plan
    if plan.warnings and not force:
        raise SystemExit("[print] refused: mapping has warnings and there is no terminal "
                         "to confirm on — pass --yes (or --force) to accept them")
    return plan


def cmd_upload(args: argparse.Namespace) -> int:
    """FTPS-implicit-TLS upload of a single file to the printer's
    SD card. `--remote <name>` overrides the destination filename;
    by default the source basename is used."""
    from beambam.config import Creds
    from beambam.ftps import upload_file

    import ftplib
    import ssl
    creds = Creds.resolve(args)
    try:
        upload_file(creds, Path(args.file), remote_name=args.remote)
    except FileNotFoundError as e:
        sys.exit(str(e))
    except (ssl.SSLError, ftplib.Error, OSError, EOFError) as e:
        sys.exit(f"upload failed: {type(e).__name__}: {e}")
    print(f"uploaded {args.file} -> {creds.ip}:/"
          f"{args.remote or Path(args.file).name}")
    return 0


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
        start_print,
    )

    local = Path(args.file)
    use_ams = not args.no_ams
    force = bool(getattr(args, "force", False))

    # Step 0: --dry-run analyzes the file and refuses on excessive purge
    # waste BEFORE touching creds / network / FTPS. Lets `uvx beambam`
    # users sanity-check a print on a workstation that isn't on the
    # printer's LAN.
    if getattr(args, "dry_run", False):
        from beambam.analyze import analyze_3mf, format_report
        report = analyze_3mf(local)
        print(format_report(report))
        flush_g = float(report.totals.get("flush_volume_g", 0.0))
        max_g = float(getattr(args, "max_flush_g", 10.0))
        if flush_g > max_g:
            sys.stderr.write(
                f"\n[print --dry-run] REFUSED: predicted purge "
                f"{flush_g:.1f} g exceeds --max-flush-g {max_g:.1f} g. "
                f"Re-slice with fewer color swaps OR raise the "
                f"threshold.\n")
            return 2
        sys.stderr.write(
            f"\n[print --dry-run] OK: predicted purge {flush_g:.1f} g "
            f"<= --max-flush-g {max_g:.1f} g\n")
        return 0

    creds = Creds.resolve(args)

    # Step 1: derive authoritative bed_type / bed_temp / filament
    # expectations from the 3MF before we touch the network. If the
    # 3MF is unreadable we fail loud here, well before publish.
    plate = int(getattr(args, "plate", 1) or 1)
    try:
        derived = _derive_print_params_from_3mf(local, plate=plate)
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

    # Step 3: open the MQTT client, map every filament the plate uses to a
    # tray against LIVE state, and confirm — before anything is uploaded or
    # published, so a cancelled / refused print leaves no trace.
    cli = X2DClient(creds)
    cli.connect()
    name = args.remote or local.name
    try:
        try:
            live = cli.request_state(timeout=15.0)
        except TimeoutError:
            raise SystemExit(
                "could not pull live printer state to map filaments to "
                "trays before sending. Either the printer dropped, or "
                "credentials are wrong. Re-run when `beambam status` works.")
        plan = _plan_and_confirm(args, live, local, use_ams=use_ams,
                                 force=force, plate=plate)
        # Step 4: upload into cache/ — the firmware-accepted LAN print URL is
        # ftp:///cache/<file>.
        if not args.no_upload:
            upload_file(creds, local, remote_name=args.remote, remote_dir="cache")
        # Per code-review #4 (race window): the mapping is only as good as
        # the AMS state it was computed from.
        sys.stderr.write(
            "[print] mapping confirmed; do not change AMS spools until the "
            "printer reaches PREPARE/RUNNING.\n")
        try:
            ack = start_print(cli, name,
                              use_ams=plan.use_ams,
                              bed_levelling=not args.no_bed_level,
                              flow_cali=args.flow_cali,
                              timelapse=args.timelapse,
                              vibration_cali=args.vib_cali,
                              bed_type=bed_type,
                              bed_temp=bed_temp,
                              local_path=local,
                              plan=plan,
                              wait_ack=_ACK_WAIT_S)
        except PrintRefusal as e:
            raise SystemExit(str(e))
        _report_start_ack(ack, f"{name} plate {plate}", plan)
    finally:
        cli.disconnect()
    return 0


# Re-exported for callers of the CLI module; the canonical home is print_job.
from beambam.print_job import BUSY_STATES, STARTED_STATES, await_print_start  # noqa: E402,F401


def _dry_run_report(local: Path, max_g: float, max_waste_g: float | None = None) -> int:
    """`print --dry-run` tail: show beambam's analysis, gate on purge grams
    (`--max-flush-g`) and, when given, on all waste (`--max-waste-g`: purge +
    prime tower). The tower is always reported: with one is printed, the
    purge alone can read ~0 while the tower is most of the filament."""
    from beambam.analyze import analyze_3mf, format_report
    report = analyze_3mf(local)
    print(format_report(report))
    t = report.totals
    flush_g = float(t.get("flush_volume_g", 0.0))
    tower_g = float(t.get("prime_tower_g", 0.0))
    waste_g = float(t.get("waste_g", flush_g + tower_g))
    detail = f"(prime tower {tower_g:.1f} g; total waste {waste_g:.1f} g)"
    if flush_g > max_g:
        sys.stderr.write(
            f"\n[print --dry-run] REFUSED: predicted purge "
            f"{flush_g:.1f} g exceeds --max-flush-g {max_g:.1f} g {detail}. "
            f"Re-slice with fewer color swaps OR raise the "
            f"threshold.\n")
        return 2
    if max_waste_g is not None and waste_g > max_waste_g:
        sys.stderr.write(
            f"\n[print --dry-run] REFUSED: predicted waste {waste_g:.1f} g "
            f"(purge {flush_g:.1f} g + prime tower {tower_g:.1f} g) exceeds "
            f"--max-waste-g {max_waste_g:.1f} g.\n")
        return 2
    sys.stderr.write(
        f"\n[print --dry-run] OK: offline checks passed; predicted purge "
        f"{flush_g:.1f} g <= --max-flush-g {max_g:.1f} g {detail}\n")
    return 0


def _template_filament_type(template: Path) -> str:
    """First filament type of a slicing template ('' when unreadable)."""
    import json as _json
    import zipfile as _zf
    try:
        with _zf.ZipFile(template) as z:
            ps = _json.loads(z.read("Metadata/project_settings.config"))
    except (OSError, KeyError, ValueError, _zf.BadZipFile):
        return ""
    ft = ps.get("filament_type")
    return str(ft[0]) if isinstance(ft, list) and ft else ""


def _sync_colours_from_ams(args: argparse.Namespace, root: Path):
    """Resolve tray references in `--color` / `--colors` against live AMS state.

    Returns an AmsColourSync, or None when no entry names a tray (nothing to
    sync — the colours go to the slicer unchanged). Exits with a message when
    a tray is missing/empty or holds a different material than the slice
    profile, before any slicing time is spent."""
    from beambam.config import Creds
    from beambam.filament_mapping import (
        MappingError, has_tray_refs, material_conflicts, sync_colours_from_ams,
    )
    from beambam.mqtt import X2DClient

    raw = getattr(args, "colors", None) or getattr(args, "color", None)
    if not raw:
        return None
    entries = [e.strip() for e in str(raw).split(",") if e.strip()]
    if not has_tray_refs(entries):
        return None
    creds = Creds.resolve(args)
    cli = X2DClient(creds)
    cli.connect()
    try:
        live = cli.request_state(timeout=15.0)
    except TimeoutError:
        raise SystemExit("could not read the AMS to sync colours from trays "
                         f"({', '.join(entries)}); re-run when `beambam status` works.")
    finally:
        cli.disconnect()
    try:
        sync = sync_colours_from_ams(entries, live)
    except MappingError as e:
        raise SystemExit(f"[slice-print] refused: {e}")
    template = Path(args.template) if getattr(args, "template", None) \
        else root / "rumi_frame.gcode.3mf"
    conflicts = material_conflicts(sync, _template_filament_type(template))
    if conflicts:
        raise SystemExit("[slice-print] refused: " + "; ".join(conflicts)
                         + " — pick trays of that material, or slice from a template "
                           "with the right filament profile (--template)")
    sys.stderr.write("[slice-print] colours synced from the AMS: " + ", ".join(
        f"{i + 1}={entries[i]}→{c}" if i in sync.explicit else f"{i + 1}={c}"
        for i, c in enumerate(sync.colours)) + "\n")
    return sync


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
    from beambam.print_job import start_print
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

    # AMS colour sync: tray references in --color / --colors (A1, B3,
    # ext-left) are replaced by the colours loaded in those trays, and each
    # such filament is pinned to its tray for the print.
    sync = None
    if stl.suffix.lower() != ".3mf":
        sync = _sync_colours_from_ams(args, X2D_ROOT_PATH)

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
            if sync is not None:
                cmd.extend(["--colors", ",".join(sync.colours)])
            else:
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

        # Map + confirm against live state first, then upload + print.
        creds = Creds.resolve(args)
        cli = X2DClient(creds)
        cli.connect()
        name = args.remote or out_3mf.name
        # "auto" sentinel = let start_print() derive from the 3MF we
        # just produced, which is the slicer's authoritative contract.
        sliced_bed = (args.bed_type
                      if args.bed_type and args.bed_type != "auto"
                      else None)
        try:
            live = cli.request_state(timeout=15.0)
        except TimeoutError:
            cli.disconnect()
            raise SystemExit(
                "could not pull live printer state to map filaments to "
                "trays before sending. Re-run when the printer is reachable.")
        try:
            # slice-print has no --force: soft mismatches need a yes on
            # the terminal or --yes.
            plan = _plan_and_confirm(args, live, out_3mf,
                                     use_ams=not args.no_ams, force=False,
                                     extra_explicit=sync.explicit if sync else None)
            upload_file(creds, out_3mf, remote_name=args.remote, remote_dir="cache")
            ack = start_print(cli, name,
                              use_ams=plan.use_ams,
                              bed_levelling=not args.no_bed_level,
                              flow_cali=args.flow_cali,
                              timelapse=args.timelapse,
                              vibration_cali=args.vib_cali,
                              bed_type=sliced_bed,
                              local_path=out_3mf,
                              plan=plan,
                              wait_ack=_ACK_WAIT_S)
            _report_start_ack(ack, f"{name} on {creds.ip}", plan)
        finally:
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
