"""Tests for beambam.ams — pretty-printer + subcommand dispatch."""
from __future__ import annotations

import argparse
import json
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


# ----- ams set / sync ------------------------------------------------------


from beambam.ams import (
    _current_tray_color,
    _load_flat_profile,
    _normalize_tray_color,
    _profile_tray_fields,
    build_tray_metadata_payload,
)


def test_build_tray_metadata_payload_canonicalizes_color():
    p = build_tray_metadata_payload(
        2, tray_type="PLA", tray_info_idx="GFL99",
        nozzle_temp_min=190, nozzle_temp_max=240,
        tray_color="#7c4b00",
    )["print"]
    # slot 2 → unit 0, tray 2; 6-char color gets FF alpha
    assert p["ams_id"] == 0
    assert p["slot_id"] == 2 and p["tray_id"] == 2
    assert p["tray_color"] == "7C4B00FF"
    assert p["command"] == "ams_filament_setting"


def test_build_tray_metadata_payload_slot_crosses_units():
    p = build_tray_metadata_payload(
        9, tray_type="PETG", tray_info_idx="GFG99",
        nozzle_temp_min=230, nozzle_temp_max=270,
    )["print"]
    # slot 9 → unit 2 tray 1
    assert p["ams_id"] == 2 and p["slot_id"] == 1
    assert "tray_color" not in p   # color omitted when None


def test_build_tray_metadata_payload_rejects_bad_slot():
    with pytest.raises(ValueError, match="out of range"):
        build_tray_metadata_payload(16, tray_type="PLA",
                                    tray_info_idx="GFL99",
                                    nozzle_temp_min=190, nozzle_temp_max=240)


def test_build_tray_metadata_payload_rejects_bad_color():
    with pytest.raises(ValueError, match="must be RRGGBB"):
        build_tray_metadata_payload(0, tray_type="PLA",
                                    tray_info_idx="GFL99",
                                    nozzle_temp_min=190, nozzle_temp_max=240,
                                    tray_color="not-a-color")


def test_normalize_tray_color_accepts_hash_and_alpha():
    assert _normalize_tray_color("#abcdef") == "ABCDEFFF"
    assert _normalize_tray_color("ABCDEF12") == "ABCDEF12"


def test_profile_tray_fields_pulls_temps_and_idx(tmp_path):
    prof = tmp_path / "demo.json"
    prof.write_text(json.dumps({
        "type": "filament",
        "name": "DemoBrand Glow @BBL X2D 0.4 nozzle",
        "filament_type": ["PETG"],
        "nozzle_temperature_range_low": ["220"],
        "nozzle_temperature_range_high": ["260"],
        "setting_id": "GFSDEMO01",
    }))
    p = _load_flat_profile(str(prof))
    fields = _profile_tray_fields(p)
    assert fields["tray_type"] == "PETG"
    assert fields["tray_info_idx"] == "GFG99"     # generic PETG
    assert fields["nozzle_temp_min"] == 220
    assert fields["nozzle_temp_max"] == 260
    assert fields["setting_id"] == "GFSDEMO01"
    assert fields["tray_sub_brands"] == "DemoBrand Glow"


def test_profile_tray_fields_rejects_non_filament(tmp_path):
    prof = tmp_path / "process.json"
    prof.write_text(json.dumps({"type": "process", "name": "x"}))
    with pytest.raises(ValueError, match="not a filament"):
        _load_flat_profile(str(prof))


def test_current_tray_color_reads_live_state():
    block = {"ams": [{"id": "0", "tray": [
        {"id": "0", "tray_color": "ABCDEF12"},
        {"id": "1", "tray_color": ""},                # explicitly empty
    ]}]}
    assert _current_tray_color(block, 0) == "ABCDEF12"
    assert _current_tray_color(block, 1) is None       # empty → None
    assert _current_tray_color(block, 9) is None       # absent unit


def test_cmd_ams_set_dry_run_emits_payload(tmp_path, capsys):
    prof = tmp_path / "f.json"
    prof.write_text(json.dumps({
        "type": "filament", "name": "eSUN PLA+ @BBL X2D 0.4 nozzle",
        "filament_type": ["PLA"],
        "nozzle_temperature_range_low": ["190"],
        "nozzle_temperature_range_high": ["240"],
        "setting_id": "GFSE00",
    }))
    args = argparse.Namespace(
        ams_cmd="set", slot=3, profile=str(prof),
        color="00C896", dry_run=True,
    )
    # Patch Printer to prove the dry-run path never instantiates it.
    with patch("beambam.Printer") as Printer:
        rc = cmd_ams(args)
        Printer.assert_not_called()
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["print"]["ams_id"] == 0     # slot 3 → unit 0 tray 3
    assert parsed["print"]["slot_id"] == 3
    assert parsed["print"]["tray_color"] == "00C896FF"
    assert parsed["print"]["nozzle_temp_min"] == 190


def test_cmd_ams_set_dry_run_color_keep_omits_color(tmp_path, capsys):
    """Without --color and with --dry-run, no live state pull happens
    so tray_color must be omitted (firmware keeps existing)."""
    prof = tmp_path / "f.json"
    prof.write_text(json.dumps({
        "type": "filament", "name": "test",
        "filament_type": ["PLA"],
        "nozzle_temperature_range_low": ["190"],
        "nozzle_temperature_range_high": ["240"],
    }))
    args = argparse.Namespace(ams_cmd="set", slot=0, profile=str(prof),
                              color=None, dry_run=True)
    with patch("beambam.Printer") as Printer:
        rc = cmd_ams(args)
        Printer.assert_not_called()
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert "tray_color" not in parsed["print"]


def test_cmd_ams_set_missing_profile_returns_1(tmp_path, capsys):
    args = argparse.Namespace(
        ams_cmd="set", slot=0,
        profile=str(tmp_path / "missing.json"),
        color=None, dry_run=True,
    )
    rc = cmd_ams(args)
    assert rc == 1
    assert "failed to load profile" in capsys.readouterr().err


def test_cmd_ams_set_publishes_via_printer(tmp_path, capsys):
    prof = tmp_path / "f.json"
    prof.write_text(json.dumps({
        "type": "filament", "name": "eSUN PLA+ @BBL X2D 0.4",
        "filament_type": ["PLA"],
        "nozzle_temperature_range_low": ["190"],
        "nozzle_temperature_range_high": ["240"],
    }))
    args = argparse.Namespace(
        ams_cmd="set", slot=5, profile=str(prof),
        color="FF0000", dry_run=False,
    )
    with patch("beambam.Printer") as Printer:
        fake = MagicMock()
        Printer.return_value.__enter__.return_value = fake
        rc = cmd_ams(args)
    assert rc == 0
    fake.set_tray_metadata.assert_called_once()
    kwargs = fake.set_tray_metadata.call_args.kwargs
    assert kwargs["tray_type"] == "PLA"
    assert kwargs["tray_color"] == "FF0000"
    assert "tray_sub_brands" not in kwargs    # dropped from signature


def test_cmd_ams_sync_dry_run_walks_map(tmp_path, capsys):
    prof = tmp_path / "f.json"
    prof.write_text(json.dumps({
        "type": "filament", "name": "test",
        "filament_type": ["PLA"],
        "nozzle_temperature_range_low": ["190"],
        "nozzle_temperature_range_high": ["240"],
    }))
    syncmap = tmp_path / "ams-sync.json"
    syncmap.write_text(json.dumps({
        "slots": [
            {"slot": 0, "profile": "f.json", "color": "111111"},
            {"slot": 1, "profile": "f.json"},
        ],
    }))
    args = argparse.Namespace(
        ams_cmd="sync", map_path=str(syncmap),
        profiles_dir=None, dry_run=True,
    )
    with patch("beambam.Printer") as Printer:
        rc = cmd_ams(args)
        Printer.assert_not_called()
    assert rc == 0
    out = capsys.readouterr().out
    assert "slot  0" in out and "color=111111" in out
    assert "slot  1" in out and "(keep)" in out


def test_cmd_ams_sync_missing_map_returns_1(tmp_path, capsys):
    args = argparse.Namespace(
        ams_cmd="sync", map_path=str(tmp_path / "no.json"),
        profiles_dir=None, dry_run=True,
    )
    rc = cmd_ams(args)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_cmd_ams_sync_empty_slots_returns_1(tmp_path, capsys):
    syncmap = tmp_path / "ams-sync.json"
    syncmap.write_text(json.dumps({"slots": []}))
    args = argparse.Namespace(
        ams_cmd="sync", map_path=str(syncmap),
        profiles_dir=None, dry_run=True,
    )
    rc = cmd_ams(args)
    assert rc == 1
    assert "no 'slots'" in capsys.readouterr().err


def test_cmd_ams_sync_aborts_before_publish_on_bad_profile(tmp_path, capsys):
    """Half-batches are a footgun — verify the validate-up-front guard
    refuses to publish any slot when one profile is broken."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "type": "filament", "name": "OK",
        "filament_type": ["PLA"],
        "nozzle_temperature_range_low": ["190"],
        "nozzle_temperature_range_high": ["240"],
    }))
    syncmap = tmp_path / "ams-sync.json"
    syncmap.write_text(json.dumps({
        "slots": [
            {"slot": 0, "profile": "good.json"},
            {"slot": 1, "profile": "missing.json"},
        ],
    }))
    args = argparse.Namespace(
        ams_cmd="sync", map_path=str(syncmap),
        profiles_dir=None, dry_run=False,
    )
    with patch("beambam.Printer") as Printer:
        rc = cmd_ams(args)
        Printer.assert_not_called()    # no half-batch
    assert rc == 1
    assert "missing.json" in capsys.readouterr().err


# ----- subparser for set/sync ----------------------------------------------


def test_subparser_set_requires_slot_and_profile():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["ams", "set"])         # missing both
    with pytest.raises(SystemExit):
        p.parse_args(["ams", "set", "3"])    # missing profile


def test_subparser_set_full():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["ams", "set", "3", "/tmp/x.json",
                         "--color", "ABCDEF", "--dry-run"])
    assert args.ams_cmd == "set" and args.slot == 3
    assert args.profile == "/tmp/x.json"
    assert args.color == "ABCDEF" and args.dry_run is True


def test_subparser_sync_optional_map():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["ams", "sync"])
    assert args.ams_cmd == "sync"
    assert args.map_path is None and args.dry_run is False


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
