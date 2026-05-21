"""Tests for beambam.doctor — printer health diagnostic."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.doctor import (
    Check,
    add_subparser,
    check_ams_humidity,
    check_camera,
    check_hms_errors,
    check_print_state,
    check_thermistor_sanity,
    check_wifi_signal,
    cmd_doctor,
    decode_hms,
    format_report,
    run_all_checks,
)


# ----- decode_hms ---------------------------------------------------------


def test_decode_hms_known():
    desc = decode_hms("0700_2000_0002_0006")
    assert "clogged" in desc.lower()


def test_decode_hms_unknown_falls_back():
    desc = decode_hms("9999_9999_9999_9999")
    assert "unknown" in desc.lower()
    assert "9999" in desc


# ----- check_ams_humidity -------------------------------------------------


def test_check_ams_humidity_levels():
    state = {"print": {"ams": {"ams": [
        {"id": "0", "humidity": "1"},
        {"id": "1", "humidity": "3"},
        {"id": "2", "humidity": "4"},
    ]}}}
    checks = check_ams_humidity(state)
    sevs = {c.name: c.severity for c in checks}
    assert sevs["unit 0 humidity"] == "pass"
    assert sevs["unit 1 humidity"] == "warn"
    assert sevs["unit 2 humidity"] == "fail"


def test_check_ams_humidity_no_units():
    assert check_ams_humidity({}) == []


def test_check_ams_humidity_handles_non_numeric():
    state = {"print": {"ams": {"ams": [{"id": "0", "humidity": "?"}]}}}
    checks = check_ams_humidity(state)
    assert len(checks) == 1
    assert checks[0].severity == "pass"   # 0 treated as fine


# ----- check_hms_errors ---------------------------------------------------


def test_check_hms_no_errors_passes():
    checks = check_hms_errors({"print": {"hms": []}})
    assert len(checks) == 1
    assert checks[0].severity == "pass"
    assert "no active" in checks[0].detail


def test_check_hms_with_codes_warns_or_fails():
    state = {"print": {"hms": [
        {"attr": "0700", "code": "2000", "p": 1},
        {"attr": "0500", "code": "0100", "p": 2},
    ]}}
    checks = check_hms_errors(state)
    assert len(checks) == 2
    # p=1 → fail, p=2 → warn
    sevs = [c.severity for c in checks]
    assert "fail" in sevs
    assert "warn" in sevs


# ----- check_thermistor_sanity --------------------------------------------


def test_thermistor_normal():
    state = {"print": {"bed_temper": 60.0, "nozzle_temper": 215.0}}
    checks = check_thermistor_sanity(state)
    assert all(c.severity == "pass" for c in checks)


def test_thermistor_disconnected_fails():
    state = {"print": {"bed_temper": -999.0, "nozzle_temper": 999.0}}
    checks = check_thermistor_sanity(state)
    assert all(c.severity == "fail" for c in checks)


def test_thermistor_missing_skips():
    """Missing readings just produce no check (no spurious warning)."""
    assert check_thermistor_sanity({"print": {}}) == []


# ----- check_wifi_signal --------------------------------------------------


def test_wifi_strong():
    checks = check_wifi_signal({"print": {"wifi_signal": "-52dBm"}})
    assert checks[0].severity == "pass"
    assert "-52" in checks[0].detail


def test_wifi_weak_warns():
    checks = check_wifi_signal({"print": {"wifi_signal": "-75dBm"}})
    assert checks[0].severity == "warn"


def test_wifi_unreliable_fails():
    checks = check_wifi_signal({"print": {"wifi_signal": "-85dBm"}})
    assert checks[0].severity == "fail"


def test_wifi_missing_info():
    checks = check_wifi_signal({"print": {}})
    assert checks[0].severity == "info"


def test_wifi_non_numeric_info():
    checks = check_wifi_signal({"print": {"wifi_signal": "weird"}})
    assert checks[0].severity == "info"


# ----- check_camera -------------------------------------------------------


def test_camera_with_state():
    state = {"print": {"ipcam": {"resolution": "1080p",
                                   "ipcam_record": "enable"}}}
    checks = check_camera(state)
    assert checks[0].severity == "pass"
    assert "1080p" in checks[0].detail
    assert "enable" in checks[0].detail


def test_camera_no_state_info():
    assert check_camera({"print": {}})[0].severity == "info"


# ----- check_print_state -------------------------------------------------


def test_print_state_idle():
    checks = check_print_state({"print": {"gcode_state": "IDLE"}})
    assert checks[0].severity == "pass"


def test_print_state_running_info():
    checks = check_print_state({"print": {
        "gcode_state": "RUNNING", "mc_percent": 42,
        "layer_num": 10, "total_layer_num": 100, "mc_remaining_time": 23,
    }})
    assert checks[0].severity == "info"
    assert "42%" in checks[0].detail
    assert "layer 10/100" in checks[0].detail


def test_print_state_failed_with_reason():
    checks = check_print_state({"print": {
        "gcode_state": "FAILED", "fail_reason": "Z_HOME_FAIL",
    }})
    assert checks[0].severity == "fail"
    assert "Z_HOME_FAIL" in checks[0].detail


def test_print_state_with_print_error():
    """print_error != 0 should add a fail check."""
    checks = check_print_state({"print": {
        "gcode_state": "IDLE", "print_error": 12345,
    }})
    assert any(c.name == "print_error" and c.severity == "fail" for c in checks)


# ----- run_all_checks + format_report -----------------------------------


def test_run_all_checks_returns_list_of_check_objects():
    state = {"print": {
        "gcode_state": "IDLE",
        "bed_temper": 25.0,
        "nozzle_temper": 30.0,
        "wifi_signal": "-50dBm",
    }}
    checks = run_all_checks(state)
    assert all(isinstance(c, Check) for c in checks)


def test_format_report_groups_by_category_with_summary():
    checks = [
        Check("AMS", "x", "pass", "ok"),
        Check("Sensors", "y", "warn", "iffy"),
        Check("Sensors", "z", "fail", "bad"),
    ]
    text = format_report(checks)
    assert "AMS" in text
    assert "Sensors" in text
    assert "Summary: 1 pass, 1 warn, 1 fail" in text
    # fail listed before warn within Sensors group
    sensors_block = text[text.index("Sensors"):]
    fail_idx = sensors_block.index("bad")
    warn_idx = sensors_block.index("iffy")
    assert fail_idx < warn_idx


# ----- cmd_doctor exit codes ---------------------------------------------


def test_cmd_doctor_exit_0_all_pass():
    args = argparse.Namespace(no_color=True, json_out=False)
    state = {"print": {"gcode_state": "IDLE", "ams": {"ams": []},
                       "hms": [], "wifi_signal": "-50dBm"}}
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        fake.state.return_value = state
        rc = cmd_doctor(args)
    assert rc == 0


def test_cmd_doctor_exit_1_with_warnings():
    args = argparse.Namespace(no_color=True, json_out=False)
    state = {"print": {"gcode_state": "IDLE", "ams":
                        {"ams": [{"id": "0", "humidity": "3"}]},
                       "hms": [], "wifi_signal": "-50dBm"}}
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        fake.state.return_value = state
        rc = cmd_doctor(args)
    assert rc == 1


def test_cmd_doctor_exit_2_with_failures():
    args = argparse.Namespace(no_color=True, json_out=False)
    state = {"print": {"gcode_state": "FAILED", "fail_reason": "X",
                       "ams": {"ams": []}, "hms": [],
                       "wifi_signal": "-50dBm"}}
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        fake.state.return_value = state
        rc = cmd_doctor(args)
    assert rc == 2


def test_cmd_doctor_unreachable_printer_exit_2(capsys):
    args = argparse.Namespace(no_color=True, json_out=False)
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        fake.state.side_effect = TimeoutError("no response")
        rc = cmd_doctor(args)
    assert rc == 2
    assert "can't reach printer" in capsys.readouterr().err


def test_cmd_doctor_json_output(capsys):
    args = argparse.Namespace(no_color=True, json_out=True)
    state = {"print": {"gcode_state": "IDLE", "ams": {"ams": []},
                       "hms": [], "wifi_signal": "-50dBm"}}
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        fake.state.return_value = state
        cmd_doctor(args)
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list)
    assert all("severity" in c for c in parsed)


def test_cmd_doctor_no_color_strips_ansi(capsys):
    args = argparse.Namespace(no_color=True, json_out=False)
    state = {"print": {"gcode_state": "IDLE", "ams": {"ams": []},
                       "hms": [], "wifi_signal": "-50dBm"}}
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        fake.state.return_value = state
        cmd_doctor(args)
    out = capsys.readouterr().out
    assert "\033[" not in out          # no escape sequences


# ----- subparser ---------------------------------------------------------


def test_subparser_defaults():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["doctor"])
    assert args.no_color is False
    assert args.json_out is False
