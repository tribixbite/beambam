"""tests/test_cloud_state.py — robust cloud state reader (AMS fix).

The bug: the printer's full pushall state arrives across several MQTT report
messages — the first is often a tiny ~5-key delta carrying `gcode_state`, while
the ~100-key snapshot with `ams` comes a beat later. A reader that returns on
the first message (as the old `_cloud_printer_state` did) silently dropped AMS.
These tests pin the completion heuristic + the LAN→cloud fallback.
"""
from __future__ import annotations

import argparse
import types

from beambam.cli import cloud as CL


def test_state_is_complete_rejects_first_delta():
    # A small first delta that happens to include gcode_state must NOT be
    # treated as complete — that's exactly the bug.
    delta = {"gcode_state": "RUNNING", "mc_percent": 12,
             "subtask_id": "1", "command": "push_status", "msg": 1}
    assert CL._state_is_complete(delta) is False


def test_state_is_complete_accepts_full_snapshot():
    snap = {f"k{i}": i for i in range(60)}
    snap["gcode_state"] = "IDLE"
    snap["ams"] = {"ams": []}
    assert CL._state_is_complete(snap) is True


def test_state_is_complete_requires_all_keys():
    snap = {f"k{i}": i for i in range(60)}      # 60 keys but no gcode_state
    assert CL._state_is_complete(snap) is False
    assert CL._state_is_complete(snap, require=("k0", "k1")) is True


def test_merge_across_messages_lands_full_state():
    """Simulate the on_message merge: delta first, snapshot second."""
    merged: dict = {}
    delta = {"gcode_state": "IDLE", "mc_percent": 0}
    snapshot = {f"k{i}": i for i in range(80)}
    snapshot["gcode_state"] = "IDLE"
    snapshot["ams"] = {"ams": [{"id": "0", "tray": [{"id": "1",
                                                     "tray_type": "PETG"}]}]}
    for msg in (delta, snapshot):
        merged.update(msg)
    assert CL._state_is_complete(merged)
    assert "ams" in merged                       # the whole point


# --- LAN → cloud fallback ----------------------------------------------------

def test_fetch_printer_state_uses_lan_first(monkeypatch):
    from beambam import ams
    sentinel = {"print": {"ams": {"ams": []}, "gcode_state": "IDLE"}}

    class _P:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def state(self, timeout=10.0): return sentinel
    monkeypatch.setattr("beambam.Printer", _P)
    out = ams.fetch_printer_state(argparse.Namespace())
    assert out is sentinel


def test_fetch_printer_state_falls_back_to_cloud(monkeypatch):
    from beambam import ams

    class _P:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def state(self, timeout=10.0): raise OSError("LAN down")
    monkeypatch.setattr("beambam.Printer", _P)

    sess = types.SimpleNamespace(empty=False)
    fake_cc = types.SimpleNamespace(
        CloudClient=types.SimpleNamespace(load_or_anonymous=lambda: types.SimpleNamespace(session=sess)))
    monkeypatch.setitem(__import__("sys").modules, "cloud_client", fake_cc)
    monkeypatch.setattr("beambam.config.Creds.resolve",
                        classmethod(lambda cls, a: types.SimpleNamespace(serial="S1")))
    monkeypatch.setattr("beambam.cli.cloud.cloud_pull_state",
                        lambda cli, serial, **k: {"ams": {"ams": []},
                                                  "gcode_state": "IDLE"})
    out = ams.fetch_printer_state(argparse.Namespace())
    assert out is not None
    assert out["print"]["gcode_state"] == "IDLE"


def test_fetch_printer_state_both_fail_returns_none(monkeypatch):
    from beambam import ams

    class _P:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def state(self, timeout=10.0): raise OSError("LAN down")
    monkeypatch.setattr("beambam.Printer", _P)
    sess = types.SimpleNamespace(empty=True)        # no cloud session
    fake_cc = types.SimpleNamespace(
        CloudClient=types.SimpleNamespace(load_or_anonymous=lambda: types.SimpleNamespace(session=sess)))
    monkeypatch.setitem(__import__("sys").modules, "cloud_client", fake_cc)
    assert ams.fetch_printer_state(argparse.Namespace()) is None
