"""A project's filaments set to the printer's spools (beambam.project_sync).

The miniature profile tree mirrors the bundled X2D ones: a family `@base` sets
the filament id, an `include` template sets the per-variant arrays and their
variant names, the printer preset sets its own values. The project is laid out
as Bambu Studio saves one: per-filament keys in filament-major blocks of one
entry per extruder variant, [print, filaments…, printer] lists, an extruders ×
n × n flush matrix. Checked against a GUI-saved X2D project (2026-11-26).
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beambam import project_sync as ps  # noqa: E402
from beambam import slice_options as so  # noqa: E402

VARIANTS = ["Direct Drive Standard", "Direct Drive High Flow", "Direct Drive E3D High Flow"]
PETG = "Bambu PETG HF @BBL X2D 0.4 nozzle"
PLA = "Bambu PLA Basic @BBL X2D 0.4 nozzle"


def _resources(root: Path) -> Path:
    """Bundled-style filament profiles: PETG HF and PLA Basic through @base parents and one include template
    whose variants are not the project's (no E3D, a Bowden one, a different order than a same-length list).
    Laid out as the phone build keeps them under `root`; returns the BBL profiles folder."""
    folder = root / "bs-bionic" / "resources" / "profiles" / "BBL" / "filament"
    folder.mkdir(parents=True)
    fil: dict[str, dict[str, Any]] = {
        "tmpl": {"instantiation": "false",
                 "filament_extruder_variant": ["Direct Drive Standard", "Bowden Standard", "Direct Drive High Flow"],
                 "filament_wipe": ["1", "1", "1"], "filament_retraction_length": ["nil", "nil", "nil"]},
        "Bambu PETG HF @base": {"filament_id": "GFG02", "filament_type": ["PETG"], "filament_vendor": ["Bambu Lab"],
                                "filament_wipe": ["0"], "filament_flow_ratio": ["0.95"]},
        PETG: {"inherits": "Bambu PETG HF @base", "include": ["tmpl"], "compatible_printers": [so.MESH_PRINTER],
               "setting_id": "GFSG02_12",
               # variant order of the template: DD Standard, Bowden Standard, DD High Flow
               "nozzle_temperature": ["250", "240", "260"], "filament_retraction_length": ["0.4", "nil", "nil"]},
        "Bambu PLA Basic @base": {"filament_id": "GFA00", "filament_type": ["PLA"], "filament_vendor": ["Bambu Lab"]},
        PLA: {"inherits": "Bambu PLA Basic @base", "include": "tmpl", "compatible_printers": [so.MESH_PRINTER],
              "nozzle_temperature": ["220"]},
    }
    for name, d in fil.items():
        (folder / f"{name}.json").write_text(json.dumps({"type": "filament", "name": name, **d}))
    so._filament_index.cache_clear()
    ps._filament_files.cache_clear()
    return root / "bs-bionic" / "resources" / "profiles" / "BBL"


def _settings(n: int = 3) -> dict[str, Any]:
    """An X2D-style project with n Generic PLA filaments, 3 variants each, 2 extruders."""
    k = len(VARIANTS)
    return {
        "filament_settings_id": ["Generic PLA @BBL X2D 0.4 nozzle"] * n,
        "filament_ids": ["GFL99"] * n,
        "filament_type": ["PLA"] * n,
        "filament_vendor": ["Generic"] * n,
        "filament_colour": ["#000000", "#FFFFFF", "#0000FF"][:n],
        "filament_extruder_variant": VARIANTS * n,
        "filament_self_index": [str(i + 1) for i in range(n) for _ in range(k)],
        "nozzle_temperature": ["210"] * (n * k),
        "filament_wipe": ["nil"] * (n * k),
        "filament_flow_ratio": ["0.98"] * (n * k),
        "filament_retraction_length": ["0.8"] * (n * k),
        # The same length as the filament list by coincidence: the bed outline, never a filament key.
        "printable_area": ["0x0", "300x0", "300x300"][:n],
        "nozzle_diameter": ["0.4", "0.4"],
        "different_settings_to_system": ["layer_height"] + ["filament_flow_ratio"] * n + [""],
        "inherits_group": ["0.20mm Standard @BBL X2D"] + ["Generic PLA @base"] * n + [""],
        "flush_volumes_matrix": [str(v) for v in [0, 1, 2, 3, 0, 4, 5, 6, 0] + [0, 10, 20, 30, 0, 40, 50, 60, 0]],
        "curr_bed_type": "Textured PEI Plate",
        "layer_height": "0.2",
    }


def test_profiles_flatten_parent_then_include_then_own(tmp_path):
    root = _resources(tmp_path / "r")
    v = ps.flatten_profile(root, so.filament_profile(root, "GFG02", "PETG"))
    assert v["name"] == PETG and v["filament_id"] == "GFG02" and v["filament_type"] == ["PETG"]
    assert v["filament_wipe"] == ["1", "1", "1"]                 # the include over the parent
    assert v["filament_retraction_length"] == ["0.4", "nil", "nil"]  # own keys over the include
    (root / "filament" / "Loop.json").write_text(
        json.dumps({"name": "Loop", "inherits": "Gone"}))
    ps._filament_files.cache_clear()
    with pytest.raises(ValueError, match="missing 'Gone'"):
        ps.flatten_profile(root, so._profiles(root, "filament") / "Loop.json")


def test_spool_presets_replace_their_filaments_blocks_by_variant(tmp_path):
    root = _resources(tmp_path / "r")
    st = _settings()
    before = json.loads(json.dumps(st))
    out = ps.adopt_filaments(st, root, [("GFG02", "PETG", "#ff0000"), ("GFA00", None, "00FF00")])
    assert st == before                                          # the input is not changed
    assert out["filament_settings_id"] == [PETG, PLA, "Generic PLA @BBL X2D 0.4 nozzle"]
    assert out["filament_ids"] == ["GFG02", "GFA00", "GFL99"]
    assert out["filament_type"] == ["PETG", "PLA", "PLA"]
    assert out["filament_vendor"] == ["Bambu Lab", "Bambu Lab", "Generic"]
    assert out["filament_colour"] == ["#FF0000", "#00FF00", "#0000FF"]
    # Variant arrays by name: E3D High Flow takes the drive's High Flow value; filament 3 untouched.
    assert out["nozzle_temperature"] == ["250", "260", "260", "220", "220", "220", "210", "210", "210"]
    # "nil" is written through (the project stores it), not kept from the previous filament.
    assert out["filament_retraction_length"] == ["0.4", "nil", "nil", "nil", "nil", "nil", "0.8", "0.8", "0.8"]
    assert out["filament_wipe"] == ["1"] * 6 + ["nil"] * 3
    assert out["filament_flow_ratio"] == ["0.95"] * 3 + ["0.98"] * 6   # a single value repeated per variant
    # Layout keys and non-filament lists keep their shape and values.
    assert out["filament_extruder_variant"] == VARIANTS * 3
    assert out["filament_self_index"] == st["filament_self_index"]
    assert out["printable_area"] == st["printable_area"] and out["nozzle_diameter"] == ["0.4", "0.4"]
    assert out["different_settings_to_system"] == ["layer_height", "", "", "filament_flow_ratio", ""]
    assert out["inherits_group"] == ["0.20mm Standard @BBL X2D", "", "", "Generic PLA @base", ""]
    assert out["layer_height"] == "0.2" and out["curr_bed_type"] == "Textured PEI Plate"
    assert all(len(out[k]) == len(st[k]) for k in st if isinstance(st[k], list))
    assert "compatible_printers" not in out and "setting_id" not in out and "include" not in out


def test_a_spool_with_no_preset_info_only_recolours_and_extra_spools_are_ignored(tmp_path):
    root = _resources(tmp_path / "r")
    st = _settings(2)
    out = ps.adopt_filaments(st, root, [(None, None, "#123456"), ("GFG02", "PETG", "#000000"),
                                        ("GFA00", "PLA", "#FFFFFF")])
    assert out["filament_settings_id"] == ["Generic PLA @BBL X2D 0.4 nozzle", PETG]
    assert out["filament_colour"] == ["#123456", "#000000"]
    assert out["nozzle_temperature"][:3] == ["210"] * 3
    assert len(out["filament_settings_id"]) == 2 and len(out["nozzle_temperature"]) == 6


def test_a_spool_whose_filament_has_no_bundled_profile_says_which_preset_stands_in(tmp_path):
    """Seen live: Bambu PLA Pure (GFA19) and eSUN PLA+ (GFL03) have no X2D profile in the bundled build; they
    slice with PLA Basic, and the note says so (else the plan's "profile GFA00 vs loaded GFA19" reads as a
    failed sync)."""
    root = _resources(tmp_path / "r")
    notes: list[str] = []
    out = ps.adopt_filaments(_settings(2), root, [("GFA19", "PLA", "#FFFFFF"), ("GFA00", "PLA", "#000000")], notes)
    assert out["filament_settings_id"] == [PLA, PLA]
    assert notes == [f"filament 1: no bundled X2D profile for GFA19: it slices with {PLA} (same material), "
                     "not that filament's own temperatures and flow"]
    # One note per filament, not per slot (a full AMS of eSUN PLA+ gave seven near-identical lines).
    notes = []
    ps.adopt_filaments(_settings(4), root, [("GFL03", "PLA", "#FF0000"), ("GFL03", "PLA", "#00FF00"),
                                            ("GFA19", "PLA", "#0000FF"), ("GFL03", "PLA", "#000000")], notes)
    assert notes == [f"filaments 1, 2 and 4: no bundled X2D profile for GFL03: they slice with {PLA} (same "
                     "material), not that filament's own temperatures and flow",
                     f"filament 3: no bundled X2D profile for GFA19: it slices with {PLA} (same material), "
                     "not that filament's own temperatures and flow"]
    notes = []
    ps.adopt_filaments(_settings(2), root, [("GFZZ9", "PPS-CF", "#FF0000"), ("GFZZ9", "PPS-CF", "#00FF00")], notes)
    assert notes == ["filaments 1 and 2: no bundled Bambu profile for PPS-CF: they keep the project's preset "
                     "(Generic PLA @BBL X2D 0.4 nozzle); only the colour changed"]


def test_flush_matrix_follows_the_closest_old_colours(tmp_path):
    root = _resources(tmp_path / "r")
    # Black's slot turns red (closest old colour: black, so its row stays); white's turns almost-blue (closest:
    # the blue slot, which is unchanged) — that pair has no measured volume and gets the median.
    out = ps.adopt_filaments(_settings(), root, [(None, None, "#FF0000"), (None, None, "#0000FEAA")])
    assert out["filament_colour"] == ["#FF0000", "#0000FE", "#0000FF"]
    assert out["flush_volumes_matrix"] == ["0", "2", "2", "5", "0", "3.5", "5", "3.5", "0",
                                           "0", "20", "20", "50", "0", "35", "50", "35", "0"]
    same = ps.adopt_filaments(_settings(), root, [(None, None, "#000000")])
    assert same["flush_volumes_matrix"] == [f"{float(v):g}" for v in _settings()["flush_volumes_matrix"]]


def test_bad_input_is_refused(tmp_path):
    root = _resources(tmp_path / "r")
    with pytest.raises(ValueError, match="colour"):
        ps.adopt_filaments(_settings(), root, [("GFG02", "PETG", "red")])
    with pytest.raises(ValueError, match="filament list"):
        ps.adopt_filaments({"layer_height": "0.2"}, root, [])


def _project(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        z.writestr(ps.SETTINGS_MEMBER, json.dumps(_settings(), indent=4))
        z.writestr("Metadata/model_settings.config", "<config/>")
        z.writestr(zipfile.ZipInfo("Auxiliaries/.thumbnails/thumbnail_3mf.png"), bytes(range(256)) * 4)
    return path


def test_adopt_project_rewrites_only_the_settings(tmp_path):
    root = _resources(tmp_path / "r")
    src, dst = _project(tmp_path / "p.3mf"), tmp_path / "out.3mf"
    new = ps.adopt_project(src, dst, root, [("GFG02", "PETG", "#FF0000")], bed_type="Cool Plate")
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
        assert b.namelist() == a.namelist()
        for info in a.infolist():
            if info.filename != ps.SETTINGS_MEMBER:
                assert b.read(info.filename) == a.read(info.filename)
                assert b.getinfo(info.filename).compress_type == info.compress_type
        written = json.loads(b.read(ps.SETTINGS_MEMBER))
    assert written == new
    assert written["curr_bed_type"] == "Cool Plate" and written["filament_settings_id"][0] == PETG
    kept = ps.adopt_project(src, tmp_path / "kept.3mf", root, [])
    assert kept["curr_bed_type"] == "Textured PEI Plate"
    assert not list(tmp_path.glob("*.part"))


def test_adopt_project_refuses_a_bad_plate_or_a_non_project(tmp_path):
    root = _resources(tmp_path / "r")
    src = _project(tmp_path / "p.3mf")
    with pytest.raises(ValueError, match="bed type"):
        ps.adopt_project(src, tmp_path / "x.3mf", root, [], bed_type="Smooth PEI")
    assert not (tmp_path / "x.3mf").exists()
    with zipfile.ZipFile(tmp_path / "mesh.3mf", "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(ValueError, match="not a Bambu Studio project"):
        ps.adopt_project(tmp_path / "mesh.3mf", tmp_path / "y.3mf", root, [])
    (tmp_path / "junk.3mf").write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="not a 3MF"):
        ps.adopt_project(tmp_path / "junk.3mf", tmp_path / "z.3mf", root, [])


_BUNDLED = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not (_BUNDLED / "bs-bionic" / "resources" / "profiles" / "BBL" / "filament").is_dir(),
                    reason="bs-bionic's bundled profiles are not installed")
def test_bundled_profiles_flatten():
    so._filament_index.cache_clear()
    ps._filament_files.cache_clear()
    bbl = _BUNDLED / "bs-bionic" / "resources" / "profiles" / "BBL"
    v = ps.flatten_profile(bbl, so.filament_profile(bbl, "GFG02", "PETG"))
    assert v["filament_type"] == ["PETG"] and ps._resolved_id(v) == "GFG02"
    assert len(v["filament_extruder_variant"]) == len(v["nozzle_temperature"])


# --------------------------------------------------------------------------- through the slice options and HTTP

def _zip(path: Path, settings: dict[str, Any]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        z.writestr("Metadata/model_settings.config", "<config><plate></plate></config>")
        z.writestr("3D/3dmodel.model", "<model/>")
    return path


def test_spools_option_parses_and_refuses_junk():
    o = so.SliceOptions.from_dict({"spools": [{"id": "GFG02", "type": "PETG", "colour": "#ff0000ff"},
                                              {"id": None, "type": "PLA", "colour": "#00FF00"}]})
    assert o.spools == (("GFG02", "PETG", "#FF0000"), (None, "PLA", "#00FF00"))
    assert o.filament(0) == ("GFG02", "PETG") and o.filament(1) == (None, "PLA") and o.filament(2) == (None, None)
    for bad in ([], [{"id": "GFG02"}], [{"colour": "red"}], [{"id": "<b>", "colour": "#000000"}],
                [{"id": "GFG02", "colour": "#000000", "x": 1}], "GFG02"):
        with pytest.raises(ValueError):
            so.SliceOptions.from_dict({"spools": bad})


def test_apply_to_project_takes_the_spools_and_the_plate(tmp_path):
    root = _resources(tmp_path / "r")
    src = _zip(tmp_path / "p.3mf", _settings())
    opts = so.SliceOptions(spools=(("GFG02", "PETG", "#FF0000"),), bed_type="Supertack Plate", nozzles=(2,))
    out = so.apply_to_project(src, tmp_path / "o.3mf", opts, root)
    assert out["filament_settings_id"][0] == PETG and out["filament_colour"][0] == "#FF0000"
    assert out["curr_bed_type"] == "Supertack Plate" and out["filament_map"] == ["2", "1", "1"]
    with pytest.raises(ValueError, match="profiles folder"):
        so.apply_to_project(src, tmp_path / "o2.3mf", opts)


def test_post_project_update_hands_the_project_back(tmp_path, monkeypatch):
    import urllib.error
    import urllib.request

    import beambam.slice_service as ss
    from tests.test_http_camera import _daemon
    _resources(tmp_path / "r")
    monkeypatch.setattr(ss, "_root", lambda: tmp_path / "r")      # the phone build's tree, without its slicer
    port = _daemon("http://127.0.0.1:9")
    body = _zip(tmp_path / "p.3mf", _settings()).read_bytes()
    opts = json.dumps({"spools": [{"id": "GFA00", "type": "PLA", "colour": "#123456"}], "bed_type": "Supertack Plate"})

    def post(data: bytes, name: str, options: str = opts):
        r = urllib.request.Request(f"http://127.0.0.1:{port}/project/update", data=data, method="POST",
                                   headers={"X-Filename": name, "X-Slice-Options": options,
                                            "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    code, data, headers = post(body, "jolteon.3mf")
    assert code == 200 and headers["Content-Disposition"] == 'attachment; filename="jolteon_printer.3mf"'
    out = tmp_path / "back.3mf"
    out.write_bytes(data)
    with zipfile.ZipFile(out) as z:
        st = json.loads(z.read("Metadata/project_settings.config"))
        assert z.read("3D/3dmodel.model") == b"<model/>"
    assert st["filament_settings_id"][0] == PLA and st["filament_colour"][0] == "#123456"
    assert st["curr_bed_type"] == "Supertack Plate"
    assert post(body, "x.gcode.3mf")[0] == 400
    assert post(body, "x.3mf", '{"spools": "no"}')[0] == 400
    geometry = tmp_path / "g.3mf"
    with zipfile.ZipFile(geometry, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    code, data, _ = post(geometry.read_bytes(), "g.3mf")
    assert code == 400 and b"not a Bambu Studio project" in data
