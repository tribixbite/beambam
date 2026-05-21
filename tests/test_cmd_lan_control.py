"""tests/test_cmd_lan_control.py — LAN print-control cmd_* handlers.

Audit (this session) found 63/67 cmd_* in x2d_bridge.py have no direct
test. This file covers the LAN print-control tier — all the verbs that
go through `_publish_one()` → X2DClient.publish:

  cmd_pause / cmd_resume / cmd_stop          — print job control
  cmd_gcode / cmd_home / cmd_level           — gcode injection
  cmd_set_temp (bed / nozzle / chamber)      — heater control
  cmd_chamber_light (on / off / flashing)    — LED control
  cmd_jog (X / Y / Z / E)                    — manual motion

Each handler:
  1. Builds a payload dict via `_print_cmd(...)` or `_system_cmd(...)`
  2. Calls `_publish_one(args, payload)` → X2DClient.publish

We mock X2DClient by replacing `x2d_bridge.X2DClient` with a capturing
shim (same pattern as tests/test_ipcam_commands.py). The payload is
captured; we assert verb + relevant params. Sequence-id changes per
test invocation (global counter), so we don't pin it — just assert
its presence."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


# ----- shared fixtures ----------------------------------------------------


class _CapturingClient:
    """X2DClient stand-in — collects published payloads."""

    def __init__(self, *_args, **_kwargs):
        self.published: list[dict] = []
        self.connected = False

    def connect(self, *_args, **_kwargs):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def publish(self, payload: dict, qos: int = 1, **_kw):
        self.published.append(payload)


@pytest.fixture
def captured(monkeypatch):
    """Hijack x2d_bridge.X2DClient so every _publish_one() instance writes
    into our shared list. Yields the list."""
    import x2d_bridge

    bucket: list[dict] = []

    class _Cli(_CapturingClient):
        def publish(self, payload, qos=1, **kw):
            bucket.append(payload)

    monkeypatch.setattr(x2d_bridge, "X2DClient", _Cli)
    return bucket


def _args(**kw) -> argparse.Namespace:
    """argparse.Namespace with the cred fields Creds.resolve() expects."""
    defaults = dict(
        ip="192.168.0.42",
        code="abcdef12",
        serial="00P9AJ000000000",
        printer=None,
        config=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ----- simple print-control verbs (pause / resume / stop) ----------------


@pytest.mark.parametrize("fn_name,expected_verb", [
    ("cmd_pause", "pause"),
    ("cmd_resume", "resume"),
    ("cmd_stop", "stop"),
])
def test_simple_print_control_verbs(captured, fn_name, expected_verb):
    """pause / resume / stop emit `{"print":{"command":<verb>,...}}`."""
    import x2d_bridge

    rc = getattr(x2d_bridge, fn_name)(_args())
    assert rc == 0
    assert len(captured) == 1
    payload = captured[0]
    assert "print" in payload, payload
    body = payload["print"]
    assert body["command"] == expected_verb
    assert body["param"] == ""
    assert "sequence_id" in body


# ----- gcode injection (cmd_gcode / cmd_home / cmd_level) ----------------


def test_cmd_gcode_appends_newline_if_missing(captured):
    """`cmd_gcode` must terminate the line with `\\n` — Bambu firmware
    silently drops unterminated commands."""
    import x2d_bridge

    rc = x2d_bridge.cmd_gcode(_args(gcode="G1 X10"))
    assert rc == 0
    body = captured[0]["print"]
    assert body["command"] == "gcode_line"
    assert body["param"] == "G1 X10\n"


def test_cmd_gcode_preserves_existing_newline(captured):
    """If the user already added \\n, don't double-up."""
    import x2d_bridge

    rc = x2d_bridge.cmd_gcode(_args(gcode="M104 S200\n"))
    assert rc == 0
    assert captured[0]["print"]["param"] == "M104 S200\n"


def test_cmd_home_sends_G28(captured):
    import x2d_bridge

    rc = x2d_bridge.cmd_home(_args())
    assert rc == 0
    body = captured[0]["print"]
    assert body["command"] == "gcode_line"
    assert body["param"] == "G28\n"


def test_cmd_level_sends_G29(captured):
    """G29 = canonical auto-level gcode that X2D firmware accepts."""
    import x2d_bridge

    rc = x2d_bridge.cmd_level(_args())
    assert rc == 0
    assert captured[0]["print"]["param"] == "G29\n"


# ----- cmd_set_temp ------------------------------------------------------


def test_set_temp_bed_uses_set_bed_temp_verb(captured):
    import x2d_bridge

    rc = x2d_bridge.cmd_set_temp(_args(target="bed", value=60, idx=0))
    assert rc == 0
    body = captured[0]["print"]
    assert body["command"] == "set_bed_temp"
    assert body["temp"] == 60


def test_set_temp_nozzle_uses_set_nozzle_temp_with_extruder_index(captured):
    import x2d_bridge

    rc = x2d_bridge.cmd_set_temp(_args(target="nozzle", value=220, idx=1))
    assert rc == 0
    body = captured[0]["print"]
    assert body["command"] == "set_nozzle_temp"
    assert body["target_temp"] == 220
    assert body["extruder_index"] == 1


def test_set_temp_chamber_falls_back_to_M141_gcode(captured):
    """No native chamber-temp MQTT verb in BambuStudio source — handler
    falls back to gcode_line + M141 Sxxx."""
    import x2d_bridge

    rc = x2d_bridge.cmd_set_temp(_args(target="chamber", value=45, idx=0))
    assert rc == 0
    body = captured[0]["print"]
    assert body["command"] == "gcode_line"
    assert body["param"] == "M141 S45\n"


def test_set_temp_unknown_target_exits(captured):
    """Unknown --target should sys.exit (caught by argparse usually, but
    the handler validates again defensively)."""
    import x2d_bridge

    with pytest.raises(SystemExit):
        x2d_bridge.cmd_set_temp(_args(target="hotend", value=200, idx=0))


# ----- cmd_chamber_light ------------------------------------------------


@pytest.mark.parametrize("state", ["on", "off", "flashing"])
def test_chamber_light_valid_states(captured, state):
    """All three valid states emit a `{"system":{"command":"ledctrl",...}}`
    with the correct led_mode."""
    import x2d_bridge

    args = _args(state=state, on_time=500, off_time=500, loops=1, interval=1000)
    rc = x2d_bridge.cmd_chamber_light(args)
    assert rc == 0
    payload = captured[0]
    assert "system" in payload
    body = payload["system"]
    assert body["command"] == "ledctrl"
    assert body["led_node"] == "chamber_light"
    assert body["led_mode"] == state


def test_chamber_light_invalid_state_exits():
    """state must be on/off/flashing — anything else sys.exits."""
    import x2d_bridge

    with pytest.raises(SystemExit):
        x2d_bridge.cmd_chamber_light(_args(state="strobe", on_time=0,
                                            off_time=0, loops=0, interval=0))


def test_chamber_light_state_is_case_insensitive(captured):
    """`chamber-light ON` / `chamber-light On` should also work — handler
    lowercases before validating."""
    import x2d_bridge

    rc = x2d_bridge.cmd_chamber_light(_args(state="ON", on_time=0, off_time=0,
                                             loops=0, interval=0))
    assert rc == 0
    assert captured[0]["system"]["led_mode"] == "on"


# ----- cmd_jog ----------------------------------------------------------


@pytest.mark.parametrize("axis,distance,feed", [
    ("X", 10.0, 3000),
    ("y", -5.5, 1500),  # lowercase axis should be uppercased
    ("Z", 0.2, 600),
    ("E", 5.0, 300),
])
def test_jog_builds_relative_gcode_sequence(captured, axis, distance, feed):
    """Jog uses standard G91 (relative) / G1 / G90 (absolute) — same shape
    works on every Bambu firmware that accepts arbitrary gcode."""
    import x2d_bridge

    rc = x2d_bridge.cmd_jog(_args(axis=axis, distance=distance, feed=feed))
    assert rc == 0
    body = captured[0]["print"]
    assert body["command"] == "gcode_line"
    param = body["param"]
    assert param.startswith("G91\n")
    assert param.endswith("G90\n")
    # G1 line: axis(upper)+distance(no trailing zeros via :g) F<feed>
    assert f"G1 {axis.upper()}" in param
    assert f"F{feed}" in param


def test_jog_invalid_axis_exits():
    """Only X/Y/Z/E are valid axes."""
    import x2d_bridge

    with pytest.raises(SystemExit):
        x2d_bridge.cmd_jog(_args(axis="W", distance=1.0, feed=600))


# ----- coverage smoke: every handler advanced the seq counter -----------


def test_each_publish_has_unique_sequence_id(captured):
    """Sanity: two back-to-back commands must have different sequence_id
    (the seq counter is global and monotonic)."""
    import x2d_bridge

    x2d_bridge.cmd_pause(_args())
    x2d_bridge.cmd_resume(_args())
    assert len(captured) == 2
    seq1 = captured[0]["print"]["sequence_id"]
    seq2 = captured[1]["print"]["sequence_id"]
    assert seq1 != seq2
    # Sequence ids are stringified ints; int(seq2) > int(seq1).
    assert int(seq2) > int(seq1)
