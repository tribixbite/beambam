"""beambam.slicer_paths — find Bambu Studio and its files on any machine, and run its CLI.

The slicing helpers (x2d_slice.py, `beambam slice` / `slice-print`) were written on a
Termux phone: every path defaulted to `/data/data/com.termux/files/home/git/x2d` and the
source-built `bs-bionic/build/src/bambu-studio`, so on Windows, macOS or desktop Linux they
reported "bambu-studio not found" even with Bambu Studio installed. Resolution order:

Bambu Studio executable (:func:`bambu_studio_bin`):
  1. ``$BAMBU_STUDIO`` (or legacy ``$X2D_BS_BIN``)
  2. the Termux/bionic build under the repo (``bs-bionic/build/src/bambu-studio``)
  3. the official build ``beambam slicer install`` downloaded (``<config dir>/slicer/current.json``,
     :func:`downloaded_slicer`): asked for, the version CI slices with, so it outranks the rest
  4. ``bambu-studio`` / ``BambuStudio`` on PATH
  5. the standard install: ``%ProgramFiles%\\Bambu Studio\\bambu-studio.exe`` (Windows),
     ``/Applications/BambuStudio.app/Contents/MacOS/BambuStudio`` (macOS),
     ``/usr/bin/bambu-studio`` / ``/opt/bambu-studio/bambu-studio`` (Linux)

Profiles (:func:`bbl_profiles_dir`): ``resources/profiles/BBL`` of whichever executable was found:
recorded by ``slicer install``, else beside it (Windows), ``../resources`` (Linux AppImage, which
keeps the binary in ``bin/``), ``../Resources`` (macOS bundle); the bionic tree keeps it in
``bs-bionic/resources``.

Template (:func:`default_template`): ``$X2D_TEMPLATE``, else
``<config dir>/templates/x2d_default.gcode.3mf``, else the repo's ``rumi_frame.gcode.3mf``.

:func:`run_bambu_cli` runs the CLI with its working directory set to the output dir (it
writes ``result.json`` into the CWD) and stops a process that hangs AFTER finishing — Bambu
Studio 02.08 on Windows does that in roughly 2 of 12 runs once ``result.json`` is written.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from beambam.paths import config_path

__all__ = [
    "REPO_ROOT",
    "bambu_studio_bin",
    "bbl_profiles_dir",
    "default_template",
    "is_bionic_build",
    "run_bambu_cli",
]

# The checkout (or site-packages) dir that holds x2d_slice.py, bs-bionic/, runtime/.
REPO_ROOT = Path(os.environ.get("X2D_ROOT") or Path(__file__).resolve().parent.parent)

_BIONIC_BIN = Path("bs-bionic") / "build" / "src" / "bambu-studio"


def _standard_installs() -> list[Path]:
    if os.name == "nt":
        bases = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramW6432", ""), os.environ.get("LOCALAPPDATA", "")]
        return [Path(b) / "Bambu Studio" / "bambu-studio.exe" for b in bases if b]
    if sys.platform == "darwin":
        return [Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")]
    return [Path("/usr/bin/bambu-studio"), Path("/usr/local/bin/bambu-studio"),
            Path("/opt/bambu-studio/bambu-studio"), Path("/opt/BambuStudio/bin/bambu-studio")]


def downloaded_slicer() -> dict[str, str] | None:
    """The Bambu Studio `beambam slicer install` put in the config dir (its manifest: "exe", "profiles",
    "version", …), or None when there is none or its files are gone."""
    try:
        man = json.loads(config_path("slicer", "current.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(man, dict) or not all(isinstance(man.get(k), str) for k in ("exe", "profiles")):
        return None
    return man if Path(man["exe"]).is_file() and Path(man["profiles"]).is_dir() else None


def bambu_studio_bin() -> Path | None:
    """The Bambu Studio executable to slice with, or None if none is installed."""
    for var in ("BAMBU_STUDIO", "X2D_BS_BIN"):
        raw = os.environ.get(var)
        if raw:
            p = Path(raw).expanduser()
            return p if p.is_file() else None          # explicit setting: no guessing
    bionic = REPO_ROOT / _BIONIC_BIN
    if bionic.is_file():
        return bionic
    got = downloaded_slicer()                           # asked for: before whatever else is installed
    if got:
        return Path(got["exe"])
    for name in ("bambu-studio", "BambuStudio", "bambu-studio.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return next((p for p in _standard_installs() if p.is_file()), None)


def is_bionic_build(binary: Path) -> bool:
    """True for the Termux source build, which needs its bundled libs + headless GL env."""
    try:
        return binary.resolve() == (REPO_ROOT / _BIONIC_BIN).resolve()
    except OSError:
        return False


def bbl_profiles_dir(binary: Path | None = None) -> Path | None:
    """`resources/profiles/BBL` of the given (default: resolved) Bambu Studio."""
    binary = binary or bambu_studio_bin()
    candidates: list[Path] = []
    got = downloaded_slicer()
    if binary is not None and got and Path(got["exe"]) == binary:
        candidates.append(Path(got["profiles"]))        # recorded at install: no layout guessing
    if binary is not None:
        if is_bionic_build(binary):
            candidates.append(REPO_ROOT / "bs-bionic" / "resources" / "profiles" / "BBL")
        candidates += [binary.parent / "resources" / "profiles" / "BBL",              # Windows, Linux
                       binary.parent.parent / "resources" / "profiles" / "BBL",       # AppImage: bin/../resources
                       binary.parent.parent / "Resources" / "profiles" / "BBL",       # macOS bundle
                       binary.parent.parent / "share" / "BambuStudio" / "profiles" / "BBL"]
    candidates.append(REPO_ROOT / "bs-bionic" / "resources" / "profiles" / "BBL")
    return next((c for c in candidates if c.is_dir()), None)


def default_template() -> Path:
    """The reference X2D .gcode.3mf whose embedded profile STL slices inherit."""
    raw = os.environ.get("X2D_TEMPLATE")
    if raw:
        return Path(raw).expanduser()
    user = config_path("templates", "x2d_default.gcode.3mf")
    if user.is_file():
        return user
    repo = REPO_ROOT / "rumi_frame.gcode.3mf"
    return repo if repo.is_file() else user


def _result_complete(out_dir: Path) -> bool:
    """Bambu Studio writes result.json last; complete = parses with a return code."""
    try:
        data = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "return_code" in data


def _absolutize_args(cmd: list[str]) -> list[str]:
    """Make the path arguments of a Bambu Studio command absolute.

    run_bambu_cli starts the CLI with its CWD set to the output directory, so
    a relative path built by the caller (relative to the caller's CWD) would
    be resolved against the wrong directory: `-o out/x.gcode.3mf` made the
    slicer write into `out/out/`. `--outputdir`'s value is always made
    absolute; any other non-flag argument is made absolute when it names an
    existing file or directory (each part of a `;`-joined list likewise).
    `--export-3mf`'s value is left alone: the CLI resolves it against
    --outputdir."""
    out: list[str] = []
    prev = ""
    for arg in cmd:
        if prev == "--outputdir":
            arg = str(Path(arg).resolve())
        elif prev != "--export-3mf" and arg and not arg.startswith("-"):
            parts = arg.split(";")
            fixed = [str(Path(p).resolve()) if p and not Path(p).is_absolute()
                     and Path(p).exists() else p for p in parts]
            arg = ";".join(fixed)
        out.append(arg)
        prev = arg if arg.startswith("-") else ""
    return out


def run_bambu_cli(cmd: list[str], out_dir: Path, *, env: dict[str, str] | None = None,
                  timeout: float = 1800.0, exit_grace: float = 20.0, log: Path | None = None) -> int:
    """Run a Bambu Studio CLI command; return its exit code.

    Runs in `out_dir` (the CLI writes result.json into its CWD), with no stdin (and no console
    window on Windows). If the process is still alive `exit_grace` seconds after a complete
    result.json appeared, it is killed and the code from result.json is returned (the
    hang-at-exit is after all output is written). `log`: its stdout and stderr go there
    (default: inherited). Raises TimeoutError after `timeout` seconds without a result.
    """
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = _absolutize_args(cmd)             # paths stay valid after the cwd change
    stale = out_dir / "result.json"
    try:
        stale.unlink()                      # never read a previous run's verdict
    except OSError:
        pass
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    sink = log.open("wb") if log is not None else None
    try:
        return _wait_cli(subprocess.Popen(cmd, cwd=str(out_dir), env=env, creationflags=flags,
                                          stdin=subprocess.DEVNULL, stdout=sink,
                                          stderr=subprocess.STDOUT if sink else None),
                         out_dir, timeout, exit_grace)
    finally:
        if sink is not None:
            sink.close()


def _wait_cli(proc: subprocess.Popen[bytes], out_dir: Path, timeout: float, exit_grace: float) -> int:
    """`run_bambu_cli`'s wait: the exit code, or result.json's after a hang at exit."""
    started = time.monotonic()
    finished_at: float | None = None
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc
        now = time.monotonic()
        if finished_at is None and _result_complete(out_dir):
            finished_at = now
        if finished_at is not None and now - finished_at > exit_grace:
            proc.kill()
            proc.wait(10)
            code = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))["return_code"]
            print(f"[slicer] bambu-studio hung after writing result.json; stopped it "
                  f"(return_code {code})", file=sys.stderr)
            return int(code)
        if now - started > timeout:
            proc.kill()
            proc.wait(10)
            raise TimeoutError(f"bambu-studio did not finish within {timeout:.0f} s")
        time.sleep(0.5)
