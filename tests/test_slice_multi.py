"""tests/test_slice_multi.py — multi-instance + multi-color slicer helpers.

Unit tests for the helpers added this session:
  * `_grid_layout` — N copies tiled on a plate
  * `_patch_wrapper_for_copies` — N-object structure in 3D/3dmodel.model
  * `_patch_model_settings_for_copies` — matching <object>+<model_instance>
  * `patch_project_settings_for_multi_color` — expand filament_* lists

No BS CLI required — these test the static 3MF rewriting only.
"""
from __future__ import annotations
import json
import re
import struct
import tempfile
import zipfile
from pathlib import Path

import pytest

from x2d_slice import (
    _grid_layout,
    _bbox_xy,
    _bbox_z,
    build_3mf_object,
    _patch_wrapper_for_copies,
    _patch_model_settings_for_copies,
    patch_project_settings_for_color,
    patch_project_settings_for_multi_color,
    patch_model_settings_for_per_object_extruder,
    graft_stl_into_template,
    resolve_color_name,
    DEFAULT_TEMPLATE,
)


# ----- _grid_layout -------------------------------------------------------


def test_grid_layout_one_copy():
    """Single copy stays at the model's native origin."""
    assert _grid_layout(1, 30, 30) == [(0.0, 0.0)]


def test_grid_layout_four_copies_2x2():
    """4 copies tile on a 2×2 grid with `margin` mm of gap."""
    placements = _grid_layout(4, 30, 30, margin=5.0)
    assert len(placements) == 4
    cell = 30 + 5  # cell_w = w + margin
    assert (0.0, 0.0) in placements
    assert (cell, 0.0) in placements
    assert (0.0, cell) in placements
    assert (cell, cell) in placements


def test_grid_layout_nine_copies_3x3():
    placements = _grid_layout(9, 20, 20, margin=5.0)
    assert len(placements) == 9
    cell = 25
    # Should be a 3×3 grid (cols=ceil(sqrt(9))=3, rows=ceil(9/3)=3)
    xs = sorted({p[0] for p in placements})
    ys = sorted({p[1] for p in placements})
    assert xs == [0.0, cell, 2 * cell]
    assert ys == [0.0, cell, 2 * cell]


def test_grid_layout_raises_when_too_many_copies():
    """100 copies of a 30×30 model don't fit a 256×256 plate."""
    with pytest.raises(ValueError, match="can't fit"):
        _grid_layout(100, 30, 30)


def test_grid_layout_just_fits_at_edge():
    """Exactly N copies that fill the plate should succeed."""
    # 4 copies of 100×100 with 5mm margin → cell 105 × 2 + 5 margin = 215 ≤ 256
    placements = _grid_layout(4, 100, 100, margin=5.0)
    assert len(placements) == 4


# ----- _bbox helpers ------------------------------------------------------


def test_bbox_xy_empty():
    assert _bbox_xy([]) == (0.0, 0.0, 0.0, 0.0)


def test_bbox_xy_simple():
    verts = [(0, 0, 0), (10, 0, 5), (0, 20, 2), (5, 5, 8)]
    assert _bbox_xy(verts) == (0, 10, 0, 20)


def test_bbox_z_simple():
    verts = [(0, 0, -3), (10, 0, 5), (0, 20, 0)]
    assert _bbox_z(verts) == (-3, 5)


# ----- build_3mf_object ---------------------------------------------------


def _tiny_mesh():
    """A 20mm cube as (vlist, tris). Pre-shared across tests."""
    verts = [
        (0, 0, 0), (20, 0, 0), (0, 20, 0), (20, 20, 0),
        (0, 0, 30), (20, 0, 30), (0, 20, 30), (20, 20, 30),
    ]
    tris = [
        (0, 1, 2), (1, 3, 2), (4, 6, 5), (5, 6, 7),
        (0, 4, 1), (1, 4, 5), (2, 3, 6), (3, 7, 6),
        (0, 2, 4), (2, 6, 4), (1, 5, 3), (3, 5, 7),
    ]
    return verts, tris


def test_build_3mf_object_single_item():
    """copies=1 emits exactly one <item objectid="1"/> entry."""
    verts, tris = _tiny_mesh()
    xml = build_3mf_object(verts, tris, scale=1.0, copies=1)
    items = re.findall(r'<item\s+objectid="1"', xml)
    assert len(items) == 1


def test_build_3mf_object_scale_applied_to_verts():
    """Scale is baked into vertex coordinates (since BS CLI doesn't honor
    the build-item transform during slicing)."""
    verts, tris = _tiny_mesh()
    xml_1 = build_3mf_object(verts, tris, scale=1.0)
    xml_05 = build_3mf_object(verts, tris, scale=0.5)
    # Scaled output should contain a vertex with x=10.0 (20 * 0.5)
    assert 'x="10.0"' in xml_05 or 'x="10.0' in xml_05
    # Unscaled has x="20.0"
    assert 'x="20.0"' in xml_1


# ----- _patch_wrapper_for_copies + _patch_model_settings_for_copies -------


def test_patch_wrapper_for_copies_emits_n_objects():
    """Multi-object structure: 4 copies → 4 <object> in resources +
    4 <item> in <build>."""
    verts, _ = _tiny_mesh()
    template = DEFAULT_TEMPLATE
    with zipfile.ZipFile(template) as z:
        wrapper = z.read("3D/3dmodel.model")
    patched = _patch_wrapper_for_copies(wrapper, copies=4, vlist=verts, scale=1.0)
    text = patched.decode("utf-8")
    objs = re.findall(r'<object\s+id="(\d+)"', text)
    items = re.findall(r'<item\s+objectid="(\d+)"', text)
    # Should be 4 objects (ids 2, 3, 4, 5) + 4 items referencing each
    assert len(objs) == 4
    assert sorted(int(x) for x in objs) == [2, 3, 4, 5]
    assert len(items) == 4
    assert sorted(int(x) for x in items) == [2, 3, 4, 5]


def test_patch_wrapper_for_copies_unique_transforms():
    """Each <item> gets a distinct (dx, dy) so copies don't overlap."""
    verts, _ = _tiny_mesh()
    with zipfile.ZipFile(DEFAULT_TEMPLATE) as z:
        wrapper = z.read("3D/3dmodel.model")
    patched = _patch_wrapper_for_copies(wrapper, copies=4, vlist=verts, scale=1.0)
    transforms = re.findall(r'<item[^/]*transform="([^"]+)"', patched.decode("utf-8"))
    assert len(transforms) == 4
    translations = set()
    for t in transforms:
        nums = t.split()
        assert len(nums) == 12
        # Last 3 are translation
        translations.add((float(nums[9]), float(nums[10]), float(nums[11])))
    # All 4 placements should be unique
    assert len(translations) == 4


def test_patch_wrapper_for_copies_one_copy_is_noop():
    """copies=1 returns the input bytes unchanged."""
    verts, _ = _tiny_mesh()
    with zipfile.ZipFile(DEFAULT_TEMPLATE) as z:
        wrapper = z.read("3D/3dmodel.model")
    patched = _patch_wrapper_for_copies(wrapper, copies=1, vlist=verts, scale=1.0)
    assert patched == wrapper


def test_patch_model_settings_for_copies():
    """4 copies → 4 <object> blocks + 4 <model_instance> blocks with
    distinct object_id refs."""
    verts, _ = _tiny_mesh()
    with zipfile.ZipFile(DEFAULT_TEMPLATE) as z:
        cfg = z.read("Metadata/model_settings.config")
    patched = _patch_model_settings_for_copies(cfg, copies=4, vlist=verts, scale=1.0)
    text = patched.decode("utf-8")
    cfg_objs = re.findall(r'<object\s+id="(\d+)"', text)
    obj_id_refs = re.findall(r'object_id"\s+value="(\d+)"', text)
    # 4 <object> + 4 <model_instance> entries; the latter has 4 ids that
    # match the former.
    assert len(cfg_objs) == 4
    # model_instance entries reference the object ids — should match the
    # set of object_ids exactly.
    inst_refs = obj_id_refs[-4:]
    assert sorted(inst_refs) == sorted(cfg_objs)


# ----- color resolution + multi-color expansion ---------------------------


def test_resolve_color_name_hex_passthrough():
    """Hex input should round-trip cleanly (with the leading #)."""
    assert "FF0000" in resolve_color_name("#FF0000").upper()
    assert "E4BD68" in resolve_color_name("#E4BD68").upper()


def test_resolve_color_name_known_name():
    """'Gold' resolves to a real Bambu color hex."""
    result = resolve_color_name("Gold")
    assert result, "no color resolved for 'Gold'"
    # E4BD68 is the PLA Silk Gold hex
    assert "E4BD68" in result.upper() or "FFD700" in result.upper()


def test_patch_project_settings_for_color_replaces_first_entry():
    """Single-color patch replaces filament_colour[0] only."""
    with zipfile.ZipFile(DEFAULT_TEMPLATE) as z:
        proj = z.read("Metadata/project_settings.config")
    patched = patch_project_settings_for_color(proj, "FF0000")
    j = json.loads(patched)
    assert j["filament_colour"][0] == "#FF0000"


def test_patch_project_settings_for_multi_color_expands_lists():
    """4 colors → filament_colour has 4 entries + parallel filament_*
    lists are expanded to 4 entries each + flush_volumes_matrix is N²."""
    with zipfile.ZipFile(DEFAULT_TEMPLATE) as z:
        proj = z.read("Metadata/project_settings.config")
    colors = ["#FF0000", "#00FF00", "#0000FF", "#FFFF00"]
    patched = patch_project_settings_for_multi_color(proj, colors)
    j = json.loads(patched)
    assert j["filament_colour"] == colors
    # Parallel lists that were len=1 in the template should be len=4
    for key in ("filament_type", "filament_settings_id",
                "filament_ids", "filament_vendor"):
        assert len(j[key]) == 4, f"{key} not expanded: {j.get(key)}"
    # flush_volumes_matrix should be N² = 16
    assert len(j["flush_volumes_matrix"]) == 16
    # flush_volumes_vector should be 2N = 8
    assert len(j["flush_volumes_vector"]) == 8


def test_patch_project_settings_for_multi_color_empty_noop():
    """Empty color list returns the input unchanged."""
    with zipfile.ZipFile(DEFAULT_TEMPLATE) as z:
        proj = z.read("Metadata/project_settings.config")
    assert patch_project_settings_for_multi_color(proj, []) == proj


# ----- per-object extruder + safety guard ---------------------------------


def test_patch_per_object_extruder_one_copy_is_noop():
    """The per-object-extruder helper is a no-op for copies<=1."""
    with zipfile.ZipFile(DEFAULT_TEMPLATE) as z:
        cfg = z.read("Metadata/model_settings.config")
    assert patch_model_settings_for_per_object_extruder(cfg, 1) == cfg


# ----- end-to-end graft (no slicer call) ----------------------------------


def _write_cube_stl() -> Path:
    """Build a tiny 20×20×30 cube as a binary STL."""
    verts, tris = _tiny_mesh()
    out = Path(tempfile.mkdtemp(prefix="t_slice_")) / "cube.stl"
    with open(out, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            f.write(struct.pack("<3f", 0, 0, 1))  # bogus normal
            for vidx in (a, b, c):
                f.write(struct.pack("<3f", *verts[vidx]))
            f.write(b"\x00\x00")
    return out


def test_graft_4_copies_produces_valid_3mf():
    """End-to-end graft: cube STL + --copies 4 produces a 3MF where
    both the wrapper and model_settings.config have 4 object entries."""
    stl = _write_cube_stl()
    out = stl.parent / "out.gcode.3mf"
    graft_stl_into_template(DEFAULT_TEMPLATE, stl, out,
                             scale=1.0, color=None, bed_type=None, copies=4)
    with zipfile.ZipFile(out) as z:
        wrap = z.read("3D/3dmodel.model").decode("utf-8")
        cfg = z.read("Metadata/model_settings.config").decode("utf-8")
    wrap_objs = re.findall(r'<object\s+id="(\d+)"', wrap)
    cfg_objs = re.findall(r'<object\s+id="(\d+)"', cfg)
    assert len(wrap_objs) == 4
    assert len(cfg_objs) == 4
    assert sorted(wrap_objs) == sorted(cfg_objs)


def test_graft_with_multi_color_expands_filaments():
    """End-to-end graft with --colors expands filament_colour list."""
    stl = _write_cube_stl()
    out = stl.parent / "out_multi.gcode.3mf"
    graft_stl_into_template(DEFAULT_TEMPLATE, stl, out,
                             scale=1.0, color=None, bed_type=None,
                             copies=4,
                             colors=["#FF0000", "#00FF00", "#0000FF", "#FFFF00"])
    with zipfile.ZipFile(out) as z:
        proj = json.loads(z.read("Metadata/project_settings.config"))
    assert len(proj["filament_colour"]) == 4
    assert proj["filament_colour"] == ["#FF0000", "#00FF00", "#0000FF", "#FFFF00"]
    assert len(proj["flush_volumes_matrix"]) == 16
