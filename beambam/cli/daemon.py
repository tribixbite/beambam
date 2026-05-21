"""beambam.cli.daemon — long-running background-service handlers.

Phase 5d scaffold (`docs/BRIDGE_SPLIT_PLAN.md`). Hosts the daemons
that run indefinitely until SIGINT/SIGTERM — each one stands up its
own server/socket/MQTT-loop and parks the main thread waiting for
the OS signal.

Currently:
  cmd_webrtc       — WebRTC video gateway (delegates to runtime.webrtc.server)
  cmd_ha_publish   — Home Assistant MQTT publisher (one HAPublisher
                     per [printer:NAME] credentials section)

The bigger daemons (cmd_camera, cmd_serve, cmd_daemon) still live in
x2d_bridge.py because they share infrastructure with classes
(ServeServer, the multi-printer connection pool, the chamber-camera
ffmpeg pump) that haven't been hoisted into their own modules yet.
A later batch will move those once the supporting class hierarchies
relocate too.

x2d_bridge.py re-exports each handler so external + test callers
keep working.
"""
from __future__ import annotations

import argparse
import sys


def cmd_webrtc(args: argparse.Namespace) -> int:
    """Run the WebRTC video gateway (item #45). Pulls JPEG frames from
    a running camera daemon and re-publishes them as a live VP8/H.264
    track over WebRTC. Sub-second latency vs HLS's ~6-8 s.

    The signaling endpoint is POST /cam.webrtc/offer; the static viewer
    page is GET /cam.webrtc.html.
    """
    try:
        from runtime.webrtc.server import run as _run_webrtc
    except ImportError as e:
        print(f"[x2d-bridge] webrtc deps missing: {e}\n"
              f"  Install: python3.12 -m pip install --no-build-isolation "
              f"aiortc 'av==13.1.0' aiohttp\n"
              f"  See docs/MCP.md §2 for Termux-specific libsrtp "
              f"build steps.", file=sys.stderr)
        return 2
    host_part, _, port_part = args.bind.rpartition(":")
    host = host_part or "127.0.0.1"
    port = int(port_part)
    stun = ([s.strip() for s in args.stun.split(",") if s.strip()]
            if args.stun else None)
    return _run_webrtc(host=host, port=port,
                       camera_url=args.camera_url,
                       frame_hz=float(args.frame_hz),
                       stun_servers=stun)


def cmd_ha_publish(args: argparse.Namespace) -> int:
    """Bridge a running x2d_bridge.py daemon's state to a Home Assistant
    MQTT broker via the HA discovery protocol (item #50). Without
    `--printer`, spawns one HAPublisher per `[printer:NAME]` section
    in ~/.x2d/credentials so HA gets a separate Device per printer
    (item #54). Connection failures are isolated — if one printer's
    publisher errors out, the others stay up."""
    import logging
    import os
    import signal as _signal
    from threading import Event
    from beambam.config import Creds

    try:
        from runtime.ha.publisher import HAPublisher
    except ImportError as e:
        print(f"[x2d-bridge] HA publisher import failed: {e}\n"
              "  Required: paho-mqtt (already a bridge dep).",
              file=sys.stderr)
        return 2

    # Build the work list: one entry per printer.
    if args.printer:
        targets = [(args.printer, args.device_serial)]
    else:
        names = Creds.list_names() or [""]
        targets = []
        for name in names:
            serial = ""
            try:
                ns = argparse.Namespace(ip=None, code=None,
                                         serial=None,
                                         printer=(name or None))
                creds = Creds.resolve(ns)
                serial = creds.serial
            except SystemExit:
                pass
            targets.append((name, serial))
    if args.device_serial and len(targets) == 1:
        targets = [(targets[0][0], args.device_serial)]

    host_part, _, port_part = args.broker.rpartition(":")
    host = host_part or args.broker
    port = int(port_part) if port_part.isdigit() else 1883

    logging.basicConfig(
        level=os.environ.get("X2D_HA_LOG", "INFO"),
        format="[%(asctime)s] %(name)s %(levelname)s %(message)s")

    publishers: list = []
    failed: list[tuple[str, str]] = []
    for name, serial in targets:
        try:
            pub = HAPublisher(
                broker_host=host, broker_port=port,
                broker_username=args.broker_username or None,
                broker_password=args.broker_password or None,
                daemon_url=args.daemon_url,
                daemon_token=args.daemon_token or None,
                discovery_prefix=args.discovery_prefix,
                printer_name=name or "",
                device_serial=serial or name or "default",
                device_model=args.device_model)
            pub.start()
            publishers.append(pub)
            print(f"[x2d-ha] {name or '<default>'}: started "
                  f"device_id={pub.device_id} "
                  f"base_topic={pub.base_topic}",
                  file=sys.stderr, flush=True)
        except Exception as e:
            failed.append((name, str(e)))
            print(f"[x2d-ha] {name or '<default>'}: start failed: "
                  f"{e} — other printers continue", file=sys.stderr)

    if not publishers:
        print(f"[x2d-ha] no publishers started: {failed}",
              file=sys.stderr)
        return 2

    # Run until interrupted.
    stop = Event()
    def _handle(_n, _f): stop.set()
    _signal.signal(_signal.SIGINT, _handle)
    _signal.signal(_signal.SIGTERM, _handle)
    try:
        while not stop.is_set():
            stop.wait(1)
    finally:
        for p in publishers:
            try: p.stop()
            except Exception: pass
    return 0
