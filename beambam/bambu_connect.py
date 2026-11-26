"""beambam.bambu_connect — start a print through Bambu Connect: the printer owner's own Bambu login, no key.

X2D / H2D firmware runs a print only when the command is signed by a key Bambu certified. beambam signs with the
key `beambam key` recovers from Bambu Handy; without one, Bambu's own bridge app, Bambu Connect, can send the
file: it prints over the cloud (with the account the user signed in to it with) or the LAN. A third-party program
hands it a *sliced* file by opening this URL (Bambu wiki, "Third-party integration"):

    bambu-connect://import-file?path=<absolute path>&name=<name>&version=1.0.0

(each value percent-encoded; `version` is fixed at 1.0.0). Bambu Connect then asks for the printer, plate and
filament mapping and the user presses Send: beambam does not start that print itself. Bambu Connect exists for
Windows 10+ (x86-64) and macOS 13+; there is none for Linux yet.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

SCHEME = "bambu-connect"
HANDOFF_VERSION = "1.0.0"
DOWNLOAD = "https://wiki.bambulab.com/en/software/bambu-connect"


class ConnectError(RuntimeError):
    """The file cannot be handed to Bambu Connect, in words for the user."""


def handoff_url(path: Path, name: str | None = None) -> str:
    """The `bambu-connect://import-file` URL for a sliced file at `path` (made absolute)."""
    q = urllib.parse.quote
    full = str(Path(path).resolve())
    return (f"{SCHEME}://import-file?path={q(full, safe='')}&name={q(name or Path(path).name, safe='')}"
            f"&version={HANDOFF_VERSION}")


def is_sliced(path: Path) -> bool:
    """A Bambu print file: a zip with at least one plate's G-code (`Metadata/plate_<n>.gcode`)."""
    try:
        with zipfile.ZipFile(path) as z:
            return any(n.startswith("Metadata/plate_") and n.endswith(".gcode") for n in z.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def installed() -> bool | None:
    """Whether Bambu Connect is installed here: True / False where it can be checked, None where it cannot
    (no build for this system: Linux)."""
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, SCHEME):         # its URL handler
                return True
        except OSError:
            return False
    if platform.system() == "Darwin":
        return any(Path(p).is_dir() for p in ("/Applications/Bambu Connect.app",
                                             str(Path.home() / "Applications" / "Bambu Connect.app")))
    return None


def problem() -> str:
    """Why a hand-off cannot work here ('' when it can)."""
    got = installed()
    if got is None:
        return "Bambu Connect has no build for this system (Windows and macOS only)"
    if not got:
        return f"Bambu Connect is not installed: get it from {DOWNLOAD}"
    return ""


def open_file(path: Path, name: str | None = None) -> str:
    """Hand the sliced file at `path` to Bambu Connect; the URL opened. ConnectError when it cannot be."""
    path = Path(path)
    if not path.is_file():
        raise ConnectError(f"{path} does not exist")
    if not is_sliced(path):
        raise ConnectError(f"{path.name} is not sliced: Bambu Connect takes a sliced file (.gcode.3mf); slice it "
                           "first (`beambam slice`, or POST /slice)")
    why = problem()
    if why:
        raise ConnectError(why)
    url = handoff_url(path, name)
    if sys.platform == "win32":
        os.startfile(url)                                     # noqa: S606  (the registered URL handler)
    else:
        r = subprocess.run(["open", url], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise ConnectError(f"could not open Bambu Connect: {r.stderr.strip() or r.returncode}")
    return url


# --------------------------------------------------------------------------- CLI

def add_subparser(sub) -> None:  # noqa: ANN001  (argparse._SubParsersAction)
    p = sub.add_parser("connect", aliases=["bambu-connect"],
                       help="Hand a sliced file to Bambu Connect to print with your Bambu login (no key needed)")
    p.add_argument("file", type=Path, help="the sliced print file (.gcode.3mf)")
    p.add_argument("--name", help="the job name Bambu Connect shows (default: the file name)")
    p.add_argument("--print-url", action="store_true",
                   help="only print the bambu-connect:// URL (to open it elsewhere)")
    p.set_defaults(fn=cmd_connect)


def cmd_connect(args) -> int:  # noqa: ANN001
    if args.print_url:
        print(handoff_url(args.file, args.name))
        return 0
    try:
        open_file(args.file, args.name)
    except ConnectError as e:
        print(f"beambam connect: {e}", file=sys.stderr)
        return 1
    print(f"handed {args.file.name} to Bambu Connect: choose the printer and filaments there, then press Send")
    return 0
