"""tests/test_ha_derive.py — what the Home Assistant bridge reads from an X2D.

`runtime/ha/publisher.derive_state` turns a merged printer report into the
flat `beambam` block HA templates read; `beambam.state_hub.merge_report` is
how the daemon builds that report from the MQTT stream. Values below mirror a
live X2D capture (2026-11-25: 2 nozzles, 4× AMS 2 Pro through the Filament
Track Switch) without its serial numbers.
"""
from __future__ import annotations

import pytest

pytest.importorskip("paho.mqtt.client")

from beambam.ams import build_change_filament_payload  # noqa: E402
from beambam.state_hub import merge_report  # noqa: E402
from runtime.ha.publisher import RETIRED_KEYS, ams_entities, derive_state  # noqa: E402


def _x2d() -> dict:
    tray = lambda i, t, c: {"id": str(i), "tray_type": t, "tray_color": c + "FF",  # noqa: E731
                            "tray_info_idx": "GFL99", "remain": -1, "state": 11}
    return {
        "print": {
            "command": "push_status", "aux": "22801004", "fun": "44029FD1B30F9CB3",
            "spd_lvl": 3, "nozzle_temper": 28.0, "bed_temper": 25.0,
            "net": {"info": [{"ip": 2315299008, "mask": 16777215}, {"ip": 0, "mask": 0}]},
            "lights_report": [{"mode": "on", "node": "chamber_light"},
                              {"mode": "flashing", "node": "work_light"}],
            "device": {
                "ctc": {"info": {"temp": 25}, "state": 0},
                "extruder": {"info": [
                    {"id": 0, "temp": 27, "info": 1176, "snow": 65535},
                    {"id": 1, "temp": 28 | (220 << 16), "info": 1034, "snow": 0x0302}]},
            },
            "ams": {"tray_now": "255", "ams": [
                {"id": "0", "info": "10001E03", "humidity": "2", "humidity_raw": "37",
                 "temp": "24.2", "tray": [tray(0, "PETG", "F72323"), tray(1, "PLA", "7C4B00"),
                                          tray(2, "PLA", "D3C5A3"), {"id": "3"}]},
                {"id": "3", "info": "11001E03", "humidity": "4", "humidity_raw": "71",
                 "temp": "26.0", "tray": [tray(0, "PLA", "D8C9BA")]},
            ]},
            "upgrade_state": {"ota_new_version_number": "01.02.00.88"},
        },
    }


def test_derive_state_reads_the_x2d_fields():
    d = derive_state(_x2d())
    assert d["ip"] == "192.168.0.138"
    assert (d["chamber_temp"], d["chamber_target"]) == (25, 0)
    assert (d["nozzle_right_temp"], d["nozzle_right_target"]) == (27, 0)
    assert (d["nozzle_left_temp"], d["nozzle_left_target"]) == (28, 220)
    # info bit 1 = filament in the extruder; snow = ams_id << 8 | slot.
    assert d["nozzle_right_tray"] == "none" and d["nozzle_left_tray"] == "D3"
    assert d["speed"] == "sport" and d["chamber_light"] == "on"
    assert d["track_switch"] == "set up" and d["active_tray"] == "none"
    # No get_version reply yet: version unknown, the push's update still shows.
    assert d["firmware"] == "unknown" and d["firmware_available"] == "01.02.00.88"
    assert sorted(d["ams"]) == ["a1", "a2", "a3", "a4", "d1"]
    assert d["ams"]["a1"] == {"color": "#F72323", "material": "PETG", "name": "GFL99",
                              "remain": -1}
    assert d["ams"]["a4"]["material"] == "empty"
    assert d["ams_units"]["a"]["damp"] == "OFF" and d["ams_units"]["d"]["damp"] == "ON"
    assert d["ams_units"]["d"]["humidity"] == 71.0


def test_derive_state_firmware_from_get_version():
    st = _x2d()
    st["info"] = {"command": "get_version", "module": [
        {"name": "ota", "sw_ver": "01.02.00.88", "new_ver": "01.02.00.88"},
        {"name": "n3f/0", "sw_ver": "05.00.22.19"}]}
    d = derive_state(st)
    assert d["firmware"] == "01.02.00.88" and d["firmware_available"] == "up to date"


def test_derive_state_single_nozzle_and_empty_report():
    d = derive_state({"print": {"nozzle_temper": 210.0, "nozzle_target_temper": 215,
                                "chamber_temper": 31.5}})
    assert (d["nozzle_right_temp"], d["nozzle_right_target"]) == (210.0, 215)
    assert d["nozzle_left_temp"] is None and d["chamber_temp"] == 31.5
    empty = derive_state({})
    assert empty["ip"] == "" and empty["ams"] == {} and empty["speed"] == "unknown"


def test_ams_entities_follow_the_reported_units():
    keys = {e.key for e in ams_entities(["a1", "d1"], ["a", "d"])}
    assert {"ams_a1_color", "ams_a1_material", "ams_a1_load", "ams_d1_color",
            "ams_a_humidity", "ams_d_damp", "ams_d_temp"} <= keys
    assert not any(k.startswith("ams_b") for k in keys)
    retired = {k for _, k in RETIRED_KEYS}
    assert "ams_slot1_color" in retired and not retired & keys


def test_merge_report_keeps_state_across_acks_and_deltas():
    full = _x2d()
    st = merge_report(None, full)
    # A command ack must not wipe the live fields …
    st2 = merge_report(st, {"print": {"command": "project_file", "result": "fail",
                                      "sequence_id": "9"}})
    assert st2["print"]["bed_temper"] == 25.0 and "result" not in st2["print"]
    assert st2["last_command"]["command"] == "project_file"
    # … a delta push only updates what it carries …
    st3 = merge_report(st2, {"print": {"command": "push_status", "bed_temper": 60.0,
                                       "device": {"ctc": {"info": {"temp": 30}}}}})
    assert st3["print"]["bed_temper"] == 60.0 and st3["print"]["spd_lvl"] == 3
    assert st3["print"]["device"]["extruder"]["info"][1]["snow"] == 0x0302
    assert st3["print"]["device"]["ctc"]["info"]["temp"] == 30
    # … other topics merge beside it, and inputs are never mutated.
    st4 = merge_report(st3, {"info": {"command": "get_version", "module": []}})
    assert st4["info"]["command"] == "get_version" and "print" in st4
    assert st["print"]["bed_temper"] == 25.0 and "info" not in st3


@pytest.mark.parametrize("tray, want", [
    (0, (0, 0, 0)), (5, (1, 1, 5)), (15, (3, 3, 15)), (254, (254, 0, 254)),
    (255, (255, 0, 255)), (None, (255, 255, 255)),
])
def test_change_filament_payload(tray, want):
    p = build_change_filament_payload(tray)["print"]
    assert p["command"] == "ams_change_filament"
    assert (p["ams_id"], p["slot_id"], p["target"]) == want
    assert "extruder_id" not in p


def test_change_filament_payload_extruder_and_bounds():
    assert build_change_filament_payload(3, extruder=1)["print"]["extruder_id"] == 1
    for bad in (64, -1, 200):
        with pytest.raises(ValueError):
            build_change_filament_payload(bad)
    with pytest.raises(ValueError):
        build_change_filament_payload(3, extruder=2)
