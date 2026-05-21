"""End-to-end tests for the v1.3.0 daemon HTTP routes:

  * GET  /ams       — surface the AMS subtree of the latest state
  * GET  /doctor    — run every Check on cached state, return summary + per-check rows
  * POST /analyze   — accept a raw .gcode.3mf body, return analyze.Report as JSON

All three are surfaced so the web UI + Home Assistant + ad-hoc dashboards
can show the same data that the CLI subcommands of the same name produce
without re-dialing MQTT. Tests bring up `_serve_http` on a random port
with stubbed `get_state` / `get_hub`, so no real printer is needed."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: F401  (test discovery)

from beambam.state_hub import StateHub
from x2d_bridge import _serve_http


# --- harness -----------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    """Poll the daemon's bind point until it accepts a TCP connection.

    Default timeout is 15 s (not the more obvious 3 s) because GitHub
    Actions macOS runners take noticeably longer to boot a ThreadingHTTPServer
    + Handler closure than Linux runners — empirically ~5-10 s during
    heavy CI load. 3 s was tight enough to spuriously time out on macOS
    even after the server was about to bind."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server on :{port} never came up "
                       f"(timeout={timeout}s)")


def _start_server(states: dict[str, dict | None]):
    port = _free_port()
    hubs = {n: StateHub() for n in states}

    def get_state(p: str):
        return states.get(p)

    def get_hub(p: str):
        return hubs.get(p)

    t = threading.Thread(
        target=_serve_http,
        kwargs={
            "bind":          f"127.0.0.1:{port}",
            "get_state":     get_state,
            "get_last_ts":   lambda _p: time.time(),
            "printer_names": list(states.keys()),
            "get_hub":       get_hub,
        },
        daemon=True,
    )
    t.start()
    _wait_for_port(port)
    return port


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5
        ) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post(port: int, path: str, body: bytes,
          ctype: str = "application/octet-stream",
          timeout: float = 30.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method="POST",
        headers={"Content-Type": ctype, "Content-Length": str(len(body))})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --- /ams --------------------------------------------------------------

def test_ams_returns_subtree_when_state_has_it():
    """Happy path — a printer with an AMS payload returns the
    `state["print"]["ams"]` block under `ams` plus the printer name."""
    ams_payload = {
        "ams": [
            {"id": 0, "humidity": "2",
             "tray": [{"id": 0, "tray_color": "FFFFFFFF"}]},
            {"id": 1, "humidity": "3", "tray": []},
        ],
        "ams_exist_bits": "3",
    }
    states = {"": {"print": {"ams": ams_payload}}}
    port = _start_server(states)

    status, body = _get(port, "/ams")
    assert status == 200, body
    assert body["printer"] == ""
    assert body["ams"]["ams"][0]["id"] == 0
    assert body["ams"]["ams"][1]["humidity"] == "3"
    assert body["ams"]["ams_exist_bits"] == "3"


def test_ams_returns_404_when_state_has_no_ams_block():
    """A printer with no AMS hardware (or pre-pushall boot) returns a
    helpful 404 instead of a misleading empty object."""
    states = {"": {"print": {"nozzle_temper": 220.0}}}
    port = _start_server(states)

    status, body = _get(port, "/ams")
    assert status == 404, body
    assert "no AMS payload" in body["error"]


def test_ams_returns_404_when_state_is_none():
    """A printer the daemon has never heard from at all also returns
    404 — the same code as 'no AMS hardware' so HA can render a
    single 'AMS unknown' state for both."""
    states: dict[str, dict | None] = {"": None}
    port = _start_server(states)

    status, body = _get(port, "/ams")
    assert status == 404


# --- /doctor -----------------------------------------------------------

def test_doctor_returns_pass_summary_for_healthy_state():
    """A healthy printer with dry AMS, no HMS errors, and good wifi
    should report worst='pass'."""
    states = {
        "": {
            "print": {
                "ams": {"ams": [{"id": 0, "humidity": "0", "tray": []}]},
                "hms": [],
                "nozzle_temper":  220.0,
                "bed_temper":     60.0,
                "chamber_temper": 30.0,
                "wifi_signal":    "-45dBm",
                "ipcam": {"resolution": "1080p", "record": "disable"},
                "gcode_state": "IDLE",
            }
        }
    }
    port = _start_server(states)

    status, body = _get(port, "/doctor")
    assert status == 200, body
    assert body["worst"] == "pass"
    assert isinstance(body["checks"], list) and body["checks"]
    assert all(c["severity"] in {"pass", "warn", "fail", "info"}
               for c in body["checks"])
    # Each Check must serialize as a dict with the four canonical fields.
    for c in body["checks"]:
        assert "category" in c and "name" in c
        assert "severity" in c and "detail" in c


def test_doctor_summary_counts_match_checks():
    """`summary[severity]` must equal len([c for c in checks if c.severity
    == severity]) for each bucket. Catches off-by-one bugs in the
    aggregator that other tests would silently miss."""
    states: dict[str, dict | None] = {"": {"print": {}}}
    port = _start_server(states)

    status, body = _get(port, "/doctor")
    assert status == 200, body
    expected = {"pass": 0, "warn": 0, "fail": 0, "info": 0}
    for c in body["checks"]:
        expected[c["severity"]] = expected.get(c["severity"], 0) + 1
    assert body["summary"] == expected


def test_doctor_returns_warn_when_ams_humidity_is_high():
    """level 3 humidity should be flagged as warn; worst summary
    bumps from pass to warn so HA can fire a single binary_sensor."""
    states = {
        "": {
            "print": {
                "ams": {"ams": [
                    {"id": 0, "humidity": "3", "tray": []},
                ]},
                "hms": [],
                "nozzle_temper": 220.0,
                "bed_temper":    60.0,
                "wifi_signal":   "-45dBm",
                "ipcam":         {"resolution": "1080p"},
                "gcode_state":   "IDLE",
            }
        }
    }
    port = _start_server(states)

    status, body = _get(port, "/doctor")
    assert status == 200, body
    assert body["worst"] in {"warn", "fail"}
    assert body["summary"]["warn"] >= 1


# --- /analyze ----------------------------------------------------------

def test_analyze_rejects_empty_body_with_400():
    states: dict[str, dict | None] = {"": None}
    port = _start_server(states)

    status, body = _post(port, "/analyze", b"")
    assert status == 400, body
    assert "Content-Length" in body["error"]


def test_analyze_rejects_oversized_body_with_413():
    """Anything > 64 MiB is refused before we read the body — protects
    the daemon against a misuploaded video or a DoS attempt."""
    states: dict[str, dict | None] = {"": None}
    port = _start_server(states)

    big = 65 * 1024 * 1024
    # Don't actually send 65 MiB; lie about Content-Length so the
    # server returns 413 before reading. urllib still sends the body
    # but we use a tiny one — the server checks the header first.
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/analyze",
        data=b"x" * 16,  # actual body we send
        method="POST",
        headers={"Content-Length": str(big),  # but claim huge
                 "Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status, body = r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        status, body = e.code, json.loads(e.read())
    assert status == 413, body
    assert "64 MiB" in body["error"]


def test_analyze_rejects_malformed_3mf_with_400():
    """A non-3MF body (random bytes / wrong format) must surface a clean
    400 + JSON error — never a raw traceback. Catches the case where
    `analyze_3mf` raises BadZipFile / ValueError / KeyError."""
    states: dict[str, dict | None] = {"": None}
    port = _start_server(states)

    status, body = _post(port, "/analyze", b"this is not a 3mf bundle")
    assert status == 400, body
    assert "analyze failed" in body["error"]


def test_analyze_returns_report_for_real_3mf():
    """End-to-end: POST a real sample .gcode.3mf and verify the route
    returns a parsed Report dataclass with the canonical fields."""
    sample = (Path(__file__).resolve().parents[1] /
              "samples" / "x2d_cache_toothpaste.gcode.3mf")
    if not sample.exists():
        pytest.skip(f"sample fixture missing: {sample}")
    raw = sample.read_bytes()
    states: dict[str, dict | None] = {"": None}
    port = _start_server(states)

    status, body = _post(port, "/analyze", raw)
    assert status == 200, body
    # analyze.Report key surface — see beambam/analyze.py:Report. We
    # assert the canonical nested groups, not every leaf field, so this
    # test tracks intent ("the route returns a Report") without trapping
    # every future field addition.
    assert "file" in body and "sha256" in body["file"]
    assert body["file"]["size"] == len(raw)
    assert "filaments" in body and isinstance(body["filaments"], list)
    assert "flush_matrix_mm" in body
