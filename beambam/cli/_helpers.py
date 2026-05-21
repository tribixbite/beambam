"""beambam.cli._helpers — small helpers shared by CLI modules.

Phase 5b/5a infrastructure. These were defined inline in x2d_bridge.py
and reached for via module-level access (`x2d_bridge._print_cmd(...)`)
from the handlers. Once handlers move into `beambam/cli/*.py`, this is
the canonical home for the helpers they share.

Currently:
  _next_seq       monotonic per-process sequence ID generator
  _print_cmd      build `{"print":  {"command": ..., "sequence_id": ..., ...}}`
  _system_cmd     build `{"system": {"command": ..., "sequence_id": ..., ...}}`
  _camera_cmd     build `{"camera": {"command": ..., "sequence_id": ..., ...}}`

x2d_bridge.py re-exports each name so external callers + tests using
`from x2d_bridge import _print_cmd` keep working unchanged.
"""
from __future__ import annotations

from typing import Any


_SEQ_COUNTER = 0


def _next_seq() -> str:
    """Return the next per-process sequence ID as a decimal string.

    Bambu's MQTT protocol uses sequence_id as the request/response
    correlation key on `device/<sn>/request` ↔ `device/<sn>/report`.
    Monotonic + per-process — never reset within a single x2d_bridge run.
    """
    global _SEQ_COUNTER
    _SEQ_COUNTER += 1
    return str(_SEQ_COUNTER)


def _print_cmd(command: str, **extra: Any) -> dict:
    """Build a `{"print": {"command":..., "sequence_id":..., **extra}}`."""
    body = {"command": command, "sequence_id": _next_seq(), **extra}
    return {"print": body}


def _system_cmd(command: str, **extra: Any) -> dict:
    """Build a `{"system": {"command":..., "sequence_id":..., **extra}}`."""
    body = {"command": command, "sequence_id": _next_seq(), **extra}
    return {"system": body}


def _camera_cmd(command: str, **extra: Any) -> dict:
    """Build a `{"camera": {"command":..., "sequence_id":..., **extra}}`.

    Used for `ipcam_record_set` / `ipcam_timelapse` / `ipcam_resolution_set`
    — all unsigned MQTT publishes to `device/<sn>/request`. See
    BambuStudio's DeviceManager.cpp:2027-2080 for the shape.
    """
    body = {"command": command, "sequence_id": _next_seq(), **extra}
    return {"camera": body}
