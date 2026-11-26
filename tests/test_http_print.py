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
import sys
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

        def poll(self):
            return None
    monkeypatch.setattr(subprocess, "Popen", _P)
    code, body, _ = _req(port, "/slice-print", b"solid x\nendsolid x\n",
                         headers={"X-Filename": "part.stl", "X-Accept-Warnings": "1"})
    assert code == 202, body
    argv = seen["argv"]
    assert argv[1:4] == ["-m", "beambam.cli", "slice-print"]
    assert "--slot" not in argv and "--printer" not in argv and "--yes" in argv


# The headless slicer (bs-bionic) is a POSIX executable, and so is its stand-in (a shebang script):
# Windows cannot exec either, so the daemon's slicing is tested where it exists.
_POSIX_SLICER = pytest.mark.skipif(sys.platform == "win32", reason="the headless slicer is POSIX-only")


def _fake_slicer(root: Path, *, ok: bool = True) -> Path:
    """A stand-in `bs-bionic/.../bambu-studio`: records its argv, writes result.json and the
    .gcode.3mf the way the real CLI does (into --outputdir, named by --export-3mf)."""
    exe = root / "bs-bionic" / "build" / "src" / "bambu-studio"
    exe.parent.mkdir(parents=True)
    machine = root / "bs-bionic" / "resources" / "profiles" / "BBL" / "machine"
    machine.mkdir(parents=True)
    (machine / "Bambu Lab X2D 0.4 nozzle.json").write_text("{}")
    exe.write_text(f"#!{sys.executable}\n" + """import json, os, sys
a = sys.argv[1:]
out = a[a.index("--outputdir") + 1]; name = a[a.index("--export-3mf") + 1]
open(os.path.join(out, "argv.json"), "w").write(json.dumps(a))
ok = %r
if ok:
    open(os.path.join(out, name), "wb").write(b"PK\\x03\\x04sliced")
json.dump({"return_code": 0 if ok else -100, "error_string": "" if ok else "Failed slicing the model",
           "sliced_plates": [{"total_predication": 3600.5, "warning_message": "",
                              "filaments": [{"total_used_g": 10.0}, {"total_used_g": 2.5}]}] if ok else []},
          open(os.path.join(out, "result.json"), "w"))
""" % ok)
    exe.chmod(0o755)
    return exe


def _project(tmp_path: Path) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/project_settings.config",
                   json.dumps({"printer_settings_id": "Bambu Lab X2D 0.4 nozzle",
                               "has_filament_switcher": "1", "enable_filament_dynamic_map": "0"}))
        z.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


def _wait(port: int, poll: str) -> dict:
    for _ in range(200):
        _, job, _ = _req(port, poll, method="GET")
        if job.get("done"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job never finished: {job}")


@_POSIX_SLICER
def test_slice_a_project_then_plan_it(tmp_path, daemon, monkeypatch):
    import beambam.slice_service as ss
    port, _ = daemon
    root = tmp_path / "root"
    _fake_slicer(root)
    monkeypatch.setattr(ss, "_root", lambda: root)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    code, body, _ = _req(port, "/slice", _project(tmp_path),
                         headers={"X-Filename": "jolteon pokefit.3mf", "Origin": "https://pokefit.party"})
    assert code == 202, body
    job = _wait(port, body["poll"])
    assert job["state"] == "done", job
    # Named as sent (the printer shows it as the job), not after the stored upload's time prefix.
    assert Path(job["file"]).name == "jolteon_pokefit.gcode.3mf" and Path(job["file"]).is_file()
    assert job["source"] == "jolteon_pokefit.3mf"
    assert job["seconds"] == 3600.5 and job["grams"] == 12.5 and job["grams_per_filament"] == [10.0, 2.5]
    argv = json.loads((Path(job["file"]).parent / "argv.json").read_text())
    # The bundled machine profile replaces the project's (newer Bambu Studio start G-code).
    assert argv[argv.index("--load-settings") + 1].endswith("Bambu Lab X2D 0.4 nozzle.json")
    assert argv[0] == "--slice" and argv[-1].endswith("_jolteon_pokefit.3mf")
    # The CLI would reset the track switch to off: it is passed on, the dynamic map (off) is not.
    assert "--has-filament-switcher=1" in argv and not any("dynamic" in a for a in argv)


@_POSIX_SLICER
def test_slice_refusals(tmp_path, daemon, monkeypatch):
    import beambam.slice_service as ss
    port, _ = daemon
    root = tmp_path / "root"
    _fake_slicer(root, ok=False)
    monkeypatch.setattr(ss, "_root", lambda: root)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    code, body, _ = _req(port, "/slice", _project(tmp_path), headers={"X-Filename": "x.gcode.3mf"})
    assert code == 400 and "already sliced" in body["error"]
    code, body, _ = _req(port, "/slice", b"PK\x03\x04junk", headers={"X-Filename": "x.3mf"})
    assert code == 400 and "not a Bambu Studio project" in body["error"]
    code, body, _ = _req(port, "/slice", _project(tmp_path), headers={"X-Filename": "x.3mf"})
    job = _wait(port, body["poll"])
    assert job["state"] == "failed" and "Failed slicing the model" in job["message"]
    assert _req(port, "/slice/jobs/nope", method="GET")[0] == 404


def test_private_network_preflight(daemon):
    port, _ = daemon
    code, _, hdrs = _req(port, "/slice", None, method="OPTIONS", headers={
        "Origin": "https://pokefit.party", "Access-Control-Request-Private-Network": "true"})
    assert code == 204 and hdrs.get("Access-Control-Allow-Private-Network") == "true"
    code, _, hdrs = _req(port, "/slice", None, method="OPTIONS", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Private-Network": "true"})
    assert code == 403 and "Access-Control-Allow-Private-Network" not in hdrs


def test_a_refused_setting_is_named_from_the_slicer_log(tmp_path):
    """Code -51 only says some setting is wrong; the log line names it (seen: a 0.2 mm outer wall at 0.2 mm layers)."""
    from beambam.slice_service import validate_reason
    log = tmp_path / "slicer.log"
    log.write_text("[2026-11-25 22:45:30.28] [0x7a] [error]   got error when validate: Too small line width\n"
                   "run found error, return -51, exit...\n")
    assert validate_reason(log) == "Too small line width"
    log.write_text("nothing to see\n")
    assert validate_reason(log) == ""
    assert validate_reason(tmp_path / "missing.log") == ""
