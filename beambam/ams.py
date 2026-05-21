"""beambam.ams — pretty-printer + subcommands for AMS state.

Sub-CLI tree under `beambam ams`:

    beambam ams status                  # all units, all trays — table view
    beambam ams info SLOT               # one tray (slot = global 0..15)
    beambam ams load SLOT               # alias of top-level ams-load
    beambam ams unload                  # alias of top-level ams-unload
    beambam ams dry UNIT --temp N --hours M   # start drying cycle

Color rendering uses 24-bit ANSI sequences — terminals that don't
support truecolor degrade to the plain hex. Add --no-color to force off.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


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


def _humidity_bar(level: str) -> str:
    """Convert humidity '0'..'4' to a 5-cell bar (4 = wet, 0 = dry).
    Lower numbers print fewer fill blocks."""
    try:
        n = int(level)
    except (TypeError, ValueError):
        return "?"
    n = max(0, min(4, n))
    return "▓" * n + "░" * (4 - n)


# ----- formatting ---------------------------------------------------------


def format_status(ams_block: dict[str, Any], *, color: bool = True) -> str:
    """Pretty-print the print.ams block to a multi-line string."""
    units = ams_block.get("ams") or []
    if not units:
        return "no AMS units reported by the printer"

    lines = []
    for unit in units:
        uid = unit.get("id", "?")
        humidity = _humidity_bar(unit.get("humidity", "?"))
        temp = unit.get("temp", "?")
        lines.append(f"AMS {uid}  humidity {humidity}  temp {temp}°C  "
                     f"version {unit.get('info', '?')}")
        trays = unit.get("tray") or []
        for t in trays:
            slot_global = int(uid) * 4 + int(t.get("id", 0))
            color_str = _swatch(t.get("tray_color", "808080"), color=color)
            remain = t.get("remain", -1)
            remain_str = "—" if remain < 0 else f"{remain}%"
            tray_type = t.get("tray_type", "—") or "(empty)"
            idx = t.get("tray_info_idx", "—") or "—"
            state_label = _tray_state_label(int(t.get("state", 0)))
            mark = " ◀" if state_label == "ACTIVE" else "  "
            lines.append(f"  slot {slot_global:>2} [{t.get('id', '?')}]"
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


# ----- CLI dispatch -------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    p = sub.add_parser(
        "ams",
        help="AMS state + control. Subcommands: status, info <slot>, "
             "load <slot>, unload, dry <unit>.",
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

    ld = ams_sub.add_parser("load", help="Load filament from a slot (alias)")
    ld.add_argument("slot", type=int)

    ams_sub.add_parser("unload", help="Unload the currently-loaded filament")

    d = ams_sub.add_parser("dry", help="Start a drying cycle on an AMS unit")
    d.add_argument("unit", type=int, help="AMS unit id (0..3)")
    d.add_argument("--temp", type=int, required=True,
                   help="Drying temperature °C (typically 40..70)")
    d.add_argument("--hours", type=int, required=True,
                   help="Cycle duration in hours (1..24)")

    p.set_defaults(fn=cmd_ams)
    return p


def cmd_ams(args: argparse.Namespace) -> int:
    from beambam import Printer

    if args.ams_cmd in ("status", "info"):
        # Read-only: pull state.
        try:
            with Printer() as printer:
                state = printer.state(timeout=10.0)
        except Exception as e:                              # noqa: BLE001
            print(f"failed to fetch state: {e}", file=sys.stderr)
            return 1
        ams_block = state.get("print", {}).get("ams", {})
        if args.ams_cmd == "status":
            if args.json_out:
                print(json.dumps(ams_block, indent=2))
            else:
                print(format_status(ams_block, color=not args.no_color))
        else:                                               # info
            print(format_tray_info(ams_block, args.slot,
                                    color=not args.no_color))
        return 0

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

    if args.ams_cmd == "dry":
        # Drying control payload — Bambu uses `ams.cmd=drying_set` with
        # target_temp + target_time. Sent against the AMS unit, not the
        # active extruder.
        from beambam import Printer
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "ams_filament_setting",
                "ams_id": int(args.unit),
                "tray_id": 255,                              # whole unit
                "drying_temp": int(args.temp),
                "drying_time": int(args.hours),
            }
        }
        with Printer() as printer:
            printer.mqtt.publish(payload)
        print(f"drying cycle requested: AMS {args.unit} {args.temp}°C × {args.hours}h")
        return 0

    print(f"unknown ams subcommand: {args.ams_cmd}", file=sys.stderr)
    return 2
