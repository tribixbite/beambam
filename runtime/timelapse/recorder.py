"""Per-printer timelapse capture + ffmpeg stitch (item #56).

`TimelapseRecorder.on_state(printer, state)` hooks into the daemon's
per-printer state callback. Lifecycle:

* Print transitions OFF→ON (gcode_state in {RUNNING, PREPARE} or
  mc_percent climbs above 0): create
  ``~/.x2d/timelapses/<printer>/<job_id>/`` and start a per-printer
  capture thread that polls a snapshot URL every `interval_s`
  seconds and writes ``00001.jpg``, ``00002.jpg``, ...
* Print transitions ON→OFF (gcode_state in {FINISH, IDLE} or
  mc_percent hits 100): stop the capture thread, write meta.json
  with subtask_name + frame count + start/end timestamps.

`stitch(printer, job_id)` runs ffmpeg to encode all frames into
``timelapse.mp4`` at 30 fps using H.264.

Only reports that carry ``print.gcode_state`` move the lifecycle: the
printer also sends partial deltas and command replies (a light toggle's
reply used to read as "not printing" and end the capture). The daemon
passes its merged per-printer state.

On disk and in the HTTP API a printer is a slug: the unnamed default
printer is ``_default`` (it used to be the empty string, which put its
jobs directly under the root where `list_jobs` never looked; such a
legacy layout is migrated on start), a named printer is its name with
anything outside ``[A-Za-z0-9._-]`` replaced by ``_``. Printer and job
ids from a URL must be plain slugs and resolve inside the root.

The HTTP / web UI surface (browser, thumbnails, stitch button,
inline player) is wired from `beambam.serve_http._serve_http`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Iterable
from beambam.paths import config_path

LOG = logging.getLogger("x2d.timelapse")

_DEFAULT_DIR = config_path("timelapses")

# Directory / URL slug of the unnamed default printer ("" in the daemon).
DEFAULT_PRINTER_SLUG = "_default"

# A plain path component: no separators, no leading dot ("..", ".x").
_SLUG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,119}")


@dataclass
class JobMeta:
    job_id:        str
    printer:       str
    subtask_name:  str
    started:       float
    ended:         float = 0.0
    frame_count:   int = 0
    mp4_ready:     bool = False
    mp4_size:      int = 0


def _is_print_active(state: dict | None) -> bool:
    """True when the printer is mid-job. Mirrors the queue manager's
    idle detection inverse: gcode_state in {RUNNING, PREPARE,
    SLICING, PAUSE} OR 0 < mc_percent < 100."""
    if not state:
        return False
    p = state.get("print", {})
    gs = (p.get("gcode_state") or "").upper()
    if gs in ("RUNNING", "PREPARE", "SLICING", "PAUSE"):
        return True
    pct = p.get("mc_percent")
    if isinstance(pct, (int, float)) and 0 < pct < 100:
        return True
    return False


def _safe_id(name: str) -> str:
    """File-system-safe slug for a job_id (subtask_name often has
    spaces, slashes, or non-ASCII). Never starts with a dot."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120]
    if slug.startswith("."):
        slug = "_" + slug[1:]
    return slug or "job"


def printer_slug(printer: str) -> str:
    """Directory / URL name of a printer: ``_default`` for the unnamed
    default printer ("" or already ``_default``), else a safe slug."""
    if printer in ("", DEFAULT_PRINTER_SLUG):
        return DEFAULT_PRINTER_SLUG
    return _safe_id(printer)


def _has_gcode_state(state: dict | None) -> bool:
    p = state.get("print") if isinstance(state, dict) else None
    return isinstance(p, dict) and bool(p.get("gcode_state"))


class _Capture:
    """One running capture thread for one (printer, job_id)."""

    def __init__(self, root: Path, printer: str, job_id: str,
                 subtask_name: str, snapshot_url: str,
                 interval_s: float, auth_token: str | None = None):
        self.root = root
        self.printer = printer
        self.job_id = job_id
        self.dir = root / printer_slug(printer) / job_id
        self.snapshot_url = snapshot_url
        self.interval_s = interval_s
        self.auth_token = auth_token
        self._stop = threading.Event()
        self.frame_count = 0
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta = JobMeta(
            job_id=job_id, printer=printer,
            subtask_name=subtask_name,
            started=time.time())
        self._save_meta()

    def _save_meta(self) -> None:
        try:
            (self.dir / "meta.json").write_text(
                json.dumps(asdict(self.meta), indent=2))
        except OSError:
            pass

    def _capture_loop(self) -> None:
        LOG.info("recording %s/%s → %s every %.0fs",
                 self.printer, self.job_id, self.dir, self.interval_s)
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(self.snapshot_url)
                if self.auth_token:
                    # The daemon's own /snapshot.jpg is behind the same
                    # Bearer gate as every route when --auth-token is set.
                    req.add_header("Authorization",
                                   f"Bearer {self.auth_token}")
                with urllib.request.urlopen(req, timeout=8) as r:
                    if r.status == 200:
                        data = r.read()
                        if data and data[:3] == b"\xff\xd8\xff":
                            self.frame_count += 1
                            self.meta.frame_count = self.frame_count
                            (self.dir / f"{self.frame_count:05d}.jpg")\
                                .write_bytes(data)
                            self._save_meta()
            except (urllib.error.URLError, urllib.error.HTTPError,
                    ConnectionError, TimeoutError, OSError) as e:
                LOG.debug("snapshot fetch failed: %s", e)
            if self._stop.wait(self.interval_s):
                break

    def start(self) -> None:
        self.t = threading.Thread(target=self._capture_loop,
                                   name=f"timelapse-{self.printer}-{self.job_id}",
                                   daemon=True)
        self.t.start()

    def stop(self) -> None:
        self._stop.set()
        self.meta.ended = time.time()
        self._save_meta()


class TimelapseRecorder:
    def __init__(self, *,
                 snapshot_url: str,
                 root: Path | None = None,
                 interval_s: float = 30.0,
                 auth_token: str | None = None) -> None:
        self.snapshot_url = snapshot_url
        self.root = root or _DEFAULT_DIR
        self.interval_s = interval_s
        self.auth_token = auth_token or None
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # printer → active _Capture (or None)
        self._active: dict[str, _Capture] = {}
        self._migrate_legacy_default()

    def _migrate_legacy_default(self) -> None:
        """Move jobs recorded for the default printer under the old
        layout (``<root>/<job>/meta.json``, because the printer name was
        "") into ``<root>/_default/<job>/``."""
        dest_root = self.root / DEFAULT_PRINTER_SLUG
        try:
            entries = sorted(self.root.iterdir())
        except OSError:
            return
        for d in entries:
            if not d.is_dir() or d.name == DEFAULT_PRINTER_SLUG \
                    or not (d / "meta.json").is_file():
                continue
            dest_root.mkdir(exist_ok=True)
            target = dest_root / d.name
            n = 1
            while target.exists():
                n += 1
                target = dest_root / f"{d.name}_{n}"
            try:
                d.rename(target)
                LOG.info("migrated legacy timelapse %s → %s", d, target)
            except OSError as e:
                LOG.warning("could not migrate legacy timelapse %s: %s", d, e)

    def _job_dir(self, printer: str, job_id: str) -> Path | None:
        """``<root>/<printer slug>/<job_id>`` when both ids are plain
        slugs and the path resolves inside the root; else None. Ids come
        from URLs (already percent-decoded, so ``%2F`` arrives as "/")."""
        slug = printer_slug(printer) if printer == "" else printer
        if not (_SLUG_RE.fullmatch(slug) and _SLUG_RE.fullmatch(job_id)):
            return None
        d = self.root / slug / job_id
        try:
            d.resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            return None
        return d

    def on_state(self, printer: str, state: dict | None) -> None:
        """Start/stop the printer's capture from a (merged) report.
        Messages without ``print.gcode_state`` are ignored."""
        if not _has_gcode_state(state):
            return
        active = _is_print_active(state)
        with self._lock:
            cap = self._active.get(printer)
            if active and cap is None:
                # Start a new capture.
                subtask = state.get("print", {}).get(
                    "subtask_name", "") if state else ""
                job_id = (_safe_id(subtask) if subtask
                          else "job_" + uuid.uuid4().hex[:8])
                # Disambiguate if a job with that subtask name
                # ran today already.
                base = job_id
                n = 1
                while (self.root / printer_slug(printer) / job_id).exists():
                    n += 1
                    job_id = f"{base}_{n}"
                cap = _Capture(self.root, printer, job_id, subtask,
                                self.snapshot_url, self.interval_s,
                                auth_token=self.auth_token)
                cap.start()
                self._active[printer] = cap
            elif (not active) and cap is not None:
                cap.stop()
                self._active.pop(printer, None)

    # --- listing for the HTTP API -------------------------------------
    def list_jobs(self) -> list[dict]:
        """Every recorded job. ``printer`` is the printer's slug (what the
        URLs take; ``_default`` for the unnamed printer)."""
        out: list[dict] = []
        if not self.root.exists():
            return out
        for printer_dir in sorted(self.root.iterdir()):
            if not printer_dir.is_dir():
                continue
            for job_dir in sorted(printer_dir.iterdir()):
                if not job_dir.is_dir():
                    continue
                meta_path = job_dir / "meta.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                    except (json.JSONDecodeError, OSError):
                        continue
                    if not isinstance(meta, dict):
                        continue
                    meta["printer"] = printer_dir.name
                    meta["job_id"] = job_dir.name
                    meta["mp4_ready"] = (job_dir / "timelapse.mp4").exists()
                    if meta["mp4_ready"]:
                        meta["mp4_size"] = (job_dir
                            / "timelapse.mp4").stat().st_size
                    out.append(meta)
        return out

    def list_frames(self, printer: str, job_id: str) -> list[str]:
        d = self._job_dir(printer, job_id)
        if d is None or not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir()
                      if p.suffix.lower() == ".jpg" and p.stem.isdigit())

    def frame_path(self, printer: str, job_id: str, frame: str) -> Path | None:
        if not re.fullmatch(r"\d{1,8}\.jpg", frame):
            return None
        d = self._job_dir(printer, job_id)
        if d is None:
            return None
        p = d / frame
        return p if p.is_file() else None

    def mp4_path(self, printer: str, job_id: str) -> Path | None:
        d = self._job_dir(printer, job_id)
        if d is None:
            return None
        p = d / "timelapse.mp4"
        return p if p.is_file() else None

    def stitch(self, printer: str, job_id: str, fps: int = 30) -> dict:
        """Run ffmpeg over the captured frames → timelapse.mp4 in the
        same dir. Idempotent: re-stitching overwrites the previous
        mp4. Returns {"ok": bool, "size": int, "frames": int,
        "error": str}."""
        if shutil.which("ffmpeg") is None:
            return {"ok": False, "error": "ffmpeg not in PATH",
                    "frames": 0, "size": 0}
        d = self._job_dir(printer, job_id)
        if d is None or not d.is_dir():
            return {"ok": False, "error": "job not found",
                    "frames": 0, "size": 0}
        frames = self.list_frames(printer, job_id)
        if not frames:
            return {"ok": False, "error": "no frames captured",
                    "frames": 0, "size": 0}
        out = d / "timelapse.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", str(d / "%05d.jpg"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",   # H.264 wants even dims
            "-movflags", "+faststart",
            str(out),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=300)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "ffmpeg timed out after 5 min",
                    "frames": len(frames), "size": 0}
        if proc.returncode != 0 or not out.exists():
            return {"ok": False,
                    "error": (proc.stderr.splitlines()[-1] if proc.stderr
                              else f"ffmpeg exit {proc.returncode}"),
                    "frames": len(frames), "size": 0}
        return {"ok": True, "frames": len(frames),
                "size": out.stat().st_size, "error": ""}

    def stop_all(self) -> None:
        with self._lock:
            for cap in self._active.values():
                try: cap.stop()
                except Exception: pass
            self._active.clear()
