#!/usr/bin/env python3
"""End-to-end Bambu LAN print — query AMS state, upload .gcode.3mf via FTPS,
then issue a *signed* MQTT start_print command with an ams_mapping that
points at the AMS slot whose filament matches what the 3MF was sliced for.

Workflow:
  1. Connect to the printer (FTPS for upload + signed-MQTT for state/control)
     using x2d_bridge's X2DClient. The MQTT connection takes a few seconds
     to receive the first `pushall` state report, after which we read AMS
     contents.
  2. Walk every AMS tray on the printer. Match by --filament-match (default
     "Silk", case-insensitive substring against tray_sub_brands and
     tray_id_name). Refuse to proceed if zero or >1 matching trays are
     found unless --slot is forced explicitly.
  3. Cross-check that the matched tray's filament type agrees with the
     filament_type baked into the 3MF's project_settings.config (e.g. PLA
     in 3MF must match PLA in the slot, otherwise the printer would refuse
     anyway after the file load).
  4. Hand the file and the chosen slots to `beambam print`
     (beambam.cli.lan.cmd_print), which uploads into the printer's cache/
     dir, derives bed type/temperature from the 3MF, re-validates every slot
     against the live AMS, publishes the signed project_file (recovered
     per-install key, see `beambam key`) and waits for the printer's
     verdict: exit 0 started, 1 refused, 3 not confirmed.

This script exists for its filament/colour slot matching; for a known slot
list use `beambam print FILE --slot a,b,...` directly.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import zipfile
from pathlib import Path

# Reuse beambam's connection/state helpers. This
# requires bambu_cert.py (publicly-leaked Bambu Connect signing key) to
# be importable next to x2d_bridge.py.
from beambam.config import Creds
from beambam.mqtt import X2DClient
import preflight_3mf

log = logging.getLogger("lan_print")


def read_3mf_filament_types(path: Path) -> list[str]:
    """Pull `filament_type` out of the 3MF's project_settings.config."""
    with zipfile.ZipFile(path, "r") as z:
        with z.open("Metadata/project_settings.config") as f:
            cfg = json.load(f)
    return cfg.get("filament_type") or []


def read_3mf_bed_type(path: Path, plate: int = 1) -> str | None:
    """Pull bed_type from Metadata/plate_<N>.json — the per-plate truth.

    project_settings.config doesn't carry bed_type; only the plate JSON
    does. Mismatching bed_type at start_print time triggers a printer
    warning (sometimes refusal). Auto-reading this avoids relying on the
    user remembering --bed-type for every print.
    """
    try:
        with zipfile.ZipFile(path, "r") as z:
            with z.open(f"Metadata/plate_{plate}.json") as f:
                pj = json.load(f)
        return pj.get("bed_type")
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return None


def collect_trays(state: dict | None) -> list[dict]:
    """Flatten the printer's AMS hub into a list of {slot_index, type, sub_brand, color}.

    `slot_index` is the global ams_mapping index Bambu firmware expects:
    AMS unit U with tray index T (both 0-indexed) becomes U*4 + T. So slot
    A4 of the first AMS == 3 in the firmware's wire format.

    `state` is the raw printer report (the JSON dict X2DClient.request_state
    returns). Walks `state["print"]["ams"]["ams"]` directly because the
    X2D's tray records omit some fields older library helpers expect.
    """
    if not state:
        return []
    ams_block = (state.get("print") or {}).get("ams") or {}
    ams_units = ams_block.get("ams") or []
    trays: list[dict] = []
    for unit in ams_units:
        ams_idx = int(unit.get("id", 0))
        for tray in unit.get("tray", []):
            # state == 0 means empty slot; skip
            if int(tray.get("state", 0)) == 0:
                continue
            tray_idx = int(tray.get("id", 0))
            trays.append({
                "slot_index": ams_idx * 4 + tray_idx,
                "ams": ams_idx,
                "tray": tray_idx,
                "type": tray.get("tray_type"),
                "sub_brand": tray.get("tray_sub_brands") or "",
                "id_name": tray.get("tray_id_name") or "",
                "info_idx": tray.get("tray_info_idx") or "",
                "color": tray.get("tray_color"),
            })
    return trays


def _norm_color(c: str) -> str:
    """Normalise a color spec for comparison.

    Accepts:
      - "#RRGGBB"      -> "RRGGBBFF"
      - "#RRGGBBAA"    -> "RRGGBBAA"
      - "RRGGBBAA"     -> "RRGGBBAA"
      - "RRGGBB"       -> "RRGGBBFF"
      - "0xRRGGBBAA"   -> "RRGGBBAA"
    All upper-case so the printer's `tray_color` (already upper, e.g.
    "057748FF") compares directly with anything the user types.
    """
    if not c:
        return ""
    s = c.strip().upper().lstrip("#")
    if s.startswith("0X"):
        s = s[2:]
    if len(s) == 6:
        s += "FF"
    return s


def _color_distance(a: str, b: str) -> int | None:
    """Manhattan distance between two normalised RGBA hex colors, or None
    if either is malformed. Lets us pick the closest tray when an exact
    color match isn't found (--filament-color-fuzzy)."""
    try:
        ra, ga, ba = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
        rb, gb, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
    except (ValueError, IndexError):
        return None
    return abs(ra - rb) + abs(ga - gb) + abs(ba - bb)


def match_slot(trays: list[dict], *,
               match_substr: str = "",
               match_color: str = "",
               match_info_idx: str = "",
               color_fuzzy_max: int | None = None,
               expected_type: str | None = None) -> dict:
    """Find the unique tray matching the given criteria.

    Match modes (apply in this priority order — first non-empty wins):
      1. `match_color`  exact RGBA hex match against `tray_color`. With
         `color_fuzzy_max` set, falls back to nearest within that
         Manhattan distance threshold (e.g. 30 ≈ visually close).
      2. `match_info_idx` exact (case-insensitive) match against
         `tray_info_idx` — Bambu's filament catalog ID like "GFL95".
      3. `match_substr` case-insensitive substring against tray_sub_brands
         and tray_id_name. The X2D omits these; on its loadouts this
         matcher will silently match nothing.
      4. If nothing was given, the only candidate filter is
         `expected_type` (e.g. "PLA"); we error out if more than one
         tray matches that alone.

    `expected_type` filters every mode — mismatched filament_type trays
    are dropped before reporting "ambiguous" or "no match".
    """
    candidates: list[dict] = []
    for t in trays:
        if expected_type and (t.get("type") or "").upper() != expected_type.upper():
            continue
        if match_color:
            want = _norm_color(match_color)
            have = _norm_color(t.get("color") or "")
            if want and have == want:
                candidates.append(t)
            continue
        if match_info_idx:
            if (t.get("info_idx") or "").upper() == match_info_idx.upper():
                candidates.append(t)
            continue
        if match_substr:
            needle = match_substr.lower()
            haystacks = [t.get("sub_brand") or "", t.get("id_name") or ""]
            if any(needle in (h or "").lower() for h in haystacks):
                candidates.append(t)
            continue
        # No criteria given: every tray of expected_type is a candidate.
        candidates.append(t)

    # Color fuzzy fallback: if exact color match returned nothing, try
    # nearest-within-threshold among the type-filtered trays.
    if not candidates and match_color and color_fuzzy_max is not None:
        want = _norm_color(match_color)
        scored: list[tuple[int, dict]] = []
        for t in trays:
            if expected_type and (t.get("type") or "").upper() != expected_type.upper():
                continue
            d = _color_distance(want, _norm_color(t.get("color") or ""))
            if d is not None and d <= color_fuzzy_max:
                scored.append((d, t))
        if scored:
            scored.sort(key=lambda p: p[0])
            return scored[0][1]

    criteria_desc = (
        f"color={match_color!r}" if match_color
        else f"info_idx={match_info_idx!r}" if match_info_idx
        else f"substring={match_substr!r}"
    )
    if not candidates:
        raise SystemExit(
            f"No AMS tray matches {criteria_desc}"
            + (f" of type {expected_type}" if expected_type else "")
            + ". Available trays:\n"
            + "\n".join(f"  slot {t['slot_index']}: type={t.get('type')!r} "
                       f"color={t.get('color')!r} info_idx={t.get('info_idx')!r}"
                       for t in trays)
        )
    if len(candidates) > 1:
        raise SystemExit(
            f"Multiple trays match {criteria_desc}; pass --slot N to disambiguate:\n"
            + "\n".join(f"  slot {t['slot_index']}: type={t.get('type')!r} "
                       f"color={t.get('color')!r} info_idx={t.get('info_idx')!r}"
                       for t in candidates)
        )
    return candidates[0]


def main() -> int:
    from beambam.console import utf8_stdio
    utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Printer credentials — match x2d_bridge's flag names so Creds.resolve() works.
    ap.add_argument("--ip", default="", help="Printer IP. Defaults to ~/.x2d/credentials.")
    ap.add_argument("--code", default="",
                    help="Printer LAN access code (8 hex chars). "
                         "Defaults to ~/.x2d/credentials.")
    ap.add_argument("--serial", default="",
                    help="Printer serial. Defaults to ~/.x2d/credentials.")
    ap.add_argument("--printer", default="",
                    help="Named [printer:NAME] section in ~/.x2d/credentials.")
    ap.add_argument("--file", required=True, type=Path,
                    help="Path to the .gcode.3mf to upload+print")
    ap.add_argument("--plate", type=int, default=1, help="Plate number in the 3MF (default 1)")
    # Match modes — pick exactly one. Color is the most reliable on X2D
    # since the firmware's MQTT report omits sub_brand/id_name strings on
    # some loadouts (so --filament-match would match nothing). info_idx
    # is the next-best — Bambu's catalog ID like "GFL95" — same idea but
    # tied to the filament SKU rather than its color.
    ap.add_argument("--filament-match", default="",
                    help="Case-insensitive substring matched against tray_sub_brands "
                         "and tray_id_name. Pre-X2D printers populate these; X2D "
                         "often doesn't — prefer --filament-color or --filament-info-idx.")
    ap.add_argument("--filament-color", default="",
                    help='RGBA hex of the AMS slot color, e.g. "#057748" or '
                         '"057748FF". For multi-color prints, comma-separate '
                         'one color per filament index in the 3MF, e.g. '
                         '"--filament-color #057748,#FFFFFF". Compares against '
                         "the tray's tray_color (the firmware reports it "
                         'upper-case with alpha=FF).')
    ap.add_argument("--filament-color-fuzzy", type=int, default=None,
                    metavar="MAX",
                    help="With --filament-color, if no exact match falls back to "
                         "the nearest tray within MAX Manhattan distance in RGB "
                         "(0-765). 30 ≈ visually close, 100 = same family.")
    ap.add_argument("--filament-info-idx", default="",
                    help='Bambu filament catalog ID, e.g. "GFL95" (Bambu Basic '
                         'PLA Bambu Green) or "GFA05" (Bambu Generic PLA). '
                         'Inspect AMS state with --query-only to see what '
                         'each loaded tray reports.')
    ap.add_argument("--slot", default="",
                    help="Force a specific AMS slot (skips auto-match). For "
                         "multi-color prints, comma-separate one slot per "
                         "filament index in the 3MF, e.g. '--slot 1,5'.")
    ap.add_argument("--no-flow-cali", action="store_true",
                    help="Disable per-print flow calibration (faster start, tiny risk)")
    ap.add_argument("--bed-type", default="",
                    help="textured_plate | cool_plate | engineering_plate | "
                         "high_temp_plate. If unset, read from the 3MF's "
                         "Metadata/plate_<N>.json (the per-plate truth) and "
                         "fall back to textured_plate only if that's missing.")
    ap.add_argument("--query-only", action="store_true",
                    help="Connect, dump AMS contents + matched slot, then exit "
                         "without uploading or starting a print. Use this to verify "
                         "auto-matching before committing to a real print run.")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="Skip the 3MF preflight validator (printer-model match, "
                         "bed_type sanity, MD5 sidecar, max-temp guards, AMS "
                         "filament cross-check). Default is to run it inline.")
    ap.add_argument("--preflight-strict", action="store_true",
                    help="With preflight: refuse to print on warnings as well as "
                         "errors (default: only errors block).")
    ap.add_argument("--verbose", "-v", action="count", default=0)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else
              logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("lan_print").setLevel(logging.INFO)

    if not args.file.exists():
        print(f"file not found: {args.file}", file=sys.stderr)
        return 1

    fil_types = read_3mf_filament_types(args.file)
    log.info("3MF declares %d filament(s): %s", len(fil_types), fil_types)
    if len(fil_types) != 1:
        log.warning("3MF has %d filaments; ams_mapping will be a list of length %d",
                    len(fil_types), len(fil_types))
    expected_type = fil_types[0] if fil_types else None

    creds = Creds.resolve(args)
    log.info("Connecting (signed MQTT) to %s [%s]…", creds.ip, creds.serial)
    client = X2DClient(creds)
    client.connect()

    # Pre-flight check — uses the same X2DClient connection's first
    # state push for the AMS cross-check so we don't double-connect.
    if not args.skip_preflight and not args.query_only:
        try:
            ams_state = client.request_state(timeout=8.0)
        except TimeoutError:
            ams_state = None
        # Resolve the printer model from the credentials section name if
        # we know it, else just run structural+MD5+temp checks.
        want_model = ""
        if "X2D" in (creds.serial or "").upper():
            want_model = "Bambu Lab X2D"
        # bed_type cross-check: pre-resolve what we'll send so warnings
        # match exactly. lan_print auto-reads from plate_<N>.json by
        # default; respecting --bed-type if explicitly set.
        bed_for_pf = args.bed_type or read_3mf_bed_type(args.file, args.plate) or ""
        result = preflight_3mf.validate(
            args.file,
            want_printer=want_model or None,
            want_bed=bed_for_pf or None,
            ams_state=ams_state,
        )
        if result.errors:
            client.disconnect()
            for f in result.findings:
                log.error("preflight %s: %s", f.severity, f.message)
            raise SystemExit("preflight rejected the 3MF — re-run with "
                             "--skip-preflight to override")
        if result.warnings and args.preflight_strict:
            client.disconnect()
            for f in result.findings:
                log.warning("preflight %s: %s", f.severity, f.message)
            raise SystemExit("preflight strict-mode: warnings present, refusing")
        for f in result.findings:
            log.warning("preflight %s [%s]: %s", f.severity, f.code, f.message)
        if not result.findings:
            log.info("preflight: clean (printer=%s bed=%s slicer=%s layers=%s "
                     "weight=%sg time=%s)",
                     result.summary.get("printer_model"),
                     result.summary.get("bed_type_from_plate_json"),
                     result.summary.get("slicer"),
                     result.summary.get("total_layers"),
                     result.summary.get("filament_weight_g"),
                     result.summary.get("estimated_time"))

    # request_state sends a signed `pushall` and waits for the next report.
    # The X2D usually replies within a second; AMS comes in either the same
    # message or the immediately-following push. Poll a few times so we
    # don't race the second push.
    state = None
    trays: list[dict] = []
    for i in range(8):  # ~8 s budget total
        try:
            state = client.request_state(timeout=1.5)
        except TimeoutError:
            continue
        trays = collect_trays(state)
        if trays:
            break
        time.sleep(0.5)
    if not trays:
        client.disconnect()
        raise SystemExit(
            "Timed out waiting for AMS state report. Is the AMS attached and "
            "powered? (The X2D will sometimes report no AMS if a tray is mid-load.)"
        )

    log.info("AMS contents (%d slot(s)):", len(trays))
    for t in trays:
        log.info("  slot %d (AMS%d.tray%d): %s %s color=%s",
                 t["slot_index"], t["ams"], t["tray"],
                 t.get("type"), t.get("sub_brand"), t.get("color"))

    n_filaments = max(len(fil_types), 1)
    forced_slots: list[int] = []
    if args.slot:
        try:
            forced_slots = [int(s.strip()) for s in args.slot.split(",") if s.strip()]
        except ValueError:
            client.disconnect()
            raise SystemExit(f"--slot expects comma-separated ints, got {args.slot!r}")
        if len(forced_slots) != n_filaments:
            client.disconnect()
            raise SystemExit(
                f"--slot has {len(forced_slots)} values but the 3MF needs "
                f"{n_filaments} filament(s). Pass one slot per filament."
            )

    color_list: list[str] = []
    if args.filament_color:
        color_list = [c.strip() for c in args.filament_color.split(",") if c.strip()]
        if color_list and len(color_list) != n_filaments:
            client.disconnect()
            raise SystemExit(
                f"--filament-color has {len(color_list)} entries but the 3MF "
                f"needs {n_filaments} filament(s). Pass one color per filament."
            )

    def _do_match_one(filament_idx: int) -> dict:
        # Per-filament expected type — multi-color 3MFs may have different
        # types per filament index (rare but possible: PLA + PETG support).
        per_type = fil_types[filament_idx] if filament_idx < len(fil_types) else expected_type
        per_color = color_list[filament_idx] if color_list else args.filament_color
        return match_slot(
            trays,
            match_substr=args.filament_match,
            match_color=per_color,
            match_info_idx=args.filament_info_idx,
            color_fuzzy_max=args.filament_color_fuzzy,
            expected_type=per_type,
        )

    if args.query_only:
        log.info("--query-only set; not uploading or printing. Exiting.")
        if not forced_slots:
            try:
                for fi in range(n_filaments):
                    chosen = _do_match_one(fi)
                    log.info("filament %d would auto-match slot %d "
                             "(type=%s color=%s info_idx=%s)",
                             fi, chosen["slot_index"], chosen.get("type"),
                             chosen.get("color"), chosen.get("info_idx"))
            except SystemExit as e:
                log.warning("Auto-match would fail: %s", e)
        client.disconnect()
        return 0

    if forced_slots:
        chosen_slots: list[int] = []
        for s in forced_slots:
            tray = next((t for t in trays if t["slot_index"] == s), None)
            if tray is None:
                client.disconnect()
                raise SystemExit(f"--slot {s} not found in AMS state")
            chosen_slots.append(s)
        log.info("Using forced slots %s", chosen_slots)
    else:
        chosen_slots = []
        for fi in range(n_filaments):
            chosen = _do_match_one(fi)
            chosen_slots.append(chosen["slot_index"])
            log.info("Auto-matched filament %d -> slot %d (type=%s color=%s info_idx=%s)",
                     fi, chosen["slot_index"], chosen.get("type"),
                     chosen.get("color"), chosen.get("info_idx"))

    # Upload + start through `beambam print` (beambam.cli.lan.cmd_print): it
    # uploads into cache/ (the only location the X2D firmware's file-verify
    # accepts; this script used to upload to the FTP root, err 84033544),
    # derives bed type/temperature from the 3MF's curr_bed_type, re-checks
    # every slot against the live AMS, refuses a busy printer, and waits
    # for the printer's verdict instead of assuming success.
    client.disconnect()
    from beambam.cli.lan import cmd_print
    log.info("Starting %s with AMS slots %s via beambam print", args.file.name, chosen_slots)
    if args.plate != 1:
        # TODO: plate selection — print_job's project_file is fixed to plate_1;
        # `beambam plate select` writes a single-plate file to print instead.
        raise SystemExit("--plate other than 1 is not supported by the start path yet; "
                         "extract the plate with `beambam plate select` first")
    return cmd_print(argparse.Namespace(
        ip=creds.ip, code=creds.code, serial=creds.serial, printer=None,
        file=str(args.file), remote=None, slot=chosen_slots,
        no_upload=False, no_ams=False, no_bed_level=False,
        bed_type=args.bed_type or None, bed_temp=None, force=False,
        flow_cali=not args.no_flow_cali, timelapse=False, vib_cali=False,
        dry_run=False, max_flush_g=10.0, confirm_timeout=45.0,
    ))


if __name__ == "__main__":
    sys.exit(main())
