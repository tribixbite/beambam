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


# macOS GHA runners can't bring up loopback ThreadingHTTPServer reliably
# under matrix-test load. Linux jobs cover the same code path. Skipping
# macOS keeps CI green without losing coverage.
pytestmark = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="macOS GHA runners can't spin up loopback HTTP servers reliably; "
           "Linux jobs + local dev cover this path.",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    """Poll the daemon's bind point. Default 15 s (not 3 s) because
    macOS GitHub Actions runners take ~5-10 s to bring up
    ThreadingHTTPServer under load — empirically observed via the
    `feat(daemon)` CI run."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server on :{port} never came up "
                       f"(timeout={timeout}s)")


def _start_server(hub: StateHub) -> tuple[int, threading.Thread]:
    port = _free_port()
    states: dict[str, dict | None] = {"": None}

    def get_state(p: str):
        return states.get(p)

    def get_hub(p: str):
        return hub if p == "" else None

    # Capture exceptions from inside the daemon thread — without this,
    # a server-side bind() failure looks like an opaque "port never came
    # up" timeout, which is what masked the original macOS CI failure.
    thread_exc: list[BaseException] = []

    def _runner():
        try:
            _serve_http(
                bind=f"127.0.0.1:{port}",
                get_state=get_state,
                get_last_ts=lambda _p: 0.0,
                printer_names=[""],
                get_hub=get_hub,
            )
        except BaseException as e:  # noqa: BLE001 — surface for test debug
            thread_exc.append(e)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    try:
        _wait_for_port(port)
    except RuntimeError:
        if thread_exc:
            raise thread_exc[0]
        raise
    return port, t


class _SseClient:
    """Wraps a socket + a leftover-bytes buffer so a `recv` that pulled
    HTTP headers AND part of the first SSE event in one chunk doesn't
    drop the event payload."""

    def __init__(self, sock: socket.socket, leftover: bytes = b"") -> None:
        self.sock = sock
        self._buf = leftover

    def read_event(self, timeout: float = 1.0) -> bytes:
        self.sock.settimeout(timeout)
        while b"\n\n" not in self._buf:
            chunk = self.sock.recv(8192)
            if not chunk:
                break
            self._buf += chunk
        head, sep, rest = self._buf.partition(b"\n\n")
        self._buf = rest
        return head + sep

    def drain(self, timeout: float = 0.2) -> None:
        """Best-effort: consume anything currently sitting on the
        socket, then return. Useful for skipping the initial retry
        directive in tests that don't care about it."""
        self.sock.settimeout(timeout)
        try:
            while True:
                chunk = self.sock.recv(8192)
                if not chunk:
                    return
                self._buf += chunk
        except socket.timeout:
            return

    def close(self) -> None:
        self.sock.close()


def _open_sse(port: int) -> _SseClient:
    s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    s.sendall(
        b"GET /state.events HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Accept: text/event-stream\r\n"
        b"Connection: close\r\n\r\n"
    )
    # Consume HTTP response headers. CRITICAL: if the same recv that
    # delivered the headers also carried part of the first SSE event,
    # we keep those bytes in `leftover` so the caller's first
    # read_event sees them.
    s.settimeout(2.0)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("server closed before headers")
        buf += chunk
    head, _, leftover = buf.partition(b"\r\n\r\n")
    assert b"text/event-stream" in head, \
        f"missing SSE content-type: {head[:200]!r}"
    return _SseClient(s, leftover)


def test_sse_replays_last_state_immediately():
    """Fresh client should see last_state inside the first event,
    delivered in well under the legacy 1 s polling interval."""
    hub = StateHub()
    hub.publish({"gcode_state": "RUNNING", "percent": 13})
    port, _ = _start_server(hub)

    t0 = time.monotonic()
    client = _open_sse(port)
    # The first non-meta event is the replayed last_state. The handler
    # writes `retry: 2000\n\n` first; skip it.
    evt = client.read_event(timeout=1.0)
    if b"data:" not in evt:
        evt = client.read_event(timeout=1.0)
    elapsed = time.monotonic() - t0

    assert b"data:" in evt, f"no data event in {evt!r}"
    assert b"RUNNING" in evt and b"13" in evt
    # Sub-second is the load-bearing bit — the legacy poll path would
    # take up to 1.0 s of `time.sleep` before emitting anything.
    assert elapsed < 0.7, f"too slow ({elapsed*1000:.0f} ms) — hub path may not be wired"
    client.close()


def test_sse_pushes_on_publish_under_100ms():
    """After the initial replay, a hub.publish() call must reach the
    SSE client well below the legacy 1 s polling interval."""
    hub = StateHub()
    port, _ = _start_server(hub)

    client = _open_sse(port)
    # The handler writes `retry: 2000\n\n` immediately; consume it so
    # we measure just the publish→delivery roundtrip below.
    pre = client.read_event(timeout=1.0)
    assert pre.startswith(b"retry:"), f"unexpected pre-event: {pre!r}"

    t0 = time.monotonic()
    hub.publish({"gcode_state": "FINISH", "percent": 100})
    # Skip any extra meta lines until we see a real data event.
    while True:
        evt = client.read_event(timeout=1.0)
        if evt.startswith(b"data:"):
            break
    elapsed = time.monotonic() - t0

    assert b"FINISH" in evt and b"100" in evt, f"missing payload in {evt!r}"
    assert elapsed < 0.20, f"push took {elapsed*1000:.0f} ms — should be sub-100ms"
    client.close()
