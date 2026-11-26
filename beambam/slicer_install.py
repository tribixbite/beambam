"""beambam.slicer_install — download Bambu Lab's official Bambu Studio for beambam to slice with.

beambam slices with Bambu Studio's command line, which is not a Python package: the phone has a source build
(bs-bionic), a laptop usually has none, or one whose version beambam has not been tested with. `beambam slicer
install` fetches the official release from GitHub (bambulab/BambuStudio) and unpacks it into the config dir:

    <config dir>/slicer/<tag>/          the unpacked build
    <config dir>/slicer/current.json    {"version", "exe", "profiles", "asset", "sha256"}: what
                                        slicer_paths.downloaded_slicer() and the daemon use

Per platform: Linux x86_64 an AppImage (by the system's glibc: Ubuntu 24.04's build needs 2.39, 22.04's 2.35),
extracted with its own `--appimage-extract` (no FUSE); macOS the .dmg (attached read-only, the .app copied);
Windows x64 the portable .zip. Linux on ARM has no official build: point $BAMBU_STUDIO at one instead.

The archive's sha256 is checked against the digest GitHub lists for it (corruption and truncation; it comes
from the same origin as the file). The tag defaults to the release CI slices with (DEFAULT_TAG); `--version
latest` takes the newest. Nothing here runs unless asked: the daemon never downloads on its own.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from beambam.paths import config_path

REPO = "bambulab/BambuStudio"
# The release `beambam slicer install` takes by default: the one CI slices with (.github/workflows/ci.yml,
# job "slicer"). Raise it with that job.
DEFAULT_TAG = "v02.09.02.62"
# What a build must have to slice for the X2D (slice_options.MESH_PRINTER's machine profile).
X2D_MACHINE = "Bambu Lab X2D 0.4 nozzle"
CHUNK = 1 << 20


class SlicerInstallError(RuntimeError):
    """A download or unpack that cannot go ahead, in words for the user."""


# --------------------------------------------------------------------------- choosing the asset

def _glibc() -> tuple[int, int] | None:
    lib, ver = platform.libc_ver()
    if lib != "glibc" or not ver:
        return None
    try:
        major, minor = ver.split(".")[:2]
        return int(major), int(minor)
    except ValueError:
        return None


def pick_asset(assets: list[dict[str, Any]], system: str | None = None, machine: str | None = None,
               glibc: tuple[int, int] | None = None) -> dict[str, Any]:
    """The release asset for this machine (`system` / `machine` / `glibc` default to this one's), or
    SlicerInstallError saying why there is none."""
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    names = {a.get("name", ""): a for a in assets}

    def first(pred: Callable[[str], bool]) -> dict[str, Any] | None:
        return next((a for n, a in names.items() if pred(n.lower())), None)

    if system == "Windows":
        if machine not in ("amd64", "x86_64"):
            raise SlicerInstallError(f"Bambu Lab publishes Bambu Studio for 64-bit Intel/AMD Windows only "
                                     f"(this is {machine}); set BAMBU_STUDIO to a build that runs here")
        got = first(lambda n: n.startswith("bambu_studio_win") and n.endswith(".zip"))
    elif system == "Darwin":
        got = first(lambda n: "mac" in n and n.endswith(".dmg"))
    elif system == "Linux":
        if machine not in ("x86_64", "amd64"):
            raise SlicerInstallError(f"Bambu Lab publishes no Bambu Studio for Linux on {machine}; build one or "
                                     "set BAMBU_STUDIO to a Bambu Studio executable that runs here")
        libc = glibc if glibc is not None else _glibc()
        if libc is None:
            raise SlicerInstallError("the official Linux builds need glibc (this system has another C library); "
                                     "set BAMBU_STUDIO to a Bambu Studio that runs here")
        got = None
        if libc >= (2, 39):
            got = first(lambda n: "ubu24" in n and n.endswith(".appimage"))
        if got is None and libc >= (2, 35):
            got = first(lambda n: "ubu22" in n and n.endswith(".appimage"))
        if got is None:
            raise SlicerInstallError(f"the official Linux builds need glibc 2.35 or newer (this system has "
                                     f"{libc[0]}.{libc[1]}); set BAMBU_STUDIO to a Bambu Studio that runs here")
    else:
        raise SlicerInstallError(f"no official Bambu Studio for {system}")
    if got is None:
        raise SlicerInstallError(f"the release has no Bambu Studio download for {system} {machine}")
    return got


# --------------------------------------------------------------------------- GitHub

def _get(url: str, accept: str = "application/vnd.github+json") -> Any:
    from beambam import __version__
    headers = {"Accept": accept, "User-Agent": f"beambam/{__version__}"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"          # the anonymous limit is 60 requests an hour
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)


def release(tag: str = DEFAULT_TAG) -> dict[str, Any]:
    """The GitHub release `tag` ("latest": the newest) of Bambu Studio."""
    path = "latest" if tag == "latest" else f"tags/{tag}"
    try:
        with _get(f"https://api.github.com/repos/{REPO}/releases/{path}") as r:
            data = json.load(r)
    except OSError as e:
        raise SlicerInstallError(f"cannot read Bambu Studio's release {tag} from GitHub: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
        raise SlicerInstallError(f"GitHub answered no release for {tag}")
    return data


def download(url: str, dest: Path, size: int | None, digest: str | None,
             progress: Callable[[str], None] = lambda _: None) -> Path:
    """Stream `url` to `dest` (through `dest.part`) and check its size and `digest` ("sha256:<hex>")."""
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    got = 0
    shown = -1
    try:
        with _get(url, accept="application/octet-stream") as r, part.open("wb") as f:
            while chunk := r.read(CHUNK):
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if size:
                    pct = got * 100 // size
                    if pct // 10 != shown:
                        shown = pct // 10
                        progress(f"  {pct:3d}%  {got / 1e6:6.0f} of {size / 1e6:.0f} MB")
    except OSError as e:
        part.unlink(missing_ok=True)
        raise SlicerInstallError(f"download failed ({url}): {e}") from e
    if size is not None and got != size:
        part.unlink(missing_ok=True)
        raise SlicerInstallError(f"download incomplete: {got} of {size} bytes")
    if digest:
        algo, _, want = digest.partition(":")
        if algo != "sha256" or h.hexdigest() != want.lower():
            part.unlink(missing_ok=True)
            raise SlicerInstallError(f"download corrupt: sha256 {h.hexdigest()} is not the published {digest}")
    else:
        progress("  (GitHub lists no checksum for this file: only its size was checked)")
    part.replace(dest)
    return dest


# --------------------------------------------------------------------------- unpacking

def safe_extract_zip(archive: Path, dest: Path) -> None:
    """Unzip `archive` into `dest`, refusing entries that would land outside it."""
    root = dest.resolve()
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if target != root and root not in target.parents:
                raise SlicerInstallError(f"refusing {info.filename!r} in {archive.name}: outside the folder")
        z.extractall(dest)


def _extract_appimage(archive: Path, dest: Path) -> None:
    archive.chmod(archive.stat().st_mode | stat.S_IXUSR)
    r = subprocess.run([str(archive), "--appimage-extract"], cwd=dest, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
    if r.returncode != 0 or not (dest / "squashfs-root").is_dir():
        raise SlicerInstallError(f"cannot unpack {archive.name} (exit {r.returncode}): "
                                 f"{r.stderr.decode(errors='replace').strip()[-400:]}")


def _extract_dmg(archive: Path, dest: Path) -> None:
    mount = Path(tempfile.mkdtemp(prefix="beambam-dmg-"))
    # `input`: a disk image with a licence asks for it to be accepted on stdin.
    r = subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount),
                        str(archive)], input=b"Y\n", capture_output=True, timeout=600)
    if r.returncode != 0:
        raise SlicerInstallError(f"cannot open {archive.name}: {r.stderr.decode(errors='replace').strip()}")
    try:
        apps = sorted(mount.glob("*.app"))
        if not apps:
            raise SlicerInstallError(f"{archive.name} holds no .app")
        shutil.copytree(apps[0], dest / apps[0].name, symlinks=True)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True, timeout=120)
        shutil.rmtree(mount, ignore_errors=True)


def extract(archive: Path, dest: Path) -> None:
    """Unpack a downloaded asset into the (empty) folder `dest`."""
    name = archive.name.lower()
    if name.endswith(".zip"):
        safe_extract_zip(archive, dest)
    elif name.endswith(".appimage"):
        _extract_appimage(archive, dest)
    elif name.endswith(".dmg"):
        _extract_dmg(archive, dest)
    else:
        raise SlicerInstallError(f"do not know how to unpack {archive.name}")


def locate(tree: Path) -> tuple[Path, Path]:
    """(executable, resources/profiles/BBL folder) in an unpacked build, or SlicerInstallError."""
    exes = [tree / "squashfs-root" / "AppRun",                            # AppImage: sets its library path
            *sorted(tree.glob("*.app/Contents/MacOS/BambuStudio")),
            *sorted(tree.rglob("bambu-studio.exe")),
            tree / "squashfs-root" / "bin" / "bambu-studio"]
    exe = next((p for p in exes if p.is_file()), None)
    if exe is None:
        raise SlicerInstallError(f"no Bambu Studio executable in {tree}")
    profiles = next((d for d in sorted(tree.rglob("BBL"))
                     if d.is_dir() and d.parent.name == "profiles" and (d / "machine").is_dir()), None)
    if profiles is None:
        raise SlicerInstallError(f"no Bambu profiles (resources/profiles/BBL) in {tree}")
    return exe, profiles


# --------------------------------------------------------------------------- install

def slicer_root() -> Path:
    return config_path("slicer")


def install(tag: str = DEFAULT_TAG, force: bool = False,
            progress: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> dict[str, Any]:
    """Download and unpack Bambu Studio `tag` (DEFAULT_TAG; "latest") and make it the one beambam slices
    with. Returns the manifest written to `<config dir>/slicer/current.json`."""
    from beambam.profile_flatten import flatten, missing
    rel = release(tag)
    version = str(rel.get("tag_name") or tag)
    asset = pick_asset(rel["assets"])
    root = slicer_root()
    final = root / version
    manifest_path = root / "current.json"
    if final.is_dir() and not force:
        try:
            exe, profiles = locate(final)
        except SlicerInstallError:
            pass                                    # half-gone: unpack it again
        else:
            man = _manifest(version, exe, profiles, asset)
            _write_manifest(manifest_path, man)
            progress(f"Bambu Studio {version} is already installed ({exe})")
            return man
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=root))
    try:
        archive = work / asset["name"]
        progress(f"Downloading {asset['name']} ({int(asset.get('size') or 0) / 1e6:.0f} MB) from {REPO}…")
        download(asset["browser_download_url"], archive, asset.get("size"), asset.get("digest"), progress)
        unpacked = work / "unpacked"
        unpacked.mkdir()
        progress("Unpacking…")
        extract(archive, unpacked)
        archive.unlink()                            # the unpacked tree is what stays
        exe, profiles = locate(unpacked)
        if not (profiles / "machine" / f"{X2D_MACHINE}.json").is_file():
            raise SlicerInstallError(f"Bambu Studio {version} has no X2D profile ({X2D_MACHINE}): "
                                     "take a newer version")
        if missing(profiles):
            progress(f"Writing the flattened profiles it lacks ({', '.join(missing(profiles))})…")
            flatten(profiles)
        if final.exists():
            shutil.rmtree(final)
        unpacked.replace(final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    exe, profiles = locate(final)
    man = _manifest(version, exe, profiles, asset)
    _write_manifest(manifest_path, man)
    progress(f"Installed Bambu Studio {version}: {exe}")
    return man


def _manifest(version: str, exe: Path, profiles: Path, asset: dict[str, Any]) -> dict[str, Any]:
    return {"version": version, "exe": str(exe), "profiles": str(profiles), "asset": asset.get("name", ""),
            "sha256": str(asset.get("digest") or "")}


def _write_manifest(path: Path, man: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(man, indent=2), encoding="utf-8")
    tmp.replace(path)


def remove() -> bool:
    """Delete every downloaded build and the manifest; False when there was none."""
    root = slicer_root()
    if not root.exists():
        return False
    shutil.rmtree(root)
    return True


# --------------------------------------------------------------------------- CLI

def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """`beambam slicer` (which Bambu Studio slices), `slicer install`, `slicer remove`."""
    p = sub.add_parser("slicer", help="The Bambu Studio beambam slices with: show it, install it, remove it")
    s = p.add_subparsers(dest="slicer_cmd")
    i = s.add_parser("install", help="Download Bambu Lab's official Bambu Studio for beambam to slice with")
    i.add_argument("--version", default=DEFAULT_TAG, dest="tag",
                   help=f"Release tag to install, or 'latest' (default {DEFAULT_TAG}, the one CI slices with)")
    i.add_argument("--force", action="store_true", help="Download and unpack again even if it is installed")
    s.add_parser("status", help="Which Bambu Studio slices, and whether it can (the default)")
    s.add_parser("remove", help="Delete the Bambu Studio `slicer install` downloaded")
    p.set_defaults(fn=cmd_slicer)
    return p


def cmd_slicer(args: argparse.Namespace) -> int:
    action = getattr(args, "slicer_cmd", None) or "status"
    if action == "install":
        try:
            install(args.tag, force=args.force)
        except SlicerInstallError as e:
            print(f"beambam slicer install: {e}", file=sys.stderr)
            return 1
        return _status()
    if action == "remove":
        print("removed the downloaded Bambu Studio" if remove() else "no downloaded Bambu Studio to remove")
        return 0
    return _status()


def _status() -> int:
    from beambam.slice_service import resolve_slicer, slicer_status
    st = slicer_status()
    s = resolve_slicer()
    if s is None:
        print(st["problem"])
        return 1
    kind = {"phone": "this checkout's source build", "downloaded": f"downloaded ({st['version']})",
            "installed": "installed Bambu Studio"}[st["kind"]]
    print(f"slicer:   {s.exe}\nkind:     {kind}\nprofiles: {s.profiles}")
    print("ready" if st["ready"] else f"not ready: {st['problem']}")
    return 0 if st["ready"] else 1
