#!/usr/bin/env python3
"""tools/capture_lan_request.py — capture the X2D's MQTT request topic on the
LAN broker, to recover the exact `print.project_file` (and any print command)
a known-good client (Bambu Studio desktop LAN print) publishes.

The cloud broker ACL-denies subscribing to `device/<serial>/request` (SUBACK
0x87), but the LAN broker (`bblp`/access-code) ALLOWS it. So while this runs,
trigger one LAN print from Bambu Studio desktop to the X2D and this records the
full signed command — the missing X2D dual-nozzle field set that a
beambam-built project_file is still guessing at (err 84033544).

Captured commands are appended (pretty-printed JSON) to the --out file and
echoed to stdout. Exits after capturing a `project_file` (unless --all-forever).

Usage:
  python3 tools/capture_lan_request.py [--timeout 900] [--out ~/.x2d/captured_project_file.json]
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=900.0,
                    help="seconds to listen (default 900 = 15 min)")
    ap.add_argument("--out", default=str(Path.home() / ".x2d" / "captured_project_file.json"))
    ap.add_argument("--all-forever", action="store_true",
                    help="keep listening after a project_file (capture every command)")
    args = ap.parse_args()

    # local imports so the tool is usable without these on the path at import
    import paho.mqtt.client as mqtt
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from beambam.config import Creds

    creds = Creds.resolve(argparse.Namespace())
    serial = creds.serial
    topic = f"device/{serial}/request"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    captured: list[dict] = []
    done = {"project_file": False}

    def on_connect(c, d, f, rc, props=None):
        c.subscribe(topic, qos=1)
        print(f"[capture] subscribed to {topic} on LAN broker {creds.ip} — "
              f"now do ONE Bambu Studio desktop LAN print to the X2D…",
              file=sys.stderr)

    def on_message(c, d, m):
        try:
            j = json.loads(m.payload)
        except Exception:                                  # noqa: BLE001
            return
        # the print command sits under j["print"]; skip our own pushall noise
        p = j.get("print") if isinstance(j, dict) else None
        cmd = p.get("command") if isinstance(p, dict) else None
        if not cmd or cmd in ("pushall", "push_status"):
            return
        captured.append(j)
        with out.open("a") as f:
            f.write(json.dumps(j, indent=2) + "\n")
        print(f"\n[capture] >>> command={cmd!r} ({len(m.payload)} B) -> {out}",
              file=sys.stderr)
        if cmd == "project_file":
            print(json.dumps(j, indent=2))
            done["project_file"] = True

    c = mqtt.Client(client_id=f"bb-cap-{serial[-4:]}", protocol=mqtt.MQTTv311)
    c.username_pw_set("bblp", creds.code)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    c.tls_set_context(ctx)
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(creds.ip, 8883, keepalive=60)
    c.loop_start()
    deadline = time.time() + args.timeout
    try:
        while time.time() < deadline:
            if done["project_file"] and not args.all_forever:
                break
            time.sleep(0.5)
    finally:
        c.loop_stop()
        c.disconnect()

    if not captured:
        print("[capture] nothing captured (no LAN print seen in the window)",
              file=sys.stderr)
        return 1
    print(f"[capture] captured {len(captured)} command(s) -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
