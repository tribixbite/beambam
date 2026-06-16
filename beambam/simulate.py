"""beambam.simulate — dry-run MQTT command preview.

Builds the same signed wire payload `beambam print` (or pause/resume/
gcode/etc.) would send, but prints it as pretty JSON instead of
publishing to the printer. Useful for:

  * Verifying ams_mapping / bed_type / file references BEFORE the print
    starts hot.
  * CI regression checks (golden-file diff: ensure the signed envelope
    is byte-stable across refactors).
  * Demoing what the bridge does, without a printer in the room.

Usage:

    beambam simulate print model.gcode.3mf --slot 3
    beambam simulate print model.gcode.3mf --slot 1,5,9
    beambam simulate pause
    beambam simulate gcode "G28 Z"
    beambam simulate light --on

The output is the SIGNED envelope (sign_payload wraps it), exactly what
the bridge would publish to device/<serial>/request. Cert/sig are
deterministic for a given payload so the golden-file diff is stable.
"""
from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path
from typing import Any


class _CapturingClient:
    """Mock X2DClient that captures published payloads instead of
    sending them. Drop-in for any function that calls .publish() and
    .creds.serial."""

    def __init__(self, serial: str = "SIMULATED000000") -> None:
        self.creds = types.SimpleNamespace(serial=serial)
        self.captured: list[dict[str, Any]] = []

    def publish(self, payload: dict[str, Any], qos: int = 1, **_) -> None:
        self.captured.append(payload)


def simulate_start_print(
    gcode_filename: str,
    *,
    use_ams: bool = True,
    ams_slot: int | list[int] = 0,
    bed_type: str = "cool_plate",
    bed_temp: int = 35,
    local_path: str | Path | None = None,
    timelapse: bool = False,
    flow_cali: bool = False,
    bed_levelling: bool = True,
    vibration_cali: bool = False,
    serial: str = "SIMULATED000000",
    sign: bool = True,
) -> dict[str, Any]:
    """Build a start_print payload. Returns the SIGNED envelope (with
    cert + signature) by default; pass sign=False to get the inner
    unsigned dict."""
    from beambam.mqtt import sign_payload
    from beambam.print_job import start_print
    client = _CapturingClient(serial=serial)
    # _CapturingClient is a deliberate duck-typed stand-in for X2DClient — it
    # captures the payload instead of publishing, so start_print never touches a
    # socket here. It has the .creds/.publish surface start_print uses.
    start_print(
        client,  # type: ignore[arg-type]
        gcode_filename,
        use_ams=use_ams, ams_slot=ams_slot,
        bed_type=bed_type, bed_temp=bed_temp,
        local_path=Path(local_path) if local_path else None,
        timelapse=timelapse, flow_cali=flow_cali,
        bed_levelling=bed_levelling, vibration_cali=vibration_cali,
    )
    if not client.captured:
        raise RuntimeError("start_print produced no payload")
    payload = client.captured[0]
    if sign:
        return sign_payload(payload)
    return payload


def simulate_simple(command: str, *, param: str | None = None,
                    target: int | None = None,
                    serial: str = "SIMULATED000000",
                    sign: bool = True) -> dict[str, Any]:
    """Build a simple print-control payload (pause/resume/stop/gcode/
    ams_change_filament). Returns the SIGNED envelope by default."""
    from beambam.mqtt import sign_payload
    from beambam.print_job import start_print
    inner: dict[str, Any] = {"sequence_id": "0", "command": command}
    if param is not None:
        inner["param"] = param
    if target is not None:
        inner["target"] = target
        inner["curr_temp"] = 215
        inner["tar_temp"] = 215
    payload = {"print": inner}
    return sign_payload(payload) if sign else payload


def simulate_light(*, on: bool, serial: str = "SIMULATED000000",
                   sign: bool = True) -> dict[str, Any]:
    """Build a chamber-light ledctrl payload."""
    from beambam.mqtt import sign_payload
    from beambam.print_job import start_print
    payload = {
        "system": {
            "sequence_id": "0",
            "command": "ledctrl",
            "led_node": "chamber_light",
            "led_mode": "on" if on else "off",
            "led_on_time": 500,
            "led_off_time": 500,
            "loop_times": 0,
            "interval_time": 0,
        }
    }
    return sign_payload(payload) if sign else payload


# ---------------------------------------------------------------------------
# CLI entry point — invoked from `beambam simulate <subcmd>`
# ---------------------------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    p = sub.add_parser(
        "simulate",
        help="Dry-run: build a signed MQTT payload but print it as JSON "
             "instead of publishing. Doesn't talk to the printer.",
    )
    sim_sub = p.add_subparsers(dest="sim_cmd", required=True)

    sp = sim_sub.add_parser("print", help="Simulate beambam print")
    sp.add_argument("file", help="Path to a .gcode.3mf file (or just filename)")
    sp.add_argument("--slot", default="0",
                    help="AMS global slot (0..15). For multi-color, pass "
                         "comma-separated: 1,5,9. Default: 0")
    sp.add_argument("--no-ams", action="store_true",
                    help="External-spool mode (use_ams=False)")
    sp.add_argument("--bed-type", default="cool_plate")
    sp.add_argument("--bed-temp", type=int, default=35)
    sp.add_argument("--timelapse", action="store_true")
    sp.add_argument("--no-sign", action="store_true",
                    help="Output the inner unsigned dict")

    for c in ("pause", "resume", "stop"):
        cmd = sim_sub.add_parser(c, help=f"Simulate {c}")
        cmd.add_argument("--no-sign", action="store_true")

    sg = sim_sub.add_parser("gcode", help="Simulate raw gcode line")
    sg.add_argument("line", help="G-code line to send")
    sg.add_argument("--no-sign", action="store_true")

    sl = sim_sub.add_parser("light", help="Simulate chamber light toggle")
    g = sl.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", dest="light_on", action="store_true")
    g.add_argument("--off", dest="light_on", action="store_false")
    sl.add_argument("--no-sign", action="store_true")

    p.set_defaults(fn=cmd_simulate)
    return p


def cmd_simulate(args: argparse.Namespace) -> int:
    """Dispatcher for the `simulate` subcommand."""
    sign = not getattr(args, "no_sign", False)
    try:
        if args.sim_cmd == "print":
            slot_raw = args.slot
            slots: int | list[int]
            if "," in slot_raw:
                slots = [int(s) for s in slot_raw.split(",") if s]
            else:
                slots = int(slot_raw)
            payload = simulate_start_print(
                args.file,
                use_ams=not args.no_ams,
                ams_slot=slots,
                bed_type=args.bed_type,
                bed_temp=args.bed_temp,
                local_path=args.file,
                timelapse=args.timelapse,
                sign=sign,
            )
        elif args.sim_cmd in ("pause", "resume", "stop"):
            payload = simulate_simple(args.sim_cmd, sign=sign)
        elif args.sim_cmd == "gcode":
            line = args.line + ("\n" if not args.line.endswith("\n") else "")
            payload = simulate_simple("gcode_line", param=line, sign=sign)
        elif args.sim_cmd == "light":
            payload = simulate_light(on=args.light_on, sign=sign)
        else:
            print(f"error: unknown simulate subcommand: {args.sim_cmd}",
                  file=sys.stderr)
            return 2
    except Exception as e:                                  # noqa: BLE001
        print(f"simulate failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2, sort_keys=False))
    return 0
