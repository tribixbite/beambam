"""beambam.config — printer credentials + env/file resolution.

Canonical home of the `Creds` dataclass as of v1.2.0. The bridge
(x2d_bridge.py) imports Creds from here. New code should use:

    from beambam.config import Creds

Credentials sources, in priority order:
  1. argparse args (--ip --code --serial --printer)
  2. env vars  X2D_IP / X2D_CODE / X2D_SERIAL  (+ X2D_PRINTER)
  3. ~/.x2d/credentials INI file:
        [printer]                 default section
        ip = 192.168.x.y
        code = 12345678
        serial = 03ABC0001234567

        [printer:NAME]            multi-printer setups
        ip = ...
"""
from __future__ import annotations

import argparse
import configparser
import os
import sys
from dataclasses import dataclass
from pathlib import Path


__all__ = ["Creds"]


@dataclass
class Creds:
    """Printer connection credentials. Use `Creds.resolve()` with an
    argparse.Namespace, or `Creds.resolve_default()` / `Creds.from_section()`
    for the env/INI-only flows."""

    ip: str
    code: str
    serial: str
    name: str = ""   # which [printer:NAME] section we came from (if any)

    # ----- convenience constructors -----------------------------------

    @classmethod
    def resolve_default(cls) -> "Creds":
        """Resolve from env vars or ~/.x2d/credentials [printer] section."""
        return cls.resolve(argparse.Namespace(
            ip=None, code=None, serial=None, printer=None,
        ))

    @classmethod
    def from_section(cls, name: str) -> "Creds":
        """Resolve from a specific [printer:NAME] section."""
        return cls.resolve(argparse.Namespace(
            ip=None, code=None, serial=None, printer=name,
        ))

    # ----- INI inspection ---------------------------------------------

    @staticmethod
    def list_names(ini_path: Path | None = None) -> list[str]:
        """Return all `[printer:NAME]` section names in the creds file,
        in declaration order. The plain `[printer]` is reported as ''."""
        if ini_path is None:
            ini_path = Path.home() / ".x2d" / "credentials"
        if not ini_path.exists():
            return []
        cp = configparser.ConfigParser()
        cp.read(ini_path)
        names: list[str] = []
        for sec in cp.sections():
            if sec == "printer":
                names.append("")
            elif sec.startswith("printer:"):
                names.append(sec.split(":", 1)[1])
        return names

    # ----- canonical resolver -----------------------------------------

    @classmethod
    def resolve(cls, args: argparse.Namespace) -> "Creds":
        """Resolve creds from CLI args + env vars + ~/.x2d/credentials.

        Exits with sys.exit (printing a helpful message) if no creds
        can be found, or if a `--printer NAME` request matches no
        section."""
        env_ip = os.environ.get("X2D_IP", "")
        env_code = os.environ.get("X2D_CODE", "")
        env_serial = os.environ.get("X2D_SERIAL", "")

        ini_ip = ini_code = ini_serial = ""
        chosen_name = ""
        ini_path = Path.home() / ".x2d" / "credentials"
        if ini_path.exists():
            cp = configparser.ConfigParser()
            cp.read(ini_path)
            requested = (getattr(args, "printer", None)
                          or os.environ.get("X2D_PRINTER", ""))
            named_sections = [s for s in cp.sections()
                               if s.startswith("printer:")]
            if requested:
                target = f"printer:{requested}"
                if not cp.has_section(target):
                    sys.exit(
                        f"no [{target}] section in {ini_path}.\n"
                        f"available: {', '.join(named_sections) or '(none)'}"
                    )
                section = target
                chosen_name = requested
            elif cp.has_section("printer"):
                section = "printer"
            elif len(named_sections) == 1:
                section = named_sections[0]
                chosen_name = section.split(":", 1)[1]
            elif len(named_sections) > 1:
                sys.exit(
                    "multiple [printer:NAME] sections found and no "
                    "--printer/X2D_PRINTER set; choose one of: "
                    f"{', '.join(s.split(':',1)[1] for s in named_sections)}"
                )
            else:
                section = "printer"  # falls through to "missing" below
            if cp.has_section(section):
                ini_ip = cp.get(section, "ip", fallback="")
                ini_code = cp.get(section, "code", fallback="")
                ini_serial = cp.get(section, "serial", fallback="")

        ip = (getattr(args, "ip", None) or env_ip or ini_ip)
        code = (getattr(args, "code", None) or env_code or ini_code)
        serial = (getattr(args, "serial", None) or env_serial or ini_serial)
        if not (ip and code and serial):
            sys.exit(
                "credentials missing — provide --ip --code --serial, or\n"
                "set X2D_IP / X2D_CODE / X2D_SERIAL env vars, or write\n"
                "  ~/.x2d/credentials\n\n"
                "  # default printer\n"
                "  [printer]\n"
                "  ip = 192.168.x.y\n  code = 12345678\n  serial = 03ABC...\n"
                "\n"
                "  # OR multiple, selected via --printer NAME / X2D_PRINTER\n"
                "  [printer:studio]\n  ip = …\n  code = …\n  serial = …\n"
            )
        return cls(ip=ip, code=code, serial=serial, name=chosen_name)
