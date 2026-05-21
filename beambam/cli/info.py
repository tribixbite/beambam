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


# `tail` streams events derived from the printer's MQTT push stream
# (state transitions, progress milestones, HMS code add/clear) as a
# live log — push-based, not poll-based. Distinct from `watch` (which
# polls request_state every N seconds and prints one full-status line)
# in two ways: (a) events surface within ms of the printer's push
# rather than once per polling interval, and (b) only deltas are
# emitted, so the log stays terse.
class _TailDispatcher:
    """Pure diff engine for `beambam tail`. Keeps the previous-push
    snapshot and exposes `events_for(state)` returning a list of
    (category, message, level) tuples for what changed since the last
    call. Pulled outside `cmd_tail` so unit tests can drive it directly
    without spinning up MQTT, threads, or signal handlers."""

    def __init__(self, *, no_progress: bool = False,
                 no_hms: bool = False,
                 every_state: bool = False) -> None:
        self.no_progress = no_progress
        self.no_hms      = no_hms
        self.every_state = every_state
        self._prev_state:   str | None = None
        self._prev_layer:   int        = -1
        self._prev_percent: int        = -1
        self._prev_hms:     set[str]   = set()

    @staticmethod
    def _hms_code(h: dict) -> str:
        """Render an HMS dict as `AAAA_BBBB_CCCC_DDDD` hex matching the
        canonical form HMS_DESCRIPTIONS uses + the Bambu error-page URLs.

        Two firmware variants seen in the wild:
          * `{a,b,c,d}` — four 16-bit ints, one per hex group
          * `{attr,code}` — two 32-bit ints; split each into
            high/low 16-bit halves
        """
        if all(k in h for k in ("a", "b", "c", "d")):
            return "_".join(f"{int(h.get(k, 0)):04X}"
                            for k in ("a", "b", "c", "d"))
        if "attr" in h and "code" in h:
            try:
                attr = int(h["attr"])
                code = int(h["code"])
            except (TypeError, ValueError):
                # Already-formatted strings? Pass through but normalise
                # the join so downstream lookup against HMS_DESCRIPTIONS
                # works in the common case.
                return (f"{h.get('attr','')}_{h.get('code','')}"
                        .strip("_"))
            return (
                f"{(attr >> 16) & 0xFFFF:04X}_{attr & 0xFFFF:04X}"
                f"_{(code >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}"
            )
        return ""

    def events_for(self, state: dict) -> list[tuple[str, str, str]]:
        from beambam.doctor import decode_hms
        out: list[tuple[str, str, str]] = []
        p = state.get("print", {}) or {}

        gs = p.get("gcode_state")
        if gs and gs != self._prev_state:
            if self._prev_state is None:
                out.append(("state", f"observed {gs}", "info"))
            else:
                lvl = "fail" if gs == "FAILED" else "info"
                out.append(("state",
                            f"{self._prev_state} -> {gs}", lvl))
            self._prev_state = gs

        if not self.no_progress:
            pct = int(p.get("mc_percent", 0) or 0)
            if pct >= 0 and self._prev_percent >= 0:
                step = (pct // 10) - (self._prev_percent // 10)
                if step >= 1 and pct < 100:
                    out.append(("progress", f"{pct}%", "info"))
                elif pct == 100 and self._prev_percent < 100:
                    out.append(("progress",
                                "100% — print finished", "ok"))
            self._prev_percent = pct

        if self.every_state:
            layer = int(p.get("layer_num", 0) or 0)
            total = int(p.get("total_layer_num", 0) or 0)
            if layer != self._prev_layer and layer > 0:
                out.append(("layer", f"L{layer}/{total}", "info"))
                self._prev_layer = layer

        if not self.no_hms:
            hms_now: set[str] = set()
            for h in (p.get("hms") or []):
                code = self._hms_code(h)
                if code:
                    hms_now.add(code)
            for code in sorted(hms_now - self._prev_hms):
                out.append(("hms",
                            f"{code}: {decode_hms(code)}", "fail"))
            for code in sorted(self._prev_hms - hms_now):
                out.append(("hms", f"{code} cleared", "ok"))
            self._prev_hms = hms_now

        return out


def _tail_print(events: list[tuple[str, str, str]],
                *, as_json: bool) -> None:
    """Render dispatcher events as either ndjson lines or the
    human-readable [HH:MM:SS] ICON CATEGORY MESSAGE format."""
    import time as _time
    icons = {"info": "·", "warn": "⚠", "fail": "✗", "ok": "✓"}
    if as_json:
        ts = _time.time()
        for category, message, level in events:
            print(json.dumps({"ts": ts, "category": category,
                              "level": level, "message": message},
                              separators=(",", ":")), flush=True)
        return
    ts_str = _time.strftime("%H:%M:%S")
    for category, message, level in events:
        icon = icons.get(level, "·")
        print(f"[{ts_str}] {icon} {category:<8} {message}", flush=True)


def cmd_tail(args: argparse.Namespace) -> int:
    """Stream printer-pushed state transitions, progress milestones,
    and HMS code add/clear as a live event log. Push-based, not
    poll-based — events surface within ms of the printer's push."""
    import sys
    from threading import Event as _Event

    from beambam.cli._helpers import _next_seq
    from beambam.creds import Creds
    from beambam.mqtt import X2DClient

    creds = Creds.resolve(args)
    disp = _TailDispatcher(no_progress=args.no_progress,
                           no_hms=args.no_hms,
                           every_state=args.every_state)

    def _on_state(state: dict) -> None:
        events = disp.events_for(state)
        if events:
            _tail_print(events, as_json=args.json)

    cli = X2DClient(creds, on_state=_on_state)
    try:
        cli.connect(timeout=8.0)
    except Exception as e:
        print(f"[tail] connect failed: {e}", file=sys.stderr)
        return 2
    # Force an initial push so we don't have to wait up to ~30 s for
    # the printer's next periodic push.
    try:
        cli.publish({"pushing": {"sequence_id": _next_seq(),
                                  "command": "pushall"}})
    except Exception as e:
        print(f"[tail] initial pushall failed: {e}",
              file=sys.stderr)

    if not args.json:
        print(f"[tail] connected to {creds.ip}; streaming events… "
              f"(Ctrl-C to exit)", file=sys.stderr)
    stop = _Event()
    try:
        stop.wait()
    except KeyboardInterrupt:
        if not args.json:
            print("\n[tail] stopped", file=sys.stderr)
    finally:
        try:
            cli.disconnect()
        except Exception:
            pass
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Background-poll printer state and fire a termux-notification on
    every state transition (RUNNING → FINISH / FAILED / PAUSE) and at
    each `--layer-milestone` boundary while RUNNING. Requires the
    termux-api package: `pkg install termux-api`."""
    import shutil
    import subprocess as _sp
    import sys
    import time as _time

    from beambam.creds import Creds
    from beambam.mqtt import X2DClient

    if not shutil.which("termux-notification"):
        print("termux-notification not found — install `termux-api` "
              "package", file=sys.stderr)
        return 1

    creds = Creds.resolve(args)
    cli = X2DClient(creds)
    cli.connect(timeout=8.0)

    poll_interval = max(5, int(args.interval))
    last_state = None
    last_layer = -1
    notified_complete = False

    print(f"[{_time.strftime('%H:%M:%S')}] notify started — polling "
          f"every {poll_interval}s")
    try:
        while True:
            try:
                state = cli.request_state(timeout=8.0)
            except Exception as e:
                print(f"[{_time.strftime('%H:%M:%S')}] error: {e}",
                      file=sys.stderr)
                _time.sleep(poll_interval)
                continue

            ps = state.get("print", {})
            gs = ps.get("gcode_state", "?")
            layer = int(ps.get("layer_num", 0) or 0)
            total = int(ps.get("total_layer_num", 0) or 0)
            mc_pct = int(ps.get("mc_percent", 0) or 0)

            changed = (gs != last_state)
            milestone = (args.layer_milestone > 0 and total > 0
                         and layer >= last_layer + args.layer_milestone)

            if changed:
                title = f"X2D: {gs}"
                if gs == "RUNNING" and total > 0:
                    msg = f"Layer {layer}/{total} ({mc_pct}%)"
                elif gs == "FINISH":
                    msg = f"Print complete ({total} layers)"
                    notified_complete = True
                elif gs == "FAILED":
                    msg = f"Print failed at layer {layer}/{total}"
                elif gs == "PAUSE":
                    msg = f"Paused at layer {layer}/{total}"
                else:
                    msg = f"State: {gs}"
                _sp.run(["termux-notification",
                         "--id", "x2d_print",
                         "--title", title,
                         "--content", msg,
                         "--ongoing"] if gs == "RUNNING" else
                        ["termux-notification",
                         "--id", "x2d_print",
                         "--title", title,
                         "--content", msg],
                        check=False)
                print(f"[{_time.strftime('%H:%M:%S')}] notified: "
                      f"{title} — {msg}")

            elif milestone and gs == "RUNNING":
                _sp.run(["termux-notification",
                         "--id", "x2d_print",
                         "--title", f"X2D: layer {layer}/{total}",
                         "--content", f"{mc_pct}% complete",
                         "--ongoing"],
                        check=False)
                last_layer = layer

            last_state = gs
            if notified_complete and args.exit_on_finish:
                break
            _time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n[notify] stopped")
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
