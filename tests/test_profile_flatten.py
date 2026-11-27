"""Flattening Bambu profiles and picking the flattened machine profile.

Regression guard for the air-print bug: the X2D machine profile carries its start / end / change-filament
G-code only through `include` templates, and the slice loads the machine by path. If flattening drops the
includes, or the slice loads the raw machine profile, the print gets a bare start G-code with no AMS load
(M620) — the nozzle is never fed and it extrudes air.
"""
from __future__ import annotations

import json
from pathlib import Path

from beambam.profile_flatten import FLATTEN_VERSION, _flatten_kind, flatten, missing
from beambam.slice_options import machine_file


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def test_flatten_resolves_include_templates(tmp_path: Path) -> None:
    bbl = tmp_path / "BBL"
    mdir = bbl / "machine"
    # A base with a bare start G-code, a template holding the real one, and a profile that includes it.
    _write(mdir / "base.json", {"name": "base", "machine_start_gcode": "G28\nG1 Z5"})
    _write(mdir / "X template start.json",
           {"name": "X template start", "instantiation": "false",
            "machine_start_gcode": ";===== X2D start =====\nM620 S0A\nM109 S220"})
    _write(mdir / "X.json", {"name": "X", "inherits": "base", "include": ["X template start"]})

    assert _flatten_kind(bbl, "machine") == 3
    full = json.loads((bbl / "machine_full" / "X.json").read_text(encoding="utf-8"))
    # The include overrode the bare base start G-code with the real one (so the slice loads the AMS).
    assert "M620" in full["machine_start_gcode"]
    assert full["machine_start_gcode"].startswith(";===== X2D start")
    # Bookkeeping keys are resolved away.
    assert "include" not in full and "inherits" not in full


def test_own_keys_win_over_includes(tmp_path: Path) -> None:
    bbl = tmp_path / "BBL"
    mdir = bbl / "machine"
    _write(mdir / "tpl.json", {"name": "tpl", "machine_end_gcode": "FROM_TEMPLATE"})
    _write(mdir / "Y.json", {"name": "Y", "include": ["tpl"], "machine_end_gcode": "OWN"})
    _flatten_kind(bbl, "machine")
    full = json.loads((bbl / "machine_full" / "Y.json").read_text(encoding="utf-8"))
    assert full["machine_end_gcode"] == "OWN"  # a profile's own key beats its include


def test_stale_full_folders_rebuild_on_a_version_bump(tmp_path: Path) -> None:
    bbl = tmp_path / "BBL"
    for kind in ("process", "filament", "machine"):
        _write(bbl / kind / "p.json", {"name": "p"})
    # A fresh flatten stamps the version, so nothing is missing.
    flatten(bbl)
    assert missing(bbl) == []
    # An older stamp (a previous flattener) marks every kind for rebuild even though the folders exist.
    (bbl / ".flatten_version").write_text(str(FLATTEN_VERSION - 1), encoding="utf-8")
    assert sorted(missing(bbl)) == ["filament_full", "machine_full", "process_full"]
    # No stamp at all (profiles flattened before versioning) also rebuilds.
    (bbl / ".flatten_version").unlink()
    assert sorted(missing(bbl)) == ["filament_full", "machine_full", "process_full"]


def test_machine_file_prefers_the_flattened_copy(tmp_path: Path) -> None:
    bbl = tmp_path / "BBL"
    _write(bbl / "machine" / "X.json", {})
    # No machine_full yet: fall back to the raw one.
    assert machine_file(bbl, "X") == bbl / "machine" / "X.json"
    _write(bbl / "machine_full" / "X.json", {})
    # Flattened copy exists: prefer it (it carries the resolved start G-code).
    assert machine_file(bbl, "X") == bbl / "machine_full" / "X.json"
