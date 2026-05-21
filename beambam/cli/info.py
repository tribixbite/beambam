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
