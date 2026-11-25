"""tests/test_print_slots.py — `beambam print --slot 4,0,12`: one AMS slot per used filament.

A multi-colour .gcode.3mf needs every filament it uses mapped to a loaded tray (the X2D
project_file's ams_mapping is per filament). `--slot` takes a comma-separated list in the order
of the plate's used filaments; the filament planner (beambam.filament_mapping) checks each slot
against its own filament, and start_print gets the whole plan.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from beambam.print_job import parse_slots
from tests.test_filament_mapping import make_3mf, make_state, tray, SWITCH_A, SWITCH_B


def test_parse_slots() -> None:
    assert parse_slots("4") == [4]
    assert parse_slots("4,0, 12") == [4, 0, 12]
    assert parse_slots(7) == [7]
    for bad in ("", "16", "-1", "a,b"):
        with pytest.raises(ValueError):
            parse_slots(bad)


def test_cli_parser_accepts_a_list(capsys: pytest.CaptureFixture[str]) -> None:
    from beambam.cli import build_parser
    parser = build_parser()
    assert parser.parse_args(["print", "x.gcode.3mf", "--slot", "4,0"]).slot == [4, 0]
    # One slot stays an int (the single-filament form); no --slot means auto-map.
    assert parser.parse_args(["print", "x.gcode.3mf", "--slot", "6"]).slot == 6
    assert parser.parse_args(["print", "x.gcode.3mf"]).slot is None
    assert parser.parse_args(["slice-print", "x.stl", "--slot", "1,2"]).slot == [1, 2]
    with pytest.raises(SystemExit):
        parser.parse_args(["print", "x.gcode.3mf", "--slot", "99"])
    assert "AMS slots are 0..15" in capsys.readouterr().err


def _ns(file: Path, slot: object) -> argparse.Namespace:
    return argparse.Namespace(
        ip="192.168.0.42", code="abcdef12", serial="00P9AJ000000000", printer=None, config=None,
        file=str(file), remote=None, slot=slot, map=None, plate=1, yes=False,
        no_upload=False, no_ams=False, no_bed_level=False, bed_type=None, bed_temp=None,
        force=False, flow_cali=False, timelapse=False, vib_cali=False, dry_run=False,
        max_flush_g=10.0)


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> dict:
    """cmd_print with the network replaced: live AMS state in, the start_print call out."""
    import beambam.ftps as ftps
    import beambam.mqtt as mqtt
    import beambam.print_job as pj
    state = make_state([
        (SWITCH_B, [tray(0, "PETG", "#8ECAD8", "GFG99"), tray(1, "PLA", "#B49478", "GFG99"),
                    tray(2, "PLA", "#D3C5A3"), tray(3, "PLA", "#F98C36")]),
        (SWITCH_A, [tray(0, "PETG", "#F4EE2A", "GFG99"), tray(1, "PETG", "#FCECD6", "GFG99"),
                    tray(2, "PLA", "#B49478", "GFG99"), tray(3, "PLA", "#FFFFFF")]),
    ])
    seen: dict = {"start": None, "uploaded": False}

    class _Client:
        def __init__(self, creds): self.creds = creds
        def connect(self): pass
        def disconnect(self): pass
        def request_state(self, timeout=15.0): return state

    monkeypatch.setattr(mqtt, "X2DClient", _Client)
    monkeypatch.setattr(ftps, "upload_file", lambda *a, **k: seen.__setitem__("uploaded", True))
    monkeypatch.setattr(pj, "start_print", lambda cli, name, **kw: seen.__setitem__("start", kw))
    return seen


def _tray_of(plan, filament: int) -> int:
    return next(a.tray.tray_id for a in plan.assignments if a.filament.number == filament)


def test_multi_slot_print_maps_every_filament(tmp_path: Path, offline: dict) -> None:
    from beambam.cli.lan import cmd_print
    f = make_3mf(tmp_path, [("PETG", "#8ECAD8", "GFG99", "2"), ("PLA", "#B49478", "GFG99", "2")],
                 used={1: [1, 2]})
    assert cmd_print(_ns(f, [0, 6])) == 0
    plan = offline["start"]["plan"]
    assert (_tray_of(plan, 1), _tray_of(plan, 2)) == (0, 6)    # each filament on its own slot
    assert offline["uploaded"]


def test_single_slot_still_an_int(tmp_path: Path, offline: dict) -> None:
    from beambam.cli.lan import cmd_print
    f = make_3mf(tmp_path, [("PLA", "#B49478", "GFG99", "2")])
    assert cmd_print(_ns(f, 1)) == 0
    assert _tray_of(offline["start"]["plan"], 1) == 1


def test_raw_slot_text_from_a_caller_is_parsed(tmp_path: Path, offline: dict) -> None:
    from beambam.cli.lan import cmd_print
    f = make_3mf(tmp_path, [("PLA", "#B49478", "GFG99", "2")])
    assert cmd_print(_ns(f, "6")) == 0
    assert _tray_of(offline["start"]["plan"], 1) == 6


def test_more_slots_than_filaments_is_refused(tmp_path: Path, offline: dict) -> None:
    from beambam.cli.lan import cmd_print
    f = make_3mf(tmp_path, [("PLA", "#B49478", "GFG99", "2")])
    with pytest.raises(SystemExit, match="2 slots given but plate 1 uses 1"):
        cmd_print(_ns(f, [1, 2]))
    assert offline["start"] is None and not offline["uploaded"]   # refused before upload
