"""beambam.init_wizard — `beambam init` first-run setup.

Interactive flow: discover → pick → prompt for code → test → write
credentials → suggest doctor. Single command from blank slate to
working install.

Run `beambam init` after `pip install beambam`. The wizard:

  1. Runs SSDP discovery on the LAN (3s default)
  2. If multiple printers found, prompts to pick one
  3. Asks for the 8-digit access code (printer screen → Settings →
     Network → LAN Mode → Access Code — NOT broadcast over SSDP)
  4. Tests TCP connectivity to MQTT (8883) + FTPS (990)
  5. Writes the credentials section (default `[printer]` for the
     first one, `[printer:NAME]` if user passed --name)
  6. Suggests `beambam doctor` for a deeper sanity check

`beambam init` is idempotent and safe to re-run — it'll find existing
sections via `Creds.list_names()` and offer to update them, not
duplicate.

Usage:
  beambam init                          # interactive
  beambam init --name studio            # save as [printer:studio]
  beambam init --ip 192.168.1.42        # skip discovery, use this IP
  beambam init --non-interactive --ip ... --code ... --serial ... --name ...
"""
from __future__ import annotations

import argparse
import socket
import sys
from typing import Optional


def _check_tcp(ip: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """Return (reachable, error_msg). Fast — connect-only, no protocol."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True, ""
    except (OSError, socket.timeout) as e:
        return False, str(e)


def _prompt(text: str, default: str = "") -> str:
    """Stripped input() with default-on-empty."""
    if default:
        suffix = f" [{default}]"
    else:
        suffix = ""
    try:
        ans = input(f"{text}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted.", file=sys.stderr)
        raise SystemExit(1)
    return ans or default


def _pick_printer(printers: list) -> int:
    """Multi-printer disambiguator — returns index into the list."""
    print(f"\nFound {len(printers)} printers:")
    for i, p in enumerate(printers, 1):
        print(f"  {i}. {p.ip:<15} {p.serial:<22} {p.model:<6} {p.name}")
    while True:
        choice = _prompt(f"\nPick one (1-{len(printers)})")
        try:
            n = int(choice)
            if 1 <= n <= len(printers):
                return n - 1
        except ValueError:
            pass
        print(f"  invalid — enter a number 1-{len(printers)}")


def _resolve_target(args: argparse.Namespace):
    """Return a (ip, serial, name, model) tuple by either:
    (a) using --ip/--serial if supplied, or
    (b) running SSDP discovery and asking the user to pick."""
    if args.ip and args.serial:
        return args.ip, args.serial, "?", ""

    from beambam.find import discover, format_table

    print(f"Scanning LAN via SSDP ({args.timeout}s)…")
    printers = discover(timeout=args.timeout)
    if not printers:
        print(format_table([]), file=sys.stderr)
        print(f"\n  Pass --ip + --serial to skip discovery.", file=sys.stderr)
        raise SystemExit(1)

    if len(printers) == 1:
        p = printers[0]
        print(f"\nFound: {p.ip}  {p.serial}  {p.model}  {p.name}")
    else:
        idx = _pick_printer(printers)
        p = printers[idx]

    return p.ip, p.serial, p.name or "?", p.model or ""


def cmd_init(args: argparse.Namespace) -> int:
    from beambam.config import Creds
    from beambam.configcli import _section_name, list_sections, _save, _load

    print("beambam init — first-run setup wizard\n")

    # Step 1+2: discover + pick (or use --ip/--serial)
    try:
        ip, serial, model_name, model = _resolve_target(args)
    except SystemExit:
        raise
    except Exception as e:                                  # noqa: BLE001
        print(f"discovery failed: {e}", file=sys.stderr)
        return 1

    # Step 3: access code
    if args.code:
        code = args.code
    else:
        print(f"\nGet the access code from the printer:")
        print(f"  Settings → Network → LAN Mode → Access Code (8 digits)")
        code = _prompt("\nAccess code")
    if not code.isdigit() or len(code) != 8:
        print(f"\n  access code must be 8 digits (got {code!r})",
              file=sys.stderr)
        return 2

    # Step 4: test connectivity (skipped in --non-interactive mode if no IP)
    print(f"\nTesting connectivity to {ip}…")
    ok_mqtt, mqtt_err = _check_tcp(ip, 8883, timeout=3.0)
    ok_ftps, ftps_err = _check_tcp(ip, 990, timeout=3.0)
    print(f"  MQTT (port 8883):  {'✓ open' if ok_mqtt else f'✗ {mqtt_err}'}")
    print(f"  FTPS (port 990):   {'✓ open' if ok_ftps else f'✗ {ftps_err}'}")
    if not (ok_mqtt and ok_ftps):
        if args.non_interactive:
            print(f"\n  connectivity failed — proceeding anyway "
                  f"(--non-interactive)", file=sys.stderr)
        else:
            ans = _prompt("\n  Connectivity failed. Save credentials anyway? "
                          "(y/N)", default="N")
            if ans.lower() != "y":
                print("aborted.", file=sys.stderr)
                return 1

    # Step 5: write credentials section
    # Priority: explicit --name > printer's broadcast Devname > "printer" (default).
    # The previous one-liner had an operator-precedence bug where args.name
    # was overridden by the ternary's else branch when model_name=="?".
    if args.name:
        section_name = args.name
    elif model_name and model_name != "?":
        section_name = model_name
    else:
        section_name = ""                                  # → use default [printer]

    if not section_name:
        section = "printer"
    else:
        section = _section_name(section_name)

    # Check for existing section
    existing = [n for n, _ in list_sections()]
    short = "(default)" if section == "printer" else section_name
    if short in existing or (short == "" and "(default)" in existing):
        if not args.force:
            ans = _prompt(f"\nSection [{section}] already exists. "
                          f"Overwrite? (y/N)", default="N")
            if ans.lower() != "y":
                print("aborted (use --force to skip this prompt).",
                      file=sys.stderr)
                return 1

    cp = _load()
    if not cp.has_section(section):
        cp.add_section(section)
    cp.set(section, "ip", ip)
    cp.set(section, "code", code)
    cp.set(section, "serial", serial)
    _save(cp)
    print(f"\n✓ saved [{section}] → ~/.x2d/credentials (chmod 0600)")

    # Step 6: suggest doctor
    print(f"\nNext steps:")
    print(f"  beambam status            — pull live state")
    print(f"  beambam doctor            — full health check")
    print(f"  beambam ams status        — see AMS loadout")
    print(f"  beambam --help            — every subcommand")
    return 0


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    p = sub.add_parser(
        "init",
        help="First-run setup wizard: discover printer + prompt for code "
             "+ test connectivity + write ~/.x2d/credentials.",
    )
    p.add_argument("--name", help="Save as [printer:NAME] section "
                                    "(default: use printer's name / 'printer')")
    p.add_argument("--ip", help="Skip discovery; use this IP")
    p.add_argument("--serial", help="Printer serial (paired with --ip)")
    p.add_argument("--code", help="8-digit access code (skip the prompt)")
    p.add_argument("--timeout", type=float, default=3.0,
                   help="SSDP discovery timeout in seconds (default 3)")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing section without prompting")
    p.add_argument("--non-interactive", action="store_true",
                   help="Don't prompt; require all of --ip --serial --code")
    p.set_defaults(fn=cmd_init)
    return p
