"""beambam.cli.info — read-only / status `cmd_*` handlers.

Phase 5c scaffold (`docs/BRIDGE_SPLIT_PLAN.md`). These commands only
read state from the printer or local config — no MQTT publishes, no
side effects. Migrating them out of x2d_bridge.py lets the monolith
shed observation-shaped code that doesn't share much with the print-
issuing / serve-mode infrastructure that still lives there.

Currently:
  cmd_status        — MQTT request_state → JSON dump
  cmd_printers      — list configured [printer] sections from
                      ~/.x2d/credentials → JSON

x2d_bridge re-exports each handler so external callers + tests using
`from x2d_bridge import cmd_status` keep working unchanged.
"""
from __future__ import annotations

import argparse
import configparser
import json
from pathlib import Path


def cmd_status(args: argparse.Namespace) -> int:
    """Connect, request the latest pushed printer state once, print as
    JSON, disconnect. The canonical introspection verb — every other
    read-only command builds on the same X2DClient.request_state path."""
    from beambam.creds import Creds
    from beambam.mqtt import X2DClient

    creds = Creds.resolve(args)
    cli = X2DClient(creds)
    cli.connect()
    state = cli.request_state(timeout=args.timeout)
    cli.disconnect()
    print(json.dumps(state, indent=2))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """One-shot install / connectivity diagnostic. Probes TCP ports,
    requests MQTT state, summarizes AMS / print / camera / SD-card
    posture, and checks the bridge-daemon UNIX socket. Output is one
    color-coded line per check so it fits on a phone terminal.

    Exit code: 0 if every check passed; 1 if any failed."""
    import socket as _socket
    import time as _time

    from beambam.creds import Creds
    from beambam.mqtt import X2DClient

    creds = Creds.resolve(args)
    print(f"x2d health check — printer {creds.serial} @ {creds.ip}")
    fail_count = 0

    def _ok(label: str, detail: str = "") -> None:
        print(f"  \033[32m✓\033[0m {label:<28}"
              f"{(' '+detail) if detail else ''}")

    def _fail(label: str, detail: str) -> None:
        nonlocal fail_count
        print(f"  \033[31m✗\033[0m {label:<28} {detail}")
        fail_count += 1

    def _info(label: str, detail: str) -> None:
        print(f"  \033[36m·\033[0m {label:<28} {detail}")

    # 1. TCP reachability for each port BS uses
    for port, label in ((8883, "MQTT-TLS"),
                         (322,  "RTSPS-camera"),
                         (6000, "LVL-Local"),
                         (990,  "FTPS-upload")):
        try:
            with _socket.create_connection((creds.ip, port), timeout=3.0):
                _ok(f"port {port} ({label})", "open")
        except (OSError, _socket.timeout) as e:
            _fail(f"port {port} ({label})", str(e))

    # 2. MQTT connect + state
    state: dict | None = None
    try:
        cli = X2DClient(creds)
        cli.connect(timeout=8.0)
        t0 = _time.time()
        state = cli.request_state(timeout=8.0)
        elapsed = _time.time() - t0
        cli.disconnect()
        _ok("MQTT request_state", f"{int(elapsed*1000)}ms")
    except Exception as e:
        _fail("MQTT request_state", str(e))

    # 3. AMS slots summary
    if state:
        ams = state.get("print", {}).get("ams", {}).get("ams", [])
        if ams:
            for unit in ams:
                trays = unit.get("tray", [])
                loaded = sum(1 for t in trays if t.get("type"))
                _info(f"AMS{unit.get('id', '?')}",
                      f"{loaded}/{len(trays)} slots loaded")
        else:
            _info("AMS", "no AMS reported (single-spool / unbound)")

        # 4. Print state
        print_state = state.get("print", {})
        gcode_state = print_state.get("gcode_state", "?")
        layer = print_state.get("layer_num", 0)
        total_layers = print_state.get("total_layer_num", 0)
        if gcode_state and gcode_state != "?":
            _info("print state",
                  f"{gcode_state}, layer {layer}/{total_layers}")

        # 5. Camera state
        ipcam = print_state.get("ipcam", {})
        rtsp = ipcam.get("rtsp_url", "?")
        if rtsp == "disable":
            _info("camera",
                  "rtsp disabled (toggle on touchscreen → Settings → "
                  "Network → Liveview)")
        elif rtsp.startswith("rtsps://"):
            _ok("camera", "rtsps URL ready")
        else:
            _info("camera", f"rtsp_url={rtsp}")

        # 6. SD card state
        sdcard = print_state.get("sdcard", "?")
        if sdcard == "0" or sdcard == 0:
            _info("SD card", "not inserted")
        elif sdcard:
            _ok("SD card", f"state={sdcard}")

    # 7. Bridge daemon socket (if running)
    sock_path = Path.home() / ".x2d" / "bridge.sock"
    if sock_path.exists():
        try:
            with _socket.socket(_socket.AF_UNIX,
                                _socket.SOCK_STREAM) as us:
                us.settimeout(2.0)
                us.connect(str(sock_path))
                _ok("bridge daemon",
                    "socket alive at ~/.x2d/bridge.sock")
        except OSError as e:
            _fail("bridge daemon", f"socket present but {e}")
    else:
        _info("bridge daemon", "not running (no ~/.x2d/bridge.sock)")

    print()
    if fail_count == 0:
        print("\033[32mAll checks passed.\033[0m")
        return 0
    print(f"\033[31m{fail_count} check(s) failed.\033[0m")
    return 1


def cmd_watch(args: argparse.Namespace) -> int:
    """Poll printer state every N seconds and print one status line
    per poll: gcode_state, layer, percent, ETA, nozzle/bed temps.
    Useful for shell pipelines and status bars."""
    import sys
    import time as _time

    from beambam.creds import Creds
    from beambam.mqtt import X2DClient

    creds = Creds.resolve(args)
    interval = max(1, int(args.interval))
    cli = X2DClient(creds)
    cli.connect(timeout=8.0)

    try:
        while True:
            try:
                state = cli.request_state(timeout=8.0)
            except Exception as e:
                print(f"[{_time.strftime('%H:%M:%S')}] error: {e}",
                      file=sys.stderr)
                _time.sleep(interval)
                continue

            print_state = state.get("print", {})
            gcode_state = print_state.get("gcode_state", "?")
            layer = int(print_state.get("layer_num", 0) or 0)
            total = int(print_state.get("total_layer_num", 0) or 0)
            mc_pct = int(print_state.get("mc_percent", 0) or 0)
            mc_remaining = int(
                print_state.get("mc_remaining_time", 0) or 0)

            nozzle_l = float(print_state.get("nozzle_temper", 0.0) or 0.0)
            nozzle_l_t = float(
                print_state.get("nozzle_target_temper", 0.0) or 0.0)
            bed = float(print_state.get("bed_temper", 0.0) or 0.0)
            bed_t = float(
                print_state.get("bed_target_temper", 0.0) or 0.0)

            if mc_remaining > 0:
                hours = mc_remaining // 60
                mins  = mc_remaining % 60
                eta = f"{hours:02d}h{mins:02d}m"
            else:
                eta = "--:--"

            ts = _time.strftime("%H:%M:%S")
            line = (
                f"[{ts}] {gcode_state:<8} "
                f"L{layer}/{total} {mc_pct}% eta={eta}  "
                f"N:{nozzle_l:.0f}/{nozzle_l_t:.0f}°C "
                f"B:{bed:.0f}/{bed_t:.0f}°C"
            )
            print(line, flush=True)

            if args.once:
                break
            _time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[watch] stopped", file=sys.stderr)
    finally:
        cli.disconnect()
    return 0


def cmd_printers(_args: argparse.Namespace) -> int:
    """List every [printer] / [printer:NAME] section in ~/.x2d/credentials.

    Output is JSON `{"printers": [{"name", "ip", "serial"}, ...]}` so
    MCP / scripts can consume it without re-parsing INI."""
    ini_path = Path.home() / ".x2d" / "credentials"
    out: list[dict] = []
    if ini_path.exists():
        cp = configparser.ConfigParser()
        cp.read(ini_path)
        for section in cp.sections():
            if section == "printer":
                name = ""
            elif section.startswith("printer:"):
                name = section.split(":", 1)[1]
            else:
                continue
            out.append({
                "name":   name,
                "ip":     cp.get(section, "ip", fallback=""),
                "serial": cp.get(section, "serial", fallback=""),
            })
    print(json.dumps({"printers": out}, indent=2))
    return 0
