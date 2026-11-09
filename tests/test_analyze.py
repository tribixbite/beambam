"""Tests for beambam.analyze — 3mf print-plan dissector.

Uses samples/x2d_cloud_print_mira_official.gcode.3mf as a fixture
(single-filament, single-extruder PLA, 6 layers). The multi-color
behaviour is exercised against a synthetic in-memory 3mf assembled
from the mira fixture + a mutated slice_info.config.
"""
from __future__ import annotations

import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.analyze import (
    analyze_3mf,
    format_report,
    report_to_dict,
    _parse_layer_ranges,
    _count_gcode_toolchanges,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIRA = REPO_ROOT / "samples/x2d_cloud_print_mira_official.gcode.3mf"


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_parse_layer_ranges_single():
    assert _parse_layer_ranges("0 5") == [(0, 5)]


def test_parse_layer_ranges_multiple():
    assert _parse_layer_ranges("105 136,68 90") == [(105, 136), (68, 90)]


def test_parse_layer_ranges_empty():
    assert _parse_layer_ranges("") == []


def test_count_toolchanges_handles_real_extruders_only():
    """T0..T9 are real extruder selects; high-numbered Tnnnnn are unload
    sentinels in start/end gcode and must be counted separately."""
    gcode = b"""
T0
G1 X10
T1
G1 X20
T0
M620 S65279 B
T65279
T65535
"""
    counts = _count_gcode_toolchanges(gcode)
    assert counts["tool_calls"] == {"T0": 2, "T1": 1}
    assert counts["tool_calls_total"] == 3
    assert counts["unload_sentinels"] == 2


def test_count_toolchanges_m620_cycles():
    gcode = b"""
M620 S1 B
M620.10 A1 L150
M621 S1 B
"""
    counts = _count_gcode_toolchanges(gcode)
    assert counts["m620_cycles"] == 1
    assert counts["m620_flushes"] == 1


# ---------------------------------------------------------------------------
# Real-fixture tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mira_report():
    assert MIRA.is_file(), f"fixture missing: {MIRA}"
    return analyze_3mf(MIRA)


def test_mira_file_header(mira_report):
    assert mira_report.file["size"] > 0
    assert len(mira_report.file["sha256"]) == 64


def test_mira_slicer_version(mira_report):
    assert mira_report.slicer["client"] == "slicer"
    assert mira_report.slicer["version"] == "02.06.00.51"


def test_mira_printer(mira_report):
    assert mira_report.printer["model_id"] == "N6"
    # Single-extruder slice profile → 1 <nozzle> element even though
    # nozzle_diameters attribute lists two values for printer-info purposes.
    assert mira_report.printer["nozzle_count"] == 1


def test_mira_filament(mira_report):
    assert len(mira_report.filaments) == 1
    f = mira_report.filaments[0]
    assert f.type == "PLA"
    assert f.color == "#00AE42"
    assert f.tray_idx == "GFA00"
    assert f.used_g == pytest.approx(3.19, rel=0.01)
    assert f.used_m == pytest.approx(1.05, rel=0.01)


def test_mira_phase_single_color(mira_report):
    assert len(mira_report.phases) == 1
    p = mira_report.phases[0]
    assert p.layer_start == 0
    assert p.layer_end == 5
    assert p.filaments == [1]
    assert p.real_flushes == 0
    assert p.flush_volume_mm == 0.0


def test_mira_totals_no_flushes(mira_report):
    """Single-color print → 0 nozzle swaps and 0 flush volume."""
    t = mira_report.totals
    assert t["nozzle_swaps"] == 0
    assert t["flush_volume_mm"] == 0.0
    # Single-color gcode shouldn't have real T0/T1 calls in body —
    # only the unload sentinels in end gcode.
    assert t["tool_calls_total"] == 0
    assert t["unload_sentinels"] >= 1


def test_mira_hints_empty_for_simple_print(mira_report):
    """A 1-filament 6-layer print has no waste, no warnings worth surfacing."""
    assert mira_report.hints == []


def test_mira_format_report_smoke(mira_report):
    """format_report should produce a non-empty string with the file path."""
    text = format_report(mira_report)
    assert "beambam analyze" in text
    assert "PLA" in text
    assert "Filaments (1)" in text


def test_mira_report_to_dict_is_json_serializable(mira_report):
    """report_to_dict output must round-trip through json.dumps/loads."""
    d = report_to_dict(mira_report)
    s = json.dumps(d, default=str)
    back = json.loads(s)
    assert back["slicer"]["version"] == "02.06.00.51"
    assert back["totals"]["nozzle_swaps"] == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_missing_file_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        analyze_3mf(Path("/nonexistent/foo.gcode.3mf"))


def test_not_a_zip_raises(tmp_path):
    bad = tmp_path / "not_a_zip.gcode.3mf"
    bad.write_bytes(b"this is plain text, not a zip")
    with pytest.raises(zipfile.BadZipFile):
        analyze_3mf(bad)


def test_zip_missing_slice_info_raises(tmp_path):
    """A 3mf without Metadata/slice_info.config should fail loudly."""
    bad = tmp_path / "empty.gcode.3mf"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("dummy.txt", "hi")
    with pytest.raises(KeyError):
        analyze_3mf(bad)


# ---------------------------------------------------------------------------
# G-code-driven flush accounting (exact, per layer)
# ---------------------------------------------------------------------------

# A 6-layer, 2-nozzle plate: layers 0-1 use filaments 1+2 on separate nozzles
# (changes with L0 = no purge), layers 2-3 add filament 3 on nozzle 0 (two
# purging changes, 112.253 + 224.506 mm), layers 4-5 are filament 1 only.
_SYNTH_GCODE = b"""; HEADER_BLOCK_START
; total layer number: 6
; filament_density: 1.27,1.27,1.50
; HEADER_BLOCK_END
M620 S0A H0 B
T0 H0
M73 L1
M620 S1A B H-1
M620.10 A0 F299 L0 H0.4 T270 P255 S1
M620.10 A1 F299 L0 H0.4 T270 P255 S1
T1 H-1
M73 L2
M73 L3
M620 S2A B H-1
M620.10 A0 F299 L112.253 H0.4 T270 P255 S1
M620.10 A1 F299 L112.253 H0.4 T270 P255 S1
T2 H-1
M73 L4
M620 S0A B H-1
M620.10 A0 F299 L224.506 H0.4 T270 P255 S1
M620.10 A1 F299 L224.506 H0.4 T270 P255 S1
T0 H-1
M73 L5
M73 L6
M620 S65279 B
T65279
"""

_SYNTH_SLICE_INFO = b"""<?xml version="1.0" encoding="UTF-8"?>
<config><header><header_item key="X-BBL-Client-Type" value="slicer"/>
<header_item key="X-BBL-Client-Version" value="02.08.02.61"/></header>
<plate><metadata key="index" value="1"/><metadata key="nozzle_diameters" value="0.4,0.4"/>
<metadata key="prediction" value="600"/><metadata key="weight" value="3.0"/>
<filament id="1" tray_info_idx="GFG99" type="PETG" color="#FFFFFF" used_m="1" used_g="1" group_id="0" nozzle_diameter="0.40"/>
<filament id="2" tray_info_idx="GFG99" type="PETG" color="#000000" used_m="1" used_g="1" group_id="1" nozzle_diameter="0.40"/>
<filament id="3" tray_info_idx="GFG99" type="PETG" color="#FF0000" used_m="1" used_g="1" group_id="0" nozzle_diameter="0.40"/>
<layer_filament_lists>
<layer_filament_list filament_list="0 1" layer_ranges="0 1"/>
<layer_filament_list filament_list="0 1 2" layer_ranges="2 3"/>
<layer_filament_list filament_list="0" layer_ranges="4 5"/>
</layer_filament_lists>
</plate></config>
"""


@pytest.fixture
def synth_3mf(tmp_path: Path) -> Path:
    path = tmp_path / "synth.gcode.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", _SYNTH_SLICE_INFO)
        z.writestr("Metadata/plate_1.gcode", _SYNTH_GCODE)
        # A sequence whose proportional split would put swaps in layers 4-5.
        z.writestr("Metadata/filament_sequence.json", json.dumps(
            {"plate_1": {"sequence": [1, 2, 3, 1, 1, 1, 1, 1],
                         "nozzle_sequence": [0, 1, 0, 0, 0, 0, 0, 0]}}))
    return path


def test_gcode_flushes_are_attributed_to_their_layers(synth_3mf: Path):
    r = analyze_3mf(synth_3mf)
    assert [(p.layer_start, p.layer_end, p.real_flushes, p.filament_changes) for p in r.phases] == [
        (0, 1, 0, 1), (2, 3, 2, 2), (4, 5, 0, 0)]
    assert r.phases[2].flush_g == 0.0          # a single-colour phase never purges
    assert r.plate["layer_count"] == 6         # from the G-code header


def test_gcode_flush_units_and_density(synth_3mf: Path):
    t = analyze_3mf(synth_3mf).totals
    assert t["flush_source"] == "gcode"
    assert t["filament_changes"] == 3
    assert t["nozzle_swaps"] == 2
    assert t["flush_length_mm"] == pytest.approx(336.759)
    assert t["flush_volume_mm3"] == pytest.approx(336.759 * 2.4053)
    assert t["flush_volume_mm"] == t["flush_volume_mm3"]       # legacy key, mm³
    # Each purge weighs at the INCOMING filament's density: #3 is 1.50, #1 is 1.27.
    expected_g = (112.253 * 1.50 + 224.506 * 1.27) * 2.4053 / 1000
    assert t["flush_volume_g"] == pytest.approx(expected_g)


def test_no_gcode_falls_back_to_labelled_estimate(tmp_path: Path):
    path = tmp_path / "nogcode.gcode.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", _SYNTH_SLICE_INFO)
    assert analyze_3mf(path).totals["flush_source"] == "estimate"


def test_single_colour_gcode_is_exact_not_estimate(tmp_path: Path):
    path = tmp_path / "mono.gcode.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", _SYNTH_SLICE_INFO)
        z.writestr("Metadata/plate_1.gcode", b"; total layer number: 6\nM73 L1\nM73 L2\n")
    t = analyze_3mf(path).totals
    assert t["flush_source"] == "gcode" and t["flush_volume_g"] == 0.0 and t["filament_changes"] == 0


# ---------------------------------------------------------------------------
# Prime tower and same-nozzle purges (review: a tower-dominated X2D slice
# reported purge 0 g because Bambu writes L0 and prints the purge/prime into
# the tower)
# ---------------------------------------------------------------------------

_TOWER_GCODE = b"""; total layer number: 2
; filament_density: 1.27,1.50
M83
M620 S0A H0 B
M73 L1
; FEATURE: Outer wall
G1 X1 Y1 E5.0
; FEATURE: Prime tower
G1 X2 Y2 E10.0
G1 E-0.8
G1 E0.8
G1 X3 Y3 E2.0
M620 S1A B H-1
M620.10 A0 F299 L0 H0.4 T270 P255 S1
M620.10 A1 F299 L0 H0.4 T270 P255 S1
T1 H-1
M73 L2
; FEATURE: Prime tower
G1 X4 Y4 E4.0
; FEATURE: Inner wall
G1 X5 Y5 E3.0
"""


def _tower_3mf(path: Path, filament_maps: str | None) -> Path:
    meta = (f'<metadata key="filament_maps" value="{filament_maps}"/>'
            if filament_maps is not None else "")
    slice_info = _SYNTH_SLICE_INFO.replace(
        b'<metadata key="weight" value="3.0"/>',
        b'<metadata key="weight" value="3.0"/>' + meta.encode())
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info)
        z.writestr("Metadata/plate_1.gcode", _TOWER_GCODE)
    return path


def test_prime_tower_is_measured_net_per_filament(tmp_path: Path):
    t = analyze_3mf(_tower_3mf(tmp_path / "t.gcode.3mf", "1 2")).totals
    # Filament 1 in the tower: 10 - 0.8 + 0.8 + 2 = 12 mm (net: an un-retract
    # is not new material); filament 2: 4 mm. Walls are not tower.
    assert t["prime_tower_length_mm"] == pytest.approx(16.0)
    expected = (12.0 * 1.27 + 4.0 * 1.50) * 2.4053 / 1000
    assert t["prime_tower_g"] == pytest.approx(expected)
    assert t["flush_volume_g"] == 0.0                       # L0 everywhere
    assert t["waste_g"] == pytest.approx(expected)          # purge + tower
    assert t["nozzle_swaps"] == 0                           # other nozzle took over


def test_same_nozzle_change_counts_as_purging_even_with_l0(tmp_path: Path):
    """Both filaments on nozzle 1: the L0 change still purges (into the tower)."""
    t = analyze_3mf(_tower_3mf(tmp_path / "s.gcode.3mf", "1 1")).totals
    assert t["nozzle_swaps"] == 1 and t["filament_changes"] == 1


def test_dry_run_reports_tower_and_gates_on_max_waste(tmp_path: Path, capsys):
    from beambam.cli.lan import _dry_run_report
    f = _tower_3mf(tmp_path / "d.gcode.3mf", "1 2")
    assert _dry_run_report(f, 10.0) == 0                    # purge 0 g passes --max-flush-g
    assert "prime tower" in capsys.readouterr().err
    assert _dry_run_report(f, 10.0, max_waste_g=0.01) == 2  # but not a waste cap
    assert "--max-waste-g" in capsys.readouterr().err
