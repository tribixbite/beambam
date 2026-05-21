"""beambam.mqttcli — `beambam mqtt {sub,pub}` raw MQTT debug helpers.

Direct subscribe/publish for the printer's signed-MQTT topic pair:

    device/<serial>/request    — we publish here (signed)
    device/<serial>/report     — printer publishes here (state pushes)

Subcommands:

    beambam mqtt sub                   # subscribe to /report, stream JSON
    beambam mqtt sub --topic 'device/+/report'
    beambam mqtt sub --raw             # don't pretty-print
    beambam mqtt pub PAYLOAD           # sign + publish PAYLOAD JSON
    beambam mqtt pub --file -          # read payload from stdin

The reverse-engineering tool, basically. When firmware drops new
fields into pushall (or a new command appears in a Bambu Studio update),
sub'ing to /report shows them in real time. pub lets you experiment
with command shapes without committing them to a Printer method.

NOT for production use — most callers should reach for `Printer.publish`
or one of the typed methods (start_print, pause, etc.) instead.
"""
from __future__ import annotations

import argparse
import json
import signal
import ssl
import sys
import time

import paho.mqtt.client as mqtt


# ----- subscribe ----------------------------------------------------------


def subscribe_loop(*, creds, topic: str | None = None,
                   raw: bool = False, max_messages: int | None = None,
                   out_stream=sys.stdout) -> int:
    """Connect, subscribe to `topic`, print each message until SIGINT
    or max_messages reached."""
    if topic is None:
        topic = f"device/{creds.serial}/report"

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"beambam-mqtt-sub-{int(time.time())}",
        protocol=mqtt.MQTTv311,
    )
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ssl_ctx)
    # Bambu LAN MQTT: username is the literal "bblp", password is the
    # 8-digit access code (NOT the serial).
    client.username_pw_set("bblp", creds.code)

    received = [0]
    stopped = [False]

    def on_connect(c, _u, _f, rc, _props=None):
        if rc == 0:
            c.subscribe(topic)
            print(f"[mqtt] subscribed to {topic}", file=sys.stderr)
        else:
            print(f"[mqtt] connect rc={rc}", file=sys.stderr)
            stopped[0] = True

    def on_message(_c, _u, msg):
        received[0] += 1
        if raw:
            out_stream.write(msg.payload.decode("utf-8", errors="replace") + "\n")
        else:
            try:
                d = json.loads(msg.payload)
                out_stream.write(json.dumps(d, indent=2) + "\n---\n")
            except json.JSONDecodeError:
                out_stream.write(msg.payload.decode("utf-8", errors="replace")
                                 + "\n---\n")
        out_stream.flush()
        if max_messages is not None and received[0] >= max_messages:
            stopped[0] = True

    client.on_connect = on_connect
    client.on_message = on_message

    def _stop(*_a):
        stopped[0] = True
    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):
        pass

    try:
        client.connect(creds.ip, 8883, keepalive=60)
        client.loop_start()
        while not stopped[0]:
            time.sleep(0.1)
    except Exception as e:                                  # noqa: BLE001
        print(f"[mqtt] error: {e}", file=sys.stderr)
        return 1
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:                                   # noqa: BLE001
            pass
    return 0


# ----- publish ------------------------------------------------------------


def publish_signed(*, creds, payload: dict, qos: int = 1,
                   timeout: float = 5.0) -> int:
    """Sign payload + publish to device/<serial>/request via X2DClient."""
    from beambam.mqtt import X2DClient
    cli = X2DClient(creds)
    cli.connect()
    try:
        cli.publish(payload, qos=qos)
    finally:
        cli.disconnect()
    return 0


# ----- CLI ----------------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    p = sub.add_parser(
        "mqtt",
        help="Raw MQTT debug helpers (sub + pub). For protocol reverse-"
             "engineering; production code should use Printer methods.",
    )
    mq_sub = p.add_subparsers(dest="mqtt_cmd", required=True)

    s = mq_sub.add_parser("sub", help="Subscribe to printer's reply topic")
    s.add_argument("--topic",
                   help="MQTT topic (default: device/<serial>/report)")
    s.add_argument("--raw", action="store_true",
                   help="Don't pretty-print JSON")
    s.add_argument("--max-messages", type=int, default=None,
                   help="Exit after N messages (testing)")

    pu = mq_sub.add_parser("pub", help="Sign + publish a payload")
    pu.add_argument("payload", nargs="?",
                    help="JSON payload (the inner dict, NOT the wire envelope; "
                         "sign_payload wraps it). Use --file to read from a file.")
    pu.add_argument("--file", metavar="PATH",
                    help="Read JSON from PATH (- for stdin)")
    pu.add_argument("--qos", type=int, default=1, choices=[0, 1, 2])

    p.set_defaults(fn=cmd_mqtt)
    return p


def cmd_mqtt(args: argparse.Namespace) -> int:
    from beambam.config import Creds

    try:
        creds = Creds.resolve(argparse.Namespace(
            ip=args.ip, code=args.code, serial=args.serial,
            printer=args.printer,
        ))
    except Exception as e:                                  # noqa: BLE001
        print(f"can't resolve creds: {e}", file=sys.stderr)
        return 2

    if args.mqtt_cmd == "sub":
        return subscribe_loop(
            creds=creds, topic=args.topic, raw=args.raw,
            max_messages=args.max_messages,
        )

    if args.mqtt_cmd == "pub":
        # Determine payload source
        if args.file:
            if args.file == "-":
                raw = sys.stdin.read()
            else:
                raw = open(args.file).read()
        elif args.payload:
            raw = args.payload
        else:
            print("error: provide PAYLOAD positional arg or --file",
                  file=sys.stderr)
            return 2
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"invalid JSON: {e}", file=sys.stderr)
            return 2
        try:
            publish_signed(creds=creds, payload=payload, qos=args.qos)
        except Exception as e:                              # noqa: BLE001
            print(f"publish failed: {e}", file=sys.stderr)
            return 1
        print(f"published to device/{creds.serial}/request")
        return 0

    print(f"unknown mqtt subcommand: {args.mqtt_cmd}", file=sys.stderr)
    return 2
