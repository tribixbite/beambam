"""tests/test_http_print.py — printing through the daemon's HTTP API.

The path a separate web app (pokefit, a dashboard) uses instead of running
`beambam print` per click: POST /print/plan → show the plan → POST /print
{"confirm": true} → poll GET /print/jobs/<id>. Also the browser-origin guard
(a web page must not drive a loopback daemon) and the /slice-print spawn.
No printer: a fake MQTT client, and upload / start_print patched out.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from beambam.serve_http import _serve_http
from tests.test_filament_mapping import SWITCH_A, SWITCH_B, make_3mf, make_state, tray


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


STATE = make_state([
    (SWITCH_B, [tray(0, "PETG", "#F72323", "GFG99"), tray(1, "PLA", "#7C4B00")]),
    (SWITCH_A, [tray(0, "PLA", "#FFFFFF"), tray(1, "PLA", "#161616")]),
])


class _Client:
    creds = SimpleNamespace(ip="192.0.2.1", code="x", serial="SERIAL")

    def publish(self, payload: dict) -> None:
        pass

    def request_state(self, timeout: float = 15.0) -> dict:
        return STATE


@pytest.fixture
def daemon(monkeypatch):
    """A daemon on a free port; returns (port, calls)."""
    import beambam.ftps as ftps
    import beambam.print_job as pj
    calls: dict = {"upload": [], "start": []}
    monkeypatch.setattr(ftps, "upload_file",
                        lambda creds, path, **kw: calls["upload"].append((path, kw)))

    def fake_start(cli, name, **kw):
        calls["start"].append((name, kw))
        return {"command": "project_file", "result": "success"}
    monkeypatch.setattr(pj, "start_print", fake_start)
    port = _free_port()
    threading.Thread(target=_serve_http, daemon=True, kwargs=dict(
        bind=f"127.0.0.1:{port}", get_state=lambda _p: STATE,
        get_last_ts=lambda _p: time.time(), printer_names=[""],
        clients={"": _Client()}, cors_origins=["https://pokefit.party"])).start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            break
        except OSError:
            time.sleep(0.05)
    return port, calls


def _req(port: int, path: str, body=None, *, method="POST", headers=None):
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {}), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {}), dict(e.headers)


def _file(tmp_path: Path, colour: str = "#FFFFFF") -> str:
    return str(make_3mf(tmp_path, [("PLA", colour, "GFL99", "1")]))


def test_plan_then_print_then_poll(tmp_path, daemon):
    port, calls = daemon
    f = _file(tmp_path)
    code, plan, _ = _req(port, "/print/plan", {"file": f})
    assert code == 200 and plan["ok"] and not plan["needs_confirmation"]
    assert plan["filaments"][0]["tray"]["label"] == "B1 (slot 4)"
    assert calls["upload"] == [] and calls["start"] == []        # planning touches nothing

    code, body, _ = _req(port, "/print", {"file": f})
    assert code == 400 and "confirm" in body["error"]

    code, body, _ = _req(port, "/print", {"file": f, "confirm": True})
    assert code == 202 and body["poll"].startswith("/print/jobs/")
    for _ in range(100):
        code, job, _ = _req(port, body["poll"], method="GET")
        if job.get("done"):
            break
        time.sleep(0.05)
    assert job["state"] == "accepted", job
    (path, kw), = calls["upload"]
    assert Path(path).name == Path(f).name and kw["remote_dir"] == "cache"
    (name, kw), = calls["start"]
    assert kw["plan"].assignments[0].tray.tray_id == 4 and kw["bed_type"]


def test_warnings_need_explicit_acceptance(tmp_path, daemon):
    port, calls = daemon
    f = _file(tmp_path, "#00FF00")          # no green loaded → colour warning
    code, plan, _ = _req(port, "/print/plan", {"file": f})
    assert code == 200 and plan["ok"] and plan["needs_confirmation"] and plan["warnings"]
    code, body, _ = _req(port, "/print", {"file": f, "confirm": True})
    assert code == 409 and body["needs_confirmation"]
    code, body, _ = _req(port, "/print", {"file": f, "confirm": True, "accept_warnings": True})
    assert code == 202


def test_hard_errors_and_bad_requests(tmp_path, daemon):
    port, _ = daemon
    f = _file(tmp_path)
    code, body, _ = _req(port, "/print", {"file": f, "confirm": True, "slot": 3})   # A4 does not exist
    assert code == 409 and "refused" in body["error"]
    code, body, _ = _req(port, "/print/plan", {"file": f, "plate": 2})
    assert code == 422 and "no sliced plate 2" in body["error"]
    code, body, _ = _req(port, "/print/plan", {"file": str(tmp_path / "nope.gcode.3mf")})
    assert code == 400 and "file not found" in body["error"]
    code, body, _ = _req(port, "/print/plan", {"file": "/etc/passwd"})
    assert code == 400 and "not a sliced" in body["error"]
    code, body, _ = _req(port, "/print/plan", {"file": f, "slot": "4,0"})
    assert code == 422                       # 2 slots for a 1-filament plate


def test_upload_then_plan(tmp_path, daemon, monkeypatch):
    port, _ = daemon
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    raw = Path(_file(tmp_path)).read_bytes()
    code, body, _ = _req(port, "/print/upload", raw,
                         headers={"Content-Type": "application/octet-stream",
                                  "X-Filename": "figure.gcode.3mf"})
    assert code == 200 and body["file"].endswith("_figure.gcode.3mf")
    assert _req(port, "/print/plan", {"file": body["file"]})[0] == 200
    code, body, _ = _req(port, "/print/upload", b"not a zip",
                         headers={"X-Filename": "x.gcode.3mf"})
    assert code == 400


def test_browser_origins(tmp_path, daemon):
    port, _ = daemon
    f = _file(tmp_path)
    code, body, _ = _req(port, "/print/plan", {"file": f},
                         headers={"Origin": "https://evil.example"})
    assert code == 403 and "--cors-origin" in body["error"]
    code, _, hdrs = _req(port, "/print/plan", {"file": f},
                         headers={"Origin": "https://pokefit.party"})
    assert code == 200 and hdrs.get("Access-Control-Allow-Origin") == "https://pokefit.party"
    code, _, _ = _req(port, "/print/plan", {"file": f},
                      headers={"Origin": f"http://127.0.0.1:{port}"})   # the daemon's own UI
    assert code == 200
    code, _, hdrs = _req(port, "/print", None, method="OPTIONS",
                         headers={"Origin": "https://pokefit.party"})
    assert code == 204 and "POST" in hdrs.get("Access-Control-Allow-Methods", "")
    assert _req(port, "/control/stop", {}, headers={"Origin": "https://evil.example"})[0] == 403


def test_slice_print_spawns_the_cli(tmp_path, daemon, monkeypatch):
    import subprocess
    port, _ = daemon
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    seen: dict = {}

    class _P:
        pid = 4242

        def __init__(self, argv, **kw):
            seen["argv"] = argv
    monkeypatch.setattr(subprocess, "Popen", _P)
    code, body, _ = _req(port, "/slice-print", b"solid x\nendsolid x\n",
                         headers={"X-Filename": "part.stl", "X-Accept-Warnings": "1"})
    assert code == 202, body
    argv = seen["argv"]
    assert argv[1:4] == ["-m", "beambam.cli", "slice-print"]
    assert "--slot" not in argv and "--printer" not in argv and "--yes" in argv
