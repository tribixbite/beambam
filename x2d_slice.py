#!/usr/bin/env python3
"""x2d_slice.py — slice an STL with the X2D dual-extruder profile via BS CLI,
producing a .gcode.3mf whose metadata (weight, tray_info_idx, prediction)
matches what the GUI would produce.

Why this wrapper exists (resolves #97 in IMPROVEMENTS.md):

BambuStudio's `--slice` CLI mode supports two input forms:
  * a bare STL/STP/OBJ/etc. + `--load-settings <process>;<machine>` +
    `--load-filaments <filament>` — but the X2D dual-extruder profile
    expects 4 filament slots and the CLI doesn't synthesize the
    missing tray_info_idx / density linkage. Output ships with empty
    weight, GIF=Generic Input Filament, and prediction times off by
    ~50%.
  * an existing .gcode.3mf project file with all settings already
    embedded — re-slices correctly with weight, density, prediction
    matching the original.

This script bridges the gap: it takes an STL, opens a known-good
template .gcode.3mf, swaps in the STL's geometry, and feeds the
resulting hybrid 3MF to BS for re-slicing.

Usage:
    x2d_slice.py model.stl --out model.gcode.3mf
    x2d_slice.py model.stl --out model.gcode.3mf --template ref.gcode.3mf
    x2d_slice.py model.stl --out model.gcode.3mf --process 0.16mm

Default template lives at $X2D_ROOT/rumi_frame.gcode.3mf.
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

X2D_ROOT = Path(os.environ.get("X2D_ROOT", "/data/data/com.termux/files/home/git/x2d"))
DEFAULT_TEMPLATE = X2D_ROOT / "rumi_frame.gcode.3mf"
BS_BIN = X2D_ROOT / "bs-bionic" / "build" / "src" / "bambu-studio"
COLOR_CODES_JSON = (X2D_ROOT / "bs-bionic" / "resources" / "profiles"
                    / "BBL" / "filament" / "filaments_color_codes.json")


def resolve_color_name(spec: str) -> str:
    """Accept either a #RRGGBB / #RRGGBBAA hex literal or a Bambu color name
    (e.g. "Gold", "PLA Silk Gold", "GFA05 Gold") and return a hex string.

    Bambu's filaments_color_codes.json maps `fila_color_name.en` → hex per
    `fila_id` × `fila_type` × name. Accepted query forms:
      - "Gold"                   → first match by name (English)
      - "PLA Silk Gold"          → match by type+name
      - "GFA05 Gold"             → match by fila_id+name
    Case-insensitive. Falls through to the literal hex if it parses as one.
    """
    import json
    import re as _re

    s = spec.strip()
    # Already a hex literal? Pass through.
    if s.startswith("#"):
        body = s.lstrip("#")
        if _re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", body):
            return s
    # Try the color-codes JSON
    if not COLOR_CODES_JSON.exists():
        raise ValueError(f"--color not a hex literal and {COLOR_CODES_JSON} "
                         f"missing for name lookup; got {spec!r}")
    data = json.loads(COLOR_CODES_JSON.read_text())["data"]
    sl = s.lower()
    candidates = []
    for entry in data:
        nm = entry.get("fila_color_name", {}).get("en", "")
        ftype = entry.get("fila_type", "")
        fid = entry.get("fila_id", "")
        cols = entry.get("fila_color", [])
        if not (nm and cols):
            continue
        full = f"{fid} {ftype} {nm}".lower()
        # Build a flexible token-set match so "GFA05 Gold" matches
        # "GFA05 PLA Silk Gold", "PLA Silk Gold" matches "GFA05 PLA Silk Gold",
        # and bare "Gold" matches the first PLA-Basic Gold.
        toks = sl.split()
        if sl == nm.lower():
            candidates.append((0, fid, ftype, nm, cols[0]))           # exact name
        elif all(t in full for t in toks):
            candidates.append((1, fid, ftype, nm, cols[0]))           # all tokens present
        elif sl in full:
            candidates.append((2, fid, ftype, nm, cols[0]))           # contiguous subset
    if not candidates:
        raise ValueError(f"no Bambu color matches {spec!r}; try one of: "
                         f"Gold, Silver, Bronze, Copper, Champagne, "
                         f"or e.g. 'PLA Silk Gold' / 'GFA05 Gold'")
    candidates.sort()
    rank, fid, ftype, nm, hx = candidates[0]
    print(f"[x2d_slice] color {spec!r} → {hx} ({fid} {ftype} {nm})",
          file=sys.stderr)
    return hx

# 3MF model XML namespace
NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
ET.register_namespace("", NS_3MF)


def parse_stl(path: Path) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse a binary or ASCII STL into (vertices, triangles) — vertices
    deduplicated to keep the 3MF compact."""
    data = path.read_bytes()
    is_ascii = data[:5].lower() == b"solid" and b"\nfacet" in data[:512]
    verts: dict[tuple[float, float, float], int] = {}
    tris: list[tuple[int, int, int]] = []

    def add_vert(v: tuple[float, float, float]) -> int:
        # Quantize to 6 decimal places to dedup numerically-identical verts
        k = (round(v[0], 6), round(v[1], 6), round(v[2], 6))
        if k not in verts:
            verts[k] = len(verts)
        return verts[k]

    if is_ascii:
        # ASCII parser
        text = data.decode("utf-8", errors="replace")
        cur: list[tuple[float, float, float]] = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("vertex"):
                xyz = tuple(float(x) for x in line.split()[1:4])
                cur.append(xyz)
                if len(cur) == 3:
                    tris.append((add_vert(cur[0]), add_vert(cur[1]), add_vert(cur[2])))
                    cur = []
    else:
        # Binary STL: 80-byte header, 4-byte tri count, then 50 bytes per tri
        if len(data) < 84:
            raise ValueError(f"{path} too small to be an STL")
        n_tris = struct.unpack_from("<I", data, 80)[0]
        offset = 84
        for _ in range(n_tris):
            # Skip the 12-byte normal vector
            v1 = struct.unpack_from("<fff", data, offset + 12)
            v2 = struct.unpack_from("<fff", data, offset + 24)
            v3 = struct.unpack_from("<fff", data, offset + 36)
            tris.append((add_vert(v1), add_vert(v2), add_vert(v3)))
            offset += 50
    # Stable vertex order: dict insertion order
    vlist = list(verts.keys())
    return vlist, tris


def _bbox_xy(vlist) -> tuple[float, float, float, float]:
    """Return (min_x, max_x, min_y, max_y) of the vertex list."""
    if not vlist:
        return 0.0, 0.0, 0.0, 0.0
    xs = [v[0] for v in vlist]
    ys = [v[1] for v in vlist]
    return min(xs), max(xs), min(ys), max(ys)


def _bbox_z(vlist) -> tuple[float, float]:
    """Return (min_z, max_z) of the vertex list."""
    if not vlist:
        return 0.0, 0.0
    zs = [v[2] for v in vlist]
    return min(zs), max(zs)


def _grid_layout(copies: int, w: float, d: float,
                 plate_w: float = 256.0, plate_d: float = 256.0,
                 margin: float = 5.0) -> list[tuple[float, float]]:
    """Tile `copies` instances of a w×d footprint on a plate_w × plate_d
    plate with `margin` mm of gap around each. Returns the (dx, dy)
    translation per instance — the first instance keeps the model's
    native origin (dx=dy=0), subsequent instances are nudged on the grid.

    Plate default is the X2D's 256×256 build volume. If the requested
    grid doesn't fit, raises ValueError so the caller can ask the user
    to reduce --copies or --scale."""
    if copies <= 1:
        return [(0.0, 0.0)]
    import math
    cols = max(1, int(math.ceil(math.sqrt(copies))))
    rows = max(1, int(math.ceil(copies / cols)))
    cell_w = w + margin
    cell_d = d + margin
    needed_w = cols * cell_w + margin
    needed_d = rows * cell_d + margin
    if needed_w > plate_w or needed_d > plate_d:
        raise ValueError(
            f"can't fit {copies} copies of {w:.0f}×{d:.0f}mm on a "
            f"{plate_w:.0f}×{plate_d:.0f}mm plate (need {needed_w:.0f}×{needed_d:.0f}). "
            f"Reduce --copies or --scale.")
    return [((n % cols) * cell_w, (n // cols) * cell_d) for n in range(copies)]


def build_3mf_object(vlist, tris, scale: float = 1.0, copies: int = 1) -> str:
    """Generate a single-object 3D/Objects/object_1.model XML in the 3MF
    schema. Returns the XML as a string ready to write into the zip.

    `scale` is applied to vertex coordinates directly — BS CLI doesn't
    honour the build-item transform during slicing, only during GUI
    placement. Vertex-level scaling is the only path that actually
    changes the print volume.

    Note: `copies` is accepted for the function signature but the actual
    instance multiplier lives in `3D/3dmodel.model` (wrapper build block)
    and `Metadata/model_settings.config` (plate model_instance list);
    this file is purely the mesh source. See _patch_wrapper_for_copies()
    and _patch_model_settings_for_copies()."""
    _ = copies  # kept for back-compat with earlier graft call site
    s = float(scale)
    sio = []
    sio.append('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n')
    sio.append(
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS_3MF}" '
        f'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">\n'
    )
    sio.append("  <resources>\n")
    sio.append('    <object id="1" type="model">\n')
    sio.append("      <mesh>\n        <vertices>\n")
    for x, y, z in vlist:
        sio.append(f'          <vertex x="{x*s}" y="{y*s}" z="{z*s}"/>\n')
    sio.append("        </vertices>\n        <triangles>\n")
    for a, b, c in tris:
        sio.append(f'          <triangle v1="{a}" v2="{b}" v3="{c}"/>\n')
    sio.append("        </triangles>\n      </mesh>\n    </object>\n")
    sio.append("  </resources>\n")
    sio.append('  <build>\n    <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n  </build>\n')
    sio.append("</model>\n")
    return "".join(sio)


def _patch_wrapper_for_copies(xml_bytes: bytes, copies: int, vlist, scale: float) -> bytes:
    """Patch `3D/3dmodel.model` so it carries N **separate** <object>
    entries (ids 2..N+1), each pointing at the same underlying mesh via
    a <component> with a unique transform offset, and N matching <item>
    entries in <build>.

    BS CLI dedupes <model_instance> entries by `object_id` during 3MF
    parse (Format/bbs_3mf.cpp:4806 — `obj_inst_map.emplace(object_id, ...)`
    silently drops duplicates), so the "1 object, N model_instances"
    pattern that BS GUI uses internally is NOT honored by the CLI slicer.
    Multi-OBJECT is. Each copy becomes its own ModelObject in BS's data
    model and slices as a real separate instance."""
    if copies <= 1:
        return xml_bytes
    import re as _re
    text = xml_bytes.decode("utf-8", errors="replace")

    # Capture the existing <object id="2">...</object> as a template.
    obj_m = _re.search(r'(<object\s+id="(\d+)"[^>]*>.*?</object>)', text, _re.DOTALL)
    if not obj_m:
        return xml_bytes
    obj_proto = obj_m.group(1)
    base_obj_id = int(obj_m.group(2))

    # Capture the existing <item/> as a template.
    item_m = _re.search(r'(<item[^/]+/>)', text)
    if not item_m:
        return xml_bytes
    item_proto = item_m.group(1)
    item_tm = _re.search(r'transform="([^"]+)"', item_proto)
    if not item_tm:
        return xml_bytes
    base_nums = item_tm.group(1).split()
    if len(base_nums) != 12:
        return xml_bytes
    try:
        base_floats = [float(x) for x in base_nums]
    except ValueError:
        return xml_bytes
    base_dx, base_dy, base_dz = base_floats[9], base_floats[10], base_floats[11]

    # Compute per-copy XY nudges from the bbox.
    min_x, max_x, min_y, max_y = _bbox_xy(vlist)
    w = (max_x - min_x) * scale
    d = (max_y - min_y) * scale
    placements = _grid_layout(copies, w, d)

    # 1) Emit N <object> entries — first keeps original id, rest are
    #    base+1, base+2, ... base+(N-1). Each gets a fresh UUID.
    new_objs = []
    for idx in range(copies):
        new_id = base_obj_id + idx
        obj_clone = _re.sub(r'<object\s+id="\d+"',
                            f'<object id="{new_id}"', obj_proto, count=1)
        # Bump the object's UUID so BS treats it as distinct
        obj_clone = _re.sub(
            r'p:UUID="([0-9a-f]{8})-',
            lambda mc, _i=idx: f'p:UUID="{(int(mc.group(1), 16) + _i) & 0xFFFFFFFF:08x}-',
            obj_clone, count=1,
        )
        new_objs.append(obj_clone)
    text = text.replace(obj_proto, "\n  ".join(new_objs), 1)

    # 2) Emit N <build><item> entries — each refs the matching object_id
    #    + gets its own grid-tiled translation.
    new_items = []
    for idx, (dx, dy) in enumerate(placements):
        new_obj_id = base_obj_id + idx
        nums = base_floats[:9] + [base_dx + dx, base_dy + dy, base_dz]
        new_transform = " ".join(repr(x) for x in nums)
        item = _re.sub(r'objectid="\d+"',
                       f'objectid="{new_obj_id}"', item_proto, count=1)
        item = _re.sub(r'transform="[^"]+"',
                       f'transform="{new_transform}"', item, count=1)
        item = _re.sub(
            r'p:UUID="([0-9a-f]{8})-',
            lambda mc, _i=idx: f'p:UUID="{(int(mc.group(1), 16) + _i + 1) & 0xFFFFFFFF:08x}-',
            item, count=1,
        )
        new_items.append(item)
    text = text.replace(item_proto, "\n   ".join(new_items), 1)
    return text.encode("utf-8")


def _patch_model_settings_for_copies(xml_bytes: bytes, copies: int, vlist, scale: float) -> bytes:
    """Patch `Metadata/model_settings.config` so the <plate> block holds
    N `<object id="...">` entries (one per copy) PLUS N matching
    `<model_instance>` entries pointing at distinct object_ids.

    obj_inst_map is keyed by object_id at parse time (bbs_3mf.cpp:4806)
    so distinct object_ids are required — same-id duplicates silently
    drop. This pairs with the multi-object structure
    _patch_wrapper_for_copies emits in 3D/3dmodel.model."""
    if copies <= 1:
        return xml_bytes
    import re as _re
    text = xml_bytes.decode("utf-8", errors="replace")
    # Capture the existing <object id="N">...</object> block from the cfg
    obj_m = _re.search(r'(<object\s+id="(\d+)">.*?</object>)', text, _re.DOTALL)
    if not obj_m:
        return xml_bytes
    obj_proto = obj_m.group(1)
    base_obj_id = int(obj_m.group(2))

    # Capture the <model_instance> template
    inst_m = _re.search(r"(<model_instance>.*?</model_instance>)", text, _re.DOTALL)
    if not inst_m:
        return xml_bytes
    inst_proto = inst_m.group(1)
    id_m = _re.search(r'identify_id"\s+value="(\d+)"', inst_proto)
    base_identify = int(id_m.group(1)) if id_m else 1

    # 1) Emit N <object> blocks
    new_objs = []
    for idx in range(copies):
        new_id = base_obj_id + idx
        clone = _re.sub(r'<object\s+id="\d+">',
                        f'<object id="{new_id}">', obj_proto, count=1)
        new_objs.append(clone)
    text = text.replace(obj_proto, "\n  ".join(new_objs), 1)

    # 2) Emit N <model_instance> blocks — each refs a different object_id.
    new_insts = []
    for idx in range(copies):
        new_obj_id = base_obj_id + idx
        inst = _re.sub(r'object_id"\s+value="\d+"',
                       f'object_id" value="{new_obj_id}"', inst_proto, count=1)
        inst = _re.sub(r'instance_id"\s+value="\d+"',
                       f'instance_id" value="0"', inst, count=1)
        inst = _re.sub(r'identify_id"\s+value="\d+"',
                       f'identify_id" value="{base_identify + idx}"', inst, count=1)
        new_insts.append(inst)
    text = text.replace(inst_proto, "\n    ".join(new_insts), 1)
    return text.encode("utf-8")


def patch_model_settings_for_scale(xml_bytes: bytes, scale: float) -> bytes:
    """Update the per-object 4x4 affine transform in
    Metadata/model_settings.config so the slicer scales the model.

    The matrix is space-separated row-major
    `r0c0 r0c1 r0c2 r0c3  r1c0 r1c1 r1c2 r1c3  r2c0 r2c1 r2c2 r2c3  r3c0 r3c1 r3c2 r3c3`
    (16 floats). To apply uniform scale s, multiply diagonal entries
    [0,0], [1,1], [2,2] by s, leaving the rest (esp. translation) alone.
    """
    if scale == 1.0:
        return xml_bytes
    text = xml_bytes.decode("utf-8", errors="replace")
    import re as _re
    pat = _re.compile(r'(<metadata key="matrix" value=")([^"]+)(")')
    def _repl(m):
        nums = m.group(2).split()
        if len(nums) != 16:
            return m.group(0)
        try:
            v = [float(x) for x in nums]
        except ValueError:
            return m.group(0)
        v[0] *= scale     # [0,0]
        v[5] *= scale     # [1,1]
        v[10] *= scale    # [2,2]
        new_value = " ".join(repr(x) for x in v)
        return m.group(1) + new_value + m.group(3)
    text2, n = pat.subn(_repl, text)
    if n == 0:
        # No matrix entry — append one to the first <object> (rare for
        # template-derived 3mfs).
        text2 = _re.sub(
            r"(<object[^>]*>)",
            rf'\g<1>\n      <metadata key="matrix" value="{scale} 0 0 0 0 {scale} 0 0 0 0 {scale} 0 0 0 0 1"/>',
            text, count=1,
        )
    return text2.encode("utf-8")


def patch_model_settings_for_color(xml_bytes: bytes, color: str) -> bytes:
    """Update the per-object filament_id + color in the template's
    Metadata/model_settings.config. The first object's first part is what
    inherits the color; we rewrite both the `extruder` reference in the
    object element and the color hint via a metadata key."""
    text = xml_bytes.decode("utf-8", errors="replace")
    # Inject/replace a <metadata key="extruder" value="1"/> + color hint
    # under the first <object> entry.
    # Simple regex pass — model_settings.config schema is shallow XML.
    import re as _re
    new_color = color.lstrip("#").upper()
    # Accept #RRGGBB or #RRGGBBAA — Bambu's catalogue uses the 8-char form.
    if not _re.fullmatch(r"[0-9A-F]{6}([0-9A-F]{2})?", new_color):
        raise ValueError(f"--color must be #RRGGBB[AA], got {color!r}")
    new_color = new_color[:6]
    # Replace existing extruder_color metadata if any, else add as a
    # part-level attribute. Use the simplest approach: find any
    # <metadata key="extruder_filament_color" ...> and update value.
    text2 = _re.sub(
        r'(<metadata key="extruder_filament_color" value=")[^"]*(")',
        rf'\g<1>#{new_color}\g<2>',
        text,
    )
    if text2 == text:
        # No existing key — inject one under the first <object> tag.
        text2 = _re.sub(
            r"(<object[^>]*>)",
            rf'\g<1>\n      <metadata key="extruder_filament_color" value="#{new_color}"/>',
            text, count=1,
        )
    return text2.encode("utf-8")


def patch_project_settings_for_color(json_bytes: bytes, color: str) -> bytes:
    """Update the filament_colour key in Metadata/project_settings.config
    (JSON). filament_colour is a list of "#RRGGBB" strings, one per
    filament slot."""
    import json as _json
    new_color = "#" + color.lstrip("#").upper()
    data = _json.loads(json_bytes.decode("utf-8", errors="replace"))
    if isinstance(data.get("filament_colour"), list) and data["filament_colour"]:
        # Replace just the first entry; user typically only cares about the
        # primary filament for single-color prints.
        data["filament_colour"][0] = new_color
    else:
        data["filament_colour"] = [new_color]
    return _json.dumps(data, indent=4).encode("utf-8")


def patch_project_settings_for_multi_color(json_bytes: bytes,
                                             colors: list[str]) -> bytes:
    """Multi-color variant. Expands every per-slot ``filament_*`` list
    (and the parallel ``flush_volumes_matrix``/``flush_volumes_vector``)
    to N entries — one per AMS slot.

    Strategy: capture the current length of `filament_colour` (almost
    always 1 in our single-color template), then for every key that's a
    list of the same length, repeat its first entry N times. This keeps
    BS's parallel-list invariants intact — without it BS crashes with
    `std::bad_alloc` because some per-slot indexing walks off the end
    of the shorter list."""
    import json as _json
    data = _json.loads(json_bytes.decode("utf-8", errors="replace"))
    n = len(colors)
    if n == 0:
        return json_bytes
    new_colours = ["#" + c.lstrip("#").upper() for c in colors]

    old_n = len(data.get("filament_colour") or [1])
    data["filament_colour"] = list(new_colours)

    # Expand every other list whose len matches the OLD filament_colour len.
    # This catches filament_type / filament_settings_id / filament_ids etc.
    # but skips already-N lists like filament_max_volumetric_speed (len=4
    # in the template because BS pre-stages 4 AMS slot slots).
    for k in list(data.keys()):
        if k == "filament_colour":
            continue
        v = data[k]
        if not isinstance(v, list) or not v:
            continue
        if len(v) == old_n and k.startswith("filament_"):
            data[k] = [v[0]] * n
        elif len(v) == old_n and k == "default_filament_colour":
            data[k] = [v[0]] * n
    # Flush volumes: matrix is N×N, vector is 2N. Resize if present.
    if isinstance(data.get("flush_volumes_matrix"), list) and data["flush_volumes_matrix"]:
        proto = data["flush_volumes_matrix"][0]
        # square N×N
        data["flush_volumes_matrix"] = [proto] * (n * n)
    if isinstance(data.get("flush_volumes_vector"), list) and data["flush_volumes_vector"]:
        proto = data["flush_volumes_vector"][0]
        data["flush_volumes_vector"] = [proto] * (2 * n)
    return _json.dumps(data, indent=4).encode("utf-8")


def patch_model_settings_for_per_object_extruder(xml_bytes: bytes,
                                                   copies: int) -> bytes:
    """For multi-color prints with --copies, give each copy/object its
    own extruder index (1, 2, 3, ... N) so the slicer assigns a
    different filament slot to each.

    The model_settings.config <object id="N"> blocks each have
    `<metadata key="extruder" value="1"/>` — we cycle through 1..N for
    successive objects keyed by `<object id="...">`. The order of
    <object> blocks in this file matches the order of copies, which
    matches the slot assignment from _grid_layout()."""
    if copies <= 1:
        return xml_bytes
    import re as _re
    text = xml_bytes.decode("utf-8", errors="replace")
    # Walk <object id="X"> ... </object> blocks in order and rewrite
    # the inner extruder= metadata to slot (index+1).
    def _replace_block(m: "_re.Match", idx_box: list = [0]) -> str:
        block = m.group(0)
        slot = idx_box[0] + 1
        idx_box[0] += 1
        return _re.sub(
            r'(<metadata key="extruder" value=")\d+(")',
            rf'\g<1>{slot}\g<2>',
            block, count=1,
        )
    new_text = _re.sub(r'<object\s+id="\d+">.*?</object>', _replace_block,
                       text, count=copies, flags=_re.DOTALL)
    return new_text.encode("utf-8")


# curr_bed_type enum values per PrintConfig.cpp:1071-1078. Friendly aliases
# accepted on the CLI map to the canonical enum value used in the JSON.
BED_TYPE_VALUES = {
    "cool":              "Cool Plate",
    "engineering":       "Engineering Plate",
    "high_temp":         "High Temp Plate",
    "smooth_pei":        "High Temp Plate",
    "pei":               "High Temp Plate",
    "textured":          "Textured PEI Plate",
    "textured_pei":      "Textured PEI Plate",
    "supertack":         "Supertack Plate",
    "super_tack":        "Supertack Plate",
    "cool_plate_super":  "Supertack Plate",
    "5":                 "Supertack Plate",   # BambuBedType enum value (#106)
    "4":                 "Textured PEI Plate",
    "3":                 "High Temp Plate",
    "2":                 "Engineering Plate",
    "1":                 "Cool Plate",
}


def resolve_bed_type(spec: str) -> str:
    """Accept friendly bed-type names + the BambuBedType enum integers
    (1..5) and return the canonical curr_bed_type enum string."""
    if not spec:
        return spec
    s = spec.strip().lower().replace(" ", "_").replace("-", "_")
    if s in BED_TYPE_VALUES:
        return BED_TYPE_VALUES[s]
    # Direct passthrough if already a canonical enum value.
    canonicals = set(BED_TYPE_VALUES.values())
    if spec in canonicals:
        return spec
    # Title-case match
    title = spec.strip().title()
    if title in canonicals:
        return title
    raise ValueError(
        f"unknown bed type {spec!r}; try one of: {sorted(canonicals)} "
        f"or numeric BambuBedType 1..5 (1=Cool, 2=Engineering, "
        f"3=High Temp/Smooth PEI, 4=Textured PEI, 5=SuperTack)"
    )


def patch_project_settings_for_bed(json_bytes: bytes, bed_type: str) -> bytes:
    """Set curr_bed_type in project_settings.config to a canonical enum
    string. The slicer reads this when generating bed-temp gcode for
    M104/M140 commands.

    BS rejects the slice with rc=195 ("Plate N: <bed> does not support
    filament 1") when the chosen bed's temp array is `['0']` for the
    active filament. The temp arrays are per-filament-slot inside
    project_settings.config — when the user picks SuperTack/Textured/
    etc., bake in a sane PLA-compatible default so the slicer doesn't
    bail. Values mirror what BS's GUI auto-fills when you check the
    "I have this plate" box for PLA."""
    import json as _json
    data = _json.loads(json_bytes.decode("utf-8", errors="replace"))
    data["curr_bed_type"] = bed_type

    # Default initial-layer + ongoing-layer bed temps per plate type for
    # PLA. Matches BS's filament profile defaults for Bambu PLA Basic.
    PLA_BED_TEMPS = {
        "Cool Plate":          ("35", "35"),
        "Engineering Plate":   ("55", "55"),
        "High Temp Plate":     ("55", "55"),
        "Textured PEI Plate":  ("55", "55"),
        "Supertack Plate":     ("35", "35"),
    }
    init, run = PLA_BED_TEMPS.get(bed_type, (None, None))
    if init is None:
        return _json.dumps(data, indent=4).encode("utf-8")

    bed_keys = {
        "Cool Plate":         ("cool_plate_temp_initial_layer",      "cool_plate_temp"),
        "Engineering Plate":  ("eng_plate_temp_initial_layer",       "eng_plate_temp"),
        "High Temp Plate":    ("hot_plate_temp_initial_layer",       "hot_plate_temp"),
        "Textured PEI Plate": ("textured_plate_temp_initial_layer",  "textured_plate_temp"),
        "Supertack Plate":    ("supertack_plate_temp_initial_layer", "supertack_plate_temp"),
    }
    init_key, run_key = bed_keys[bed_type]
    # Each is a list keyed by filament slot index. Fill any zero-valued
    # entry with the PLA default so multi-filament projects work too.
    for k, v in ((init_key, init), (run_key, run)):
        cur = data.get(k)
        if isinstance(cur, list):
            data[k] = [v if (str(x).strip() in ("", "0")) else x for x in cur]
        else:
            data[k] = [v]
    return _json.dumps(data, indent=4).encode("utf-8")


def graft_stl_into_template(template: Path, stl: Path, out: Path,
                              scale: float = 1.0, color: str | None = None,
                              bed_type: str | None = None,
                              copies: int = 1,
                              colors: list[str] | None = None,
                              orient: str = "original") -> None:
    """Copy template 3MF, replace its 3D geometry with the STL's, and write
    to `out`. Preserves project_settings, machine, filament, etc.

    If `scale` != 1, bakes it into the build-item transform. If `color`
    is provided (e.g. "#FF0000"), patches the filament colour in
    project_settings.config so the slicer assigns it to the primary
    filament tray. If `copies` > 1, emits that many instance entries on
    a grid (see _grid_layout).

    If `colors` is provided (a list of resolved #RRGGBB hex strings),
    this is multi-AMS-slot mode: each copy/object gets a distinct
    extruder/AMS slot index (1..N), and project_settings.config gets
    its filament_colour list expanded to N entries. Typically used with
    `--copies` so a 4-copy print can hit 4 different AMS slots; when
    `copies` is 1 but `colors` has N entries, only the first colour is
    used and the rest of the slots become available for hand-edited
    multi-part prints downstream.
    """
    vlist, tris = parse_stl(stl)
    if orient and orient != "original":
        from beambam.orient import orient_mesh
        before_min_z = min(v[2] for v in vlist) if vlist else 0.0
        vlist = orient_mesh(vlist, tris, orient)
        after_min_z = min(v[2] for v in vlist) if vlist else 0.0
        print(f"[x2d_slice] applied --orient {orient} "
              f"(min Z {before_min_z:.2f} → {after_min_z:.2f})",
              file=sys.stderr)
    print(f"[x2d_slice] parsed STL: {len(vlist)} verts, {len(tris)} triangles "
          f"(scale={scale}, color={color or 'unchanged'}, copies={copies})",
          file=sys.stderr)

    new_xml = build_3mf_object(vlist, tris, scale=scale, copies=copies)
    # Pre-validate the grid for copies>1 so the user gets a clean error
    # before we touch any output files.
    if int(copies) > 1:
        _min_x, _max_x, _min_y, _max_y = _bbox_xy(vlist)
        _grid_layout(int(copies),
                     (_max_x - _min_x) * float(scale),
                     (_max_y - _min_y) * float(scale))

    with zipfile.ZipFile(template, "r") as zin:
        names = zin.namelist()
        # The geometry usually lives at 3D/Objects/object_1.model;
        # 3D/3dmodel.model has a small header that just refs it.
        target = None
        for cand in ("3D/Objects/object_1.model", "3D/3dmodel.model"):
            if cand in names:
                target = cand
                break
        if not target:
            # Pick any *.model under 3D/
            target = next((n for n in names if n.startswith("3D/") and n.endswith(".model")), None)
        if not target:
            raise FileNotFoundError(f"no .model file found in template {template}")
        print(f"[x2d_slice] grafting STL into 3MF entry {target!r}", file=sys.stderr)

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                if name == target:
                    zout.writestr(name, new_xml)
                elif name == "3D/3dmodel.model" and int(copies) > 1:
                    data = zin.read(name)
                    data = _patch_wrapper_for_copies(data, int(copies), vlist, float(scale))
                    zout.writestr(name, data)
                elif name == "Metadata/model_settings.config":
                    data = zin.read(name)
                    if scale != 1.0:
                        data = patch_model_settings_for_scale(data, scale)
                    if color:
                        data = patch_model_settings_for_color(data, color)
                    if int(copies) > 1:
                        data = _patch_model_settings_for_copies(data, int(copies), vlist, float(scale))
                    # NOTE: we intentionally do NOT call
                    # patch_model_settings_for_per_object_extruder() here.
                    # The `<metadata key="extruder" value="N"/>` on an
                    # <object> is the NOZZLE index (X2D has 2 nozzles =
                    # extruders 1 and 2), not the AMS slot. Setting it
                    # to >2 trips BS's "out of bed area" check at slice
                    # time because the slicer assigns no nozzle to those
                    # extruder ids. AMS-slot binding lives in paint maps
                    # / the wipe-tower config — out of scope for our
                    # template-graft pipeline. --colors provisions the
                    # slots in project_settings.config so a downstream
                    # paint step or hand-edited model_settings can pick
                    # them up.
                    zout.writestr(name, data)
                elif name == "Metadata/project_settings.config":
                    data = zin.read(name)
                    if colors:
                        # Multi-colour wins over single --color (and includes
                        # the first colour in the expanded list anyway).
                        data = patch_project_settings_for_multi_color(data, colors)
                    elif color:
                        data = patch_project_settings_for_color(data, color)
                    if bed_type:
                        data = patch_project_settings_for_bed(data, bed_type)
                    zout.writestr(name, data)
                else:
                    zout.writestr(name, zin.read(name))


def run_bs_slice(input_3mf: Path, out_3mf: Path, plate: int = 0, debug: int = 1) -> int:
    """Invoke BS CLI to re-slice the given 3MF and produce a fresh
    output. The output dir is the parent of `out_3mf`; BS writes
    `<basename>` plus `plate_*.gcode` files."""
    out_dir = out_3mf.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(BS_BIN),
        "--slice", str(plate),
        "--debug", str(debug),
        "--outputdir", str(out_dir),
        "--export-3mf", out_3mf.name,
        str(input_3mf),
    ]
    env = os.environ.copy()
    # bs-bionic is BIND_NOW-linked against ffmpeg 7.0 and needs its bundled libs;
    # LD_LIBRARY_PATH is filtered by some parent shells (bun-on-termux), so set
    # it explicitly. The empty ffmpeg stubs (runtime/ffmpeg-stubs) satisfy the
    # loader on the slice path (ffmpeg is GUI-camera-only). Headless software GL,
    # no X server (the GL context is only for optional thumbnails). See
    # tools/SLICER_SETUP.md.
    libdirs = [
        X2D_ROOT / "runtime" / "ffmpeg-stubs",
        X2D_ROOT / "bs-bionic" / "build" / "src",
        X2D_ROOT / "bs-bionic" / "build" / "src" / "local" / "lib",
        X2D_ROOT / "bs-bionic" / "deps" / "build" / "destdir" / "usr" / "local" / "lib",
    ]
    parts = [str(p) for p in libdirs]
    if env.get("LD_LIBRARY_PATH"):
        parts.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(parts)
    env.update({"LIBGL_ALWAYS_SOFTWARE": "1", "GALLIUM_DRIVER": "llvmpipe",
                "EGL_PLATFORM": "surfaceless", "MESA_LOADER_DRIVER_OVERRIDE": "llvmpipe",
                "LC_ALL": "C", "LANG": "C"})
    env.pop("DISPLAY", None)
    # ensure the flattened *_full preset dirs BS loads system presets from exist
    res = X2D_ROOT / "bs-bionic" / "resources" / "profiles" / "BBL"
    if (res / "machine").is_dir() and not (res / "process_full").is_dir():
        flat = X2D_ROOT / "tools" / "flatten_bbl_profiles.py"
        if flat.is_file():
            subprocess.call([sys.executable, str(flat), str(res)])
    print(f"[x2d_slice] running: {' '.join(cmd)}", file=sys.stderr)
    return subprocess.call(cmd, env=env)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("stl", type=Path, help="input STL/STP/etc.")
    p.add_argument("--out", "-o", type=Path, required=True, help="output .gcode.3mf path")
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                   help=f"reference 3mf with embedded X2D profile (default: {DEFAULT_TEMPLATE})")
    p.add_argument("--plate", type=int, default=0, help="plate to slice (0 = all)")
    p.add_argument("--scale", type=float, default=1.0,
                   help="uniform scale factor (1.0 = original). Mutually "
                        "exclusive with --scale-pct / --mm.")
    p.add_argument("--scale-pct", type=float, default=None,
                   help="scale as a percentage (75 = 0.75x, 200 = 2x). More "
                        "readable than --scale for human input.")
    p.add_argument("--mm", type=float, default=None,
                   help="auto-scale so the model's Z (height) is this many mm. "
                        "Useful when you know the target physical size — beats "
                        "fiddling with --scale.")
    p.add_argument("--copies", "--quantity", "-n", type=int, default=1,
                   dest="copies",
                   help="how many copies of the model to lay out on the plate "
                        "(default 1). Copies tile on a grid; if they don't fit "
                        "in the 256×256 mm X2D build volume you'll get a "
                        "clear ValueError. Wires through cmd_slice_print.")
    p.add_argument("--color",
                   help="primary filament color: either #RRGGBB hex or a Bambu "
                        "color name (e.g. 'Gold', 'PLA Silk Gold', 'GFA05 Gold'). "
                        "Names resolve via filaments_color_codes.json — use any "
                        "fila_color_name.en value from the catalogue.")
    p.add_argument("--colors",
                   help="Multi-AMS-slot color list, comma-separated. Each entry "
                        "resolves via the same name/hex rules as --color. With "
                        "--copies N, the Nth copy is assigned the Nth slot "
                        "(cycling if the list is shorter than N). Wins over "
                        "--color if both are given. Examples: "
                        "'Gold,Red,Blue,Green' or '#E4BD68,#FF0000,#0000FF,#00FF00'.")
    p.add_argument("--color-by-region",
                   help="Path to a JSON file mapping per-object/region color "
                        "assignments. Currently the JSON is interpreted as a list "
                        "of colors equivalent to --colors. Reserved for future "
                        "per-mesh-region splitting; today it's just --colors from "
                        "a file.")
    p.add_argument("--bed",
                   help="curr_bed_type — one of 'cool', 'engineering', "
                        "'high_temp' (Smooth PEI), 'textured', 'supertack', or "
                        "the BambuBedType enum integer 1..5 (5 = SuperTack).")
    p.add_argument("--keep-graft", action="store_true",
                   help="keep the intermediate grafted 3mf for debugging")
    p.add_argument("--orient", default="original",
                   choices=("original", "flat", "tall", "auto"),
                   help="Pre-slice mesh orientation: original (no-op, "
                        "default), flat (largest-area face on the bed), "
                        "tall (longest bbox axis aligned with +Z), or "
                        "auto (same as flat for now).")
    args = p.parse_args()

    # Resolve scale from whichever flag the user picked.
    scale = float(args.scale)
    if args.scale_pct is not None:
        if args.scale != 1.0:
            print("warning: both --scale and --scale-pct given; --scale-pct wins", file=sys.stderr)
        scale = args.scale_pct / 100.0
    if args.mm is not None:
        # Need the STL bbox to compute the scale factor that hits target Z.
        if not args.stl.exists():
            print(f"input not found: {args.stl}", file=sys.stderr); return 2
        _vl, _tr = parse_stl(args.stl)
        _zmin, _zmax = _bbox_z(_vl)
        cur_z = _zmax - _zmin
        if cur_z <= 0:
            print("can't auto-scale: STL has zero Z extent", file=sys.stderr); return 2
        scale = float(args.mm) / cur_z
        print(f"[x2d_slice] --mm {args.mm} on Z={cur_z:.2f}mm → scale={scale:.4f}", file=sys.stderr)
    args.scale = scale  # downstream calls below read args.scale

    if not args.stl.exists():
        print(f"input not found: {args.stl}", file=sys.stderr)
        return 2
    if not args.template.exists():
        print(f"template not found: {args.template}", file=sys.stderr)
        return 2
    if not BS_BIN.exists():
        print(f"bambu-studio not found at {BS_BIN}", file=sys.stderr)
        return 2

    color_hex = resolve_color_name(args.color) if args.color else None
    bed_type = resolve_bed_type(args.bed) if args.bed else None
    if bed_type:
        print(f"[x2d_slice] bed-type {args.bed!r} → {bed_type!r}", file=sys.stderr)

    # Resolve the multi-colour list from --colors or --color-by-region.
    colors_list: list[str] | None = None
    if args.color_by_region:
        import json as _json
        try:
            raw = _json.loads(Path(args.color_by_region).read_text())
        except (OSError, _json.JSONDecodeError) as e:
            print(f"--color-by-region: can't read {args.color_by_region}: {e}",
                  file=sys.stderr); return 2
        if isinstance(raw, list):
            colors_list = [resolve_color_name(c) for c in raw]
        elif isinstance(raw, dict):
            # Dict form: keys can be slot indices or arbitrary region names.
            # Today we collapse into a position-ordered list.
            colors_list = [resolve_color_name(v) for v in raw.values()]
        else:
            print(f"--color-by-region: expected JSON list or dict, got {type(raw).__name__}",
                  file=sys.stderr); return 2
    if args.colors:
        if colors_list is not None:
            print("warning: both --colors and --color-by-region given; --colors wins",
                  file=sys.stderr)
        colors_list = [resolve_color_name(c.strip())
                       for c in args.colors.split(",") if c.strip()]
    if colors_list:
        # If --copies > 1, cycle the list to match copies count so each
        # copy/object lands on a distinct slot.
        if int(args.copies) > 1 and len(colors_list) < int(args.copies):
            colors_list = [colors_list[i % len(colors_list)]
                           for i in range(int(args.copies))]
        print(f"[x2d_slice] multi-color slots: {colors_list}", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="x2d_slice_") as td:
        graft = Path(td) / "graft.gcode.3mf"
        graft_stl_into_template(args.template, args.stl, graft,
                                 scale=args.scale, color=color_hex,
                                 bed_type=bed_type, copies=int(args.copies),
                                 colors=colors_list,
                                 orient=args.orient)
        if args.keep_graft:
            kept = args.out.with_suffix(".graft.3mf")
            shutil.copy2(graft, kept)
            print(f"[x2d_slice] kept grafted 3mf: {kept}", file=sys.stderr)
        rc = run_bs_slice(graft, args.out, plate=args.plate)
        if rc != 0:
            print(f"[x2d_slice] BS CLI exited rc={rc}", file=sys.stderr)
            return rc

    # Post-process: patch slice_info.config to fill X2D-specific fields
    # the BS CLI writes blank but the firmware validates. Matches the
    # shape of an official BS Desktop export (see docs/SLICE_COMPARISON_*).
    if args.out.exists():
        patch_slice_info(args.out, color=color_hex if color_hex else None)
        # Inject placeholder thumbnail PNGs — without them the X2D
        # touchscreen file browser filters the file out entirely
        # (only files with Metadata/plate_*.png show up).
        inject_thumbnails(args.out, tint=color_hex)

    # Print summary
    if args.out.exists():
        with zipfile.ZipFile(args.out) as z:
            try:
                info = z.read("Metadata/slice_info.config").decode("utf-8", errors="replace")
            except KeyError:
                info = ""
        for key in ("prediction", "weight", "used_m", "tray_info_idx", "printer_model_id"):
            for line in info.splitlines():
                if f'key="{key}"' in line or f"{key}=" in line:
                    print(f"  {line.strip()}", file=sys.stderr)
                    break
    return 0


def inject_thumbnails(out_3mf: Path, tint: str | None = None) -> None:
    """Render isometric + top views of the geometry already inside `out_3mf`
    and inject them as Metadata/plate_1.png + plate_1_small.png +
    plate_no_light_1.png + top_1.png + pick_1.png.

    Without thumbnails the X2D touchscreen file browser silently filters
    the file out of its listing.

    Geometry source: 3D/Objects/object_<n>.model (preferred) or
    3D/3dmodel.model. We do a tiny depth-sorted painter's render with
    Pillow — no external deps beyond PIL + numpy (both already required
    elsewhere in this repo).
    """
    try:
        import numpy as np
        from PIL import Image, ImageDraw
    except ImportError as e:
        print(f"[x2d_slice] thumbnail skipped — missing PIL/numpy: {e}",
              file=sys.stderr)
        return

    # 1) extract verts + triangles from the 3MF
    with zipfile.ZipFile(out_3mf, "r") as zin:
        names = zin.namelist()
        members = {n: zin.read(n) for n in names}
    model_xml = None
    for cand in (n for n in names if n.startswith("3D/Objects/") and n.endswith(".model")):
        model_xml = members[cand]; break
    if model_xml is None and "3D/3dmodel.model" in members:
        model_xml = members["3D/3dmodel.model"]
    if not model_xml:
        return
    try:
        root = ET.fromstring(model_xml)
    except ET.ParseError:
        return
    ns = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
    verts = []
    tris = []
    for v in root.iter(f"{ns}vertex"):
        verts.append((float(v.get("x")), float(v.get("y")), float(v.get("z"))))
    for t in root.iter(f"{ns}triangle"):
        tris.append((int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))))
    if not verts or not tris:
        return
    pts = np.array(verts, dtype=np.float32)
    tri = np.array(tris, dtype=np.int32)

    # Centre + scale to unit cube
    center = pts.mean(axis=0)
    pts -= center
    span = max(np.abs(pts).max(), 1e-3)
    pts /= span

    # Parse tint hex (#RRGGBB[AA]) → RGB int
    base = (228, 189, 104)  # default amber-gold
    if tint:
        h = tint.lstrip("#")
        if len(h) >= 6:
            try:
                base = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                pass

    def render(rot_x_deg: float, rot_y_deg: float, size: int,
               bg=(255, 255, 255), with_light: bool = True) -> "Image.Image":
        rx = np.radians(rot_x_deg); ry = np.radians(rot_y_deg)
        cx, sx = np.cos(rx), np.sin(rx)
        cy, sy = np.cos(ry), np.sin(ry)
        Rx = np.array([[1,0,0],[0,cx,-sx],[0,sx,cx]], dtype=np.float32)
        Ry = np.array([[cy,0,sy],[0,1,0],[-sy,0,cy]], dtype=np.float32)
        R = Rx @ Ry
        v = pts @ R.T
        # Orthographic project to XY; scale to image
        margin = 0.10
        s = (size * (1 - 2 * margin)) / 2.0
        px = (v[:, 0] * s + size / 2.0)
        py = (-v[:, 1] * s + size / 2.0)
        depth = v[:, 2]

        img = Image.new("RGB", (size, size), bg)
        draw = ImageDraw.Draw(img)

        # Painter's algorithm: sort by mean Z descending (farther first)
        tri_z = depth[tri].mean(axis=1)
        order = np.argsort(-tri_z)

        # Per-triangle Lambertian-ish shading
        # Normal = cross(b-a, c-a); light from camera + above
        for idx in order:
            a, b, c = tri[idx]
            poly = [(float(px[a]), float(py[a])),
                    (float(px[b]), float(py[b])),
                    (float(px[c]), float(py[c]))]
            # Skip backfaces (CCW in image space → positive area)
            area2 = ((poly[1][0]-poly[0][0])*(poly[2][1]-poly[0][1])
                   - (poly[2][0]-poly[0][0])*(poly[1][1]-poly[0][1]))
            if area2 <= 0:
                continue
            if with_light:
                # screen-space normal proxy via vertex Z spread
                z_mean = (depth[a]+depth[b]+depth[c])/3.0
                # higher Z (closer to camera) = brighter
                t = max(0.35, min(1.0, 0.55 + 0.45 * z_mean))
            else:
                t = 0.85
            col = (int(base[0]*t), int(base[1]*t), int(base[2]*t))
            draw.polygon(poly, fill=col, outline=(int(col[0]*0.7),
                                                  int(col[1]*0.7),
                                                  int(col[2]*0.7)))
        return img

    # Isometric view (BS uses ~25° X, ~45° Y for plate previews)
    iso_big   = render( 25,  45, 256)
    iso_small = render( 25,  45,  96)
    iso_flat  = render( 25,  45, 256, with_light=False)
    top       = render(  0,   0, 256)
    pick      = render( 25,  45,  64)

    def to_bytes(img):
        from io import BytesIO
        b = BytesIO(); img.save(b, format="PNG", optimize=True)
        return b.getvalue()

    members["Metadata/plate_1.png"]          = to_bytes(iso_big)
    members["Metadata/plate_1_small.png"]    = to_bytes(iso_small)
    members["Metadata/plate_no_light_1.png"] = to_bytes(iso_flat)
    members["Metadata/top_1.png"]            = to_bytes(top)
    members["Metadata/pick_1.png"]           = to_bytes(pick)

    tmp = out_3mf.with_suffix(".tmp.3mf")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, d in members.items():
            zout.writestr(n, d)
    tmp.replace(out_3mf)
    print(f"[x2d_slice] injected 5 rendered thumbnails ({len(verts)} verts)",
          file=sys.stderr)


def patch_slice_info(out_3mf: Path, color: str | None = None) -> None:
    """Post-process slice_info.config in `out_3mf` to inject the X2D-specific
    metadata fields that the BS CLI build writes blank (printer_model_id,
    tray_info_idx, weight) but which the X2D firmware validates when it
    receives a print.project_file MQTT command.

    Strategy:
      * printer_model_id: always "N6" (X2D's official model identifier).
      * tray_info_idx: derive from the filament colour using the Bambu
        SKU map (see filaments_color_codes.json). Fallback "GFA00".
      * weight: copy from the filament line's `used_g` attribute when
        the slicer populated it; leave alone otherwise (let prediction-
        based formats stay consistent).
    """
    import re

    try:
        with zipfile.ZipFile(out_3mf, "r") as zin:
            try:
                raw = zin.read("Metadata/slice_info.config").decode("utf-8")
            except KeyError:
                return
            other = {n: zin.read(n) for n in zin.namelist()
                     if n != "Metadata/slice_info.config"}
    except (zipfile.BadZipFile, KeyError):
        return

    patched = raw

    # printer_model_id
    patched = re.sub(
        r'<metadata key="printer_model_id" value=""\s*/>',
        '<metadata key="printer_model_id" value="N6"/>',
        patched,
    )

    # tray_info_idx — pick first SKU matching the filament hex colour
    sku = "GFA00"
    if color and COLOR_CODES_JSON.exists():
        try:
            import json as _j
            codes = _j.loads(COLOR_CODES_JSON.read_text())
            wanted = color.upper().lstrip("#").rstrip("FF")
            for entry in codes:
                hexv = (entry.get("color_code") or "").lstrip("#").upper().rstrip("FF")
                if hexv == wanted and entry.get("sku"):
                    sku = entry["sku"]
                    break
        except Exception:
            pass
    patched = re.sub(
        r'(<filament[^/]*?)tray_info_idx=""',
        rf'\1tray_info_idx="{sku}"',
        patched,
    )

    # weight: copy used_g into weight if weight is blank
    m_used_g = re.search(r'<filament[^>]*used_g="([0-9.]+)"', patched)
    if m_used_g:
        used_g = m_used_g.group(1)
        if used_g and used_g != "0.00":
            patched = re.sub(
                r'<metadata key="weight" value=""\s*/>',
                f'<metadata key="weight" value="{used_g}"/>',
                patched,
            )

    if patched == raw:
        return

    # Rewrite zip with patched slice_info.config
    tmp = out_3mf.with_suffix(".tmp.3mf")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("Metadata/slice_info.config", patched)
        for name, data in other.items():
            zout.writestr(name, data)
    tmp.replace(out_3mf)
    print(f"[x2d_slice] patched slice_info.config "
          f"(printer_model_id=N6, tray_info_idx={sku}, weight=used_g)",
          file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
