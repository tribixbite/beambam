"""POST /slice-print must spawn the real CLI and track only its own jobs.

Audit finding P2: the route spawned ``python serve_http.py slice-print``
(that module only defines functions, so the child exited 0 having done
nothing) behind a 202, reported on ANY pid through /proc (absent on
Windows), and leaked the parent's log file handle.

The test never runs a real slice/print: the spawned argv is swapped for
a harmless interpreter one-liner, and the real argv is only checked
against the CLI parser.
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beambam import serve_http as SH  # noqa: E402
from tests.test_subparser_coverage import _build_parser  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server on :{port} never came up")


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAMBAM_HOME", str(tmp_path / "home"))
    spawned: list[list[str]] = []
    real_argv = SH._slice_print_argv

    def fake_argv(model, **kw):
        # Record the argv the route built, run something harmless.
        spawned.append(real_argv(model, **kw))
        return [sys.executable, "-c",
                "import sys; print('slice child ran'); sys.exit(3)"]

    monkeypatch.setattr(SH, "_slice_print_argv", fake_argv)
    port = _free_port()
    threading.Thread(
        target=SH._serve_http,
        kwargs={"bind": f"127.0.0.1:{port}",
                "get_state": lambda _p: None,
                "printer_names": ["", "lab"]},
        daemon=True, name=f"slice-test-{port}").start()
    _wait_for_port(port)
    return port, spawned


def _post(port: int, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/slice-print", data=body, method="POST",
        headers={"Content-Type": "application/octet-stream", **headers})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_spawned_job_is_tracked_until_exit(server):
    port, spawned = server
    status, body = _post(port, b"solid x\nendsolid x\n",
                         {"X-Filename": "cube.stl", "X-Printer": "lab",
                          "X-Slot": "2", "X-Bed-Type": "textured_plate"})
    assert status == 202, body
    pid = body["job_id"]
    deadline = time.time() + 30
    info: dict = {}
    while time.time() < deadline:
        s, info = _get(port, f"/slice-print/jobs/{pid}")
        assert s == 200, info
        if info["state"] == "exited":
            break
        time.sleep(0.1)
    assert info["state"] == "exited" and info["exit_code"] == 3
    assert info["ok_exit"] is False
    assert "slice child ran" in info["log_tail"]
    # The real argv the route built: the CLI module, the uploaded model,
    # the validated headers.
    argv = spawned[-1]
    assert argv[:4] == [sys.executable, "-m", "beambam.cli", "slice-print"]
    assert Path(argv[4]).name.endswith("_cube.stl")
    assert ["--printer", "lab"] == argv[argv.index("--printer"):][:2]
    assert ["--slot", "2"] == argv[argv.index("--slot"):][:2]


def test_unknown_pid_is_404(server):
    port, _ = server
    status, body = _get(port, f"/slice-print/jobs/{os.getpid()}")
    assert status == 404, body
    status, body = _get(port, "/slice-print/jobs/abc")
    assert status == 400, body


@pytest.mark.parametrize("headers", [
    {"X-Printer": "nope"},
    {"X-Slot": "x"},
    {"X-Slot": "16"},
    {"X-Bed-Type": "a b;rm"},
])
def test_bad_headers_are_400_and_spawn_nothing(server, headers):
    port, spawned = server
    before = len(spawned)
    status, body = _post(port, b"solid x\n", headers)
    assert status == 400, body
    assert len(spawned) == before


def test_empty_body_is_400(server):
    port, spawned = server
    status, body = _post(port, b"", {})
    assert status == 400, body
    assert spawned == []


def test_real_argv_matches_cli_parser():
    argv = SH._slice_print_argv(Path("m.stl"), printer="lab", slot=3,
                                bed_type="auto", no_ams=True)
    assert argv[1:3] == ["-m", "beambam.cli"]
    parser = _build_parser()
    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        ns = parser.parse_args(argv[3:])
    assert ns.printer == "lab" and ns.slot == 3 and ns.no_ams is True
    assert ns.bed_type == "auto"
    argv = SH._slice_print_argv(Path("m.stl"), printer="", slot=None,
                                bed_type="auto", no_ams=False)
    assert "--printer" not in argv and "--slot" not in argv
