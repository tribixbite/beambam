"""GET /camera.mjpeg — the daemon relays the camera proxy's live MJPEG to web pages it lets in (CORS), and
/healthz says whether that proxy is up. A fake proxy stands in for `beambam camera` (ffmpeg + the printer)."""
from __future__ import annotations

import http.server
import json
import socket
import threading
import time
import urllib.error
import urllib.request

from beambam.serve_http import _serve_http, camera_listening

FRAMES = [b"\xff\xd8frame-%d\xff\xd9" % i for i in range(3)]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(port: int) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(port)


def _fake_camera() -> int:
    """A camera proxy serving /cam.mjpeg the way cmd_camera does: three frames, then it ends the stream."""
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_GET(self):  # noqa: N802
            if self.path != "/cam.mjpeg":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            for f in FRAMES:
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 + f"Content-Length: {len(f)}\r\n\r\n".encode() + f + b"\r\n")
                self.wfile.flush()

    port = _free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return port


def _daemon(camera_url: str) -> int:
    port = _free_port()
    threading.Thread(target=_serve_http, daemon=True, kwargs=dict(
        bind=f"127.0.0.1:{port}", get_state=lambda _p: {"print": {}}, get_last_ts=lambda _p: time.time(),
        printer_names=[""], clients={}, cors_origins=["https://beambam.boo"], camera_url=camera_url)).start()
    _wait_port(port)
    return port


def _get(port: int, path: str, origin: str = "https://beambam.boo"):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Origin": origin})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_the_live_view_is_relayed_to_an_allowed_page():
    cam = _fake_camera()
    port = _daemon(f"http://127.0.0.1:{cam}")
    code, body, headers = _get(port, "/camera.mjpeg")
    assert code == 200
    assert headers["Content-Type"].startswith("multipart/x-mixed-replace")
    assert headers.get("Access-Control-Allow-Origin") == "https://beambam.boo"
    assert all(f in body for f in FRAMES) and body.count(b"--frame") == 3
    code, body, _ = _get(port, "/healthz")
    assert json.loads(body)["camera"] is True
    assert {"camera", "plate-check"} <= set(json.loads(body)["capabilities"])


def test_no_proxy_answers_503_with_what_to_run():
    dead = f"http://127.0.0.1:{_free_port()}"
    port = _daemon(dead)
    code, body, headers = _get(port, "/camera.mjpeg")
    assert code == 503 and b"beambam cam start" in body and b"--camera" in body
    assert headers.get("Access-Control-Allow-Origin") == "https://beambam.boo"   # the page can read why
    assert json.loads(_get(port, "/healthz")[1])["camera"] is False
    assert camera_listening(dead) is False
