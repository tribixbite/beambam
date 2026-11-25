"""`beambam daemon --http` exits when its HTTP API cannot bind.

The API runs on a thread; a bind failure (port taken) used to end only that
thread, so the daemon kept running MQTT with no API while requests reached
whatever held the port.
"""
from __future__ import annotations

import argparse
import signal
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import beambam.config as config  # noqa: E402
import beambam.mqtt as mqtt  # noqa: E402
from beambam.cli import daemon  # noqa: E402


class _FakeClient:
    """Stands in for X2DClient: connects instantly, never touches MQTT."""

    last_message_ts = 0.0

    def __init__(self, creds, on_state=None) -> None:
        self.creds = creds

    def connect(self) -> None:
        pass

    def publish(self, payload: dict) -> None:
        pass

    def disconnect(self) -> None:
        pass


def test_daemon_exits_nonzero_when_http_port_is_taken(monkeypatch):
    creds = argparse.Namespace(ip="192.0.2.1", code="x", serial="S")
    monkeypatch.setattr(config.Creds, "list_names", staticmethod(lambda: []))
    monkeypatch.setattr(config.Creds, "resolve", staticmethod(lambda _ns: creds))
    monkeypatch.setattr(mqtt, "X2DClient", _FakeClient)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        args = argparse.Namespace(
            printer=None, http=f"127.0.0.1:{port}", interval=1, quiet=True,
            max_staleness=30.0, auth_token=None, queue=False, timelapse=False,
            allow_host=None, cors_origin=None)
        result: list[int] = []
        t = threading.Thread(target=lambda: result.append(daemon.cmd_daemon(args)),
                             daemon=True)
        # cmd_daemon installs SIGINT/SIGTERM handlers, which only the main
        # thread may do; it runs on a thread here so a regression (the
        # daemon running on) fails the join instead of hanging the suite.
        monkeypatch.setattr(signal, "signal", lambda *_a: None)
        t.start()
        t.join(20)
    assert result == [1]
