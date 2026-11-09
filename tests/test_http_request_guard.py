"""CSRF / DNS-rebinding guard and request validation for the daemon's
HTTP server (beambam.serve_http + beambam.http_validate).

Audit findings covered:

* P0 — with the default loopback bind and no token, any web page could
  POST /control/stop: text/plain bodies were parsed as JSON and nothing
  checked Origin or Host.
* P3 — a non-numeric Content-Length, a JSON array body or a mistyped
  field ("slot": "x") crashed the handler instead of answering 400.
"""
from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beambam import http_validate as HV  # noqa: E402
from beambam.serve_http import _serve_http  # noqa: E402


class _FakeClient:
    """Stands in for X2DClient: records publishes, never touches MQTT."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, payload: dict) -> None:
        self.published.append(payload)


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


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    client = _FakeClient()
    threading.Thread(
        target=_serve_http,
        kwargs={"bind": f"127.0.0.1:{port}",
                "get_state": lambda _p: {"print": {"gcode_state": "IDLE"}},
                "get_last_ts": lambda _p: time.time(),
                "printer_names": [""],
                "clients": {"": client},
                "allowed_hosts": ["printer.lan"]},
        daemon=True, name=f"guard-test-{port}").start()
    _wait_for_port(port)
    return port, client


def _request(port: int, method: str, path: str, *,
             body: bytes = b"", headers: dict[str, str] | None = None,
             host: str | None = None) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True,
                        skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None
                       else f"127.0.0.1:{port}")
        hdrs = dict(headers or {})
        hdrs.setdefault("Content-Length", str(len(body)))
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return resp.status, raw
    finally:
        conn.close()


def _json_post(port: int, path: str, payload: Any, **kw) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json", **kw.pop("headers", {})}
    return _request(port, "POST", path, body=json.dumps(payload).encode(),
                    headers=headers, **kw)


# --- CSRF: content type -------------------------------------------------------

def test_text_plain_control_post_is_refused(server):
    port, client = server
    before = len(client.published)
    status, body = _request(port, "POST", "/control/stop",
                            body=b"{}", headers={"Content-Type": "text/plain"})
    assert status == 415, body
    assert len(client.published) == before


@pytest.mark.parametrize("ctype", ["", "application/x-www-form-urlencoded",
                                   "multipart/form-data; boundary=x"])
def test_form_or_missing_content_type_is_refused(server, ctype):
    port, client = server
    before = len(client.published)
    headers = {"Content-Type": ctype} if ctype else {}
    status, _ = _request(port, "POST", "/control/pause", headers=headers)
    assert status == 415
    assert len(client.published) == before


def test_json_control_post_still_works(server):
    port, client = server
    before = len(client.published)
    status, body = _json_post(port, "/control/pause", {})
    assert status == 200, body
    assert len(client.published) == before + 1


def test_raw_route_requires_non_form_content_type(server):
    port, _ = server
    status, _ = _request(port, "POST", "/analyze", body=b"PK..",
                         headers={"Content-Type": "text/plain"})
    assert status == 415
    status, body = _request(port, "POST", "/analyze",
                            headers={"Content-Type":
                                     "application/octet-stream"})
    assert status == 400 and "Content-Length" in body["error"]


# --- CSRF: origin ---------------------------------------------------------------

def test_cross_origin_post_is_refused(server):
    port, client = server
    before = len(client.published)
    status, body = _json_post(port, "/control/stop", {},
                              headers={"Origin": "https://evil.example"})
    assert status == 403, body
    status, _ = _json_post(port, "/control/stop", {},
                           headers={"Origin": "null"})
    assert status == 403
    status, _ = _json_post(port, "/control/stop", {},
                           headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    # Another port on localhost is a different origin too.
    status, _ = _json_post(port, "/control/stop", {},
                           headers={"Origin": "http://127.0.0.1:1"})
    assert status == 403
    assert len(client.published) == before


def test_same_origin_post_is_allowed(server):
    port, client = server
    before = len(client.published)
    status, body = _json_post(
        port, "/control/resume", {},
        headers={"Origin": f"http://127.0.0.1:{port}",
                 "Sec-Fetch-Site": "same-origin"})
    assert status == 200, body
    assert len(client.published) == before + 1


# --- DNS rebinding: host --------------------------------------------------------

def test_foreign_host_is_refused_for_get_and_post(server):
    port, client = server
    status, body = _request(port, "GET", "/state",
                            host=f"rebind.evil.example:{port}")
    assert status == 421, body
    before = len(client.published)
    status, _ = _json_post(port, "/control/stop", {},
                           host=f"rebind.evil.example:{port}")
    assert status == 421
    assert len(client.published) == before


@pytest.mark.parametrize("host", ["localhost:{port}", "127.0.0.1:{port}",
                                  "[::1]:{port}", "192.168.1.50:{port}",
                                  "printer.lan:{port}", "PRINTER.LAN"])
def test_known_hosts_are_accepted(server, host):
    port, _ = server
    status, _ = _request(port, "GET", "/state",
                         host=host.format(port=port))
    assert status == 200


# --- body validation ---------------------------------------------------------------

def test_non_numeric_content_length_is_400(server):
    port, _ = server
    status, body = _request(port, "POST", "/control/light",
                            headers={"Content-Type": "application/json",
                                     "Content-Length": "abc"})
    assert status == 400 and "Content-Length" in body["error"]


def test_json_array_body_is_400(server):
    port, _ = server
    status, body = _json_post(port, "/control/light", ["on"])
    assert status == 400 and "JSON object" in body["error"]


def test_invalid_json_is_400(server):
    port, _ = server
    status, body = _request(port, "POST", "/control/light", body=b"{nope",
                            headers={"Content-Type": "application/json"})
    assert status == 400 and "JSON" in body["error"]


@pytest.mark.parametrize("path,payload", [
    ("/control/ams_load", {"slot": "x"}),
    ("/control/ams_load", {"slot": True}),
    ("/control/ams_load", {"slot": 2, "curr_temp": "hot"}),
    ("/control/light", {"state": "on", "on_time": "x"}),
    ("/control/temp", {"target": "bed", "value": True}),
    ("/control/temp", {"target": "nozzle", "value": 200, "idx": "a"}),
    ("/control/ams_set", {"slot": 1, "nozzle_temp_min": "x"}),
    ("/control/ams_set", {"slot": 1, "dry_run": "yes"}),
    ("/cloud/publish", {"serial": "S", "payload": {}, "timeout": "x"}),
    ("/assistant/chat", {"message": "status", "history": "x"}),
])
def test_bad_field_types_are_400(server, path, payload):
    port, client = server
    before = len(client.published)
    status, body = _json_post(port, path, payload)
    assert status == 400, body
    assert isinstance(body, dict) and body.get("error")
    assert len(client.published) == before


def test_oversize_json_body_is_413(server):
    port, _ = server
    status, body = _request(port, "POST", "/control/light",
                            headers={"Content-Type": "application/json",
                                     "Content-Length": str(10 * 1024 * 1024)})
    assert status == 413, body


def test_cloud_state_bad_timeout_is_400(server):
    port, _ = server
    status, body = _request(port, "GET", "/cloud/state?timeout=abc")
    assert status == 400, body


# --- unit: helpers ---------------------------------------------------------------------

def test_guard_unit_rules():
    allowed = HV.allowed_host_names("127.0.0.1", ["box.lan"])
    ok = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
    assert HV.request_guard_error("POST", "/control/stop", ok, allowed) is None
    assert HV.request_guard_error(
        "GET", "/state", {"Host": "evil.com"}, allowed)[0] == 421
    assert HV.request_guard_error(
        "GET", "/state", {}, allowed) is None            # HTTP/1.0, no Host
    assert HV.request_guard_error(
        "POST", "/analyze", {"Host": "box.lan",
                             "Content-Type": "application/octet-stream"},
        allowed) is None
    assert HV.request_guard_error(
        "POST", "/control/stop",
        {"Host": "box.lan:1", "Content-Type": "application/json; charset=utf-8",
         "Origin": "http://box.lan:1"}, allowed) is None


def test_allowed_hosts_env(monkeypatch):
    monkeypatch.setenv("X2D_ALLOWED_HOSTS", "a.example, B.example")
    allowed = HV.allowed_host_names("0.0.0.0")
    assert {"a.example", "b.example", "localhost"} <= allowed
    assert "0.0.0.0" not in allowed


@pytest.mark.parametrize("value,ok", [
    ("studio", True), ("_default", True), ("rumi_frame.gcode.3mf", True),
    ("job_2", True), ("..", False), (".hidden", False), ("", False),
    ("a/b", False), ("a\\b", False), ("C:", False), ("x" * 121, False),
])
def test_is_safe_slug(value, ok):
    assert HV.is_safe_slug(value) is ok


def test_getters():
    assert HV.get_int({"a": 2.0}, "a") == 2
    assert HV.get_int({}, "a", 5) == 5
    with pytest.raises(HV.BadRequest):
        HV.get_int({"a": 2.5}, "a")
    with pytest.raises(HV.BadRequest):
        HV.get_int({"a": 9}, "a", lo=0, hi=3)
    with pytest.raises(HV.BadRequest):
        HV.get_int({}, "a")
    assert HV.get_optional_int({"a": None}, "a") is None
    assert HV.get_float({"a": 1}, "a") == 1.0
    with pytest.raises(HV.BadRequest):
        HV.get_float({"a": float("nan")}, "a")
    assert HV.get_str({"a": "x"}, "a") == "x"
    with pytest.raises(HV.BadRequest):
        HV.get_str({"a": 1}, "a")
    with pytest.raises(HV.BadRequest):
        HV.get_str({"a": "xx"}, "a", max_len=1)
    assert HV.get_bool({"a": True}, "a") is True
    with pytest.raises(HV.BadRequest):
        HV.get_bool({"a": 1}, "a")
    with pytest.raises(HV.BadRequest) as ei:
        HV.content_length({"Content-Length": "99"}, 10)
    assert ei.value.status == 413
