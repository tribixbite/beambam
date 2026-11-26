"""beambam.commands — send one control command and get the printer's verdict.

Shared by every surface that controls a printer (the CLI verbs in
:mod:`beambam.cli.control`, the HTTP daemon's ``/control/*`` routes, the
:class:`beambam.printer.Printer` facade) so they sign and judge commands the
same way:

* ``print.*`` is signed with the recovered per-install key when present
  (:func:`beambam.print_job._publish_signed_or_legacy`, the signature the LAN
  print start uses). Jan-2025+ X2D/H2D firmware refuses ``print.*`` signed
  with the leaked Bambu Connect cert (err 84033545). Other families
  (``system``, ``camera``, ``pushing``…) go through ``X2DClient.publish``.
* After publishing, the printer's reply (``{family: {"command": …,
  "result"/"err_code"/"reason"}}``) is awaited; :func:`send_command` returns
  it, or ``None`` when no reply arrives (some commands never answer).

Payload builders for commands whose shape is easy to get wrong live here too.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from beambam.signing import SIGNATURE_OK_DOWNSTREAM, SIGNATURE_REJECTED, signing_status
from beambam.state import CommandReply, command_reply

if TYPE_CHECKING:
    from beambam.mqtt import X2DClient

__all__ = [
    "CommandRefused",
    "DEFAULT_REPLY_TIMEOUT_S",
    "ams_load_payload",
    "ams_unload_payload",
    "command_payload",
    "refusal_message",
    "send_command",
]

DEFAULT_REPLY_TIMEOUT_S = 8.0
# Signed `print.*` frames whose sequence_id is under this are refused by the
# Jan-2025+ X2D/H2D verifier with 84033545 (the same code as an unauthorised
# cert) before the cert is checked — see beambam/cli/_helpers.py.
SIGNED_SEQ_FLOOR = 2000


def _next_seq() -> str:
    # The one process-wide counter (ms-seeded, monotonic); imported lazily so
    # this module does not pull in the CLI package at import time.
    from beambam.cli._helpers import _next_seq as next_seq
    return next_seq()


def _raise_low_seq(body: dict[str, Any]) -> None:
    """Give a `print.*` body a sequence id above SIGNED_SEQ_FLOOR when it has
    none or a lower one (payload builders used a literal "0")."""
    try:
        low = int(str(body.get("sequence_id", ""))) < SIGNED_SEQ_FLOOR
    except ValueError:
        low = True
    if low:
        body["sequence_id"] = _next_seq()


class CommandRefused(RuntimeError):
    """The printer answered a command with an error."""

    def __init__(self, reply: CommandReply) -> None:
        super().__init__(refusal_message(reply))
        self.reply = reply


class CommandUnconfirmed(RuntimeError):
    """The command was sent but the printer never answered it (it may or may not have applied it)."""


def command_payload(family: str, command: str, **fields: Any) -> dict[str, Any]:
    """`{family: {"command": command, "sequence_id": <n>, **fields}}`."""
    return {family: {"command": command, "sequence_id": _next_seq(), **fields}}


def ams_load_payload(ams_id: int, slot_id: int, *, curr_temp: int = 215,
                     tar_temp: int = 215) -> dict[str, Any]:
    """`print.ams_change_filament` load (Bambu Studio
    MachineObject::command_ams_change_filament). `target` is the global tray
    id ams_id*4 + slot_id, except tray 0 of AMS 0 where the firmware expects
    the bare ams_id."""
    tray_id = int(ams_id) * 4 + int(slot_id)
    return command_payload("print", "ams_change_filament",
                           curr_temp=int(curr_temp), tar_temp=int(tar_temp),
                           ams_id=int(ams_id), target=int(ams_id) if tray_id == 0 else tray_id,
                           slot_id=int(slot_id))


def ams_unload_payload(ams_id: int = 0, *, curr_temp: int = 215,
                       tar_temp: int = 215) -> dict[str, Any]:
    """`print.ams_change_filament` unload (target/slot 255 = unload sentinel)."""
    return command_payload("print", "ams_change_filament",
                           curr_temp=int(curr_temp), tar_temp=int(tar_temp),
                           ams_id=int(ams_id), target=255, slot_id=255)


def refusal_message(reply: CommandReply) -> str:
    """Human text for a refused command, with the signing cause when it is one."""
    msg = f"{reply.family}.{reply.command} refused: err_code {reply.err_code}"
    if reply.reason:
        msg += f" ({reply.reason})"
    if reply.err_code in SIGNATURE_REJECTED:
        st = signing_status()
        msg += " — signature rejected: " + st.summary()
        if st.ready:
            msg += (" (inputs look current; a too-small sequence_id returns this "
                    "same code, else the key may belong to another Handy install)")
    elif reply.err_code == SIGNATURE_OK_DOWNSTREAM:
        msg += (" — signature + cert + auth ACCEPTED; rejected only at the "
                "downstream file/task stage (a bare gcode_line like M400 has no "
                "task on an idle printer — use during a print, or pause/resume/stop)")
    return msg


def send_command(cli: "X2DClient", payload: dict[str, Any], *,
                 timeout: float = DEFAULT_REPLY_TIMEOUT_S,
                 raise_on_refusal: bool = False) -> CommandReply | None:
    """Publish `payload` on a connected client and wait up to `timeout`
    seconds for the printer's reply to it.

    Returns the reply (check ``.ok``), or None when none arrived (or
    ``timeout`` <= 0). With ``raise_on_refusal`` a refusal raises
    :class:`CommandRefused` instead of being returned.
    """
    from beambam.print_job import _publish_signed_or_legacy

    family = next(iter(payload))
    if family == "print" and isinstance(payload[family], dict):
        # Before the seq is read for correlation below; changes `payload`.
        _raise_low_seq(payload[family])
    command = str(payload[family].get("command", ""))
    # Correlate the reply to this exact publish by the sequence_id the printer
    # echoes back, so a stale reply to an earlier identical command (or another
    # client's concurrent same-named command) is not mistaken for this answer.
    seq = str(payload[family].get("sequence_id", "")) or None
    since = cli.message_seq if hasattr(cli, "message_seq") else 0
    if family == "print":
        _publish_signed_or_legacy(cli, payload)
    else:
        cli.publish(payload)
    if timeout <= 0 or not command or not hasattr(cli, "wait_for"):
        return None
    msg = cli.wait_for(lambda m: command_reply(m, family, command, seq) is not None,
                       timeout, since=since)
    reply = command_reply(msg, family, command, seq) if msg is not None else None
    if reply is not None and not reply.ok and raise_on_refusal:
        raise CommandRefused(reply)
    return reply
