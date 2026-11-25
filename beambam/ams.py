"""beambam.ams — pretty-printer + subcommands for AMS state.

Sub-CLI tree under `beambam ams`:

    beambam ams status                  # all units, all trays — table view
    beambam ams info SLOT               # one tray (slot = global 0..15)
    beambam ams load SLOT               # alias of top-level ams-load
    beambam ams unload                  # alias of top-level ams-unload
    beambam ams dry UNIT --temp N --hours M   # start drying (AMS 2 Pro/HT)
    beambam ams dry UNIT --off          # stop the drying cycle
    beambam ams set SLOT PROFILE        # push tray_info_idx + temps + color
    beambam ams sync [MAP]              # batch-push from JSON map

Color rendering uses 24-bit ANSI sequences — terminals that don't
support truecolor degrade to the plain hex. Add --no-color to force off.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from beambam.cli._helpers import _next_seq


# ----- color helpers -------------------------------------------------------


def _supports_truecolor() -> bool:
    """Heuristic: COLORTERM=truecolor / 24bit, else assume yes on most modern
    terminals (Termux, iTerm2, kitty, alacritty, GNOME Terminal all do)."""
    if os.environ.get("NO_COLOR"):
        return False
    ct = (os.environ.get("COLORTERM") or "").lower()
    if ct in ("truecolor", "24bit"):
        return True
    # Conservative: enable only when explicitly stated. CI / pipes off.
    return sys.stdout.isatty() and bool(ct)


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    """`'7C4B00FF'` or `'#7C4B00'` → (124, 75, 0). Alpha discarded."""
    h = h.lstrip("#")
    if len(h) >= 6:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 128, 128, 128


def _swatch(hex_color: str, color: bool = True) -> str:
    """A pair of colored squares + the hex code, suitable for terminal tables."""
    if not color or not _supports_truecolor():
        return f"#{hex_color.lstrip('#')[:6].upper()}"
    r, g, b = _hex_to_rgb(hex_color)
    return f"\033[48;2;{r};{g};{b}m  \033[0m #{hex_color.lstrip('#')[:6].upper()}"


# ----- tray state decoding -------------------------------------------------


# Bambu firmware tray.state values seen in the wild
_TRAY_STATE = {
    0:  "empty",
    11: "loaded",
    13: "loading",
    19: "unloading",
    27: "ACTIVE",      # currently feeding the active extruder
}


def _tray_state_label(s: int) -> str:
    return _TRAY_STATE.get(s, f"state={s}")


def humidity_level(raw: Any) -> int | None:
    """Bambu's user-facing AMS humidity level, 1 (dry) .. 5 (wet).

    The MQTT `humidity` field is an index running the OTHER way (1 = wet,
    5 = dry); Bambu Studio and Handy show `6 - index` (ha-bambulab
    definitions.py does the same). beambam used to print the raw index,
    so a damp AMS (index 2, ~40 % RH) looked nearly dry. None when the
    value is missing or outside 1..5."""
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        return None
    return 6 - idx if 1 <= idx <= 5 else None


def humidity_percent(unit: dict[str, Any]) -> int | None:
    """Measured relative humidity (%) where the AMS reports it (`humidity_raw`)."""
    try:
        return int(unit["humidity_raw"])
    except (KeyError, TypeError, ValueError):
        return None


def _humidity_bar(raw: Any) -> str:
    """5-cell bar for the MQTT humidity index: one filled cell per
    user-facing level (1 = dry .. 5 = wet), '?' when unknown."""
    level = humidity_level(raw)
    if level is None:
        return "?"
    return "▓" * level + "░" * (5 - level)


# ----- formatting ---------------------------------------------------------


def unit_routing(state: dict[str, Any]) -> tuple[dict[int, str], str]:
    """Per-AMS "which nozzle(s) can this unit feed" labels plus a one-line
    printer summary (nozzles + Filament Track Switch), from a full pushall
    state. Empty when the state has no AMS (or is just an `ams` block)."""
    from beambam.filament_mapping import nozzle_name, read_topology
    topo = read_topology(state)
    routes: dict[int, str] = {}
    for t in topo.trays:
        if t.external or t.ams_id in routes:
            continue
        if not t.extruders:
            routes[t.ams_id] = "feeds no nozzle (switch not set up)"
        elif len(t.extruders) > 1:
            routes[t.ams_id] = ("feeds both nozzles"
                                + (f" via switch input {t.switch_input}" if t.switch_input else ""))
        else:
            routes[t.ams_id] = f"feeds the {nozzle_name(next(iter(t.extruders)), topo.nozzle_count)} nozzle"
    sw = ("not installed" if not topo.switch_installed
          else "installed, set up" if topo.switch_ready else "installed, NOT set up")
    summary = f"{topo.nozzle_count} nozzle(s); Filament Track Switch {sw}"
    return routes, summary


def tray_letter_label(slot_global: int) -> str:
    """Global slot → 'A1'..'D4' (re-exported from beambam.filament_mapping)."""
    from beambam.filament_mapping import tray_letter_label as _label
    return _label(slot_global)


def format_status(ams_block: dict[str, Any], *, color: bool = True,
                  routing: dict[int, str] | None = None,
                  summary: str = "") -> str:
    """Pretty-print the print.ams block to a multi-line string. `routing`
    (from `unit_routing`) adds which nozzle(s) each unit feeds."""
    units = ams_block.get("ams") or []
    if not units:
        return "no AMS units reported by the printer"

    lines = [summary, ""] if summary else []
    for unit in units:
        uid = unit.get("id", "?")
        humidity = _humidity_bar(unit.get("humidity", "?"))
        pct = humidity_percent(unit)
        if pct is not None:
            humidity += f" {pct}%"
        temp = unit.get("temp", "?")
        # AMS HT units and external/virtual slots can carry a non-numeric
        # id; show the tray id alone there instead of crashing the whole
        # `beambam ams` output on int("?").
        try:
            unit_base = int(uid) * 4
        except (TypeError, ValueError):
            unit_base = None
        try:
            route = (routing or {}).get(int(uid), "")
        except (TypeError, ValueError):
            route = ""
        try:
            unit_name = f"AMS {chr(ord('A') + int(uid))}" if int(uid) < 16 else f"AMS HT {int(uid) - 127}"
        except (TypeError, ValueError):
            unit_name = f"AMS {uid}"
        lines.append(f"{unit_name}  humidity {humidity}  temp {temp}°C"
                     + (f"  {route}" if route else f"  info {unit.get('info', '?')}"))
        trays = unit.get("tray") or []
        for t in trays:
            try:
                tray_id = int(t.get("id", 0))
            except (TypeError, ValueError):
                tray_id = 0
            slot_global = unit_base + tray_id if unit_base is not None else tray_id
            color_str = _swatch(t.get("tray_color", "808080"), color=color)
            remain = t.get("remain", -1)
            remain_str = "—" if remain < 0 else f"{remain}%"
            tray_type = t.get("tray_type", "—") or "(empty)"
            idx = t.get("tray_info_idx", "—") or "—"
            state_label = _tray_state_label(int(t.get("state", 0)))
            mark = " ◀" if state_label == "ACTIVE" else "  "
            lines.append(f"  {tray_letter_label(slot_global)} slot {slot_global:>2}"
                         f" {color_str}  {tray_type:<8} {idx:<6}"
                         f" {remain_str:>4}  {state_label}{mark}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_tray_info(ams_block: dict[str, Any], slot: int,
                     *, color: bool = True) -> str:
    """Format one tray in detail. `slot` is global (0..15: unit*4 + tray)."""
    unit_id, tray_id = slot // 4, slot % 4
    units = ams_block.get("ams") or []
    unit = next((u for u in units if str(u.get("id")) == str(unit_id)), None)
    if unit is None:
        return f"no AMS unit {unit_id} (have {[u.get('id') for u in units]})"
    trays = unit.get("tray") or []
    tray = next((t for t in trays if str(t.get("id")) == str(tray_id)), None)
    if tray is None:
        return f"no tray {tray_id} in AMS {unit_id}"

    lines = [f"slot {slot} (AMS {unit_id}, tray {tray_id})"]
    lines.append(f"  color:        {_swatch(tray.get('tray_color', ''), color)}")
    lines.append(f"  filament:     {tray.get('tray_type')}"
                 f"  ({tray.get('tray_sub_brands') or 'generic'})")
    lines.append(f"  tray_info:    {tray.get('tray_info_idx')}  "
                 f"diameter {tray.get('tray_diameter')}mm")
    lines.append(f"  nozzle range: {tray.get('nozzle_temp_min')}..{tray.get('nozzle_temp_max')}°C")
    lines.append(f"  bed:          {tray.get('bed_temp')}°C "
                 f"(type {tray.get('bed_temp_type')})")
    lines.append(f"  drying:       {tray.get('drying_temp')}°C "
                 f"× {tray.get('drying_time')}h")
    lines.append(f"  remaining:    {tray.get('remain')}% (RFID weight {tray.get('tray_weight')})")
    lines.append(f"  state:        {_tray_state_label(int(tray.get('state', 0)))} "
                 f"(raw {tray.get('state')})")
    lines.append(f"  tag uid:      {tray.get('tag_uid')}")
    return "\n".join(lines)


# ----- profile → tray metadata --------------------------------------------


# Bambu's "tray_info_idx" classifies the spool to the firmware's filament
# library. For 3rd-party / generic filaments we use the open generic IDs that
# Bambu Studio itself writes for community profiles.
# Bambu's generic "@base" filament ids (from resources/profiles/BBL/filament,
# verified against the installed Bambu Studio catalogue). Used when a 3rd-party
# profile has no setting_id of its own.
_GENERIC_INFO_IDX = {
    "PLA":      "GFL99",
    "PLA-CF":   "GFL98",
    "PLA Silk": "GFL96",
    "PETG":     "GFG99",
    "PETG-CF":  "GFG98",
    "PCTG":     "GFG97",
    "TPU":      "GFU99",
    "ABS":      "GFB99",
    "ASA":      "GFB98",
    "PC":       "GFC99",
    "PA":       "GFN99",
    "PA-CF":    "GFN98",
    "PVA":      "GFS99",
    "HIPS":     "GFS98",
    "BVOH":     "GFS97",
    "PP":       "GFP97",
    "PE":       "GFP99",
    "PPS":      "GFT97",
    "PPS-CF":   "GFT98",
    "PHA":      "GFR98",
    "EVA":      "GFR99",
}


def _generic_info_idx(tray_type: str) -> str:
    """Generic filament id for a type, trying the exact name then its base
    polymer (so "PLA-CF Plus" -> PLA-CF -> PLA). Unknown types return "" —
    better a blank tray_info than telling the firmware an ABS spool is PLA."""
    t = (tray_type or "").strip()
    if t in _GENERIC_INFO_IDX:
        return _GENERIC_INFO_IDX[t]
    base = t.split()[0] if t else ""           # "PLA-CF Plus" -> "PLA-CF"
    if base in _GENERIC_INFO_IDX:
        return _GENERIC_INFO_IDX[base]
    root = base.split("-")[0]                   # "PLA-CF" -> "PLA"
    return _GENERIC_INFO_IDX.get(root, "")


def _load_flat_profile(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if d.get("type") != "filament":
        raise ValueError(f"{path}: not a filament profile (type={d.get('type')!r})")
    return d


def _profile_tray_fields(profile: dict[str, Any]) -> dict[str, Any]:
    """Extract the AMS-side fields from a flat filament profile."""
    ftype_arr = profile.get("filament_type") or []
    if not ftype_arr:
        raise ValueError(f"profile {profile.get('name')!r} missing filament_type")
    tray_type = str(ftype_arr[0]).upper()

    # tray_sub_brands is the slicer-visible label; strip the printer suffix
    # so the AMS UI shows e.g. "eSUN PLA+" instead of "eSUN PLA+ @BBL X2D ..."
    name = str(profile.get("name", ""))
    sub_brand = name.split("@")[0].strip() or name or tray_type

    info_idx = _generic_info_idx(tray_type)

    n_lo = int(str(profile.get("nozzle_temperature_range_low", ["190"])[0]))
    n_hi = int(str(profile.get("nozzle_temperature_range_high", ["240"])[0]))
    setting_id = profile.get("setting_id") or None
    return {
        "tray_type": tray_type,
        "tray_sub_brands": sub_brand,
        "tray_info_idx": info_idx,
        "nozzle_temp_min": n_lo,
        "nozzle_temp_max": n_hi,
        "setting_id": str(setting_id) if setting_id else None,
    }


def _current_tray_color(ams_block: dict[str, Any], slot: int) -> str | None:
    """Look up the AMS-reported color for `slot`. Returns None if empty."""
    unit_id, tray_id = divmod(int(slot), 4)
    for unit in ams_block.get("ams") or []:
        if str(unit.get("id")) != str(unit_id):
            continue
        for t in unit.get("tray") or []:
            if str(t.get("id")) == str(tray_id):
                col = (t.get("tray_color") or "").strip()
                return col or None
    return None


def _normalize_tray_color(c: str) -> str:
    """Accept '#7C4B00', '7C4B00', or '7C4B00FF' and return canonical
    8-char upper-hex 'RRGGBBAA'. 6-char inputs get FF alpha appended.
    Raises ValueError on bad shape so callers fail at config-load time
    rather than mid-MQTT-publish."""
    h = c.lstrip("#").upper()
    if len(h) == 6:
        h += "FF"
    if len(h) != 8 or any(ch not in "0123456789ABCDEF" for ch in h):
        raise ValueError(
            f"tray_color {c!r} must be RRGGBB or RRGGBBAA hex")
    return h


def build_tray_metadata_payload(
    slot: int,
    *,
    tray_type: str,
    tray_info_idx: str,
    nozzle_temp_min: int,
    nozzle_temp_max: int,
    tray_color: str | None = None,
    setting_id: str | None = None,
) -> dict[str, Any]:
    """Pure payload-builder for `print.ams_filament_setting` MQTT push.

    Used by both `Printer.set_tray_metadata` (which then publishes) and
    `beambam ams set --dry-run` (which just prints the dict).
    `slot` is global 0..15 (unit*4 + tray). Color is auto-canonicalized
    to RRGGBBAA upper-hex. Raises ValueError for out-of-range slot or
    malformed color so misconfigurations surface up-front.
    """
    if not 0 <= slot <= 15:
        raise ValueError(f"slot {slot} out of range (0..15)")
    ams_id, slot_id = divmod(int(slot), 4)
    body: dict[str, Any] = {
        "sequence_id": _next_seq(),   # signed: must clear the firmware floor
        "command": "ams_filament_setting",
        "ams_id": ams_id,
        "slot_id": slot_id,
        "tray_id": slot_id,
        "tray_info_idx": tray_info_idx,
        "setting_id": setting_id or "",
        "tray_type": tray_type,
        "nozzle_temp_min": int(nozzle_temp_min),
        "nozzle_temp_max": int(nozzle_temp_max),
    }
    if tray_color is not None:
        body["tray_color"] = _normalize_tray_color(tray_color)
    return {"print": body}


# ----- CLI dispatch -------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    from beambam.print_job import slot_arg
    p = sub.add_parser(
        "ams",
        help="AMS state + control. Subcommands: status, info <slot>, "
             "map <file>, load <slot>, unload, dry <unit>, set <slot> <profile>, "
             "sync [<map>].",
    )
    ams_sub = p.add_subparsers(dest="ams_cmd", required=True)

    s = ams_sub.add_parser("status", help="Pretty-print all AMS units + trays")
    s.add_argument("--no-color", action="store_true",
                   help="Disable ANSI color output")
    s.add_argument("--json", dest="json_out", action="store_true",
                   help="Emit the raw print.ams block as JSON")

    i = ams_sub.add_parser("info", help="Detail for one tray")
    i.add_argument("slot", type=int,
                   help="Global slot (unit*4 + tray, 0..15)")
    i.add_argument("--no-color", action="store_true")

    mp = ams_sub.add_parser(
        "map",
        help="Preview how a sliced file's filaments would map to the loaded "
             "trays (the same plan `beambam print` confirms) without printing.",
        description="Reads the plate's used filaments and their nozzles from the "
                    ".gcode.3mf, reads the AMS units, external spools and Filament "
                    "Track Switch from the printer, and shows the tray each filament "
                    "would print from, with errors and warnings. Sends nothing to "
                    "the printer apart from the state request.")
    mp.add_argument("file", help="Sliced .gcode.3mf to map")
    mp.add_argument("--plate", type=int, default=1, help="Which sliced plate to map.")
    mp.add_argument("--slot", type=slot_arg, default=None, metavar="SLOT[,SLOT...]",
                    help="Force trays by global slot, one per used filament in order "
                         "(as print --slot), e.g. 4 or 4,0,12.")
    mp.add_argument("--map", metavar="F=TRAY,...",
                    help="Explicit FILAMENT=TRAY choices, as print --map.")
    mp.add_argument("--no-ams", action="store_true",
                    help="Map to the external spools instead of the AMS.")
    mp.add_argument("--no-color", action="store_true", help="Disable ANSI colour.")
    mp.add_argument("--json", dest="json_out", action="store_true",
                    help="Emit the plan, including the ams_mapping arrays, as JSON.")

    ld = ams_sub.add_parser("load", help="Load filament from a slot (alias)")
    ld.add_argument("slot", type=int)

    ams_sub.add_parser("unload", help="Unload the currently-loaded filament")

    d = ams_sub.add_parser(
        "dry", help="Start/stop a drying cycle on a heated AMS (2 Pro / HT)")
    d.add_argument("unit", type=int,
                   help="AMS unit id as the printer reports it (0..3 for "
                        "standard AMS; 128+ for AMS HT)")
    d.add_argument("--temp", type=int,
                   help="Drying temperature °C (must be >=45; AMS 2 Pro max "
                        "65, AMS HT max 85). Required unless --off.")
    d.add_argument("--hours", type=int,
                   help="Cycle duration in whole hours (>=1). Required unless "
                        "--off.")
    d.add_argument("--cooling-temp", type=int, default=None,
                   help="Cooling temperature °C (must be >=45; defaults to "
                        "--temp when omitted).")
    d.add_argument("--rotate-tray", action="store_true",
                   help="AMS HT rotating-dry mode (rotate the spool for even "
                        "drying). Ignored by AMS 2 Pro.")
    d.add_argument("--off", action="store_true",
                   help="Stop the current drying cycle on this unit.")

    st = ams_sub.add_parser("set",
        help="Push filament metadata (type/brand/temps/color) to one slot")
    st.add_argument("slot", type=int, help="Global slot (0..15)")
    st.add_argument("profile",
        help="Path to a flat filament profile JSON "
             "(e.g. flat-profiles/eSUN PLA+ @BBL X2D 0.4 nozzle.json)")
    st.add_argument("--color",
        help="Override the slot color (RRGGBB or RRGGBBAA hex). "
             "Omit to keep the current AMS color tag.")
    st.add_argument("--dry-run", action="store_true",
        help="Print the MQTT payload but don't publish.")

    sy = ams_sub.add_parser("sync",
        help="Push slot→profile assignments in batch.")
    sy.add_argument("map_path", nargs="?", default=None,
        help="ams-sync.json path (default: flat-profiles/ams-sync.json "
             "next to the profiles).")
    sy.add_argument("--dry-run", action="store_true",
        help="Print payloads but don't publish.")
    sy.add_argument("--profiles-dir", default=None,
        help="Where to resolve profile filenames (default: directory "
             "of the sync map).")

    p.set_defaults(fn=cmd_ams)
    return p


def fetch_printer_state(args: argparse.Namespace, *,
                        timeout: float = 10.0) -> dict | None:
    """Pull the printer's full pushall state, LAN first then CLOUD fallback.

    The LAN path (`Printer.state`) is fastest when on the printer's network;
    if it fails (off-LAN / printer asleep) we fall back to the cloud broker via
    `cloud_pull_state`, which merges the multi-message pushall so `ams` is
    actually captured. Returns the `{"print": {...}}`-shaped dict, or None when
    both transports fail."""
    from beambam import Printer
    try:
        with Printer() as printer:
            return printer.state(timeout=timeout)
    except Exception as lan_err:                           # noqa: BLE001
        try:
            import cloud_client
            from beambam.cli.cloud import cloud_pull_state
            from beambam import config as _config
            cli = cloud_client.CloudClient.load_or_anonymous()
            if cli.session.empty:
                raise lan_err
            serial = (_config.Creds.resolve(args).serial
                      or os.environ.get("X2D_SERIAL"))
            if not serial:
                raise lan_err
            pr = cloud_pull_state(cli, serial, timeout=timeout + 4)
            if pr:
                print("[ams] LAN unreachable — read via cloud", file=sys.stderr)
                return {"print": pr}
        except Exception:                                  # noqa: BLE001
            pass
        print(f"failed to fetch state (LAN: {lan_err}; cloud fallback failed)",
              file=sys.stderr)
        return None


def _cmd_ams_map(args: argparse.Namespace) -> int:
    """`beambam ams map FILE` — show the filament → tray plan, print nothing.
    Exit 0 when the plan is clean, 1 when it has errors, 2 for warnings only."""
    from beambam.filament_mapping import (
        MappingError, format_plan, parse_map, plan_print, plan_to_json, read_project,
    )
    try:
        project = read_project(args.file, args.plate)
        explicit = parse_map(args.map) if args.map else None
    except MappingError as e:
        print(f"ams map: {e}", file=sys.stderr)
        return 1
    state = fetch_printer_state(args, timeout=10.0)
    if state is None:
        return 1
    try:
        plan = plan_print(project, state, use_ams=not args.no_ams,
                          slot=args.slot, explicit=explicit)
    except MappingError as e:
        print(f"ams map: {e}", file=sys.stderr)
        return 1
    if args.json_out:
        print(json.dumps(plan_to_json(plan), indent=2))
    else:
        print(format_plan(plan, color=not args.no_color and sys.stdout.isatty()), end="")
    return 1 if plan.errors else 2 if plan.warnings else 0


def cmd_ams(args: argparse.Namespace) -> int:
    from beambam import Printer

    if args.ams_cmd in ("status", "info"):
        # Read-only: pull state (LAN, cloud fallback).
        state = fetch_printer_state(args, timeout=10.0)
        if state is None:
            return 1
        ams_block = state.get("print", {}).get("ams", {})
        if args.ams_cmd == "status":
            if args.json_out:
                print(json.dumps(ams_block, indent=2))
            else:
                routing, summary = unit_routing(state)
                print(format_status(ams_block, color=not args.no_color,
                                    routing=routing, summary=summary))
        else:                                               # info
            print(format_tray_info(ams_block, args.slot,
                                    color=not args.no_color))
        return 0

    if args.ams_cmd == "map":
        return _cmd_ams_map(args)

    if args.ams_cmd == "load":
        with Printer() as printer:
            printer.ams_load(args.slot)
        print(f"ams_load slot={args.slot} published")
        return 0

    if args.ams_cmd == "unload":
        with Printer() as printer:
            printer.ams_unload()
        print("ams_unload published")
        return 0

    if args.ams_cmd == "set":
        try:
            profile = _load_flat_profile(args.profile)
        except (OSError, ValueError) as e:
            print(f"failed to load profile: {e}", file=sys.stderr)
            return 1
        fields = _profile_tray_fields(profile)

        color = args.color
        if color is None and not args.dry_run:
            # Preserve the current AMS color tag unless overridden.
            with Printer() as printer:
                state = printer.state(timeout=10.0)
            ams_block = state.get("print", {}).get("ams", {})
            color = _current_tray_color(ams_block, args.slot)

        if args.dry_run:
            try:
                payload = build_tray_metadata_payload(
                    args.slot,
                    tray_type=fields["tray_type"],
                    tray_info_idx=fields["tray_info_idx"],
                    nozzle_temp_min=fields["nozzle_temp_min"],
                    nozzle_temp_max=fields["nozzle_temp_max"],
                    tray_color=color,
                    setting_id=fields["setting_id"],
                )
            except ValueError as e:
                print(f"bad input: {e}", file=sys.stderr)
                return 1
            print(json.dumps(payload, indent=2))
            return 0

        from beambam.commands import CommandRefused
        with Printer() as printer:
            try:
                printer.set_tray_metadata(
                    args.slot,
                    tray_type=fields["tray_type"],
                    tray_info_idx=fields["tray_info_idx"],
                    nozzle_temp_min=fields["nozzle_temp_min"],
                    nozzle_temp_max=fields["nozzle_temp_max"],
                    tray_color=color,
                    setting_id=fields["setting_id"],
                )
            except CommandRefused as e:
                print(f"ams set: printer refused slot {args.slot} — {e}", file=sys.stderr)
                return 1
        print(f"slot {args.slot}: {fields['tray_sub_brands']} "
              f"({fields['tray_type']}, {fields['nozzle_temp_min']}-"
              f"{fields['nozzle_temp_max']}°C)"
              + (f" color #{color.lstrip('#').upper()}" if color else "")
              + " published")
        return 0

    if args.ams_cmd == "sync":
        # Resolve map path. Search order when --map-path is not given:
        #   1. $PWD/flat-profiles/ams-sync.json (running from a repo checkout)
        #   2. $PWD/ams-sync.json (already inside the profiles dir)
        #   3. ~/.config/beambam/ams-sync.json (per-user canonical location)
        map_path = args.map_path
        if map_path is None:
            candidates = [
                os.path.join(os.getcwd(), "flat-profiles", "ams-sync.json"),
                os.path.join(os.getcwd(), "ams-sync.json"),
                os.path.expanduser("~/.config/beambam/ams-sync.json"),
            ]
            map_path = next((p for p in candidates if os.path.exists(p)), None)
            if map_path is None:
                print("sync map not found. Tried:\n  " + "\n  ".join(candidates),
                      file=sys.stderr)
                return 1
        elif not os.path.exists(map_path):
            print(f"sync map not found: {map_path}", file=sys.stderr)
            return 1
        profiles_dir = args.profiles_dir or os.path.dirname(
            os.path.abspath(map_path))

        try:
            with open(map_path, encoding="utf-8") as f:
                sync_doc = json.load(f)
        except (OSError, ValueError) as e:
            print(f"failed to load sync map: {e}", file=sys.stderr)
            return 1
        entries = sync_doc.get("slots") or []
        if not entries:
            print("sync map has no 'slots' entries", file=sys.stderr)
            return 1

        # Resolve + validate every profile up front so we don't push half a
        # batch and then crash.
        resolved: list[tuple[int, dict[str, Any], str | None]] = []
        for entry in entries:
            slot = int(entry["slot"])
            prof_rel = entry["profile"]
            prof_path = (prof_rel if os.path.isabs(prof_rel)
                         else os.path.join(profiles_dir, prof_rel))
            try:
                profile = _load_flat_profile(prof_path)
                fields = _profile_tray_fields(profile)
            except (OSError, ValueError) as exc:
                print(f"slot {slot}: failed to prepare {prof_rel!r}: {exc}",
                      file=sys.stderr)
                return 1
            resolved.append((slot, fields, entry.get("color")))

        # Pull live state once to fill in colors we don't override.
        live_colors: dict[int, str | None] = {}
        if not args.dry_run and any(c is None for (_s, _f, c) in resolved):
            with Printer() as printer:
                state = printer.state(timeout=10.0)
            ams_block = state.get("print", {}).get("ams", {})
            for slot, _f, _c in resolved:
                live_colors[slot] = _current_tray_color(ams_block, slot)

        if args.dry_run:
            for slot, fields, override_color in resolved:
                color = override_color
                print(f"slot {slot:>2}: "
                      f"{fields['tray_sub_brands']:<28} "
                      f"{fields['tray_type']:<5} "
                      f"{fields['nozzle_temp_min']}-{fields['nozzle_temp_max']}°C "
                      f"color={color or '(keep)'}")
            return 0

        from beambam.commands import CommandRefused
        with Printer() as printer:
            for slot, fields, override_color in resolved:
                color = override_color or live_colors.get(slot)
                try:
                    printer.set_tray_metadata(
                        slot,
                        tray_type=fields["tray_type"],
                        tray_info_idx=fields["tray_info_idx"],
                        nozzle_temp_min=fields["nozzle_temp_min"],
                        nozzle_temp_max=fields["nozzle_temp_max"],
                        tray_color=color,
                        setting_id=fields["setting_id"],
                    )
                except CommandRefused as e:
                    # Stop at the first refusal: the rest would most likely
                    # fail the same way (signing, or a print running).
                    print(f"ams sync: printer refused slot {slot} — {e}", file=sys.stderr)
                    return 1
                print(f"  slot {slot:>2} ← {fields['tray_sub_brands']}")
        print(f"sync complete: {len(resolved)} slot(s) updated")
        return 0

    if args.ams_cmd == "dry":
        # AMS drying control. The command is `print.ams_filament_drying`
        # (reverse-engineered + field-verified on AMS 2 Pro / AMS HT, see
        # greghesp/ha-bambulab#1448). It is a print-class command, so it must
        # be SIGNED with the recovered key — hence send_command, which also
        # reads the printer's success/fail reply instead of assuming success.
        #
        # Firmware-enforced constraints learned from that work:
        #   * temp and cooling_temp must be >= 45 or the command is ignored.
        #   * duration is whole HOURS, >= 1 — duration 0 is the STOP form and
        #     otherwise drives the UI to a bogus ~infinite time.
        #   * mode 1 = start, 0 = stop.
        #   * drying while a print runs is refused by firmware (err 402702444).
        from beambam import Printer
        from beambam.commands import send_command, refusal_message
        ams_id = int(args.unit)
        if args.off:
            payload = {"print": {"sequence_id": _next_seq(), "command": "ams_filament_drying",
                                 "ams_id": ams_id, "temp": 0, "cooling_temp": 40,
                                 "duration": 0, "humidity": 0, "mode": 0,
                                 "rotate_tray": False}}
        else:
            if args.temp is None or args.hours is None:
                print("ams dry: --temp and --hours are required to start a cycle "
                      "(or pass --off to stop one)", file=sys.stderr)
                return 2
            temp = int(args.temp)
            cooling = int(args.cooling_temp) if args.cooling_temp is not None else temp
            if temp < 45 or cooling < 45:
                print(f"ams dry: temp/cooling-temp must be >=45 °C (the firmware "
                      f"ignores lower values); got temp={temp} cooling={cooling}",
                      file=sys.stderr)
                return 2
            if int(args.hours) < 1:
                print("ams dry: --hours must be >=1 (0 is the stop form; use --off)",
                      file=sys.stderr)
                return 2
            payload = {"print": {"sequence_id": _next_seq(), "command": "ams_filament_drying",
                                 "ams_id": ams_id, "temp": temp, "cooling_temp": cooling,
                                 "duration": int(args.hours), "humidity": 0, "mode": 1,
                                 "rotate_tray": bool(args.rotate_tray)}}
        with Printer() as printer:
            reply = send_command(printer.mqtt, payload, timeout=8.0)
        if reply is not None and not reply.ok:
            print(f"ams dry: printer refused — {refusal_message(reply)}", file=sys.stderr)
            return 1
        if args.off:
            print(f"drying stopped: AMS {ams_id}"
                  + ("" if reply is not None else " (sent; no reply seen)"))
        else:
            print(f"drying started: AMS {ams_id} {args.temp}°C × {args.hours}h"
                  + (" (rotating)" if args.rotate_tray else "")
                  + ("" if reply is not None else " (sent; no reply seen)"))
        return 0

    print(f"unknown ams subcommand: {args.ams_cmd}", file=sys.stderr)
    return 2


def build_change_filament_payload(tray: int | None, *, curr_temp: int = 215,
                                  tar_temp: int = 215,
                                  extruder: int | None = None) -> dict[str, Any]:
    """`print.ams_change_filament` — load global tray `tray`, or unload (None).

    Mirrors MachineObject::command_ams_change_filament (DeviceManager.cpp:1537):
    `ams_id` / `slot_id` name the tray, `target` is the global tray id
    (ams*4 + slot; the external spools 254/255 send their own id with slot 0),
    and unload is `target = slot_id = 255`. `extruder` (0 right, 1 left) is
    sent as `extruder_id` on dual-nozzle printers, as Bambu Studio does when a
    nozzle is chosen. Raises ValueError for a tray id that cannot exist."""
    from beambam.cli._helpers import _print_cmd
    body: dict[str, Any] = {"curr_temp": int(curr_temp), "tar_temp": int(tar_temp)}
    if tray is None:
        # Unload: ams_id is ignored by the firmware for the unload sentinel;
        # Bambu Studio sends the loaded tray's unit, 255 when unknown.
        body.update(ams_id=255, target=255, slot_id=255)
    elif tray in (254, 255):
        body.update(ams_id=tray, target=tray, slot_id=0)
    elif 0 <= tray < 64:
        body.update(ams_id=tray // 4, target=tray, slot_id=tray % 4)
    else:
        raise ValueError(f"tray {tray} is not an AMS tray (0..63) or external spool (254/255)")
    if extruder is not None:
        if extruder not in (0, 1):
            raise ValueError("extruder must be 0 (right) or 1 (left)")
        body["extruder_id"] = int(extruder)
    return _print_cmd("ams_change_filament", **body)
