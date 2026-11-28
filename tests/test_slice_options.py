"""`POST /slice` options and bare-mesh slicing (beambam.slice_options).

The CLI facts behind them were checked on the X2D's bundled Bambu Studio
(2026-11-25/26): the nozzle grouping is read from the plate first; a line no
wider than the layer fails with code -51; a user process profile overlays its
keys while a user filament profile crashes the CLI.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beambam import slice_options as so  # noqa: E402


def test_options_parse_and_refuse_what_the_slicer_would_not_take():
    assert so.SliceOptions.from_header(None) == so.SliceOptions()
    assert so.SliceOptions.from_header("  ") == so.SliceOptions()
    o = so.SliceOptions.from_header(json.dumps({"layer_height": 0.2, "wall_loops": 3, "infill": 20,
                                                "supports": "plate", "brim": "no_brim", "nozzles": [1, 2],
                                                "filament_ids": ["GFA00"], "filament_types": ["PLA"]}))
    assert (o.layer_height, o.wall_loops, o.infill, o.nozzles, o.filament_ids) == (0.2, 3, 20, (1, 2), ("GFA00",))
    for bad in ({"layer_height": 0.5}, {"wall_loops": 2.5}, {"infill": True}, {"supports": "always"},
                {"brim": "mouse_ear"}, {"nozzles": [1, 3]}, {"nozzles": "12"}, {"colour": "red"},
                {"filament_ids": "GFA00"}, {"bed_type": "Glass Plate"}, {"filament_colours": ["#FFFFFF"]}):
        with pytest.raises(ValueError):
            so.SliceOptions.from_dict(bad)
    with pytest.raises(ValueError):
        so.SliceOptions.from_header("{not json")
    with pytest.raises(ValueError):
        so.SliceOptions.from_header(json.dumps({"filament_types": ["PLA"] * 64, "x": "y" * 5000}))


def test_process_keys_and_a_tall_layer_widening_thin_lines():
    current = {"outer_wall_line_width": "0.2", "line_width": "0.42", "support_line_width": "0",
               "top_surface_line_width": "100%", "support_type": "normal(auto)"}
    keys = so.SliceOptions(layer_height=0.24, wall_loops=4, infill=30, supports="plate",
                           brim="outer_only").process_keys(current)
    assert keys == {"layer_height": "0.24", "outer_wall_line_width": "0.26", "wall_loops": "4",
                    "sparse_infill_density": "30%", "enable_support": "1", "support_on_build_plate_only": "1",
                    "brim_type": "outer_only"}
    assert so.SliceOptions(supports="off").process_keys() == {"enable_support": "0"}
    assert so.SliceOptions(supports="auto").process_keys()["support_type"] == "tree(auto)"
    assert so.SliceOptions().process_keys(current) == {}
    # The plate: Bambu's own name, written as the project's / process's `curr_bed_type` (checked on an STL slice).
    bed = so.SliceOptions.from_dict({"bed_type": "Supertack Plate"})
    assert bed.process_keys() == {"curr_bed_type": "Supertack Plate"}


def test_nozzle_map_pads_left_and_cuts_to_the_project():
    assert so.nozzle_map((2, 1), 4) == ["2", "1", "1", "1"]
    assert so.nozzle_map((2, 2, 2), 2) == ["2", "2"]


def test_plate_map_replaces_any_earlier_map_on_every_plate():
    ms = ('<config>\n  <plate>\n    <metadata key="plater_id" value="1"/>\n'
          '    <metadata key="filament_map_mode" value="Auto For Flush"/>\n'
          '    <metadata key="filament_maps" value="1 1"/>\n  </plate>\n'
          '  <plate>\n    <metadata key="plater_id" value="2"/>\n  </plate>\n</config>\n')
    out = so.plate_map(ms, ["2", "1"])
    assert out.count('<metadata key="filament_map_mode" value="Manual"/>') == 2
    assert out.count('<metadata key="filament_maps" value="2 1"/>') == 2
    assert "Auto For Flush" not in out and 'value="1 1"' not in out


def _project(path: Path, n: int = 2) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps({
            "filament_settings_id": [f"F{i}" for i in range(n)], "layer_height": "0.16",
            "outer_wall_line_width": "0.2", "filament_map_mode": "Auto For Flush", "filament_map": ["1"] * n}))
        z.writestr("Metadata/model_settings.config",
                   '<config>\n  <plate>\n    <metadata key="plater_id" value="1"/>\n  </plate>\n</config>\n')
        z.writestr("3D/3dmodel.model", "<model/>")
    return path


def test_apply_to_project_writes_settings_and_the_plate(tmp_path):
    src = _project(tmp_path / "p.3mf")
    settings = so.apply_to_project(src, tmp_path / "o.3mf",
                                   so.SliceOptions(layer_height=0.2, nozzles=(2,), infill=10))
    assert (settings["layer_height"], settings["outer_wall_line_width"], settings["sparse_infill_density"],
            settings["filament_map_mode"], settings["filament_map"]) == ("0.2", "0.22", "10%", "Manual", ["2", "1"])
    with zipfile.ZipFile(tmp_path / "o.3mf") as z:
        assert json.loads(z.read("Metadata/project_settings.config")) == settings
        ms = z.read("Metadata/model_settings.config").decode()
        assert z.read("3D/3dmodel.model") == b"<model/>"
    assert '<metadata key="filament_maps" value="2 1"/>' in ms[ms.index("<plate>"):ms.index("</plate>")]
    assert so.has_print_settings(src)


def _resources(root: Path) -> Path:
    """A miniature profile tree (the phone build's layout under `root`): two process heights, a filament family
    with ids through inherits. Returns the BBL profiles folder."""
    base = root / "bs-bionic" / "resources" / "profiles" / "BBL"
    for kind in ("machine", "process", "filament"):
        (base / kind).mkdir(parents=True)
    (base / "machine" / f"{so.MESH_PRINTER}.json").write_text("{}")
    for name in ("0.20mm Standard @BBL X2D", "0.16mm Standard @BBL X2D", "0.16mm High Quality @BBL X2D",
                 "0.16mm Standard @BBL X2D 0.6 nozzle"):
        (base / "process" / f"{name}.json").write_text(json.dumps(
            {"name": name, "compatible_printers": [so.MESH_PRINTER], "outer_wall_line_width": "0.2"}))
    fil = {"Bambu PLA Basic @base": {"filament_id": "GFA00", "filament_type": ["PLA"]},
           "Bambu PLA Basic @BBL X2D 0.4 nozzle": {"inherits": "Bambu PLA Basic @base",
                                                   "compatible_printers": [so.MESH_PRINTER]},
           "Bambu PETG HF @base": {"filament_id": "GFG02", "filament_type": ["PETG"]},
           "Bambu PETG HF @BBL X2D 0.4 nozzle": {"inherits": "Bambu PETG HF @base",
                                                 "compatible_printers": [so.MESH_PRINTER]},
           "Generic ABS @base": {"filament_id": "GFB99", "filament_type": ["ABS"]},
           "Generic ABS @BBL X2D 0.4 nozzle": {"inherits": "Generic ABS @base",
                                               "compatible_printers": [so.MESH_PRINTER]}}
    for name, d in fil.items():
        (base / "filament" / f"{name}.json").write_text(json.dumps({"name": name, **d}))
    so._filament_index.cache_clear()
    return base


def test_mesh_profiles_follow_the_spool_and_the_layer(tmp_path):
    root = _resources(tmp_path / "r")
    assert so.filament_profile(root, "GFG02", None).stem == "Bambu PETG HF @BBL X2D 0.4 nozzle"
    assert so.filament_profile(root, "GFX99", "ABS").stem == "Generic ABS @BBL X2D 0.4 nozzle"
    assert so.filament_profile(root, None, "petg").stem == "Bambu PETG HF @BBL X2D 0.4 nozzle"
    assert so.filament_profile(root, None, "NYLON").stem == so.DEFAULT_FILAMENT
    assert so.process_profile(root, None).stem == so.DEFAULT_PROCESS
    assert so.process_profile(root, 0.15).stem == "0.16mm Standard @BBL X2D"      # Standard, 0.4 nozzle
    out = tmp_path / "o"
    out.mkdir()
    args = so.mesh_args(root, so.SliceOptions(layer_height=0.2, wall_loops=3, nozzles=(2,),
                                              filament_ids=("GFG02",)), out)
    assert args[args.index("--filament-map") + 1] == "2" and "Manual" in args
    assert args[args.index("--load-filaments") + 1].endswith("Bambu PETG HF @BBL X2D 0.4 nozzle.json")
    user = json.loads((out / "beambam_process.json").read_text())
    assert (user["inherits"], user["from"], user["wall_loops"], user["outer_wall_line_width"],
            user["compatible_printers"]) == ("0.20mm Standard @BBL X2D", "User", "3", "0.22", [so.MESH_PRINTER])


def test_a_mesh_takes_the_flattened_filament_profiles(tmp_path):
    """`--load-filaments` does not follow `inherits`: the source profile alone sliced PLA with density 0 (0 g)
    and the CLI's default plate temperatures (seen on the phone: hot plate 45 instead of 55 °C)."""
    root = _resources(tmp_path / "r")
    from beambam.profile_flatten import flatten
    flatten(root)
    out = tmp_path / "o"
    out.mkdir()
    args = so.mesh_args(root, so.SliceOptions(filament_ids=("GFG02",)), out)
    fil = Path(args[args.index("--load-filaments") + 1])
    assert fil.parent.name == "filament_full" and json.loads(fil.read_text())["filament_id"] == "GFG02"
    assert so.flattened(tmp_path / "nowhere" / "filament" / "x.json").parent.name == "filament"   # none: as is


def test_a_project_for_another_printer_is_sliced_for_this_one(tmp_path):
    """A P2S project from MakerWorld: its own printer profile made a one-nozzle P2S print file (every filament
    group 0, the nozzle map ignored). It is sliced for the X2D, keeping its walls / infill / supports."""
    assert so.made_for_another_printer({"printer_settings_id": "Bambu Lab P2S 0.4 nozzle"}) == "Bambu Lab P2S 0.4 nozzle"
    assert so.made_for_another_printer({"printer_settings_id": "Bambu Lab X2D 0.6 nozzle"}) is None
    assert so.made_for_another_printer({}) is None
    settings = {"layer_height": "0.2", "filament_ids": ["GFA00", "GFG02"], "filament_type": ["PLA", "PETG"],
                "wall_loops": "5", "sparse_infill_density": "40%", "enable_support": "1", "outer_wall_speed": "200"}
    opts = so.retarget(settings, so.SliceOptions(nozzles=(2, 1)))
    assert (opts.layer_height, opts.filament_ids, opts.filament_types, opts.nozzles) == (
        0.2, ("GFA00", "GFG02"), ("PLA", "PETG"), (2, 1))
    assert so.retarget(settings, so.SliceOptions(layer_height=0.12, filament_ids=("GFL99",))).filament_ids == ("GFL99",)
    root = _resources(tmp_path / "r")
    out = tmp_path / "o"
    out.mkdir()
    args = so.mesh_args(root, opts, out, n_filaments=2, keep=settings, arrange=False)
    assert "--arrange" not in args                                   # the project's own placement
    user = json.loads((out / "beambam_process.json").read_text())
    assert (user["wall_loops"], user["sparse_infill_density"], user["enable_support"]) == ("5", "40%", "1")
    assert "outer_wall_speed" not in user                            # printer-specific: the bundled process's
    # The caller's options still win over the project's.
    so.mesh_args(root, so.SliceOptions(wall_loops=2), out, keep=settings)
    assert json.loads((out / "beambam_process.json").read_text())["wall_loops"] == "2"


def test_the_print_files_own_warnings_are_passed_on(tmp_path):
    """slice_info carries warnings result.json does not (seen: PETG on the SuperTack plate, 2026-11-26)."""
    from beambam.slice_service import sliced_warnings
    f = tmp_path / "x.gcode.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("Metadata/slice_info.config",
                   '<config><plate><warning msg="bed_temperature_too_high_than_filament" level="3" '
                   'error_code ="1000C001"  /><warning msg="some_new_thing" level="1"/></plate></config>')
    got = sliced_warnings(f)
    assert got[0].startswith("Bambu Studio: the plate's bed temperature is above")
    assert got[1] == "Bambu Studio: some new thing"
    assert sliced_warnings(tmp_path / "missing.3mf") == []


def test_the_plate_choice_replaces_each_plates_own_bed_type(tmp_path):
    src = tmp_path / "p.3mf"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps({"filament_settings_id": ["F0"],
                                                                   "curr_bed_type": "Textured PEI Plate"}))
        z.writestr("Metadata/model_settings.config", '<config>\n  <plate>\n    <metadata key="plater_id" value="1"/>\n'
                   '    <metadata key="bed_type" value="Textured PEI Plate"/>\n  </plate>\n</config>\n')
    so.apply_to_project(src, tmp_path / "o.3mf", so.SliceOptions(bed_type="Supertack Plate"))
    with zipfile.ZipFile(tmp_path / "o.3mf") as z:
        ms = z.read("Metadata/model_settings.config").decode()
    assert 'key="bed_type" value="Supertack Plate"' in ms and "Textured" not in ms


def test_a_spool_without_a_bundled_profile_keeps_the_project_preset_and_says_so(tmp_path):
    from beambam import project_sync as ps
    root = _resources(tmp_path / "r")
    ps._filament_files.cache_clear()
    st = {"filament_settings_id": ["Generic PLA @BBL X2D 0.4 nozzle"], "filament_type": ["PLA"],
          "filament_colour": ["#000000"], "printer_settings_id": "Bambu Lab X2D 0.4 nozzle"}
    notes: list[str] = []
    out = ps.adopt_filaments(st, root, [("GFZZ9", "PPS-CF", "#FF0000")], notes)
    assert out["filament_settings_id"] == st["filament_settings_id"] and out["filament_colour"] == ["#FF0000"]
    assert "no bundled Bambu profile for PPS-CF" in notes[0]
    notes = []
    out = ps.adopt_filaments({**st, "printer_settings_id": "Bambu Lab X2D 0.2 nozzle"}, root,
                             [("GFG02", "PETG", "#00FF00")], notes)
    assert out["filament_settings_id"] == st["filament_settings_id"] and "0.4 mm nozzle" in notes[0]


# ----- plate + per-object quantity options (multi-plate slicing) ---------

def test_plate_and_quantity_parse_from_header():
    h = json.dumps({"plate": 2, "quantity": {"4": [[0, 0], [28.5, 0], [57, 0]], "9": []}})
    o = so.SliceOptions.from_header(h)
    assert o.plate == 2
    assert o.quantity == {"4": ((0.0, 0.0), (28.5, 0.0), (57.0, 0.0)), "9": ()}


@pytest.mark.parametrize("bad", [
    {"plate": 0},
    {"plate": -1},
    {"plate": True},
    {"quantity": {"4": [[0]]}},              # offset not a pair
    {"quantity": {"4": [["a", "b"]]}},       # non-numeric offset
    {"quantity": {"notanid": [[0, 0]]}},     # key not an object id
    {"quantity": [1, 2]},                    # not an object
])
def test_bad_plate_or_quantity_rejected(bad):
    with pytest.raises(ValueError):
        so.SliceOptions.from_dict(bad)


def test_no_plate_or_quantity_by_default():
    o = so.SliceOptions.from_header(None)
    assert o.plate is None and o.quantity is None
