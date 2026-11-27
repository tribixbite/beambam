"""beambam.slice_service — slice a Bambu Studio project on this machine for the daemon.

A web app that builds a ready-to-slice project `.3mf` but cannot slice it
itself (pokefit.party runs its engine in the browser) sends the file to the
daemon's `POST /slice`; this module slices it with Bambu Studio's command line
(`resolve_slicer`: the phone's source build `bs-bionic`, the official build
`beambam slicer install` downloads, or an installed Bambu Studio) into a
`.gcode.3mf` the daemon can then plan and print (`POST /print/plan`, `POST /print`):

    job = SliceJobs().start(project_3mf)        # background; poll job.to_dict()
    ...job.state == "done" → job.file is the .gcode.3mf

The project's own process and filament settings are kept. The printer
(machine) profile is reloaded from the slicer's own copy of the same name,
because a project saved by a newer Bambu Studio can carry start G-code that
uses variables an older build does not know (seen: pokefit's X2D export, "Not a
variable name" at `initial_no_support_filament_id`); `--load-settings` with a
machine profile overlays only the machine keys (tools/x2d_cli_slice.sh).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beambam.slice_options import (MESH_PRINTER, MESH_SUFFIXES, SliceOptions, apply_to_project,
                                   has_print_settings, made_for_another_printer, mesh_args, retarget)
from beambam.slicer_paths import bambu_studio_bin, bbl_profiles_dir, is_bionic_build, run_bambu_cli

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
    """The phone's source build of Bambu Studio (bs-bionic) under the checkout."""
    return (root or _root()) / "bs-bionic" / "build" / "src" / "bambu-studio"


def phone_profiles(root: Path | None = None) -> Path:
    """bs-bionic's Bambu profiles folder (`resources/profiles/BBL`)."""
    return (root or _root()) / "bs-bionic" / "resources" / "profiles" / "BBL"


NO_SLICER = ("no Bambu Studio to slice with: run `beambam slicer install` (downloads Bambu Lab's official "
             "build), or install Bambu Studio, or set BAMBU_STUDIO to its executable, then slice again")


def no_slicer_message() -> str:
    """Why `resolve_slicer` found nothing: NO_SLICER, or the explicit setting that points nowhere (it is not
    second-guessed, so a downloaded build is not used while it is set)."""
    for var in ("BAMBU_STUDIO", "X2D_BS_BIN"):
        raw = os.environ.get(var)
        if raw:
            return (f"{var} is set to {raw}, which is not a Bambu Studio executable (or its profiles are "
                    f"missing): fix it, or unset it to use the one `beambam slicer install` downloads")
    return NO_SLICER


@dataclass(frozen=True)
class Slicer:
    """The Bambu Studio the daemon slices with (`resolve_slicer`)."""
    exe: Path
    profiles: Path                 # its resources/profiles/BBL folder
    phone: bool                    # the Termux source build: its own libraries and software GL (`slicer_env`)

    def env(self, root: Path | None = None) -> dict[str, str]:
        """The environment to run it in."""
        if self.phone:
            return slicer_env(root)
        env = dict(os.environ)
        if os.name != "nt":
            env.update(LC_ALL="C", LANG="C")   # decimal points, not commas, in the numbers it writes
        return env

    def problem(self) -> str:
        """Why it cannot slice ('' when it can): its profiles lack the flattened `*_full` folders the CLI loads
        presets from (`profile_flatten`)."""
        from beambam.profile_flatten import missing
        gone = missing(self.profiles)
        if not gone:
            return ""
        return (f"this Bambu Studio's profiles lack {', '.join(gone)} ({self.profiles}), which its command line "
                "loads presets from: run `beambam slicer install`, or `python -m beambam.profile_flatten "
                f"\"{self.profiles}\"` where you may write")


def resolve_slicer(root: Path | None = None) -> Slicer | None:
    """The slicer to use now, or None (`NO_SLICER`). Looked up per slice, so a `beambam slicer install` made
    while the daemon runs is used without a restart. Order: $BAMBU_STUDIO, the phone build under `root`, the
    downloaded official build, then an installed Bambu Studio (slicer_paths.bambu_studio_bin)."""
    explicit = any(os.environ.get(v) for v in ("BAMBU_STUDIO", "X2D_BS_BIN"))
    phone = slicer_binary(root)
    if not explicit and phone.is_file():
        return Slicer(phone, phone_profiles(root), True)
    exe = bambu_studio_bin()
    if exe is None:
        return None
    profiles = bbl_profiles_dir(exe)
    if profiles is None:
        return None
    return Slicer(exe, profiles, is_bionic_build(exe))


def slicer_status(root: Path | None = None) -> dict[str, Any]:
    """What /healthz says about slicing: {"ready", "kind" ("phone" | "downloaded" | "installed" | None),
    "version", "problem"} (`problem`: why POST /slice would fail, '' when ready)."""
    from beambam.slicer_paths import downloaded_slicer
    s = resolve_slicer(root)
    if s is None:
        return {"ready": False, "kind": None, "version": "", "problem": no_slicer_message()}
    got = downloaded_slicer()
    downloaded = got is not None and Path(got["exe"]) == s.exe
    problem = s.problem()
    return {"ready": not problem, "kind": "phone" if s.phone else "downloaded" if downloaded else "installed",
            "version": str(got.get("version", "")) if got is not None and downloaded else "", "problem": problem}


def profiles_dir(root: Path | None = None) -> Path | None:
    """The Bambu profiles to update a project with (POST /project/update needs no slicer, only these): the
    slicer's, else the phone build's when only its profiles are present; None when there are none."""
    s = resolve_slicer(root)
    if s is not None:
        return s.profiles
    phone = phone_profiles(root)
    return phone if phone.is_dir() else None


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


def machine_profile(settings: dict[str, Any], profiles: Path) -> Path | None:
    """The slicer's own machine profile named like the project's printer, if any (`profiles`: its BBL folder)."""
    name = settings.get("printer_settings_id")
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        return None
    p = profiles / "machine" / f"{name}.json"
    return p if p.is_file() else None


def _result(out_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "return_code" in data else None


def _run(cmd: list[str], out_dir: Path, env: dict[str, str], timeout: float) -> int | None:
    """Run the CLI with its output in `out_dir/slicer.log` (slicer_paths.run_bambu_cli); its exit code, or None
    when it ran out of time."""
    try:
        return run_bambu_cli(cmd, out_dir, env=env, timeout=timeout, exit_grace=EXIT_GRACE_S,
                             log=out_dir / "slicer.log")
    except TimeoutError:
        return None


def load_problem(log: Path) -> str:
    """Why the slicer could not start, from its log: a library the system lacks (an AppImage on a bare Linux),
    which the CLI says before it writes any result."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = re.findall(r"error while loading shared libraries: ([^\n]+)", text)
    return f"a library it needs is missing ({found[-1].strip()})" if found else ""


def validate_reason(log: Path) -> str:
    """The setting Bambu Studio refused, from its log: code -51 ("some incorrect slicing parameters") says only
    that one failed, the log line `got error when validate: Too small line width` says which."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = re.findall(r"got error when validate: (.+)", text)
    return found[-1].strip() if found else ""


def is_mesh(src: Path) -> bool:
    """A file sliced with the bundled printer profiles instead of its own: STL / OBJ / STEP, or a 3MF without
    print settings (geometry only)."""
    suffix = src.suffix.lower()
    if suffix in MESH_SUFFIXES:
        return True
    # A readable 3MF with no print settings is geometry (another slicer's export); a broken one is refused
    # as a project (`project_settings` says why).
    return suffix == ".3mf" and zipfile.is_zipfile(src) and not has_print_settings(src)


def forced_flags(settings: dict[str, Any], track_switch: bool | None) -> list[str]:
    """The command-line flags that keep a project's `CLI_FORCED_OFF` settings. The CLI sets both to false unless
    they are on its command line (BambuStudio.cpp, "In CLI mode, default enable_filament_dynamic_map to false"),
    whatever the project says; the track switch changes how filaments are grouped onto nozzles (ToolOrdering).
    A project keeps its own values, except that the printer's track switch (`track_switch`) turns the switch on:
    the slice is for the printer it will print on, as Bambu Studio does when it syncs the printer (a project
    saved without the printer, e.g. pokefit's, otherwise draws "sliced without the Filament Track Switch")."""
    on = {key for key in CLI_FORCED_OFF if str(settings.get(key, "")).lower() in ("1", "true")}
    if track_switch:
        on.add("has_filament_switcher")
    return [f"--{key.replace('_', '-')}=1" for key in CLI_FORCED_OFF if key in on]


def slice_project(src: Path, out_dir: Path, *, timeout: float = SLICE_TIMEOUT_S,
                  root: Path | None = None, name: str | None = None,
                  options: SliceOptions | None = None, track_switch: bool | None = None) -> dict[str, Any]:
    """Slice every plate of `src` into `out_dir/<stem>.gcode.3mf`, `<stem>` from
    `name` (the caller's file name; the printer shows it as the job) or `src`.

    A Bambu Studio project keeps its own settings, with `options` on top
    (slice_options); a bare mesh (`is_mesh`) slices for the bundled X2D 0.4 mm
    profiles with `options`. `track_switch`: the printer has a Filament Track
    Switch (a mesh, and a project for this printer, are sliced for it: `forced_flags`).

    Returns {"ok", "file", "seconds", "grams", "grams_per_filament", "warnings",
    "error", "mesh"}; slicer failures come back as `error`, never raised.
    ValueError only for a file that is neither a project nor a mesh."""
    opts = options or SliceOptions()
    slicer = resolve_slicer(root)
    if slicer is None:
        return {"ok": False, "error": no_slicer_message()}
    if slicer.phone and slicer.problem():
        # The checkout's own build: its profiles are ours to complete (tools/x2d_cli_slice.sh did this too).
        from beambam.profile_flatten import flatten
        flatten(slicer.profiles)
    if slicer.problem():
        return {"ok": False, "error": slicer.problem()}
    prof = slicer.profiles
    out_dir.mkdir(parents=True, exist_ok=True)
    base = Path(name or src.name).name
    stem = re.sub(r"\.(3mf|stl|obj|step|stp)$", "", base, flags=re.I)
    out_name = f"{stem}.gcode.3mf"
    cmd = [str(slicer.exe), "--slice", "0", "--allow-newer-file=1"]
    mesh = is_mesh(src)
    machine: Path | None = None
    if mesh:
        machine = prof / "machine" / f"{MESH_PRINTER}.json"
        cmd += mesh_args(prof, opts, out_dir)
        if track_switch:
            cmd.append("--has-filament-switcher=1")
        # A binary STL with zero facet normals (valid STL) is refused by Bambu Studio: slice a copy with them set.
        from beambam.stl_normals import with_normals
        model = with_normals(src, out_dir).resolve()
    retargeted = None
    adopt_notes: list[str] = []          # what the spools could not change in a project (project_sync)
    if not mesh:
        settings = project_settings(src)
        retargeted = made_for_another_printer(settings)
    if retargeted:
        # Made for another printer (a P2S project from MakerWorld): sliced for this one with its bundled
        # profiles, keeping the model, paint and filament assignment and the project's portable settings;
        # its own printer profile would make a print file for the wrong printer.
        n = len(settings.get("filament_settings_id") or []) or 1
        model = src
        ropts = retarget(settings, opts)
        if ropts.spools and len(ropts.spools) > n:
            # A colour was painted onto the model (more spools than the project's filaments): grow the
            # project's embedded filament list to match, or Bambu Studio refuses it ("filament_colour's size
            # N not equal to …"). project_sync appends the new filaments; retarget loads every filament's
            # profile for MESH_PRINTER regardless.
            from beambam.project_sync import adopt_project
            grown = out_dir / f"grown_{src.name}"
            settings = adopt_project(src, grown, prof, [tuple(s) for s in ropts.spools])
            model = grown
            n = len(settings.get("filament_settings_id") or []) or n
        cmd += mesh_args(prof, ropts, out_dir, n_filaments=n, keep=settings, arrange=False)
        if track_switch:
            cmd.append("--has-filament-switcher=1")
        model = model.resolve()
        machine = prof / "machine" / f"{MESH_PRINTER}.json"
    elif not mesh:
        model = src.resolve()
        if opts != SliceOptions():
            model = out_dir / f"options_{src.name}"
            settings = apply_to_project(src, model, opts, prof, adopt_notes)
        cmd += forced_flags(settings, track_switch)
        machine = machine_profile(settings, prof)
        if machine is not None:
            cmd += ["--load-settings", str(machine)]
    cmd += ["--outputdir", str(out_dir), "--export-3mf", out_name, str(model)]
    (out_dir / "result.json").unlink(missing_ok=True)
    t0 = time.time()
    try:
        rc = _run(cmd, out_dir, slicer.env(root), timeout)
    except OSError as e:
        return {"ok": False, "error": f"cannot run the slicer ({slicer.exe}): {e}"}
    res = _result(out_dir)
    if res is None:
        why = "timed out" if rc is None else f"exited with code {rc}"
        cause = load_problem(out_dir / "slicer.log")
        return {"ok": False, "error": f"the slicer {why} without a result"
                                      + (f": {cause}" if cause else "") + f" ({out_dir / 'slicer.log'})"}
    plates = res.get("sliced_plates") or []
    warnings = [str(p["warning_message"]) for p in plates if p.get("warning_message")]
    out = out_dir / out_name
    if res.get("return_code") != 0 or not plates or not out.is_file():
        why = validate_reason(out_dir / "slicer.log")
        return {"ok": False, "warnings": warnings,
                "error": f"{res.get('error_string') or 'slicing failed'} (code {res.get('return_code')})"
                         + (f": {why}" if why else "")}
    warnings += adopt_notes + sliced_warnings(out)
    per = [float(f.get("total_used_g", 0)) for p in plates for f in p.get("filaments", [])]
    # Grams by 1-based filament number, summed over plates (`grams_per_filament` lists only the used ones,
    # in order, without their numbers).
    by_number: dict[str, float] = {}
    for pl in plates:
        for f in pl.get("filaments", []):
            if isinstance(f.get("id"), int):
                by_number[str(f["id"])] = by_number.get(str(f["id"]), 0.0) + float(f.get("total_used_g", 0))
    if retargeted:
        warnings.insert(0, f"Made for {retargeted}: sliced for the X2D with its own profiles (the project's "
                           "walls, infill, shells and supports kept)")
    return {"ok": True, "mesh": mesh, "retargeted_from": retargeted, "file": str(out), "seconds": sum(float(p.get("total_predication", 0)) for p in plates),
            "grams": sum(per), "grams_per_filament": per, "grams_by_filament": by_number,
            "plates": len(plates),
            # Per sliced plate, in order: a print starts one plate (/print `plate`, default 1).
            "plate_seconds": [float(p.get("total_predication", 0)) for p in plates],
            "plate_grams": [sum(float(f.get("total_used_g", 0)) for f in p.get("filaments", [])) for p in plates],
            # …and by filament number on each plate, so a client showing one plate shows that plate's grams.
            "plate_grams_by_filament": [{str(f["id"]): float(f.get("total_used_g", 0)) for f in p.get("filaments", [])
                                         if isinstance(f.get("id"), int)} for p in plates],
            "warnings": warnings,
            "machine_profile": machine.name if machine else None, "wall_seconds": time.time() - t0}


# Bambu Studio's slice_info warning codes in plain words (GUI: PartPlate / GCodeViewer notifications).
SLICE_WARNINGS = {
    "bed_temperature_too_high_than_filament":
        "Bambu Studio: the plate's bed temperature is above a filament's softening temperature, so that "
        "filament may soften and clog. Pick another plate, or check the filament",
}


def sliced_warnings(gcode_3mf: Path) -> list[str]:
    """The warnings the slicer wrote into the print file's slice_info (`<warning msg=… level=…/>`), which
    result.json leaves out, in plain words where known."""
    try:
        with zipfile.ZipFile(gcode_3mf) as z:
            info = z.read("Metadata/slice_info.config").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return []
    out: list[str] = []
    for m in re.finditer(r'<warning\s+msg="([^"]*)"', info):
        text = SLICE_WARNINGS.get(m.group(1), f"Bambu Studio: {m.group(1).replace('_', ' ')}")
        if text not in out:
            out.append(text)
    return out


KEEP_JOBS = 50                         # slice jobs remembered for GET /slice/jobs/<id>


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

    def start(self, src: Path, name: str | None = None, options: SliceOptions | None = None,
              track_switch: bool | None = None) -> SliceJob:
        """Check `src` is a project or a mesh now (ValueError), then slice it in a
        thread (RuntimeError when a slice is already running). `name`: the
        caller's file name, which the print file is named after; `options` /
        `track_switch`: see `slice_project`."""
        if not is_mesh(src):
            project_settings(src)
        with self._lock:
            if self._busy:
                raise RuntimeError("a slice is already running; wait for it to finish")
            self._busy = True
            job = SliceJob(id=uuid.uuid4().hex[:12], source=str(src), name=name or "")
            self._jobs[job.id] = job
            # Keep the last KEEP_JOBS jobs: a client polls its job for minutes, not days.
            for old in list(self._jobs)[:-KEEP_JOBS]:
                del self._jobs[old]
        threading.Thread(target=self._run, args=(job, src, options, track_switch), name=f"slice-{job.id}",
                         daemon=True).start()
        return job

    def _set(self, job: SliceJob, state: str, message: str, result: dict[str, Any] | None = None) -> None:
        with self._lock:
            job.state, job.message = state, message
            if result is not None:
                job.result = result
            if state in TERMINAL:
                job.finished = time.time()

    def _run(self, job: SliceJob, src: Path, options: SliceOptions | None = None,
             track_switch: bool | None = None) -> None:
        from beambam.paths import config_path
        out_root = self._out_root or (config_path("uploads") / "sliced")
        try:
            self._set(job, "slicing", f"slicing {src.name}")
            res = slice_project(src, out_root / job.id, name=job.name or None, options=options,
                                track_switch=track_switch)
            if res.get("ok"):
                self._set(job, "done", "sliced", res)
            else:
                self._set(job, "failed", str(res.get("error") or "slicing failed"), res)
        except Exception as e:                      # noqa: BLE001 — reported to the caller
            self._set(job, "failed", f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._busy = False
