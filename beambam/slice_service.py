"""beambam.slice_service — slice a Bambu Studio project on this machine for the daemon.

A web app that builds a ready-to-slice project `.3mf` but cannot slice it
itself (pokefit.party runs its engine in the browser) sends the file to the
daemon's `POST /slice`; this module slices it with the bundled headless
Bambu Studio (`bs-bionic`) into a `.gcode.3mf` the daemon can then plan and
print (`POST /print/plan`, `POST /print`):

    job = SliceJobs().start(project_3mf)        # background; poll job.to_dict()
    ...job.state == "done" → job.file is the .gcode.3mf

The project's own process and filament settings are kept. The printer
(machine) profile is reloaded from bs-bionic's bundled copy of the same name,
because a project saved by a newer Bambu Studio can carry start G-code that
uses variables this build does not know (seen: pokefit's X2D export, "Not a
variable name" at `initial_no_support_filament_id`); `--load-settings` with a
machine profile overlays only the machine keys (tools/x2d_cli_slice.sh).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Wall-clock limit for one slice. A figure on the phone takes ~10 s; a full
# multi-colour plate a few minutes.
SLICE_TIMEOUT_S = 900.0
# The CLI sometimes writes result.json and then hangs while shutting down
# (pokefit's slicer.py saw it on 02.08); after a complete result it gets this
# long to exit before it is killed — the result stands.
EXIT_GRACE_S = 5.0
TERMINAL = ("done", "failed")
# Project settings the headless CLI resets to false unless passed as options.
CLI_FORCED_OFF = ("has_filament_switcher", "enable_filament_dynamic_map")


def _root() -> Path:
    from beambam import X2D_ROOT_PATH
    return Path(X2D_ROOT_PATH)


def slicer_binary(root: Path | None = None) -> Path:
    return (root or _root()) / "bs-bionic" / "build" / "src" / "bambu-studio"


def slicer_env(root: Path | None = None) -> dict[str, str]:
    """The environment headless bs-bionic needs (tools/x2d_cli_slice.sh): its
    libraries and the ffmpeg stubs first, software GL, no X server."""
    r = root or _root()
    libs = [r / "runtime" / "ffmpeg-stubs", r / "bs-bionic" / "build" / "src",
            r / "bs-bionic" / "build" / "src" / "local" / "lib",
            r / "bs-bionic" / "deps" / "build" / "destdir" / "usr" / "local" / "lib"]
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    old = env.get("LD_LIBRARY_PATH")
    env["LD_LIBRARY_PATH"] = ":".join([str(p) for p in libs] + ([old] if old else []))
    env.update(LIBGL_ALWAYS_SOFTWARE="1", GALLIUM_DRIVER="llvmpipe",
               EGL_PLATFORM="surfaceless", MESA_LOADER_DRIVER_OVERRIDE="llvmpipe",
               LC_ALL="C", LANG="C")
    return env


def project_settings(src: Path) -> dict[str, Any]:
    """`Metadata/project_settings.config` of a project .3mf; ValueError if it has none."""
    try:
        with zipfile.ZipFile(src) as z:
            raw = z.read("Metadata/project_settings.config")
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise ValueError(f"{src.name} is not a Bambu Studio project with print settings "
                         f"(export it as a Bambu Studio 3MF): {e}") from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"{src.name}: project settings are not readable ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"{src.name}: project settings are not an object")
    return data


def machine_profile(settings: dict[str, Any], root: Path | None = None) -> Path | None:
    """bs-bionic's bundled machine profile named like the project's printer, if any."""
    name = settings.get("printer_settings_id")
    if not isinstance(name, str) or not name or "/" in name:
        return None
    for vendor in ("BBL",):
        p = (root or _root()) / "bs-bionic" / "resources" / "profiles" / vendor / "machine" / f"{name}.json"
        if p.is_file():
            return p
    return None


def _result(out_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "return_code" in data else None


def _run(cmd: list[str], out_dir: Path, env: dict[str, str], timeout: float) -> int | None:
    """Run the CLI; its exit code, or None if it had to be killed (see EXIT_GRACE_S)."""
    log = (out_dir / "slicer.log").open("wb")
    try:
        proc = subprocess.Popen(cmd, cwd=out_dir, env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        deadline = time.time() + timeout
        done_at: float | None = None
        while True:
            rc = proc.poll()
            if rc is not None:
                return rc
            now = time.time()
            if done_at is None and _result(out_dir) is not None:
                done_at = now
            if (done_at is not None and now - done_at > EXIT_GRACE_S) or now > deadline:
                proc.kill()
                proc.wait(10)
                return None
            time.sleep(0.25)
    finally:
        log.close()


def slice_project(src: Path, out_dir: Path, *, timeout: float = SLICE_TIMEOUT_S,
                  root: Path | None = None, name: str | None = None) -> dict[str, Any]:
    """Slice every plate of `src` into `out_dir/<stem>.gcode.3mf`, `<stem>` from
    `name` (the caller's file name; the printer shows it as the job) or `src`.

    Returns {"ok", "file", "seconds", "grams", "grams_per_filament", "warnings",
    "error"}; slicer failures come back as `error`, never raised. ValueError
    only for a file that is not a project."""
    settings = project_settings(src)
    exe = slicer_binary(root)
    if not exe.is_file():
        return {"ok": False, "error": f"the headless Bambu Studio is not installed ({exe})"}
    out_dir.mkdir(parents=True, exist_ok=True)
    base = Path(name or src.name).name
    stem = base[:-len(".3mf")] if base.lower().endswith(".3mf") else Path(base).stem
    out_name = f"{stem}.gcode.3mf"
    cmd = [str(exe), "--slice", "0", "--allow-newer-file=1"]
    # The CLI sets these two to false unless they are on its command line
    # (BambuStudio.cpp, "In CLI mode, default enable_filament_dynamic_map to
    # false"), whatever the project says. The track switch changes how
    # filaments are grouped onto nozzles (ToolOrdering), so keep the project's.
    cmd += [f"--{key.replace('_', '-')}=1" for key in CLI_FORCED_OFF
            if str(settings.get(key, "")).lower() in ("1", "true")]
    machine = machine_profile(settings, root)
    if machine is not None:
        cmd += ["--load-settings", str(machine)]
    cmd += ["--outputdir", str(out_dir), "--export-3mf", out_name, str(src.resolve())]
    (out_dir / "result.json").unlink(missing_ok=True)
    t0 = time.time()
    try:
        rc = _run(cmd, out_dir, slicer_env(root), timeout)
    except OSError as e:
        return {"ok": False, "error": f"cannot run the slicer: {e}"}
    res = _result(out_dir)
    if res is None:
        why = "timed out" if rc is None else f"exited with code {rc}"
        return {"ok": False, "error": f"the slicer {why} without a result ({out_dir / 'slicer.log'})"}
    plates = res.get("sliced_plates") or []
    warnings = [str(p["warning_message"]) for p in plates if p.get("warning_message")]
    out = out_dir / out_name
    if res.get("return_code") != 0 or not plates or not out.is_file():
        return {"ok": False, "warnings": warnings,
                "error": f"{res.get('error_string') or 'slicing failed'} (code {res.get('return_code')})"}
    per = [float(f.get("total_used_g", 0)) for p in plates for f in p.get("filaments", [])]
    return {"ok": True, "file": str(out), "seconds": sum(float(p.get("total_predication", 0)) for p in plates),
            "grams": sum(per), "grams_per_filament": per, "warnings": warnings,
            "machine_profile": machine.name if machine else None, "wall_seconds": time.time() - t0}


@dataclass
class SliceJob:
    id: str
    source: str
    name: str = ""                 # the caller's file name (the upload is stored with a time prefix)
    state: str = "queued"          # queued → slicing → done | failed
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    finished: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "source": self.name or Path(self.source).name, "state": self.state,
                "message": self.message, "created": self.created, "finished": self.finished,
                "done": self.state in TERMINAL, **self.result}


class SliceJobs:
    """Slices in the background for the HTTP daemon, one at a time (the phone
    has the CPU for one); a second start while one runs is refused."""

    def __init__(self, out_root: Path | None = None) -> None:
        self._jobs: dict[str, SliceJob] = {}
        self._busy = False
        self._lock = threading.Lock()
        self._out_root = out_root

    def get(self, job_id: str) -> SliceJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, src: Path, name: str | None = None) -> SliceJob:
        """Check `src` is a project now (ValueError), then slice it in a thread
        (RuntimeError when a slice is already running). `name`: the caller's
        file name, which the print file is named after."""
        project_settings(src)
        with self._lock:
            if self._busy:
                raise RuntimeError("a slice is already running; wait for it to finish")
            self._busy = True
            job = SliceJob(id=uuid.uuid4().hex[:12], source=str(src), name=name or "")
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, src), name=f"slice-{job.id}", daemon=True).start()
        return job

    def _set(self, job: SliceJob, state: str, message: str, result: dict[str, Any] | None = None) -> None:
        with self._lock:
            job.state, job.message = state, message
            if result is not None:
                job.result = result
            if state in TERMINAL:
                job.finished = time.time()

    def _run(self, job: SliceJob, src: Path) -> None:
        out_root = self._out_root or (Path.home() / ".x2d" / "uploads" / "sliced")
        try:
            self._set(job, "slicing", f"slicing {src.name}")
            res = slice_project(src, out_root / job.id, name=job.name or None)
            if res.get("ok"):
                self._set(job, "done", "sliced", res)
            else:
                self._set(job, "failed", str(res.get("error") or "slicing failed"), res)
        except Exception as e:                      # noqa: BLE001 — reported to the caller
            self._set(job, "failed", f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._busy = False
