"""beambam.configcli — `beambam config` credentials management.

Edits ~/.x2d/credentials (the file the bridge reads for printer
connection details). Sub-CLI tree:

    beambam config list                          # all sections, code masked
    beambam config show NAME                     # show one section
    beambam config add NAME --ip ... --code ... --serial ...
    beambam config remove NAME                   # delete a section
    beambam config rename OLD NEW                # rename a section

The credentials file lives at $X2D_CREDS or ~/.x2d/credentials. Each
section is `[printer]` (default) or `[printer:NAME]` (multi-printer
setups). Format:

    [printer:studio]
    ip     = 192.168.1.42
    code   = 12345678
    serial = 00M09A000000000

Access codes are sensitive — `list` masks them by default; pass --reveal
to see them. The file is chmod'd to 0600 on every write.
"""
from __future__ import annotations

import argparse
import configparser
import os
import sys
from pathlib import Path


CREDS_PATH = Path(
    os.environ.get("X2D_CREDS")
    or os.environ.get("BEAMBAM_CREDS")
    or (Path.home() / ".x2d" / "credentials")
)


# ----- core helpers -------------------------------------------------------


def _load(path: Path | None = None) -> configparser.ConfigParser:
    """Read the credentials file. `path=None` re-reads the module-level
    CREDS_PATH at call time (so tests can monkeypatch it)."""
    p = path or CREDS_PATH
    cp = configparser.ConfigParser()
    if p.exists():
        cp.read(p)
    return cp


def _save(cp: configparser.ConfigParser, path: Path | None = None) -> None:
    p = path or CREDS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        cp.write(f)
    p.chmod(0o600)


def _section_name(name: str) -> str:
    """Normalize 'studio' → 'printer:studio'; leave 'printer' / 'printer:X'
    alone."""
    if name == "printer" or name.startswith("printer:"):
        return name
    return f"printer:{name}"


def _short_name(section: str) -> str:
    """Inverse of _section_name: 'printer:studio' → 'studio'."""
    if section == "printer":
        return "(default)"
    if section.startswith("printer:"):
        return section[len("printer:"):]
    return section


def _mask(code: str) -> str:
    if not code:
        return ""
    if len(code) <= 2:
        return "*" * len(code)
    return code[:2] + "*" * (len(code) - 2)


def list_sections(path: Path | None = None) -> list[tuple[str, dict[str, str]]]:
    """Return [(short_name, {ip,code,serial}), ...] for every printer section."""
    cp = _load(path)
    out = []
    for section in cp.sections():
        if section == "printer" or section.startswith("printer:"):
            out.append((_short_name(section), dict(cp.items(section))))
    return out


# ----- subcommand impls --------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    sections = list_sections()
    if not sections:
        print(f"no printer sections in {CREDS_PATH}", file=sys.stderr)
        print(f"  add one: beambam config add NAME --ip ... --code ... --serial ...",
              file=sys.stderr)
        print(f"  or:      beambam find --add NAME", file=sys.stderr)
        return 1
    print(f"{len(sections)} section(s) in {CREDS_PATH}:")
    print(f"  {'NAME':<20} {'IP':<15} {'CODE':<10} SERIAL")
    for name, fields in sections:
        code = fields.get("code", "")
        if not args.reveal:
            code = _mask(code)
        print(f"  {name:<20} {fields.get('ip', '?'):<15} "
              f"{code:<10} {fields.get('serial', '')}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cp = _load()
    section = _section_name(args.name)
    if not cp.has_section(section):
        print(f"no such section: {section}", file=sys.stderr)
        return 1
    print(f"[{section}]")
    for k, v in cp.items(section):
        if k == "code" and not args.reveal:
            v = _mask(v)
        print(f"{k} = {v}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    if not args.code.isdigit() or len(args.code) != 8:
        print(f"access code must be 8 digits (got: {args.code!r})",
              file=sys.stderr)
        return 2
    cp = _load()
    section = _section_name(args.name)
    if cp.has_section(section) and not args.force:
        print(f"section [{section}] already exists — pass --force to overwrite",
              file=sys.stderr)
        return 1
    if not cp.has_section(section):
        cp.add_section(section)
    cp.set(section, "ip", args.ip)
    cp.set(section, "code", args.code)
    cp.set(section, "serial", args.serial)
    _save(cp)
    print(f"saved [{section}] → {CREDS_PATH}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    cp = _load()
    section = _section_name(args.name)
    if not cp.has_section(section):
        print(f"no such section: {section}", file=sys.stderr)
        return 1
    cp.remove_section(section)
    _save(cp)
    print(f"removed [{section}]")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    cp = _load()
    old, new = _section_name(args.old), _section_name(args.new)
    if not cp.has_section(old):
        print(f"no such section: {old}", file=sys.stderr)
        return 1
    if cp.has_section(new) and old != new:
        print(f"target [{new}] already exists", file=sys.stderr)
        return 1
    items = dict(cp.items(old))
    cp.remove_section(old)
    cp.add_section(new)
    for k, v in items.items():
        cp.set(new, k, v)
    _save(cp)
    print(f"renamed [{old}] → [{new}]")
    return 0


# ----- CLI ----------------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    p = sub.add_parser(
        "config",
        help="Manage ~/.x2d/credentials sections (list/show/add/remove/rename).",
    )
    cfg_sub = p.add_subparsers(dest="config_cmd", required=True)

    ls = cfg_sub.add_parser("list", help="List all printer sections")
    ls.add_argument("--reveal", action="store_true",
                    help="Show full access codes (default: masked)")
    ls.set_defaults(fn=cmd_list)

    sh = cfg_sub.add_parser("show", help="Show one section in detail")
    sh.add_argument("name", help="Section name (e.g. 'studio' for [printer:studio])")
    sh.add_argument("--reveal", action="store_true")
    sh.set_defaults(fn=cmd_show)

    ad = cfg_sub.add_parser("add", help="Add or update a printer section")
    ad.add_argument("name")
    ad.add_argument("--ip", required=True, help="Printer LAN IP")
    ad.add_argument("--code", required=True, help="8-digit access code")
    ad.add_argument("--serial", required=True, help="Printer serial number")
    ad.add_argument("--force", action="store_true",
                    help="Overwrite existing section with same NAME")
    ad.set_defaults(fn=cmd_add)

    rm = cfg_sub.add_parser("remove", help="Delete a section",
                             aliases=["rm"])
    rm.add_argument("name")
    rm.set_defaults(fn=cmd_remove)

    rn = cfg_sub.add_parser("rename", help="Rename a section")
    rn.add_argument("old")
    rn.add_argument("new")
    rn.set_defaults(fn=cmd_rename)

    return p
