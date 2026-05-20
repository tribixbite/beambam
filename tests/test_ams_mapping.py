#!/usr/bin/env python3
"""Unit tests for the ams_mapping / ams_mapping2 wire shape that
x2d_bridge.start_print emits.

Single-color (one-filament) prints and multi-color (multi-filament,
N-extruder X2D) prints exercise different shapes:

  Single  ams_mapping  = [slot]
          ams_mapping2 = [{"ams_id": slot//4, "slot_id": slot%4}]

  Multi   ams_mapping  = [slot_0, slot_1, ...]
          ams_mapping2 = [{"ams_id":..., "slot_id":...}, ...]

The X2D firmware reads ams_mapping2 (the newer form) but rejects
prints whose mapping length doesn't equal the filament count, so this
test pins both shapes against expected values.

Runs offline — no printer needed (mocks publish out). All tests pass
explicit bed_type + bed_temp so the start_print safety check
(refuses prints without a known plate / heat profile) doesn't fire."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# All tests use a known-safe plate config — Cool Plate at 35 °C for PLA.
# The values don't matter for the wire-shape assertions; they just need
# to be non-None so PrintRefusal doesn't trigger.
_SAFE_BED = {"bed_type": "cool_plate", "bed_temp": 35}


class _MockClient:
    """Stand-in for x2d_bridge.X2DClient that captures the published payload."""
    def __init__(self):
        self.creds = types.SimpleNamespace(serial="00M09A000000000")
        self.published: dict | None = None

    def publish(self, payload: dict, qos: int = 1) -> None:
        self.published = payload


def test_single_filament_int_slot():
    import x2d_bridge
    cli = _MockClient()
    x2d_bridge.start_print(cli, "test.gcode.3mf", use_ams=True, ams_slot=3,
                           **_SAFE_BED)
    p = cli.published["print"]
    assert p["ams_mapping"] == [3]
    assert p["ams_mapping2"] == [{"ams_id": 0, "slot_id": 3}]
    assert p["use_ams"] is True


def test_single_filament_global_slot_into_ams1():
    """slot 5 is AMS 1, tray 1 — verifies the //4 / %4 split for AMS#≥1."""
    import x2d_bridge
    cli = _MockClient()
    x2d_bridge.start_print(cli, "test.gcode.3mf", use_ams=True, ams_slot=5,
                           **_SAFE_BED)
    p = cli.published["print"]
    assert p["ams_mapping2"] == [{"ams_id": 1, "slot_id": 1}]


def test_multi_filament_list():
    """Two-color print: filament 0 -> AMS0 tray 1; filament 1 -> AMS1 tray 1."""
    import x2d_bridge
    cli = _MockClient()
    x2d_bridge.start_print(cli, "two_color.gcode.3mf", use_ams=True,
                           ams_slot=[1, 5], **_SAFE_BED)
    p = cli.published["print"]
    assert p["ams_mapping"] == [1, 5]
    assert p["ams_mapping2"] == [
        {"ams_id": 0, "slot_id": 1},
        {"ams_id": 1, "slot_id": 1},
    ]


def test_no_ams_keeps_mappings_empty():
    import x2d_bridge
    cli = _MockClient()
    x2d_bridge.start_print(cli, "ext_spool.gcode.3mf", use_ams=False,
                           ams_slot=0, **_SAFE_BED)
    p = cli.published["print"]
    assert p["use_ams"] is False
    assert p["ams_mapping"] == []
    assert p["ams_mapping2"] == []


def test_use_ams_true_empty_list_rejects():
    """Passing use_ams=True with ams_slot=[] is a programmer error — must
    raise rather than silently produce a malformed payload that the
    firmware would drop without an HMS code."""
    import pytest
    import x2d_bridge
    cli = _MockClient()
    with pytest.raises(ValueError):
        x2d_bridge.start_print(cli, "test.gcode.3mf", use_ams=True,
                               ams_slot=[], **_SAFE_BED)


def test_signed_publish_envelope_shape():
    """Quick sanity check that the published payload is the inner dict
    BEFORE signing — sign_payload wraps it; we want to verify nothing
    accidentally moves the print block to a different key."""
    import x2d_bridge
    cli = _MockClient()
    x2d_bridge.start_print(cli, "test.gcode.3mf", use_ams=True, ams_slot=0,
                           **_SAFE_BED)
    assert set(cli.published.keys()) == {"print"}
