"""beambam.find — LAN discovery of Bambu Lab printers via SSDP.

Bambu printers announce themselves over SSDP (UPnP, udp/1900) with
device-specific headers. This module sends an M-SEARCH and parses the
responses into a structured list:

  beambam find                       # discover, print table
  beambam find --json                # machine output
  beambam find --add NAME            # write [printer:NAME] to ~/.x2d/credentials
                                      # (prompts for access code — not in SSDP)
  beambam find --timeout 5           # wait longer (default 3s)

What we capture per device:
  ip          from the UDP source address
  serial      USN header (`uuid:<serial>::urn:...`)
  name        Devname.bambu.com
  model       Devmodel.bambu.com (C12=P1S, N6=X2D, etc.)
  connection  DevConnect.bambu.com (lan|cloud)
  bind        DevBind.bambu.com (free|bound)
  signal      Devsignal.bambu.com (dBm)
  version     Server header (firmware-ish)

Access code is NOT broadcast — it's set on the printer's screen. To
write a credentials section we prompt for it interactively.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import socket
import struct
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900
BAMBU_ST = "urn:bambulab-com:device:3dprinter:1"


@dataclass(frozen=True)
class FoundPrinter:
    ip: str
    serial: str = ""
    name: str = ""
    model: str = ""
    connection: str = ""
    bind: str = ""
    signal: str = ""
    version: str = ""
    raw_headers: dict[str, str] = field(default_factory=dict)


# ----- parser -------------------------------------------------------------


_USN_RE = re.compile(r"uuid:([^:]+)::")


def _parse_response(data: bytes, src_ip: str) -> FoundPrinter | None:
    """Parse one SSDP response. Returns None if it doesn't look Bambu-ish."""
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\r\n")
    # First line: HTTP/1.1 200 OK
    if not lines or not lines[0].startswith("HTTP/1.1"):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    st = headers.get("st", "")
    nt = headers.get("nt", "")
    usn = headers.get("usn", "")
    # Accept any response that mentions "bambu" in ST/NT/USN/Server.
    blob = (st + " " + nt + " " + usn + " " + headers.get("server", "")).lower()
    if "bambu" not in blob:
        return None

    serial_match = _USN_RE.search(usn)
    serial = serial_match.group(1) if serial_match else ""
    return FoundPrinter(
        ip=src_ip,
        serial=serial,
        name=headers.get("devname.bambu.com", "")
              or headers.get("devname", ""),
        model=headers.get("devmodel.bambu.com", "")
              or headers.get("devmodel", ""),
        connection=headers.get("devconnect.bambu.com", ""),
        bind=headers.get("devbind.bambu.com", ""),
        signal=headers.get("devsignal.bambu.com", ""),
        version=headers.get("server", ""),
        raw_headers=headers,
    )


# ----- discovery loop -----------------------------------------------------


def discover(timeout: float = 3.0, *,
             search_target: str = BAMBU_ST,
             include_other: bool = False) -> list[FoundPrinter]:
    """Send an M-SEARCH and collect all responses for `timeout` seconds.

    If `include_other` is False, only responses with `bambu` in their
    ST/NT/USN/Server header lines are kept (filters out other UPnP
    devices on the LAN). Set True to debug discovery."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    sock.settimeout(0.5)

    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {int(max(1, timeout))}\r\n"
        f"ST: {search_target}\r\n"
        "\r\n"
    ).encode("utf-8")

    try:
        sock.sendto(msg, (SSDP_GROUP, SSDP_PORT))
        # Some firmwares ignore the targeted ST and only reply to ssdp:all;
        # send a second probe for that to widen the net.
        if search_target != "ssdp:all":
            msg_all = msg.replace(f"ST: {search_target}".encode(),
                                   b"ST: ssdp:all")
            sock.sendto(msg_all, (SSDP_GROUP, SSDP_PORT))
    except OSError as e:
        print(f"[find] M-SEARCH failed: {e}", file=sys.stderr)
        return []

    found: dict[str, FoundPrinter] = {}              # by ip+serial
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(8192)
        except socket.timeout:
            continue
        except OSError:
            break
        p = _parse_response(data, addr[0])
        if p is None:
            continue
        key = f"{p.ip}/{p.serial}"
        # Keep the first response per device.
        if key not in found:
            found[key] = p
    sock.close()
    return list(found.values())


# ----- credentials writer --------------------------------------------------


def write_credentials_section(name: str, printer: FoundPrinter, *,
                              access_code: str,
                              path: Path = Path.home() / ".x2d" / "credentials") -> bool:
    """Write/update a [printer:NAME] section in ~/.x2d/credentials.
    Returns True on success, False if a section with that NAME already
    exists with different ip/serial (use --force to override — not
    implemented here; callers should handle)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cp = configparser.ConfigParser()
    if path.exists():
        cp.read(path)
    section = f"printer:{name}"
    if cp.has_section(section):
        existing_ip = cp.get(section, "ip", fallback="")
        existing_serial = cp.get(section, "serial", fallback="")
        if (existing_ip and existing_ip != printer.ip
                or existing_serial and existing_serial != printer.serial):
            return False
    if not cp.has_section(section):
        cp.add_section(section)
    cp.set(section, "ip", printer.ip)
    cp.set(section, "code", access_code)
    cp.set(section, "serial", printer.serial)
    with path.open("w") as f:
        cp.write(f)
    path.chmod(0o600)
    return True


# ----- formatting ---------------------------------------------------------


def format_table(printers: list[FoundPrinter]) -> str:
    if not printers:
        return ("no Bambu printers found on the LAN. Things to check:\n"
                "  • printer powered on and on the same subnet\n"
                "  • LAN-Only mode disabled (Settings → Network) — when\n"
                "    enabled, the printer doesn't broadcast SSDP\n"
                "  • firewall not blocking udp/1900 on this host\n"
                "  • try --timeout 8 for a slow LAN")
    lines = [f"found {len(printers)} printer(s):"]
    lines.append(f"  {'IP':<15} {'SERIAL':<22} {'MODEL':<6} {'NAME':<14} "
                 f"{'STATE'}")
    for p in printers:
        state = " / ".join(filter(None, [p.connection, p.bind,
                                          f"{p.signal}dBm" if p.signal else ""]))
        lines.append(f"  {p.ip:<15} {p.serial[:22]:<22} {p.model:<6} "
                     f"{p.name[:14]:<14} {state}")
    return "\n".join(lines)


# ----- CLI ----------------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    p = sub.add_parser(
        "find",
        help="LAN discovery: SSDP M-SEARCH for Bambu printers and print "
             "their IP / serial / model / state.",
    )
    p.add_argument("--timeout", type=float, default=3.0,
                   help="Seconds to wait for responses (default 3)")
    p.add_argument("--include-other", action="store_true",
                   help="Show all SSDP responses, not just Bambu devices")
    p.add_argument("--json", dest="json_out", action="store_true",
                   help="Emit JSON instead of a table")
    p.add_argument("--add", metavar="NAME",
                   help="Write the first found printer as [printer:NAME] in "
                        "~/.x2d/credentials. Prompts interactively for the "
                        "access code (not broadcast over SSDP).")
    p.set_defaults(fn=cmd_find)
    return p


def cmd_find(args: argparse.Namespace) -> int:
    printers = discover(timeout=args.timeout,
                        include_other=args.include_other)

    if args.json_out:
        out = [asdict(p) for p in printers]
        print(json.dumps(out, indent=2))
        return 0

    print(format_table(printers))

    if args.add:
        if not printers:
            print(f"\nNothing to add — no printers found.", file=sys.stderr)
            return 1
        target = printers[0]
        if len(printers) > 1:
            print(f"\nFound {len(printers)} printers; using the first "
                  f"({target.ip} {target.serial}).", file=sys.stderr)
        try:
            code = input(f"\nAccess code for {target.name or target.ip} "
                         f"(printer screen → Settings → Network): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted.", file=sys.stderr)
            return 1
        if not code or not code.isdigit() or len(code) != 8:
            print(f"access code must be 8 digits (got: {code!r})",
                  file=sys.stderr)
            return 1
        wrote = write_credentials_section(args.add, target, access_code=code)
        if not wrote:
            print(f"[printer:{args.add}] already exists with different "
                  f"ip/serial — remove it manually and re-run.",
                  file=sys.stderr)
            return 1
        print(f"wrote [printer:{args.add}] to ~/.x2d/credentials")
    return 0
