"""POST /control/* must send like the CLI and report the printer's verdict.

Audit findings covered:

* S5 (P0) — the routes published through X2DClient.publish (the leaked
  Bambu Connect signature, which X2D/H2D firmware refuses for print.*,
  err 84033545) and answered 200 ok without reading the printer's reply.
  They now go through beambam.commands.send_command and map the reply:
  200 ack / 409 refused / 504 silent (where a reply is expected) /
  202 silent (where it may not come) / 502 publish failed.
* S16 (P1) — ams_load sent a bare target=slot-1; it now uses the CLI's
  ams_load_payload (ams_id / slot_id / target with the tray-0 case).

No real printer: the client is a test double that scripts the reply.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beambam import serve_http as SH  # noqa: E402


class _ScriptedClient:
    """X2DClient stand-in: records publishes, answers wait_for() with the
    reply produced by `responder(payload)` (None = printer stays silent)."""

    def __init__(self) -> None:
        self.published: list[dict] = []
        self.responder: Callable[[dict], dict | None] = lambda _p: None
        self.raise_on_publish: Exception | None = None
        self.message_seq = 0

    def publish(self, payload: dict) -> None:
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        self.published.append(payload)

    def wait_for(self, predicate, timeout: float, since: int | None = None):
        reply = self.responder(self.published[-1]) if self.published else None
        if reply is not None and predicate(reply):
            return reply
        return None


def _ack(payload: dict, **fields: Any) -> dict:
    family = next(iter(payload))
    body = payload[family]
    return {family: {"command": body["command"],
                     "sequence_id": body["sequence_id"], **fields}}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    client = _ScriptedClient()
    port = _free_port()
    SH.CONTROL_REPLY_TIMEOUT_S = 0.2       # module-level; read per request
    threading.Thread(target=SH._serve_http,
                     kwargs={"bind": f"127.0.0.1:{port}",
                             "get_state": lambda _p: None,
                             "printer_names": [""],
                             "clients": {"": client}},
                     daemon=True).start()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    yield port, client
    SH.CONTROL_REPLY_TIMEOUT_S = 4.0


def _post(port: int, verb: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/control/{verb}", data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture()
def client(server):
    port, cli = server
    cli.responder = lambda _p: None
    cli.raise_on_publish = None
    return port, cli


def test_acknowledged_command_is_200_verified(client):
    port, cli = client
    cli.responder = lambda p: _ack(p, result="success", err_code=0)
    status, body = _post(port, "pause")
    assert status == 200, body
    assert body["ok"] is True and body["verified"] is True
    assert cli.published[-1]["print"]["command"] == "pause"


def test_refused_command_is_409_with_err_code(client):
    port, cli = client
    cli.responder = lambda p: _ack(p, result="failed", err_code=84033545,
                                   reason="verify failed")
    status, body = _post(port, "stop")
    assert status == 409, body
    assert body["ok"] is False
    assert body["err_code"] == 84033545
    assert body["reason"] == "verify failed"
    assert "84033545" in body["error"]


def test_silent_printer_is_504_for_job_control(client):
    port, _ = client
    status, body = _post(port, "resume")
    assert status == 504, body
    assert body["ok"] is False and "did not answer" in body["error"]


def test_silent_printer_is_202_for_commands_that_may_not_answer(client):
    port, cli = client
    status, body = _post(port, "light", {"state": "on"})
    assert status == 202, body
    assert body["ok"] is True and body["verified"] is False
    assert cli.published[-1]["system"]["command"] == "ledctrl"


def test_publish_failure_is_502(client):
    port, cli = client
    cli.raise_on_publish = ConnectionError("broker gone")
    status, body = _post(port, "pause")
    assert status == 502, body
    assert "broker gone" in body["error"]


@pytest.mark.parametrize("ui_slot,ams_id,slot_id,target", [
    (1, 0, 0, 0),       # tray 0 of AMS 0: firmware wants the bare ams_id
    (2, 0, 1, 1),
    (7, 1, 2, 6),
    (16, 3, 3, 15),
])
def test_ams_load_uses_cli_payload(client, ui_slot, ams_id, slot_id, target):
    port, cli = client
    cli.responder = lambda p: _ack(p, result="success", err_code=0)
    status, body = _post(port, "ams_load",
                         {"slot": ui_slot, "curr_temp": 220, "tar_temp": 230})
    assert status == 200, body
    sent = cli.published[-1]["print"]
    assert sent["command"] == "ams_change_filament"
    assert (sent["ams_id"], sent["slot_id"], sent["target"]) == \
        (ams_id, slot_id, target)
    assert (sent["curr_temp"], sent["tar_temp"]) == (220, 230)


@pytest.mark.parametrize("tray,ams_id,slot_id,target", [
    (0, 0, 0, 0),        # A1
    (6, 1, 2, 6),        # B3
    (15, 3, 3, 15),      # D4
    (254, 254, 0, 254),  # external spool
])
def test_ams_load_accepts_tray_id_for_ha(client, tray, ams_id, slot_id, target):
    """The HA publisher's AMS buttons POST {"tray": 0..63|254|255} (not the
    legacy {"slot": 1..16}); the route must accept it and sign it."""
    port, cli = client
    cli.responder = lambda p: _ack(p, result="success", err_code=0)
    status, body = _post(port, "ams_load", {"tray": tray})
    assert status == 200, body
    sent = cli.published[-1]["print"]
    assert sent["command"] == "ams_change_filament"
    assert (sent["ams_id"], sent["slot_id"], sent["target"]) == (ams_id, slot_id, target)


def test_ams_unload_is_supported_and_signed(client):
    """The HA publisher POSTs /control/ams_unload; the route must exist (not
    404) and send the unload sentinel (ams_id/target/slot_id = 255), signed."""
    port, cli = client
    cli.responder = lambda p: _ack(p, result="success", err_code=0)
    status, body = _post(port, "ams_unload", {})
    assert status == 200, body
    sent = cli.published[-1]["print"]
    assert sent["command"] == "ams_change_filament"
    assert (sent["ams_id"], sent["slot_id"], sent["target"]) == (255, 255, 255)


def test_print_commands_go_through_the_signed_path(client, monkeypatch):
    """print.* must use print_job._publish_signed_or_legacy (the CLI's
    signer), not X2DClient.publish directly."""
    import beambam.print_job as PJ
    port, cli = client
    used: list[dict] = []
    real = PJ._publish_signed_or_legacy

    def spy(c, payload):
        used.append(payload)
        real(c, payload)

    monkeypatch.setattr(PJ, "_publish_signed_or_legacy", spy)
    cli.responder = lambda p: _ack(p, result="success", err_code=0)
    _post(port, "pause")
    assert used and used[-1]["print"]["command"] == "pause"
    used.clear()
    _post(port, "light", {"state": "off"})
    assert used == []                      # system.* keeps the plain path
