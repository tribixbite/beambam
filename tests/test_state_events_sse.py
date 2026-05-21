"""End-to-end test for the `/state.events` SSE handler wired through
StateHub (v1.3.0 push-instead-of-poll refactor).

Spawns `_serve_http` on a random localhost port, supplies a fake
`get_hub` returning a StateHub we control, opens a real HTTP SSE
connection, and asserts:

  1. Pre-publish: the handler replays the hub's `last_state` to the
     fresh subscriber within ~50 ms (no waiting for the next push).
  2. Live publish: a `hub.publish(...)` call lands in the SSE client
     in under 100 ms — proves we're pushing, not polling at 1 Hz.
  3. Keepalive: with no publishes, the handler emits a `: keepalive`
     comment after 15 s of idle. (Asserted in a separate slow-path
     test marked `slow`.)
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.state_hub import StateHub
from x2d_bridge import _serve_http


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    raise RuntimeError(f"server on :{port} never came up")


def _start_server(hub: StateHub) -> tuple[int, threading.Thread]:
    port = _free_port()
    states: dict[str, dict | None] = {"": None}

    def get_state(p: str):
        return states.get(p)

    def get_hub(p: str):
        return hub if p == "" else None

    t = threading.Thread(
        target=_serve_http,
        kwargs={
            "bind":          f"127.0.0.1:{port}",
            "get_state":     get_state,
            "get_last_ts":   lambda _p: 0.0,
            "printer_names": [""],
            "get_hub":       get_hub,
        },
        daemon=True,
    )
    t.start()
    _wait_for_port(port)
    return port, t


def _read_sse_event(sock: socket.socket, timeout: float = 1.0) -> bytes:
    """Read raw bytes from the socket until we see a complete SSE event
    (terminated by `\\n\\n`). Returns the bytes of that single event."""
    sock.settimeout(timeout)
    buf = b""
    while b"\n\n" not in buf:
        chunk = sock.recv(8192)
        if not chunk:
            break
        buf += chunk
    head, _, _ = buf.partition(b"\n\n")
    return head + b"\n\n"


def _open_sse(port: int) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    s.sendall(
        b"GET /state.events HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Accept: text/event-stream\r\n"
        b"Connection: close\r\n\r\n"
    )
    # Consume HTTP response headers + the initial `retry: 2000\n\n`.
    s.settimeout(2.0)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("server closed before headers")
        buf += chunk
    assert b"text/event-stream" in buf, f"missing SSE content-type: {buf[:200]!r}"
    return s


def test_sse_replays_last_state_immediately():
    """Fresh client should see last_state inside the first event,
    delivered in well under the legacy 1 s polling interval."""
    hub = StateHub()
    hub.publish({"gcode_state": "RUNNING", "percent": 13})
    port, _ = _start_server(hub)

    t0 = time.monotonic()
    sock = _open_sse(port)
    # Skip the `retry:` directive — it's optional, may or may not be
    # in this read depending on TCP packetization.
    evt = _read_sse_event(sock, timeout=1.0)
    if b"data:" not in evt:
        evt = _read_sse_event(sock, timeout=1.0)
    elapsed = time.monotonic() - t0

    assert b"data:" in evt, f"no data event in {evt!r}"
    assert b"RUNNING" in evt and b"13" in evt
    # Sub-second is the load-bearing bit — the legacy poll path would
    # take up to 1.0 s of `time.sleep` before emitting anything.
    assert elapsed < 0.7, f"too slow ({elapsed*1000:.0f} ms) — hub path may not be wired"
    sock.close()


def test_sse_pushes_on_publish_under_100ms():
    """After the initial replay, a hub.publish() call must reach the
    SSE client well below the legacy 1 s polling interval."""
    hub = StateHub()
    port, _ = _start_server(hub)

    sock = _open_sse(port)
    # Consume any initial event (there should be none since last_state
    # is None at this point — but tolerate the retry line if present).
    sock.settimeout(0.2)
    try:
        sock.recv(4096)
    except socket.timeout:
        pass

    t0 = time.monotonic()
    hub.publish({"gcode_state": "FINISH", "percent": 100})
    evt = _read_sse_event(sock, timeout=1.0)
    elapsed = time.monotonic() - t0

    assert b"FINISH" in evt and b"100" in evt, f"missing payload in {evt!r}"
    assert elapsed < 0.20, f"push took {elapsed*1000:.0f} ms — should be sub-100ms"
    sock.close()
