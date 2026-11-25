"""beambam.print_service — plan and start a LAN print for a non-terminal caller.

`beambam print` confirms a mapping on the terminal. A web app, Home
Assistant or the daemon's queue has no terminal: it shows the plan to its own
user and comes back with an explicit "yes, start it". This module is that
two-step flow, shared by the daemon's `POST /print/plan` + `POST /print`
(beambam.serve_http) and `beambam print` itself (`plan_request`):

    plan = plan_request(req, live_state)        # never touches the printer
    ...show plan_to_json(plan), get consent...
    job  = PrintJobs.start(req, client, ...)    # upload + project_file, async

Rules are the CLI's: hard mapping errors always refuse; soft warnings
(brand / profile / colour) need `accept_warnings`; the bed type and bed
temperature always come from the file (no override over HTTP).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from beambam.filament_mapping import (
    MappingError, PrintPlan, parse_map, plan_print, plan_to_json, read_project,
)

if TYPE_CHECKING:
    from beambam.mqtt import X2DClient

# Seconds to wait for the printer's reply to project_file (as `beambam print`).
ACK_WAIT_S = 15.0
# How old the daemon's cached state may be before a fresh pushall is asked for.
MAX_STATE_AGE_S = 10.0


@dataclass(frozen=True)
class PrintRequest:
    """What to print and how — the JSON body of `POST /print[/plan]`."""
    file: Path
    plate: int = 1
    slot: int | list[int] | None = None    # `--slot` semantics
    map: str = ""                          # `--map 1=B3,2=ext-left`
    use_ams: bool = True
    bed_levelling: bool = True
    flow_cali: bool = False
    timelapse: bool = False
    vibration_cali: bool = False

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> "PrintRequest":
        """Validate a request body; raises ValueError with a user-facing message."""
        from beambam.print_job import parse_slots
        raw = body.get("file")
        if not isinstance(raw, str) or not raw:
            raise ValueError('expected "file": the path of a sliced .gcode.3mf on the '
                             "daemon's machine (upload one with POST /print/upload)")
        path = Path(raw).expanduser()
        if not path.name.endswith(".gcode.3mf"):
            raise ValueError(f"{path.name} is not a sliced .gcode.3mf")
        if not path.is_file():
            raise ValueError(f"file not found: {path}")
        slot = body.get("slot")
        if isinstance(slot, str):
            parsed = parse_slots(slot)
            slot = parsed[0] if len(parsed) == 1 else parsed
        elif isinstance(slot, list):
            if not all(isinstance(s, int) and (s == -1 or 0 <= s <= 255) for s in slot):
                raise ValueError("slot list entries must be tray ids (0..15, 254/255) or -1")
        elif slot is not None and not (isinstance(slot, int) and 0 <= slot <= 255):
            raise ValueError("slot must be a tray id, a list of them, or text like '4,0'")
        try:
            plate = int(body.get("plate", 1) or 1)
        except (TypeError, ValueError) as e:
            raise ValueError("plate must be a number") from e

        def flag(name: str, default: bool) -> bool:
            v = body.get(name, default)
            if not isinstance(v, bool):
                raise ValueError(f"{name} must be true or false")
            return v
        return cls(file=path, plate=plate, slot=slot, map=str(body.get("map") or ""),
                   use_ams=flag("use_ams", True), bed_levelling=flag("bed_levelling", True),
                   flow_cali=flag("flow_cali", False), timelapse=flag("timelapse", False),
                   vibration_cali=flag("vibration_cali", False))


def plan_request(req: PrintRequest, state: dict, *,
                 extra_explicit: dict[int, int] | None = None) -> PrintPlan:
    """The filament → tray plan for `req` against `state`. Raises MappingError."""
    explicit = dict(extra_explicit or {})
    if req.map:
        explicit.update(parse_map(req.map))
    return plan_print(read_project(req.file, req.plate), state, use_ams=req.use_ams,
                      slot=req.slot, explicit=explicit or None)


def plan_summary(plan: PrintPlan) -> dict[str, Any]:
    """plan_to_json plus the verdict a client acts on."""
    out = plan_to_json(plan)
    out["ok"] = not plan.errors
    out["needs_confirmation"] = bool(plan.warnings)
    return out


@dataclass
class PrintJob:
    id: str
    file: str
    state: str = "queued"      # queued → uploading → starting → accepted | sent | rejected | failed
    message: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    finished: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "file": self.file, "state": self.state,
                "message": self.message, "plan": self.plan, "created": self.created,
                "finished": self.finished,
                "done": self.state in ("accepted", "sent", "rejected", "failed")}


class PrintJobs:
    """Runs prints in the background for the HTTP daemon (one at a time per
    printer: a second start while one is uploading is refused)."""

    def __init__(self) -> None:
        self._jobs: dict[str, PrintJob] = {}
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    def get(self, job_id: str) -> PrintJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, printer: str, req: PrintRequest, client: "X2DClient",
              state: dict, *, accept_warnings: bool) -> PrintJob:
        """Check the plan now (raising MappingError / PermissionError so the
        caller can answer 409), then upload + start in a thread."""
        plan = plan_request(req, state)
        if plan.errors:
            raise MappingError("; ".join(plan.errors))
        if plan.warnings and not accept_warnings:
            raise PermissionError("the mapping has warnings the user has not accepted: "
                                  + "; ".join(plan.warnings)
                                  + ' — show them and resend with "accept_warnings": true')
        with self._lock:
            if printer in self._busy:
                raise RuntimeError("a print is already being sent to this printer")
            self._busy.add(printer)
            job = PrintJob(id=uuid.uuid4().hex[:12], file=str(req.file),
                           plan=plan_summary(plan))
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(printer, job, req, client, plan),
                         name=f"print-{job.id}", daemon=True).start()
        return job

    def _set(self, job: PrintJob, state: str, message: str = "") -> None:
        with self._lock:
            job.state, job.message = state, message
            if state in ("accepted", "sent", "rejected", "failed"):
                job.finished = time.time()

    def _run(self, printer: str, job: PrintJob, req: PrintRequest,
             client: "X2DClient", plan: PrintPlan) -> None:
        from beambam.ftps import upload_file
        from beambam.mqtt_sign import explain_ack
        from beambam.print_job import PrintRefusal, _derive_print_params_from_3mf, start_print
        try:
            derived = _derive_print_params_from_3mf(req.file, plate=req.plate)
            self._set(job, "uploading", f"uploading {req.file.name} to the printer")
            upload_file(client.creds, req.file, remote_dir="cache")
            self._set(job, "starting", "sending the print command")
            ack = start_print(client, req.file.name, use_ams=plan.use_ams,
                              bed_levelling=req.bed_levelling, flow_cali=req.flow_cali,
                              timelapse=req.timelapse, vibration_cali=req.vibration_cali,
                              bed_type=derived["bed_type"], bed_temp=derived["bed_temp"],
                              local_path=req.file, plate=req.plate, plan=plan,
                              wait_ack=ACK_WAIT_S)
            if ack is None:
                self._set(job, "sent", f"sent; the printer did not confirm within "
                                       f"{ACK_WAIT_S:g}s — watch /state")
            elif (problem := explain_ack(ack)):
                self._set(job, "rejected", f"the printer rejected the print: {problem}")
            else:
                self._set(job, "accepted", "the printer accepted the print")
        except (PrintRefusal, MappingError) as e:
            self._set(job, "failed", str(e))
        except Exception as e:                      # network, FTPS, signing
            self._set(job, "failed", f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._busy.discard(printer)


def fresh_state(cached: dict | None, last_ts: float, client: "X2DClient") -> dict:
    """The daemon's cached state if recent, else a new pushall reply."""
    if cached and time.time() - last_ts <= MAX_STATE_AGE_S:
        return cached
    return client.request_state(timeout=15.0)
