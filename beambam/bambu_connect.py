"""beambam.bambu_connect — start a print through Bambu Connect: the printer owner's own Bambu login, no key.

X2D / H2D firmware runs a print only when the command is signed by a key Bambu certified. beambam signs with the
key `beambam key` recovers from Bambu Handy; without one, Bambu's own bridge app, Bambu Connect, can send the
file: it prints over the cloud (with the account the user signed in to it with) or the LAN. A third-party program
hands it a *sliced* file by opening this URL (Bambu wiki, "Third-party integration"):

    bambu-connect://import-file?path=<absolute path>&name=<name>&version=1.0.0

(each value percent-encoded; `version` is fixed at 1.0.0). Bambu Connect then asks for the printer, plate and
filament mapping and the user presses Send: beambam does not start that print itself. Bambu Connect exists for
Windows 10+ (x86-64) and macOS 13+; there is none for Linux yet.

This is the route that needs no rooted phone: `beambam key` recovers the signing key from Bambu Handy (root
only), while Bambu Connect needs nothing but the owner's Bambu login. `beambam connect --install` downloads the
official installer for this machine (the current version from the Bambu wiki, falling back to a pinned one) and
installs it, so a user on Windows or macOS gets a key-free print without touching a phone. Linux has no build to
install, so there it still reports that and points at the download page.
"""
from __future__ import annotations

import contextlib
import http.client
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# fetch(url) -> page text (the wiki); injectable so tests need no network.
Fetch = Callable[[str], str]

SCHEME = "bambu-connect"
HANDOFF_VERSION = "1.0.0"
DOWNLOAD = "https://wiki.bambulab.com/en/software/bambu-connect"

# Where the official installers live, and the newest version known when this was written. `beambam connect
# --install` reads the wiki page for the current version and falls back to this pin when the page can't be
# read. Only this host is ever downloaded from.
CONNECT_CDN_HOST = "public-cdn.bblmw.com"
_CDN_BASE = f"https://{CONNECT_CDN_HOST}/upgrade/bambu-connect/updates/versions"
PINNED_VERSION = "2.6.0-beta.0"
# A download link on the wiki: bambu-connect-v<version>-<platform>.<ext>. The version class admits no '/',
# '%', '?', '#' or '@', so a parsed version can't break out of its path segment in the CDN URL.
_WIKI_ASSET = re.compile(r"bambu-connect-v([0-9][0-9A-Za-z.\-]*?)-(win32-x64|darwin-arm64|darwin-x64)\.(exe|dmg)")


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


@dataclass(frozen=True)
class Download:
    """One Bambu Connect installer: its version and the URL to fetch it from."""
    version: str
    url: str

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def kind(self) -> str:
        """"exe" (Windows installer) or "dmg" (macOS disk image)."""
        return "exe" if self.url.endswith(".exe") else "dmg"


def _platform_asset(system: str, machine: str) -> str | None:
    """The `<platform>` token in the installer file name for this OS/arch, or None where there is no build."""
    if system == "Windows":
        return "win32-x64"
    if system == "Darwin":
        return "darwin-arm64" if machine.lower() in ("arm64", "aarch64") else "darwin-x64"
    return None


def download_url(version: str, system: str, machine: str) -> str:
    """The official CDN URL of the Bambu Connect installer for `version` on this OS/arch."""
    token = _platform_asset(system, machine)
    if token is None:
        raise ConnectError("Bambu Connect has no build for this system (Windows and macOS only)")
    ext = "exe" if token.startswith("win32") else "dmg"
    return f"{_CDN_BASE}/{version}/bambu-connect-v{version}-{token}.{ext}"


def latest_version(html: str, token: str | None = None) -> str | None:
    """The newest Bambu Connect version named on the wiki download page, or None if none is found.
    With `token` (e.g. "win32-x64"), only versions that ship that platform's asset count — a page that
    lists a newer macOS build than Windows build must not hand a Windows machine a version it can't get."""
    found = {m.group(1) for m in _WIKI_ASSET.finditer(html) if token is None or m.group(2) == token}
    if not found:
        return None

    def cell(tok: str) -> tuple[int, int, str]:
        # A dotted token: numbers before words, never an int compared to a str.
        return (0, int(tok), "") if tok.isdigit() else (1, 0, tok)

    def key(v: str) -> tuple[tuple[int, ...], int, tuple[tuple[int, int, str], ...]]:
        # ("2.6.1" -> ((2,6,1), 1, ()); "2.6.0-beta.0" -> ((2,6,0), 0, ((0,0,""),))). The numeric head is
        # compared first (so 2.10 > 2.6 and 2.6.1 > 2.6), then release-beats-prerelease, then the tag cells.
        head, _, tail = v.partition("-")
        nums = tuple(int(x) for x in head.split(".") if x.isdigit())
        return (nums, 0 if tail else 1, tuple(cell(x) for x in tail.split(".")) if tail else ())

    return sorted(found, key=key)[-1]


def resolve_download(*, system: str | None = None, machine: str | None = None,
                     fetch: Fetch | None = None) -> Download:
    """Which installer to fetch here: the wiki's current version for THIS platform when the page can be read,
    else the pinned one. `fetch(url) -> str` reads a URL as text (the wiki page); defaults to an HTTPS GET.
    The URL is always built against our own CDN, never taken verbatim from the page."""
    system = system or platform.system()
    machine = machine or platform.machine()
    token = _platform_asset(system, machine)
    if token is None:
        raise ConnectError("Bambu Connect has no build for this system (Windows and macOS only)")
    version = PINNED_VERSION
    getter = fetch or _get_text
    try:
        found = latest_version(getter(DOWNLOAD), token)
        if found:
            version = found
    except Exception:                                 # noqa: BLE001  the pin is the fallback for any failure
        pass
    return Download(version, download_url(version, system, machine))


def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "beambam"})
    with urllib.request.urlopen(req, timeout=30) as r:             # noqa: S310  (https, our own URL)
        return r.read().decode("utf-8", "replace")


def _download(url: str, dest: Path, say) -> Path:  # noqa: ANN001
    """Stream `url` (which must stay on our HTTPS CDN, redirects included) to `dest`, with progress through
    `say`. A short read, a redirect off the CDN, or any transport error removes the partial file and raises
    ConnectError, so a truncated or redirected installer is never run."""
    if not url.startswith(f"https://{CONNECT_CDN_HOST}/"):
        raise ConnectError(f"refusing to download from an unexpected host: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "beambam"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:         # noqa: S310  (https, pinned host)
            if not r.geturl().startswith(f"https://{CONNECT_CDN_HOST}/"):
                raise ConnectError(f"download was redirected off {CONNECT_CDN_HOST}: {r.geturl()}")
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            step = max(total // 20, 1 << 20)
            with open(dest, "wb") as f:
                while chunk := r.read(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if total and done % step < (1 << 16):
                        say(f"  … {done >> 20} MiB ({done * 100 // total}%)")
            if total and done != total:
                raise ConnectError(f"download was cut short ({done} of {total} bytes)")
    except ConnectError:
        dest.unlink(missing_ok=True)
        raise
    except (OSError, http.client.HTTPException) as e:
        dest.unlink(missing_ok=True)
        raise ConnectError(f"could not download {dest.name}: {e}") from e
    return dest


def install(*, say=print, system: str | None = None, machine: str | None = None,  # noqa: ANN001
            fetch: Fetch | None = None, runner=None, dest_dir: Path | None = None,
            silent: bool = False) -> bool:
    """Download the official Bambu Connect installer for this machine and install it. Returns True when it is
    installed now, False when the interactive Windows installer was only launched (the user finishes it).
    macOS copies the app from the disk image; Linux raises ConnectError (no build). `runner` is subprocess.run,
    injectable for tests. A temp download is cleaned up once it is no longer needed."""
    system = system or platform.system()
    machine = machine or platform.machine()
    runner = runner or subprocess.run
    dl = resolve_download(system=system, machine=machine, fetch=fetch)
    own_tmp = dest_dir is None
    tmp = Path(dest_dir) if dest_dir else Path(tempfile.mkdtemp(prefix="beambam-connect-"))
    tmp.mkdir(parents=True, exist_ok=True)
    say(f"downloading Bambu Connect {dl.version} for {system} ({dl.filename}) …")
    pkg = _download(dl.url, tmp / dl.filename, say)
    try:
        if system == "Windows":
            completed = _install_windows(pkg, say, runner, silent)
        elif system == "Darwin":
            _install_macos(pkg, say, runner)
            completed = True
        else:                                          # unreachable: resolve_download already rejected it
            raise ConnectError("Bambu Connect has no build for this system (Windows and macOS only)")
    finally:
        # Keep the file only while a launched (non-silent) Windows installer still needs it.
        if own_tmp and not (system == "Windows" and not silent):
            shutil.rmtree(tmp, ignore_errors=True)
    return completed


def _install_windows(exe: Path, say, runner, silent: bool) -> bool:  # noqa: ANN001
    """Run the downloaded installer. Silent (`/S`, electron-builder NSIS) runs to completion and returns True;
    otherwise the installer GUI is launched and False is returned (it is not installed until the user finishes)."""
    if silent:
        say("running the Bambu Connect installer …")
        r = runner([str(exe), "/S"], capture_output=True, text=True)
        if r.returncode != 0:
            raise ConnectError(f"the installer exited with {r.returncode}: {(r.stderr or '').strip()}")
        say("Bambu Connect installed — sign in with your Bambu account, then try the print again.")
        return True
    say("opening the Bambu Connect installer — complete it, then run your command again.")
    getattr(os, "startfile")(str(exe))                 # noqa: B009,S606  (Windows-only; getattr dodges mypy on POSIX)
    return False


def _install_macos(dmg: Path, say, runner) -> None:  # noqa: ANN001
    """Mount the .dmg at a path we choose (no stdout parsing), copy Bambu Connect.app into /Applications (or
    ~/Applications) preserving the bundle's symlinks, and always detach."""
    mnt = dmg.parent / "mnt"
    mnt.mkdir(exist_ok=True)
    say("mounting the disk image …")
    r = runner(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mnt), str(dmg)],
               capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise ConnectError(f"could not mount {dmg.name}: {(r.stderr or '').strip() or r.returncode}")
    try:
        src = mnt / "Bambu Connect.app"
        if not src.is_dir():
            raise ConnectError(f"{dmg.name} does not contain Bambu Connect.app")
        apps = Path("/Applications") if os.access("/Applications", os.W_OK) else Path.home() / "Applications"
        apps.mkdir(exist_ok=True)
        say(f"copying Bambu Connect into {apps} …")
        staging = apps / (src.name + ".new")           # copy aside, then swap in, so a failure keeps the old app
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(src, staging, symlinks=True)   # symlinks=True keeps the .app's code-signature seal intact
        dest = apps / src.name
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
    finally:
        with contextlib.suppress(Exception):           # detach must not mask a copy error
            runner(["hdiutil", "detach", "-force", str(mnt)], capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
    say("Bambu Connect installed — sign in with your Bambu account, then try the print again.")


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
    p.add_argument("file", type=Path, nargs="?",
                   help="the sliced print file (.gcode.3mf); omit with --install")
    p.add_argument("--name", help="the job name Bambu Connect shows (default: the file name)")
    p.add_argument("--print-url", action="store_true",
                   help="only print the bambu-connect:// URL (to open it elsewhere)")
    p.add_argument("--install", action="store_true",
                   help="download and install Bambu Connect itself (Windows/macOS), then hand the file over")
    p.add_argument("--silent", action="store_true",
                   help="install (with --install, or when offered) without prompting — implies consent to download;"
                        " on Windows runs the installer unattended (/S)")
    p.set_defaults(fn=cmd_connect)


# What an install attempt did, so the caller knows whether a hand-off can follow.
_INSTALLED = True       # installed now (macOS copy, or a silent Windows run)
_LAUNCHED = False       # the Windows installer GUI is open; the user must finish it
_DECLINED = None        # not installed: declined, or can't ask in a non-interactive shell

_INSTALL_FAILED = (ConnectError, OSError, http.client.HTTPException)


def _offer_install(say, silent: bool):  # noqa: ANN001
    """Offer to install Bambu Connect. Installs when `silent`, or on a 'yes' at an interactive prompt; never
    downloads unprompted in a non-interactive shell. Returns _INSTALLED / _LAUNCHED / _DECLINED."""
    if not silent:
        if not sys.stdin.isatty():
            return _DECLINED
        try:
            ans = input("Bambu Connect isn't installed. Download and install it now (~90–120 MB)? [y/N] ")
        except EOFError:
            return _DECLINED
        if ans.strip().lower() not in ("y", "yes"):
            return _DECLINED
    return install(say=say, silent=silent)


def cmd_connect(args) -> int:  # noqa: ANN001
    say = lambda m: print(m, file=sys.stderr)  # noqa: E731
    finish = "finish the Bambu Connect installer, then run this again."
    just_installed = False
    # --print-url only prints; it never installs or opens anything.
    if args.print_url:
        if args.file is None:
            print("beambam connect: --print-url needs a sliced file", file=sys.stderr)
            return 1
        print(handoff_url(args.file, args.name))
        return 0
    if args.install:
        if installed() is None:
            print(f"beambam connect: {problem()}", file=sys.stderr)
            return 1
        try:
            completed = install(say=say, silent=args.silent) if not installed() else _INSTALLED
        except _INSTALL_FAILED as e:
            print(f"beambam connect: could not install Bambu Connect: {e}", file=sys.stderr)
            return 1
        just_installed = completed
        if args.file is None:                          # --install on its own
            say("Bambu Connect is installed." if completed else finish)
            return 0
        if not completed:                              # the Windows GUI installer is still open
            say(finish)
            return 0
    elif installed() is False:
        # Missing but could run here (Windows/macOS): offer to install it before handing off.
        try:
            outcome = _offer_install(say, args.silent)
        except _INSTALL_FAILED as e:
            print(f"beambam connect: could not install Bambu Connect: {e}", file=sys.stderr)
            return 1
        if outcome is _DECLINED:
            print(f"beambam connect: {problem()}", file=sys.stderr)
            return 1
        if outcome is _LAUNCHED:
            say(finish)
            return 0
        just_installed = True
    if args.file is None:
        print("beambam connect: a sliced file is required (or use --install on its own)", file=sys.stderr)
        return 1
    # A fresh install can register its URL handler only on first launch, so the hand-off would say "not
    # installed" moments after a successful install. Tell the user to open it once instead of that.
    if just_installed and installed() is False:
        say("Bambu Connect is installed — open it once and sign in, then run this again.")
        return 0
    try:
        open_file(args.file, args.name)
    except ConnectError as e:
        print(f"beambam connect: {e}", file=sys.stderr)
        return 1
    print(f"handed {args.file.name} to Bambu Connect: choose the printer and filaments there, then press Send")
    return 0
