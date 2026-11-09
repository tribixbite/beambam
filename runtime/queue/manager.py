"""File-backed multi-printer print queue (item #55).

`QueueManager` owns the ordered list of pending + running jobs, persists
to ``~/.x2d/queue.json`` atomically, and dispatches the next pending
job to a printer when that printer's state says it's idle.

Job lifecycle
-------------

  pending  → waiting for the target printer to be idle
  running  → dispatched (upload + project_file landed, or dispatch in
             flight on the worker thread)
  done     → printer reported the job active, then idle again
  failed   → dispatch raised / returned False, the printer reported
             FAILED after running it, or it never started the job
  cancel   → user removed before dispatch

Judging printer state
---------------------

The printer publishes full reports, *partial* delta reports and command
replies on the same topic (see :mod:`beambam.state`). Only a report that
carries ``print.gcode_state`` says anything about whether the printer is
busy, so :meth:`QueueManager.on_state` ignores every message without it:
a delta carrying only a temperature used to read as "idle", which marked
the running job done and dispatched the next one mid-print. Callers
should pass the *merged* state (``X2DClient.merged_state()`` or the
daemon's per-printer merge) so every call has a ``gcode_state``.

A dispatched job is only marked done after the printer has been seen
active (RUNNING / PREPARE / ...) for it and then idle again. The idle
report that is still in flight right after a dispatch (the printer takes
a few seconds to leave FINISH) therefore cannot complete the job early.

Concurrency
-----------

The daemon and the ``beambam queue`` CLI edit the same file. Every
mutation runs inside :meth:`QueueManager._txn`: an in-process RLock plus
a cross-process lock file (``queue.json.lock``), a reload from disk, the
change, and an atomic rewrite. Neither side can overwrite the other's
edits with a stale in-memory copy, and a CLI process no longer rewrites
the daemon's running jobs on load.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator
from beambam.paths import config_path

LOG = logging.getLogger("x2d.queue")

_DEFAULT_PATH = config_path("queue.json")

# Only sliced Bambu bundles are queueable; anything else (credentials,
# arbitrary local files) must never be uploaded to the printer by a
# dispatch. Checked by `validate_job_file` at every entry point (HTTP
# /queue/add, `beambam queue add`).
JOB_FILE_SUFFIX = ".gcode.3mf"

# A dispatched job that the printer never reports as active within this
# window is failed instead of blocking the printer's queue forever.
_START_GRACE_S = 15 * 60.0

# Cross-process lock file tuning: the lock is held for milliseconds
# (reload + modify + rewrite), so a lock older than `_LOCK_STALE_S` was
# left behind by a crashed process and is broken.
_LOCK_TIMEOUT_S = 10.0
_LOCK_STALE_S = 30.0
_LOCK_POLL_S = 0.02


@dataclasses.dataclass
class Job:
    id:        str
    printer:   str           # name of [printer:NAME] (or "" for default)
    gcode:     str           # local path to .gcode.3mf
    # Global AMS slot, 0-based: unit*4 + tray (0..15) — the same
    # convention as `beambam print --slot`, start_print and
    # _validate_ams_slot. (Queue files written before `slot_base`
    # existed stored 1..16; see from_dict.)
    slot:      int = 0
    status:    str = "pending"  # pending|running|done|failed|cancelled
    enqueued:  float = 0.0
    started:   float = 0.0
    finished:  float = 0.0
    error:     str = ""
    label:     str = ""      # human-readable display name
    # True once the printer reported an active gcode_state after this
    # job was dispatched; only then can an idle report mean "finished".
    seen_active: bool = False
    # Index base of `slot` as stored. Always SLOT_BASE (0) once written
    # by this version; a record without the field is a 1-based legacy
    # entry and is converted on load.
    slot_base: int = 0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        fields = {k: d[k] for k in d if k in cls.__dataclass_fields__}
        if "slot_base" not in d:
            # Legacy record: the queue used to take slots 1..16 and pass
            # them unchanged to the 0-based print path, so slot 1 printed
            # from the SECOND tray. Re-base to what the user meant.
            fields["slot"] = max(0, int(fields.get("slot", 1)) - 1)
        fields["slot_base"] = 0
        return cls(**fields)


class JobFileError(ValueError):
    """The path given for a queue job is not an existing .gcode.3mf."""


def validate_job_file(path: str | os.PathLike[str]) -> Path:
    """Return the resolved path of a queueable file or raise JobFileError.

    A job's file is uploaded to the printer verbatim at dispatch, so the
    queue only accepts existing regular files named ``*.gcode.3mf``.
    """
    p = Path(path).expanduser()
    try:
        resolved = p.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise JobFileError(f"file not found: {p}") from e
    # The suffix is checked on the resolved name too, so a symlink named
    # x.gcode.3mf that points at ~/.x2d/credentials is refused.
    for name in (p.name, resolved.name):
        if not name.lower().endswith(JOB_FILE_SUFFIX):
            raise JobFileError(f"not a {JOB_FILE_SUFFIX} file: {name!r}")
    if not resolved.is_file():
        raise JobFileError(f"not a regular file: {p}")
    return resolved


# Status values the manager treats as the printer being free for work.
_IDLE_GCODE_STATES = {"FINISH", "IDLE", "READY", "FAILED", "ABORTED"}
# Of those, the ones that mean the job did not complete successfully.
_FAILED_GCODE_STATES = {"FAILED", "ABORTED"}


def _gcode_state(state: dict | None) -> str | None:
    """Upper-cased ``print.gcode_state``, or None when the message does
    not carry one (a delta report or a command reply)."""
    if not isinstance(state, dict):
        return None
    p = state.get("print")
    if not isinstance(p, dict):
        return None
    gs = p.get("gcode_state")
    if not isinstance(gs, str) or not gs.strip():
        return None
    return gs.strip().upper()


def _is_printer_idle(state: dict | None) -> bool:
    """True only for a report whose gcode_state says the printer is free.

    A message without gcode_state is never idle: it cannot tell us
    anything about the print state machine.
    """
    gs = _gcode_state(state)
    if gs is None or gs not in _IDLE_GCODE_STATES:
        return False
    p = state.get("print", {}) if state else {}
    sub = p.get("mc_print_sub_stage")
    if sub and str(sub).strip() not in ("", "0"):
        return False
    pct = p.get("mc_percent")
    # Belt and braces: a FINISH report with a mid-range percentage is a
    # transitional frame; don't dispatch over it.
    if gs not in _FAILED_GCODE_STATES and isinstance(pct, (int, float)) \
            and 0 < pct < 100:
        return False
    return True


def _is_printer_active(state: dict | None) -> bool:
    """True for a report whose gcode_state says a job is in progress."""
    gs = _gcode_state(state)
    return gs is not None and gs not in _IDLE_GCODE_STATES


class _QueueFileLock:
    """Cross-process exclusive lock: an ``O_CREAT | O_EXCL`` lock file.

    Works the same on Windows and POSIX (no fcntl/msvcrt split). A lock
    file older than `stale_s` is assumed abandoned by a crashed holder
    and removed.
    """

    def __init__(self, path: Path, *, timeout_s: float = _LOCK_TIMEOUT_S,
                 stale_s: float = _LOCK_STALE_S) -> None:
        self.path = path
        self.timeout_s = timeout_s
        self.stale_s = stale_s

    def __enter__(self) -> "_QueueFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError):
                # Windows answers EACCES (PermissionError) instead of
                # EEXIST while the previous holder's unlink is still
                # pending; both mean "someone holds it, retry".
                self._break_if_stale()
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"queue lock {self.path} held for more than "
                        f"{self.timeout_s:.0f}s") from None
                time.sleep(_LOCK_POLL_S)
                continue
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
            finally:
                os.close(fd)
            return self

    def _break_if_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return                      # vanished between open and stat
        if age > self.stale_s:
            LOG.warning("breaking stale queue lock %s (%.0fs old)",
                        self.path, age)
            with contextlib.suppress(OSError):
                self.path.unlink()

    def __exit__(self, *_exc: object) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()


class QueueManager:
    """Thread- and process-safe FIFO + auto-dispatch.

    `dispatch_cb(job)` is invoked when a printer is idle and a pending
    job exists for it. The callback performs the upload + publish and
    returns True on success (job stays running), False / raises on
    failure (job → failed with the error string).

    With ``background_dispatch=True`` the callback runs on a worker
    thread, so :meth:`on_state` returns immediately. The daemon needs
    that: on_state is called from paho's network thread, and a dispatch
    that waits for the printer (``request_state``) on that same thread
    can never receive the answer.
    """

    def __init__(self,
                 dispatch_cb: Callable[[Job], bool],
                 path: Path | None = None,
                 *,
                 background_dispatch: bool = False) -> None:
        self._lock = threading.RLock()
        self._jobs: list[Job] = []
        self._path = path or _DEFAULT_PATH
        self._lockfile = _QueueFileLock(
            self._path.with_name(self._path.name + ".lock"))
        self._txn_depth = 0
        self._dispatch_cb = dispatch_cb
        self._background = background_dispatch
        # Job ids whose dispatch callback is executing right now.
        self._inflight: set[str] = set()
        with self._txn():
            LOG.info("loaded %d jobs from %s", len(self._jobs), self._path)

    # ----- persistence -------------------------------------------------
    @contextlib.contextmanager
    def _txn(self) -> Iterator[None]:
        """Hold the in-process + cross-process lock and work on a fresh
        copy of the file. Re-entrant: nested calls share the outer lock
        and reload."""
        with self._lock:
            if self._txn_depth:
                self._txn_depth += 1
                try:
                    yield
                finally:
                    self._txn_depth -= 1
                return
            with self._lockfile:
                self._txn_depth = 1
                try:
                    self._load()
                    yield
                finally:
                    self._txn_depth = 0

    def _load(self) -> None:
        """Replace the in-memory list with the file's contents. Caller
        holds the transaction. Running jobs stay running: whether they
        finished is decided by the printer's next report, never by a
        restart (demoting them to pending re-printed a job that was
        still on the bed)."""
        jobs: list[Job] = []
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._jobs = jobs
            return
        except OSError as e:
            LOG.warning("queue.json unreadable (%s); keeping in-memory copy", e)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            LOG.warning("queue.json corrupt (%s); keeping in-memory copy", e)
            return
        entries = data.get("jobs", []) if isinstance(data, dict) else []
        for d in entries:
            if not isinstance(d, dict):
                continue
            try:
                jobs.append(Job.from_dict(d))
            except (TypeError, ValueError):
                continue
        self._jobs = jobs

    def _persist(self) -> None:
        """Atomically rewrite the file. Caller holds the transaction."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(
            f"{self._path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(
            {"jobs": [j.to_dict() for j in self._jobs]}, indent=2),
            encoding="utf-8")
        os.replace(tmp, self._path)

    def _find(self, job_id: str) -> Job | None:
        """Job by id in the current transaction's list."""
        return next((j for j in self._jobs if j.id == job_id), None)

    # ----- public API --------------------------------------------------
    def add(self, *, printer: str, gcode: str, slot: int = 0,
            label: str = "") -> Job:
        """Enqueue `gcode` for `printer` on global AMS slot `slot`
        (0-based, unit*4 + tray, 0..15)."""
        if not 0 <= int(slot) <= 15:
            raise ValueError(f"slot must be a global AMS slot 0..15, got {slot}")
        with self._txn():
            job = Job(
                id=uuid.uuid4().hex,
                printer=printer or "",
                gcode=str(gcode),
                slot=int(slot),
                label=label or Path(gcode).name,
                enqueued=time.time(),
            )
            self._jobs.append(job)
            self._persist()
            return dataclasses.replace(job)

    def list(self) -> list[Job]:
        with self._txn():
            return [dataclasses.replace(j) for j in self._jobs]

    def get(self, job_id: str) -> Job | None:
        with self._txn():
            j = self._find(job_id)
            return dataclasses.replace(j) if j else None

    def cancel(self, job_id: str) -> bool:
        with self._txn():
            j = self._find(job_id)
            if j is None or j.status != "pending":
                return False
            j.status = "cancelled"
            j.finished = time.time()
            self._persist()
            return True

    def remove(self, job_id: str) -> bool:
        with self._txn():
            j = self._find(job_id)
            if j is None:
                return False
            self._jobs.remove(j)
            self._persist()
            return True

    def move(self, job_id: str, *, dest_printer: str | None = None,
             position: int | None = None) -> bool:
        """Drag-and-drop: re-target to a different printer and/or
        re-position within the queue. Position 0 = head of the
        per-printer subqueue (after running jobs)."""
        with self._txn():
            job = self._find(job_id)
            if job is None or job.status != "pending":
                return False
            if dest_printer is not None:
                job.printer = dest_printer
            if position is not None:
                # Pull job out and re-insert it in front of the job that
                # currently holds `position` among the same printer's
                # pending jobs: the dispatcher's per-printer FIFO is the
                # filtered order of this list.
                self._jobs.remove(job)
                same = [j for j in self._jobs
                        if j.printer == job.printer and j.status == "pending"]
                before = same[position] if 0 <= position < len(same) else None
                if before is None:
                    self._jobs.append(job)
                else:
                    self._jobs.insert(self._jobs.index(before), job)
            self._persist()
            return True

    def pending_for(self, printer: str) -> list[Job]:
        with self._txn():
            return [dataclasses.replace(j) for j in self._jobs
                    if j.printer == printer and j.status == "pending"]

    def has_running(self, printer: str) -> bool:
        with self._txn():
            return any(j.printer == printer and j.status == "running"
                       for j in self._jobs)

    def start_next(self, printer: str) -> Job | None:
        """Atomically mark the next pending job for `printer` as running and
        return it (or None if none pending / one already running). For
        one-shot CLI dispatch (`beambam start` print-next-in-queue) outside
        the daemon's idle-driven `on_state` loop. The caller performs the
        upload+publish, then calls `mark_failed` on error (success leaves
        status=running, matching the daemon's behaviour)."""
        with self._txn():
            job = self._claim_next(printer)
            if job is not None:
                self._persist()
            return dataclasses.replace(job) if job else None

    def mark_failed(self, job_id: str, error: str) -> None:
        """Revert a job to failed with an error string (e.g. when a one-shot
        CLI dispatch's upload/publish raised after `start_next`)."""
        self._finish(job_id, "failed", error)

    # ----- dispatch ----------------------------------------------------
    def _claim_next(self, printer: str) -> Job | None:
        """Mark the FIFO head for `printer` running (caller holds the
        transaction and persists). None when one is already running."""
        if any(j.printer == printer and j.status == "running"
               for j in self._jobs):
            return None
        job = next((j for j in self._jobs
                    if j.printer == printer and j.status == "pending"), None)
        if job is None:
            return None
        job.status = "running"
        job.started = time.time()
        job.seen_active = False
        job.error = ""
        return job

    def _finish(self, job_id: str, status: str, error: str = "") -> None:
        with self._txn():
            j = self._find(job_id)
            if j is None:
                return
            j.status = status
            j.error = error
            j.finished = time.time()
            self._persist()

    def _settle_running(self, printer: str, state: dict) -> bool:
        """Advance this printer's running jobs from a report that has a
        gcode_state. Caller holds the transaction. Returns True when
        anything changed."""
        gs = _gcode_state(state) or ""
        active = _is_printer_active(state)
        idle = _is_printer_idle(state)
        now = time.time()
        changed = False
        for j in self._jobs:
            if j.printer != printer or j.status != "running":
                continue
            if active:
                if not j.seen_active:
                    j.seen_active = True
                    changed = True
            elif idle and j.seen_active:
                j.status = "failed" if gs in _FAILED_GCODE_STATES else "done"
                j.error = (f"printer reported {gs}"
                           if j.status == "failed" else "")
                j.finished = now
                changed = True
            elif (idle and j.id not in self._inflight
                  and now - j.started > _START_GRACE_S):
                j.status = "failed"
                j.error = (f"printer stayed {gs} for "
                           f"{_START_GRACE_S / 60:.0f} min after dispatch")
                j.finished = now
                changed = True
        return changed

    def on_state(self, printer: str, state: dict | None) -> None:
        """Hook into the daemon's per-printer state callback.

        Pass the merged state. Messages without ``print.gcode_state``
        are ignored. A running job is completed once the printer has
        been active for it and is idle again; then, if the printer is
        idle, the next pending job is dispatched.
        """
        if _gcode_state(state) is None:
            return
        assert state is not None            # narrowed by _gcode_state
        with self._txn():
            changed = self._settle_running(printer, state)
            job = (self._claim_next(printer)
                   if _is_printer_idle(state) else None)
            if job is not None:
                self._inflight.add(job.id)
                changed = True
            if changed:
                self._persist()
            claimed = dataclasses.replace(job) if job else None
        if claimed is None:
            return
        if self._background:
            threading.Thread(target=self._run_dispatch, args=(claimed,),
                             name=f"queue-dispatch-{claimed.id[:8]}",
                             daemon=True).start()
        else:
            self._run_dispatch(claimed)

    def _run_dispatch(self, job: Job) -> None:
        """Run the dispatch callback OUTSIDE the lock (FTPS upload + MQTT
        publish take seconds) and record a failure."""
        try:
            ok = bool(self._dispatch_cb(job))
            error = "" if ok else "dispatch_cb returned False"
        except Exception as e:              # callback owns its own logging
            LOG.exception("dispatch %s failed: %s", job.id, e)
            ok, error = False, str(e) or type(e).__name__
        finally:
            with self._lock:
                self._inflight.discard(job.id)
        if not ok:
            self._finish(job.id, "failed", error)
        # On success the job stays running; the printer's reports move it
        # to done (see _settle_running).

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until no dispatch is in flight (tests, shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._inflight:
                    return True
            time.sleep(0.01)
        return False


__all__ = [
    "Job",
    "JobFileError",
    "JOB_FILE_SUFFIX",
    "QueueManager",
    "validate_job_file",
]
