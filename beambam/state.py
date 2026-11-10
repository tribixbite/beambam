"""beambam.state — turn the printer's report stream into a usable state.

Bambu printers publish on ``device/<serial>/report``:

* a **full** report (``print.command == "push_status"`` with every field,
  including ``gcode_state``) in answer to ``pushing.pushall`` and every few
  minutes;
* **delta** reports in between: ``push_status`` messages carrying only the
  fields that changed (often no ``gcode_state``, no ``ams``);
* **command replies** (``{"print": {"command": "pause", "result": ...}}``,
  ``{"system": {"command": "ledctrl", ...}}``) that are not state at all.

Treating each message as "the state" (what beambam used to do) makes a
delta or a reply look like an idle, empty printer: the print queue
dispatched its next job mid-print, Home Assistant flapped to offline and
zero progress, and timelapses split. :func:`deep_merge` folds every
message into one accumulated document — what ha-bambulab and Bambu Studio
also do — and :func:`command_reply` classifies replies so a caller can wait
for the printer's verdict on a command it sent.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CommandReply",
    "command_reply",
    "deep_merge",
    "is_full_report",
    "STATE_FAMILIES",
]

# Top-level keys of a report that describe printer state (merged); other
# families' messages are replies or events and are only merged under their
# own key so nothing is lost.
STATE_FAMILIES: tuple[str, ...] = ("print", "info", "upgrade", "system", "camera", "xcam")

# A command reply carries these keys next to `command`; a push_status does not.
_REPLY_KEYS = ("result", "err_code", "reason")


def deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """Merge ``src`` into ``dst`` in place and return ``dst``.

    Dicts merge key by key, recursively; any other value (lists included)
    replaces the old one. Lists replace wholesale on purpose: the printer
    always sends a complete ``ams.ams`` / ``vt_tray`` / ``hms`` list when it
    sends one at all, and an element-wise merge would keep trays or HMS
    errors that are gone. ``src`` is deep-copied so later mutation of the
    incoming message cannot change the accumulated state.
    """
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_merge(dst[key], value)
        else:
            dst[key] = copy.deepcopy(value)
    return dst


def is_full_report(payload: dict[str, Any]) -> bool:
    """True for a state report that includes the print state machine.

    ``pushall`` answers (and periodic full pushes) always carry
    ``print.gcode_state``; deltas and command replies usually do not.
    """
    pr = payload.get("print")
    return isinstance(pr, dict) and "gcode_state" in pr


@dataclass(frozen=True)
class CommandReply:
    """The printer's answer to one command."""
    family: str                 # "print", "system", ...
    command: str                # "project_file", "ledctrl", ...
    ok: bool
    err_code: int
    reason: str
    sequence_id: str
    raw: dict[str, Any]


def command_reply(payload: dict[str, Any], family: str, command: str,
                  sequence_id: str | None = None) -> CommandReply | None:
    """Return the reply to ``family.command`` contained in ``payload``, or None.

    A reply is a ``{family: {"command": command, ...}}`` message that also
    has a ``result`` / ``err_code`` / ``reason`` (a ``push_status`` never
    does). It is a failure when ``err_code`` is non-zero or ``result`` says
    so (``"failed"``, ``"fail"``, ``"error"``).

    When ``sequence_id`` is given, the reply must echo that id. The printer
    copies the ``sequence_id`` we sent onto its reply, so this correlates a
    reply to the exact command that was published — without it, a reply to an
    earlier identical command, or to another client's concurrent command of
    the same name, would be accepted as this one's answer.
    """
    body = payload.get(family)
    if not isinstance(body, dict) or body.get("command") != command:
        return None
    if not any(k in body for k in _REPLY_KEYS):
        return None
    if sequence_id is not None and str(body.get("sequence_id", "")) != sequence_id:
        return None
    try:
        err = int(body.get("err_code") or 0)
    except (TypeError, ValueError):
        err = -1
    result = str(body.get("result", "")).lower()
    ok = err == 0 and result not in ("failed", "fail", "error")
    return CommandReply(family=family, command=command, ok=ok, err_code=err,
                        reason=str(body.get("reason", "")),
                        sequence_id=str(body.get("sequence_id", "")),
                        raw=payload)
