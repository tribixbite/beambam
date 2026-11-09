"""tests/test_print_slots.py — `beambam print --slot 4,0,12`: one AMS slot per filament.

A multi-colour .gcode.3mf needs every filament mapped to a loaded AMS slot (the X2D project_file's
ams_mapping is per filament). `--slot` takes a comma-separated list in filament order; each slot is
validated against its own filament from the 3MF, and start_print gets the whole list.
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

import pytest

from beambam.print_job import parse_slots


def test_parse_slots() -> None:
    assert parse_slots("4") == [4]
    assert parse_slots("4,0, 12") == [4, 0, 12]
    assert parse_slots(7) == [7]
    for bad in ("", "16", "-1", "a,b"):
        with pytest.raises(ValueError):
            parse_slots(bad)


def test_cli_parser_accepts_a_list(capsys: pytest.CaptureFixture[str]) -> None:
    from tests.test_subparser_coverage import _build_parser    # the CLI's own parser, captured from main()
    parser = _build_parser()
    assert parser.parse_args(["print", "x.gcode.3mf", "--slot", "4,0"]).slot == [4, 0]
    assert parser.parse_args(["print", "x.gcode.3mf"]).slot == [0]
    with pytest.raises(SystemExit):
        parser.parse_args(["print", "x.gcode.3mf", "--slot", "99"])
    assert "AMS slots are 0..15" in capsys.readouterr().err


def _three_mf(path: Path, types: list[str], colours: list[str]) -> Path:
    ps = {"curr_bed_type": "Supertack Plate", "supertack_plate_temp_initial_layer": ["70"] * len(types),
          "filament_type": types, "filament_colour": colours, "filament_ids": ["GFG99"] * len(types)}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(ps))
        z.writestr("Metadata/plate_1.gcode", "G28\n")
    return path


def _ns(file: Path, slot: object) -> argparse.Namespace:
    return argparse.Namespace(
        ip="192.168.0.42", code="abcdef12", serial="00P9AJ000000000", printer=None, config=None,
        file=str(file), remote=None, slot=slot, no_upload=False, no_ams=False, no_bed_level=False,
        bed_type=None, bed_temp=None, force=False, flow_cali=False, timelapse=False, vib_cali=False,
        dry_run=False, max_flush_g=10.0)


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> dict:
    """cmd_print with the network replaced: records validations and the start_print call."""
    import beambam.ftps as ftps
    import beambam.mqtt as mqtt
    import beambam.print_job as pj
    seen: dict = {"validated": [], "start": None}

    class _Client:
        def __init__(self, creds): self.creds = creds
        def connect(self): pass
        def disconnect(self): pass
        def request_state(self, timeout=15.0): return {"print": {"gcode_state": "FINISH"}}
        message_seq = 0
        def wait_for(self, predicate, timeout, since=None):
            # The printer answers the project_file with success.
            msg = {"print": {"command": "project_file", "result": "success", "err_code": 0}}
            return msg if predicate(msg) else None
        def merged_state(self): return {"print": {"gcode_state": "PREPARE"}}

    monkeypatch.setattr(mqtt, "X2DClient", _Client)
    monkeypatch.setattr(ftps, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_validate_ams_slot",
                        lambda live, slot, exp, force: seen["validated"].append((slot, exp["expected_filament_type"])))
    monkeypatch.setattr(pj, "start_print", lambda cli, name, **kw: seen.__setitem__("start", kw))
    return seen


def test_multi_slot_print_maps_every_filament(tmp_path: Path, offline: dict) -> None:
    from beambam.cli.lan import cmd_print
    f = _three_mf(tmp_path / "squirtle.gcode.3mf", ["PETG", "PLA"], ["#8ECAD8", "#B49478"])
    assert cmd_print(_ns(f, [8, 10])) == 0
    assert offline["validated"] == [(8, "PETG"), (10, "PLA")]     # each slot against its own filament
    assert offline["start"]["ams_slot"] == [8, 10]


def test_single_slot_still_an_int(tmp_path: Path, offline: dict) -> None:
    from beambam.cli.lan import cmd_print
    f = _three_mf(tmp_path / "one.gcode.3mf", ["PLA"], ["#FFFFFF"])
    assert cmd_print(_ns(f, 12)) == 0
    assert offline["start"]["ams_slot"] == 12 and offline["validated"] == [(12, "PLA")]


def test_more_slots_than_filaments_is_refused(tmp_path: Path, offline: dict) -> None:
    from beambam.cli.lan import cmd_print
    f = _three_mf(tmp_path / "one.gcode.3mf", ["PLA"], ["#FFFFFF"])
    with pytest.raises(SystemExit, match="does not have that many filaments"):
        cmd_print(_ns(f, [1, 2]))
    assert offline["start"] is None


# --- confirmation and pre-flight (the printer's verdict decides the exit code) ---

def _client_class(gcode_state: str, reply: dict | None, later_state: str | None = None):
    class _Client:
        message_seq = 0
        def __init__(self, creds): self.creds = creds
        def connect(self): pass
        def disconnect(self): pass
        def request_state(self, timeout=15.0): return {"print": {"gcode_state": gcode_state}}
        def wait_for(self, predicate, timeout, since=None):
            for msg in ([reply] if reply else []) + (
                    [{"print": {"command": "push_status", "gcode_state": later_state}}] if later_state else []):
                if predicate(msg):
                    return msg
            return None
        def merged_state(self): return {"print": {"gcode_state": gcode_state}}
    return _Client


@pytest.mark.parametrize("state,reply,later,rc", [
    ("FINISH", {"print": {"command": "project_file", "result": "success"}}, None, 0),
    ("IDLE", None, "PREPARE", 0),                       # no reply, but the job is preparing
    ("FINISH", {"print": {"command": "project_file", "err_code": 84033544,
                          "reason": "file verify failed", "result": "failed"}}, None, 1),
    ("FINISH", None, None, 3),                          # silence: unconfirmed, not success
])
def test_print_exit_code_follows_the_printer(tmp_path: Path, offline: dict, monkeypatch,
                                             state, reply, later, rc) -> None:
    import beambam.mqtt as mqtt
    from beambam.cli.lan import cmd_print
    monkeypatch.setattr(mqtt, "X2DClient", _client_class(state, reply, later))
    f = _three_mf(tmp_path / "one.gcode.3mf", ["PLA"], ["#FFFFFF"])
    ns = _ns(f, 12)
    ns.confirm_timeout = 0.1
    assert cmd_print(ns) == rc


def test_busy_printer_is_refused(tmp_path: Path, offline: dict, monkeypatch) -> None:
    import beambam.mqtt as mqtt
    from beambam.cli.lan import cmd_print
    monkeypatch.setattr(mqtt, "X2DClient", _client_class("RUNNING", None))
    f = _three_mf(tmp_path / "one.gcode.3mf", ["PLA"], ["#FFFFFF"])
    with pytest.raises(SystemExit, match="printer is busy"):
        cmd_print(_ns(f, 12))
    assert offline["start"] is None


def test_too_few_slots_for_the_filaments_used(tmp_path: Path, offline: dict) -> None:
    from beambam.cli.lan import cmd_print
    f = _three_mf(tmp_path / "three.gcode.3mf", ["PETG", "PETG", "PETG"], ["#111111", "#222222", "#333333"])
    with zipfile.ZipFile(f, "a") as z:
        z.writestr("Metadata/slice_info.config",
                   '<config><plate><metadata key="index" value="1"/>'
                   '<filament id="1"/><filament id="2"/><filament id="3"/></plate></config>')
    with pytest.raises(SystemExit, match=r"filament\(s\) 1, 2, 3"):
        cmd_print(_ns(f, [4]))
    assert offline["start"] is None
