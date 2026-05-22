"""tests/test_http_ams_set.py — POST /control/ams_set HTTP route.

The web UI's AMS-edit dialog drives `/control/ams_set` to push
tray_info_idx + nozzle range + color to one AMS slot. Tests cover:

  * dry_run=True: returns the payload WITHOUT publishing
  * dry_run=False (default): publishes through the supplied
    clients[printer] mock
  * slot out-of-range: 400
  * bad color hex: 400
  * unknown printer: 503 (matches the existing _publish_via_client
    contract)

Uses the same _serve_http harness as test_v12_http_routes — a free
loopback port, stubbed clients dict, no real MQTT.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.state_hub import StateHub
from beambam.serve_http import _serve_http


class _MockClient:
    """Stand-in for an X2DClient: records every .publish() call."""
    def __init__(self):
        self.published: list[dict] = []

    def publish(self, payload: dict) -> None:
        self.published.append(payload)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float | None = None) -> None:
    if timeout is None:
        env_t = os.environ.get("BEAMBAM_TEST_PORT_TIMEOUT")
        if env_t:
            timeout = float(env_t)
        elif sys.platform in ("darwin", "win32"):
            timeout = 60.0
        else:
            timeout = 15.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server on :{port} never came up "
                       f"(timeout={timeout}s, platform={sys.platform})")


def _start(clients: dict):
    port = _free_port()
    states = {n: None for n in clients}
    hubs = {n: StateHub() for n in clients}
    thread_exc: list[BaseException] = []

    def _runner():
        try:
            _serve_http(
                bind=f"127.0.0.1:{port}",
                get_state=lambda p: states.get(p),
                get_last_ts=lambda _p: time.time(),
                printer_names=list(states.keys()),
                clients=clients,
                get_hub=lambda p: hubs.get(p),
            )
        except BaseException as e:                          # noqa: BLE001
            thread_exc.append(e)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    try:
        _wait_for_port(port)
    except RuntimeError:
        if thread_exc:
            raise thread_exc[0]
        raise
    return port


def _post_json(port: int, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(data))})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ----- happy path: publish via client --------------------------------------


def test_ams_set_publishes_payload_via_client():
    """Default (dry_run absent): build payload + publish through
    clients[""].publish exactly once."""
    cli = _MockClient()
    port = _start({"": cli})
    status, body = _post_json(port, "/control/ams_set", {
        "slot": 7,
        "tray_type": "PLA",
        "tray_info_idx": "GFL99",
        "nozzle_temp_min": 210,
        "nozzle_temp_max": 230,
        "tray_color": "FF0000FF",
    })
    assert status == 200, body
    assert body["ok"] is True
    assert len(cli.published) == 1
    payload = cli.published[0]["print"]
    assert payload["command"] == "ams_filament_setting"
    assert payload["ams_id"] == 1 and payload["slot_id"] == 3
    assert payload["tray_color"] == "FF0000FF"
    assert payload["nozzle_temp_min"] == 210


def test_ams_set_dry_run_returns_payload_without_publishing():
    """dry_run=True: response carries the rendered payload, no
    cli.publish call."""
    cli = _MockClient()
    port = _start({"": cli})
    status, body = _post_json(port, "/control/ams_set", {
        "slot": 0,
        "tray_type": "PETG",
        "tray_info_idx": "GFG99",
        "nozzle_temp_min": 240,
        "nozzle_temp_max": 270,
        "tray_color": "00FF00",
        "dry_run": True,
    })
    assert status == 200, body
    assert body["dry_run"] is True
    # Builder canonicalizes 6-char color to RRGGBBAA.
    assert body["payload"]["print"]["tray_color"] == "00FF00FF"
    assert cli.published == []                    # nothing published


def test_ams_set_color_canonical_added_when_missing_alpha():
    cli = _MockClient()
    port = _start({"": cli})
    status, body = _post_json(port, "/control/ams_set", {
        "slot": 2,
        "tray_type": "PLA",
        "nozzle_temp_min": 190,
        "nozzle_temp_max": 240,
        "tray_color": "abc123",              # lowercase, no alpha
    })
    assert status == 200, body
    assert cli.published[0]["print"]["tray_color"] == "ABC123FF"


def test_ams_set_color_omitted_when_null():
    """No tray_color → field absent from payload (firmware keeps the
    current AMS color tag)."""
    cli = _MockClient()
    port = _start({"": cli})
    status, body = _post_json(port, "/control/ams_set", {
        "slot": 1,
        "tray_type": "PLA",
        "nozzle_temp_min": 190,
        "nozzle_temp_max": 240,
        "tray_color": None,
    })
    assert status == 200, body
    assert "tray_color" not in cli.published[0]["print"]


# ----- validation errors ---------------------------------------------------


def test_ams_set_rejects_out_of_range_slot():
    cli = _MockClient()
    port = _start({"": cli})
    status, body = _post_json(port, "/control/ams_set", {
        "slot": 16, "tray_type": "PLA",
        "nozzle_temp_min": 190, "nozzle_temp_max": 240,
    })
    assert status == 400
    assert "slot must be int" in body["error"]
    assert cli.published == []


def test_ams_set_rejects_bad_color():
    cli = _MockClient()
    port = _start({"": cli})
    status, body = _post_json(port, "/control/ams_set", {
        "slot": 0, "tray_type": "PLA",
        "nozzle_temp_min": 190, "nozzle_temp_max": 240,
        "tray_color": "not-hex",
    })
    assert status == 400, body
    assert "bad input" in body["error"]
    assert cli.published == []


def test_ams_set_503_when_no_client_for_printer():
    """If clients[<printer>] is missing (daemon ran without --http
    + signed MQTT), the route returns 503 so the web UI can show a
    sensible 'daemon not connected' error rather than crashing."""
    port = _start({})
    status, body = _post_json(port, "/control/ams_set", {
        "slot": 0, "tray_type": "PLA",
        "nozzle_temp_min": 190, "nozzle_temp_max": 240,
    })
    assert status == 503, body
    assert "no live MQTT client" in body["error"]
