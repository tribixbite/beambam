"""Tests for beambam.simulate — dry-run MQTT payload builder."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.simulate import (
    simulate_start_print,
    simulate_simple,
    simulate_light,
    _CapturingClient,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIRA = REPO_ROOT / "samples/x2d_cloud_print_mira_official.gcode.3mf"


# ---------- _CapturingClient ----------


def test_capturing_client_collects_publishes():
    c = _CapturingClient(serial="TEST123")
    assert c.creds.serial == "TEST123"
    c.publish({"x": 1})
    c.publish({"y": 2})
    assert len(c.captured) == 2
    assert c.captured[0] == {"x": 1}


# ---------- simulate_simple ----------


def test_simulate_pause_signed():
    p = simulate_simple("pause", sign=True)
    assert p["print"]["command"] == "pause"
    assert "header" in p
    assert p["header"]["sign_alg"] == "RSA_SHA256"
    assert p["header"]["cert_id"].startswith("GLOF")


def test_simulate_pause_unsigned():
    p = simulate_simple("pause", sign=False)
    assert p == {"print": {"sequence_id": "0", "command": "pause"}}
    assert "header" not in p


def test_simulate_gcode_includes_param():
    p = simulate_simple("gcode_line", param="G28 Z\n", sign=False)
    assert p["print"]["param"] == "G28 Z\n"


def test_simulate_ams_change_filament():
    p = simulate_simple("ams_change_filament", target=3, sign=False)
    assert p["print"]["target"] == 3
    assert p["print"]["curr_temp"] == 215


# ---------- simulate_light ----------


def test_simulate_light_on():
    p = simulate_light(on=True, sign=False)
    assert p["system"]["led_mode"] == "on"


def test_simulate_light_off():
    p = simulate_light(on=False, sign=False)
    assert p["system"]["led_mode"] == "off"


# ---------- simulate_start_print ----------


def test_simulate_start_print_single_slot():
    if not MIRA.is_file():
        pytest.skip("mira fixture missing")
    p = simulate_start_print(
        str(MIRA), ams_slot=5, bed_type="cool_plate", bed_temp=35,
        local_path=MIRA, sign=False,
    )
    pr = p["print"]
    assert pr["command"] == "project_file"
    assert pr["use_ams"] is True
    assert pr["ams_mapping"] == [5]
    assert pr["ams_mapping2"] == [{"ams_id": 1, "slot_id": 1}]
    assert pr["bed_type"] == "cool_plate"
    assert pr["bed_temp"] == 35


def test_simulate_start_print_multi_slot():
    if not MIRA.is_file():
        pytest.skip("mira fixture missing")
    p = simulate_start_print(
        str(MIRA), ams_slot=[1, 5], bed_type="cool_plate", bed_temp=35,
        local_path=MIRA, sign=False,
    )
    assert p["print"]["ams_mapping"] == [1, 5]
    assert p["print"]["ams_mapping2"] == [
        {"ams_id": 0, "slot_id": 1},
        {"ams_id": 1, "slot_id": 1},
    ]


def test_simulate_start_print_no_ams_external_spool():
    if not MIRA.is_file():
        pytest.skip("mira fixture missing")
    p = simulate_start_print(
        str(MIRA), use_ams=False, ams_slot=0,
        bed_type="cool_plate", bed_temp=35,
        local_path=MIRA, sign=False,
    )
    assert p["print"]["use_ams"] is False
    assert p["print"]["ams_mapping"] == []


def test_simulate_start_print_includes_signed_envelope_by_default():
    if not MIRA.is_file():
        pytest.skip("mira fixture missing")
    p = simulate_start_print(
        str(MIRA), ams_slot=5, bed_type="cool_plate", bed_temp=35,
        local_path=MIRA,
    )
    assert "header" in p
    assert p["header"]["sign_alg"] == "RSA_SHA256"
    assert p["header"]["payload_len"] > 0


def test_simulate_start_print_serial_propagates_to_dev_id():
    if not MIRA.is_file():
        pytest.skip("mira fixture missing")
    p = simulate_start_print(
        str(MIRA), ams_slot=0, serial="CUSTOM999",
        bed_type="cool_plate", bed_temp=35, local_path=MIRA, sign=False,
    )
    assert p["print"]["dev_id"] == "CUSTOM999"
