"""Tests for beambam.ams — pretty-printer + subcommand dispatch."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.ams import (
    _humidity_bar,
    _hex_to_rgb,
    _swatch,
    _tray_state_label,
    add_subparser,
    cmd_ams,
    format_status,
    format_tray_info,
)


FAKE_AMS = {
    "ams": [
        {
            "id": "0", "humidity": "2", "temp": "26.5", "info": "11001E03",
            "tray": [
                {"id": "0", "tray_type": "PLA", "tray_info_idx": "GFL99",
                 "tray_color": "7C4B00FF", "tray_diameter": "1.75",
                 "nozzle_temp_min": "190", "nozzle_temp_max": "240",
                 "bed_temp": "55", "bed_temp_type": "0",
                 "drying_temp": "45", "drying_time": "8",
                 "remain": 80, "state": 27, "tag_uid": "ABC123"},
                {"id": "1", "tray_type": "PETG", "tray_info_idx": "GFG99",
                 "tray_color": "FFFFFFFF", "tray_diameter": "1.75",
                 "nozzle_temp_min": "230", "nozzle_temp_max": "270",
                 "bed_temp": "70", "bed_temp_type": "0",
                 "drying_temp": "65", "drying_time": "12",
                 "remain": -1, "state": 11, "tag_uid": "0"},
                {"id": "2", "tray_type": "PLA", "tray_color": "00AE42FF",
                 "remain": 50, "state": 11},
                {"id": "3", "tray_type": "PLA", "tray_color": "F72323FF",
                 "remain": 12, "state": 13},  # loading
            ],
        },
    ],
}


# ----- helpers -------------------------------------------------------------


def test_hex_to_rgb_with_alpha():
    assert _hex_to_rgb("7C4B00FF") == (124, 75, 0)


def test_hex_to_rgb_with_hash_prefix():
    assert _hex_to_rgb("#FFFFFF") == (255, 255, 255)


def test_hex_to_rgb_too_short_returns_default():
    assert _hex_to_rgb("123") == (128, 128, 128)


def test_humidity_bar_clamps_to_0_4():
    assert _humidity_bar("0") == "░░░░"
    assert _humidity_bar("2") == "▓▓░░"
    assert _humidity_bar("4") == "▓▓▓▓"
    assert _humidity_bar("5") == "▓▓▓▓"             # clamped
    assert _humidity_bar("-1") == "░░░░"            # clamped
    assert _humidity_bar("?") == "?"                # non-int


def test_tray_state_label_known():
    assert _tray_state_label(0) == "empty"
    assert _tray_state_label(11) == "loaded"
    assert _tray_state_label(13) == "loading"
    assert _tray_state_label(27) == "ACTIVE"


def test_tray_state_label_unknown():
    """Unknown states must surface the raw int for forensic clarity."""
    assert _tray_state_label(99) == "state=99"


def test_swatch_without_color_returns_plain_hex():
    assert _swatch("7C4B00FF", color=False) == "#7C4B00"


# ----- format_status -------------------------------------------------------


def test_format_status_empty_units():
    assert "no AMS units" in format_status({"ams": []}, color=False)
    assert "no AMS units" in format_status({}, color=False)


def test_format_status_includes_every_tray():
    text = format_status(FAKE_AMS, color=False)
    # All 4 trays should appear
    assert "PLA" in text and "PETG" in text
    assert "#7C4B00" in text
    assert "#FFFFFF" in text
    assert "#00AE42" in text
    assert "#F72323" in text
    # Active marker on slot 0
    assert "ACTIVE" in text
    assert "◀" in text
    # AMS header with humidity bar + temp
    assert "AMS 0" in text
    assert "26.5°C" in text
    # Global slot numbers
    assert "slot  0" in text
    assert "slot  3" in text


def test_format_status_global_slot_index_continues_across_units():
    """Slot 4 should be AMS 1 tray 0."""
    two_units = {
        "ams": [
            {"id": "0", "humidity": "0", "temp": "20", "tray":
                [{"id": str(i), "tray_color": "808080",
                  "tray_type": "PLA", "remain": -1, "state": 11}
                 for i in range(4)]},
            {"id": "1", "humidity": "0", "temp": "20", "tray":
                [{"id": str(i), "tray_color": "808080",
                  "tray_type": "PLA", "remain": -1, "state": 11}
                 for i in range(4)]},
        ],
    }
    text = format_status(two_units, color=False)
    assert "slot  4" in text and "slot  7" in text


# ----- format_tray_info ----------------------------------------------------


def test_format_tray_info_present_slot():
    text = format_tray_info(FAKE_AMS, slot=0, color=False)
    assert "slot 0" in text and "AMS 0, tray 0" in text
    assert "PLA" in text
    assert "190..240°C" in text
    assert "#7C4B00" in text
    assert "ACTIVE" in text


def test_format_tray_info_missing_slot_unit():
    text = format_tray_info(FAKE_AMS, slot=12, color=False)  # AMS 3 doesn't exist
    assert "no AMS unit 3" in text


def test_format_tray_info_missing_tray():
    """Synthesize an AMS with only tray 0; ask for tray 1."""
    sparse = {"ams": [{"id": "0", "tray": [{"id": "0", "tray_color": "808080"}]}]}
    text = format_tray_info(sparse, slot=1, color=False)
    assert "no tray 1" in text


# ----- subparser ----------------------------------------------------------


def test_subparser_requires_ams_subcommand():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["ams"])           # no subcommand


def test_subparser_status_default():
    p = argparse.ArgumentParser()
    p.add_argument("--ip"); p.add_argument("--code")
    p.add_argument("--serial"); p.add_argument("--printer")
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["ams", "status"])
    assert args.ams_cmd == "status"
    assert args.no_color is False
    assert args.json_out is False


def test_subparser_info_requires_slot():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["ams", "info"])


def test_subparser_dry_requires_temp_and_hours():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["ams", "dry", "0"])           # missing --temp --hours


def test_subparser_dry_full():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["ams", "dry", "1", "--temp", "55", "--hours", "8"])
    assert args.ams_cmd == "dry"
    assert args.unit == 1 and args.temp == 55 and args.hours == 8


# ----- cmd_ams dispatch ---------------------------------------------------


def test_cmd_ams_status_calls_printer_state(capsys):
    args = argparse.Namespace(ams_cmd="status", no_color=True, json_out=False)
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        fake.state.return_value = {"print": {"ams": FAKE_AMS}}
        rc = cmd_ams(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "AMS 0" in out


def test_cmd_ams_status_json_emits_raw_block(capsys):
    args = argparse.Namespace(ams_cmd="status", no_color=True, json_out=True)
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        fake.state.return_value = {"print": {"ams": FAKE_AMS}}
        rc = cmd_ams(args)
    assert rc == 0
    import json as _json
    parsed = _json.loads(capsys.readouterr().out)
    assert parsed["ams"][0]["id"] == "0"


def test_cmd_ams_load_publishes(capsys):
    args = argparse.Namespace(ams_cmd="load", slot=3)
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        cmd_ams(args)
        fake.ams_load.assert_called_once_with(3)
    assert "ams_load slot=3" in capsys.readouterr().out


def test_cmd_ams_unload_publishes(capsys):
    args = argparse.Namespace(ams_cmd="unload")
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        cmd_ams(args)
        fake.ams_unload.assert_called_once()
    assert "ams_unload published" in capsys.readouterr().out


def test_cmd_ams_dry_publishes_payload(capsys):
    args = argparse.Namespace(ams_cmd="dry", unit=2, temp=55, hours=6)
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        cmd_ams(args)
        call = fake.mqtt.publish.call_args
        payload = call.args[0]
        assert payload["print"]["ams_id"] == 2
        assert payload["print"]["drying_temp"] == 55
        assert payload["print"]["drying_time"] == 6
    assert "drying cycle requested" in capsys.readouterr().out


# ----- live ---------------------------------------------------------------


@pytest.mark.live
def test_live_ams_status_renders(live_printer):
    """Real printer round-trip — verify format_status doesn't blow up
    on any state shape the firmware emits."""
    from beambam import Printer
    from beambam.config import Creds
    with Printer(Creds(ip=live_printer.ip, code=live_printer.code,
                       serial=live_printer.serial)) as p:
        state = p.state(timeout=10.0)
    ams_block = state.get("print", {}).get("ams", {})
    text = format_status(ams_block, color=False)
    assert "AMS" in text or "no AMS" in text
