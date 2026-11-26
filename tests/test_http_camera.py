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


def _fake_camera(forever: bool = False) -> int:
    """A camera proxy serving /cam.mjpeg the way cmd_camera does: three frames, then it ends the stream (or, with
    `forever`, a frame every 0.2 s until the viewer leaves)."""
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
            frames = FRAMES * (10_000 if forever else 1)
            try:
                for f in frames:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     + f"Content-Length: {len(f)}\r\n\r\n".encode() + f + b"\r\n")
                    self.wfile.flush()
                    if forever:
                        time.sleep(0.2)
            except OSError:
                return

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


def _get(port: int, path: str, origin: str | None = "https://beambam.boo", **headers: str):
    h = {k.replace("_", "-"): v for k, v in headers.items()}
    if origin:
        h["Origin"] = origin
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h)
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


def test_someone_elses_page_cannot_start_the_camera():
    """An <img> on a page the daemon does not allow arrives cross-site with no allowed Origin: 403, for the stream
    and the snapshot. The allowed pages use crossorigin images, which send their Origin."""
    port = _daemon(f"http://127.0.0.1:{_fake_camera()}")
    for path in ("/camera.mjpeg", "/snapshot.jpg"):
        assert _get(port, path, None, Sec_Fetch_Site="cross-site")[0] == 403
        assert _get(port, path, "https://evil.example", Sec_Fetch_Site="cross-site")[0] == 403
    code, body, _ = _get(port, "/camera.mjpeg", "https://beambam.boo", Sec_Fetch_Site="cross-site")
    assert code == 200 and FRAMES[0] in body
    assert _get(port, "/camera.mjpeg", None)[0] == 200          # curl, Home Assistant: no browser headers


def test_viewers_are_capped():
    from beambam.serve_http import CAMERA_MAX_VIEWERS
    port = _daemon(f"http://127.0.0.1:{_fake_camera(forever=True)}")
    held = []
    for _ in range(CAMERA_MAX_VIEWERS):
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/camera.mjpeg", timeout=10)
        r.read(10)
        held.append(r)
    assert _get(port, "/camera.mjpeg", None)[0] == 503
    held.pop().close()
    deadline = time.time() + 10
    while time.time() < deadline and _get(port, "/healthz")[0] != 200:
        time.sleep(0.1)
    for _ in range(50):                                          # the freed slot is taken again
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/camera.mjpeg", timeout=10)
            held.append(r)
            break
        except urllib.error.HTTPError:
            time.sleep(0.2)
    else:
        raise AssertionError("the closed viewer's slot was never freed")
    for r in held:
        r.close()


def test_a_bad_camera_url_is_not_listening():
    assert camera_listening("http://127.0.0.1:99999") is False
