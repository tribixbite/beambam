"""Timelapse recorder + /timelapses routes regressions.

Audit findings covered:

* P2 — jobs of the unnamed default printer ("") were written directly
  under the root, where list_jobs (one level deeper) never found them.
* P2 — any message without gcode_state (a light toggle's reply, a
  temperature delta) ended the capture mid-print.
* P2 — /snapshot.jpg was fetched without the Bearer token when the
  daemon had --auth-token, so every frame was a 401.
* P2 — /timelapses/<printer>/<job> ids were URL-decoded after the route
  matched, so %2F / %2E%2E reached the filesystem.
"""
from __future__ import annotations

import http.server
import io
import json
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beambam.serve_http import _serve_http  # noqa: E402
from runtime.timelapse.recorder import (  # noqa: E402
    DEFAULT_PRINTER_SLUG,
    TimelapseRecorder,
    printer_slug,
)


def _jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()


_FRAME = _jpeg()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def camera():
    """Snapshot server that serves frames only with the right Bearer."""
    seen_auth: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_GET(self):
            auth = self.headers.get("Authorization", "")
            seen_auth.append(auth)
            if auth != "Bearer s3cret":
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(_FRAME)))
            self.end_headers()
            self.wfile.write(_FRAME)

    port = _free_port()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/snapshot.jpg", seen_auth
    finally:
        httpd.shutdown()
        httpd.server_close()


def _running(name: str = "part.gcode.3mf") -> dict:
    return {"print": {"gcode_state": "RUNNING", "mc_percent": 10,
                      "subtask_name": name}}


def _finish(name: str = "part.gcode.3mf") -> dict:
    return {"print": {"gcode_state": "FINISH", "mc_percent": 100,
                      "subtask_name": name}}


def _wait_frames(rec: TimelapseRecorder, printer: str, job: str,
                 n: int, timeout: float = 10.0) -> list[str]:
    deadline = time.time() + timeout
    frames: list[str] = []
    while time.time() < deadline:
        frames = rec.list_frames(printer, job)
        if len(frames) >= n:
            return frames
        time.sleep(0.05)
    return frames


def test_default_printer_jobs_are_listed_and_token_is_sent(tmp_path, camera):
    url, seen_auth = camera
    rec = TimelapseRecorder(snapshot_url=url, root=tmp_path / "tl",
                            interval_s=0.05, auth_token="s3cret")
    rec.on_state("", _running())
    try:
        frames = _wait_frames(rec, DEFAULT_PRINTER_SLUG, "part.gcode.3mf", 2)
    finally:
        rec.on_state("", _finish())
    assert len(frames) >= 2
    assert seen_auth and all(a == "Bearer s3cret" for a in seen_auth)
    jobs = rec.list_jobs()
    assert [(j["printer"], j["job_id"]) for j in jobs] == \
        [(DEFAULT_PRINTER_SLUG, "part.gcode.3mf")]
    assert (tmp_path / "tl" / DEFAULT_PRINTER_SLUG / "part.gcode.3mf"
            / "meta.json").is_file()


def test_messages_without_gcode_state_do_not_end_capture(tmp_path, camera):
    url, _ = camera
    rec = TimelapseRecorder(snapshot_url=url, root=tmp_path / "tl",
                            interval_s=0.05, auth_token="s3cret")
    rec.on_state("lab", _running())
    try:
        for msg in ({"system": {"command": "ledctrl", "result": "success"}},
                    {"print": {"nozzle_temper": 220.0}},
                    {"print": {"command": "pause", "result": "success"}},
                    {}):
            rec.on_state("lab", msg)
        assert "lab" in rec._active
        meta = json.loads((tmp_path / "tl" / "lab" / "part.gcode.3mf"
                           / "meta.json").read_text())
        assert meta["ended"] == 0.0
    finally:
        rec.on_state("lab", _finish())
    assert "lab" not in rec._active


def test_legacy_default_layout_is_migrated(tmp_path):
    root = tmp_path / "tl"
    legacy = root / "old_job"
    legacy.mkdir(parents=True)
    (legacy / "meta.json").write_text(json.dumps(
        {"job_id": "old_job", "printer": "", "subtask_name": "old",
         "started": 1.0, "ended": 2.0, "frame_count": 1}))
    (legacy / "00001.jpg").write_bytes(_FRAME)
    # A named printer's directory (job subdirs, no meta.json) stays put.
    (root / "lab" / "j1").mkdir(parents=True)
    (root / "lab" / "j1" / "meta.json").write_text("{}")
    rec = TimelapseRecorder(snapshot_url="http://127.0.0.1:1/x", root=root)
    assert not legacy.exists()
    assert (root / DEFAULT_PRINTER_SLUG / "old_job" / "00001.jpg").is_file()
    listed = {(j["printer"], j["job_id"]) for j in rec.list_jobs()}
    assert listed == {(DEFAULT_PRINTER_SLUG, "old_job"), ("lab", "j1")}
    assert rec.list_frames(DEFAULT_PRINTER_SLUG, "old_job") == ["00001.jpg"]


def test_printer_slug():
    assert printer_slug("") == DEFAULT_PRINTER_SLUG
    assert printer_slug(DEFAULT_PRINTER_SLUG) == DEFAULT_PRINTER_SLUG
    assert printer_slug("my lab/2") == "my_lab_2"
    assert printer_slug("..") == "_."


@pytest.mark.parametrize("printer,job", [
    ("..", "x"), ("lab", ".."), ("../..", "x"), ("lab", "a/b"),
    ("lab", "a\\b"), ("C:", "x"), ("lab", ""),
])
def test_recorder_rejects_unsafe_ids(tmp_path, printer, job):
    root = tmp_path / "tl"
    (root / "lab" / "x").mkdir(parents=True)
    (root / "lab" / "x" / "00001.jpg").write_bytes(_FRAME)
    rec = TimelapseRecorder(snapshot_url="http://127.0.0.1:1/x", root=root)
    assert rec.list_frames(printer, job) == []
    assert rec.frame_path(printer, job, "00001.jpg") is None
    assert rec.mp4_path(printer, job) is None
    assert rec.stitch(printer, job)["ok"] is False


# --- HTTP routes -------------------------------------------------------------------

@pytest.fixture()
def tl_server(tmp_path):
    root = tmp_path / "tl"
    (root / "lab" / "job1").mkdir(parents=True)
    (root / "lab" / "job1" / "meta.json").write_text(json.dumps(
        {"job_id": "job1", "printer": "lab", "subtask_name": "j",
         "started": 1.0, "ended": 2.0, "frame_count": 1}))
    (root / "lab" / "job1" / "00001.jpg").write_bytes(_FRAME)
    # A file outside the timelapse root that traversal would reach.
    (tmp_path / "secret.jpg").write_bytes(b"SECRET")
    rec = TimelapseRecorder(snapshot_url="http://127.0.0.1:1/x", root=root)
    port = _free_port()
    threading.Thread(target=_serve_http,
                     kwargs={"bind": f"127.0.0.1:{port}",
                             "get_state": lambda _p: None,
                             "printer_names": ["lab"],
                             "timelapse_rec": rec},
                     daemon=True).start()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    return port


def _get(port: int, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_route_serves_valid_ids(tl_server):
    status, body = _get(tl_server, "/timelapses/lab/job1")
    assert status == 200 and json.loads(body)["frames"] == ["00001.jpg"]
    status, body = _get(tl_server, "/timelapses/lab/job1/00001.jpg")
    assert status == 200 and body[:3] == b"\xff\xd8\xff"


@pytest.mark.parametrize("path", [
    "/timelapses/%2E%2E/lab/00001.jpg",
    "/timelapses/lab/%2E%2E",
    "/timelapses/..%2F..%2Fx/job1",
    "/timelapses/lab%2F..%2F..%2F/job1",
    "/timelapses/lab/job1%2F..%2F..%2F..",
    "/timelapses/lab/%5C..%5C..",
])
def test_route_rejects_encoded_traversal(tl_server, path):
    status, body = _get(tl_server, path)
    assert status in (400, 404), (status, body)
    assert b"SECRET" not in body


def test_stitch_route_rejects_encoded_traversal(tl_server):
    req = urllib.request.Request(
        f"http://127.0.0.1:{tl_server}/timelapses/%2E%2E/lab/stitch",
        data=b"{}", method="POST",
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 400
