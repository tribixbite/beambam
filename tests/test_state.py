"""beambam.state + X2DClient's merged state and reply waiting (no network)."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from beambam.config import Creds
from beambam.mqtt import X2DClient
from beambam.state import command_reply, deep_merge, is_full_report

FULL = {"print": {"command": "push_status", "gcode_state": "RUNNING", "mc_percent": 40,
                  "ams": {"ams": [{"id": "0", "tray": [{"id": "0"}]}]}, "nozzle_temper": 240}}


def test_deep_merge_keeps_fields_a_delta_omits() -> None:
    state: dict = {}
    deep_merge(state, FULL)
    deep_merge(state, {"print": {"command": "push_status", "mc_percent": 41}})
    assert state["print"]["gcode_state"] == "RUNNING"
    assert state["print"]["mc_percent"] == 41
    assert state["print"]["ams"]["ams"][0]["id"] == "0"


def test_deep_merge_replaces_lists_and_copies() -> None:
    state: dict = {}
    src = {"print": {"hms": [{"attr": 1}, {"attr": 2}]}}
    deep_merge(state, src)
    deep_merge(state, {"print": {"hms": []}})       # errors cleared -> gone
    assert state["print"]["hms"] == []
    src["print"]["hms"].append({"attr": 3})         # caller mutation does not leak in
    assert state["print"]["hms"] == []


def test_is_full_report() -> None:
    assert is_full_report(FULL)
    assert not is_full_report({"print": {"command": "push_status", "mc_percent": 3}})
    assert not is_full_report({"system": {"command": "ledctrl", "result": "success"}})


def test_command_reply_classification() -> None:
    ok = command_reply({"system": {"command": "ledctrl", "result": "success"}}, "system", "ledctrl")
    assert ok is not None and ok.ok
    bad = command_reply({"print": {"command": "pause", "err_code": 84033543,
                                   "reason": "mqtt message verify failed", "result": "failed"}},
                        "print", "pause")
    assert bad is not None and not bad.ok and bad.err_code == 84033543
    # A push_status is not a reply, and another command is not this one.
    assert command_reply(FULL, "print", "push_status") is None
    assert command_reply({"print": {"command": "resume", "result": "success"}}, "print", "pause") is None


def _client() -> X2DClient:
    return X2DClient(Creds(ip="127.0.0.1", code="x", serial="TESTSERIAL"))


def _deliver(c: X2DClient, payload: dict) -> None:
    c._on_message(None, None, SimpleNamespace(payload=json.dumps(payload).encode()))


def test_client_merges_and_waits_for_later_reply(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(X2DClient, "_TS_DIR", tmp_path)
    c = _client()
    _deliver(c, FULL)
    _deliver(c, {"print": {"command": "push_status", "mc_percent": 55}})
    merged = c.merged_state()
    assert merged["print"]["gcode_state"] == "RUNNING" and merged["print"]["mc_percent"] == 55

    since = c.message_seq
    threading.Timer(0.05, _deliver, args=(c, {"system": {"command": "ledctrl",
                                                          "result": "success"}})).start()
    t0 = time.monotonic()
    reply = c.wait_reply("system", "ledctrl", timeout=2.0, since=since)
    assert reply is not None and reply.ok
    assert time.monotonic() - t0 < 1.5


def test_client_wait_ignores_messages_before_since(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(X2DClient, "_TS_DIR", tmp_path)
    c = _client()
    _deliver(c, {"system": {"command": "ledctrl", "result": "success"}})
    assert c.wait_reply("system", "ledctrl", timeout=0.1) is None     # since = now
    assert c.wait_reply("system", "ledctrl", timeout=0.1, since=0) is not None


def test_request_state_skips_a_delta_that_arrives_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(X2DClient, "_TS_DIR", tmp_path)
    c = _client()

    def fake_publish(payload, qos=1, **kw):
        # The printer streams a delta before answering pushall.
        threading.Timer(0.02, _deliver, args=(c, {"print": {"command": "push_status",
                                                           "mc_percent": 7}})).start()
        threading.Timer(0.08, _deliver, args=(c, FULL)).start()

    monkeypatch.setattr(c, "publish", fake_publish)
    state = c.request_state(timeout=2.0)
    assert "ams" in state["print"] and state["print"]["gcode_state"] == "RUNNING"


def test_on_disconnect_clears_connected_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(X2DClient, "_TS_DIR", tmp_path)
    c = _client()
    c._connected.set()
    c._on_disconnect(None, None, 1)          # lost connection (rc != 0)
    assert not c._connected.is_set()
    # A paho ReasonCode-like object with .value is handled too.
    c._connected.set()
    c._on_disconnect(None, None, type("RC", (), {"value": 7})())
    assert not c._connected.is_set()
