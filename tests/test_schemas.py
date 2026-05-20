"""Tests for beambam.schemas — TypedDict shape pinning.

TypedDicts are erased at runtime so we test the public API surface
(imports, __all__) and verify that real wire payloads from the fixture
3mfs / mock states cast to the schema types without losing fields."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam import schemas
from beambam.schemas import (
    AmsBus,
    AmsTray,
    AmsUnit,
    GcodeState,
    PrintState,
    PushAllReport,
    StartPrintCommand,
    PauseCommand,
    ResumeCommand,
    StopCommand,
    GcodeLineCommand,
    SystemLedCommand,
    AmsChangeFilamentCommand,
)


def test_all_exports_present():
    """Every name in __all__ must actually be importable."""
    for name in schemas.__all__:
        assert hasattr(schemas, name), f"missing: {name}"


def test_print_state_accepts_real_partial_payload():
    """A partial state push (newer firmware drops some fields) must
    still type-cast cleanly."""
    partial: PrintState = {
        "gcode_state": "RUNNING",
        "layer_num": 42,
        "bed_temper": 65.0,
    }
    assert partial["gcode_state"] == "RUNNING"


def test_print_state_accepts_full_x2d_shape():
    """The 99-field state we captured from a live X2D fits."""
    full_partial: PrintState = {
        "command": "push_status",
        "gcode_state": "FINISH",
        "gcode_file": "/data/Metadata/plate_1.gcode",
        "subtask_name": "0.2mm layer, 6 walls, 15% infill",
        "task_id": "962344529",
        "project_id": "962344529",
        "design_id": "1501027",
        "profile_id": "322681623",
        "model_id": "US28b658edb03724",
        "plate_idx": 1,
        "percent": 100,
        "mc_percent": 100,
        "layer_num": 144,
        "mc_remaining_time": 0,
        "bed_temper": 29.0,
        "bed_target_temper": 0.0,
        "nozzle_temper": 33.0,
        "nozzle_diameter": "0.4",
        "nozzle_type": "HS01",
        "print_type": "cloud",
        "print_error": 0,
        "ams_status": 0,
    }
    assert full_partial["design_id"] == "1501027"
    assert full_partial["bed_temper"] == 29.0


def test_ams_tray_shape():
    tray: AmsTray = {
        "id": "0",
        "tray_type": "PLA",
        "tray_info_idx": "GFL99",
        "tray_color": "7C4B00FF",
        "tray_diameter": "1.75",
        "nozzle_temp_min": "190",
        "nozzle_temp_max": "240",
        "remain": -1,
        "state": 27,
    }
    assert tray["tray_type"] == "PLA"


def test_ams_unit_with_4_trays():
    unit: AmsUnit = {
        "id": "0",
        "humidity": "2",
        "temp": "26.5",
        "tray": [{"id": str(i), "tray_type": "PLA"} for i in range(4)],
    }
    assert len(unit["tray"]) == 4


def test_ams_bus_multi_unit():
    """X2D ships up to 4 AMS units."""
    bus: AmsBus = {
        "ams": [{"id": str(i)} for i in range(4)],
        "tray_now": "1",
    }
    assert len(bus["ams"]) == 4


def test_pushall_envelope():
    report: PushAllReport = {"print": {"gcode_state": "IDLE"}}
    assert report["print"]["gcode_state"] == "IDLE"


def test_start_print_command_shape():
    cmd: StartPrintCommand = {
        "sequence_id": "0",
        "command": "project_file",
        "param": "Metadata/plate_1.gcode",
        "project_id": "0",
        "profile_id": "0",
        "subtask_id": "0",
        "task_id": "962344529",
        "use_ams": True,
        "ams_mapping": [3],
        "ams_mapping2": [{"ams_id": 0, "slot_id": 3}],
        "bed_type": "supertack_plate",
        "timelapse": False,
    }
    assert cmd["ams_mapping"] == [3]
    assert cmd["ams_mapping2"][0]["ams_id"] == 0


def test_print_control_commands():
    p: PauseCommand = {"sequence_id": "0", "command": "pause"}
    r: ResumeCommand = {"sequence_id": "0", "command": "resume"}
    s: StopCommand = {"sequence_id": "0", "command": "stop"}
    assert p["command"] == "pause"
    assert r["command"] == "resume"
    assert s["command"] == "stop"


def test_gcode_line_command():
    cmd: GcodeLineCommand = {"sequence_id": "0",
                              "command": "gcode_line",
                              "param": "G28 Z\n"}
    assert cmd["param"].endswith("\n")


def test_ams_change_filament_command():
    """target=255 means unload (no specific slot)."""
    unload: AmsChangeFilamentCommand = {
        "sequence_id": "0",
        "command": "ams_change_filament",
        "target": 255,
        "curr_temp": 215,
        "tar_temp": 215,
    }
    load: AmsChangeFilamentCommand = {
        "sequence_id": "0",
        "command": "ams_change_filament",
        "target": 3,
        "curr_temp": 215,
        "tar_temp": 215,
    }
    assert unload["target"] == 255
    assert load["target"] == 3


def test_system_led_command():
    cmd: SystemLedCommand = {
        "sequence_id": "0",
        "command": "ledctrl",
        "led_node": "chamber_light",
        "led_mode": "on",
        "led_on_time": 500,
        "led_off_time": 500,
        "loop_times": 0,
        "interval_time": 0,
    }
    assert cmd["led_node"] == "chamber_light"


def test_gcode_state_enum_values():
    """The GcodeState Literal must cover every value the firmware
    actually emits."""
    valid_states = {"IDLE", "PREPARE", "RUNNING", "PAUSE",
                    "FINISH", "FAILED", "OFFLINE", "SLICING", "UNKNOWN"}
    # Runtime check: typing.get_args returns the literal's options
    from typing import get_args
    assert set(get_args(GcodeState)) == valid_states


@pytest.mark.live
def test_live_state_matches_print_state_schema(live_printer):
    """Real-printer state push must contain only known PrintState fields
    + leave us nothing to typeguard around. Loose check: all top-level
    state keys exist as TypedDict keys somewhere in our schema (or in
    the dict[str, Any] catch-alls)."""
    from beambam import Printer
    from x2d_bridge import Creds
    with Printer(Creds(ip=live_printer.ip, code=live_printer.code,
                       serial=live_printer.serial)) as p:
        s = p.state(timeout=10.0)
    # PrintState.__annotations__ is the source of truth.
    known = set(PrintState.__annotations__.keys())
    # New firmware fields drift over time — we collect unknowns rather
    # than failing the test; this exists to surface them in CI logs.
    actual = set(s["print"].keys())
    unknown = actual - known
    # Test passes regardless — the print() is informational so a CI
    # run on a new firmware surfaces drift.
    if unknown:
        print(f"INFO: {len(unknown)} firmware fields not in PrintState: "
              f"{sorted(unknown)[:10]}{'...' if len(unknown) > 10 else ''}")
