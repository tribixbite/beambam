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
                {"filament_ids": "GFA00"}):
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
    """A miniature bs-bionic profile tree: two process heights, a filament family with ids through inherits."""
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
    return root


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
