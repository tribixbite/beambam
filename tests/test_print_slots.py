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
        def request_state(self, timeout=15.0): return {"print": {}}

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
