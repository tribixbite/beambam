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

import time
from typing import Any


# Signed `print.*` frames must carry a sequence_id above a firmware floor
# (~2000). The Jan-2025+ X2D/H2D verifier rejects a lower seq EARLY and returns
# err 84033545 — the SAME code it uses for "cert not authorized for print.*" —
# BEFORE the cert/signature even matter, so a fully correct key+cert+user_id
# still looks like a cert-trust failure when the seq is small (verified live
# 2026-11-18: seq=1 → 84033545 stage 2; seq=5001 → 84033544 stage 4, i.e.
# signature+cert+auth all pass). Real Handy frames stay above the floor (a
# captured `print.stop` used seq 2009 — tests/test_mqtt_sign.py). Seed the
# counter from a ms timestamp, exactly as the cloud-signed path already does
# (cloud_control uses str(int(time.time()*1000))): always far above the floor,
# and monotonic across process restarts so a fresh run never looks like a replay.
_SEQ_COUNTER = int(time.time() * 1000)


def _next_seq() -> str:
    """Return the next per-process sequence ID as a decimal string.

    Bambu's MQTT protocol uses sequence_id as the request/response
    correlation key on `device/<sn>/request` ↔ `device/<sn>/report`, and
    signed `print.*` frames must additionally clear the firmware's
    signed-frame floor (see the module note above). Seeded from a ms
    timestamp, then monotonic + per-process — never reset within a run.
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


def _xcam_cmd(module_name: str, on_off: bool,
              halt_print_sensitivity: str | None = None) -> dict:
    """Build an `{"xcam": {"command": "xcam_control_set", ...}}` payload.

    Used to toggle camera-driven detectors that the X2D / H2D / N7
    firmware runs during a print. Modules supported in firmware include:
      * fod_check                  — Foreign Object Detection on the build
                                     plate (firmware stage 74). When on,
                                     pre-print stage 73/74 runs; if junk is
                                     detected on the plate the firmware
                                     halts the print start (print_halt=true).
      * buildplate_marker_detector — verify plate is properly seated
      * first_layer_inspector      — AI-driven first-layer fault scan
      * printing_monitor           — general AI monitoring (spaghetti etc.)
      * spaghetti_detector         — alias of printing_monitor on older fw
      * pileup_detector            — purge-chute pile-up detection
      * clump_detector             — nozzle clumping detection
      * airprint_detector          — air-printing (extruder skip) detection
      * plate_offset_switch        — toggle plate-offset bed-leveling shortcut
    See DevPrintOptions.cpp:451 for the canonical payload shape.
    """
    body: dict[str, Any] = {
        "command":     "xcam_control_set",
        "sequence_id": _next_seq(),
        "module_name": module_name,
        "control":     bool(on_off),
        "enable":      bool(on_off),    # older firmware spelling
        "print_halt":  True,            # always honour halt-on-detect
    }
    if halt_print_sensitivity:
        body["halt_print_sensitivity"] = halt_print_sensitivity
    return {"xcam": body}
