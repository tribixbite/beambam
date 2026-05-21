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
    """Patch `3D/3dmodel.model`'s <build> block so it lists N <item>
    entries (one per copy). Each is a clone of the existing first item
    with its translation slot rewritten so copies tile on the plate.

    Layout follows _grid_layout() — first copy keeps the template's
    original (dx, dy), subsequent copies are nudged on the grid."""
    if copies <= 1:
        return xml_bytes
    import re as _re
    text = xml_bytes.decode("utf-8", errors="replace")

    # Extract the existing <item .../> — preserve its objectid, UUID, etc.
    m = _re.search(r"(<item[^/]+/>)", text)
    if not m:
        return xml_bytes
    proto = m.group(1)
    # Parse the existing transform's translation slot (last 3 of 12 numbers).
    tm = _re.search(r'transform="([^"]+)"', proto)
    if not tm:
        return xml_bytes
    base_nums = tm.group(1).split()
    if len(base_nums) != 12:
        return xml_bytes
    try:
        base_floats = [float(x) for x in base_nums]
    except ValueError:
        return xml_bytes
    base_dx, base_dy, base_dz = base_floats[9], base_floats[10], base_floats[11]

    # Compute per-copy nudges from the bbox.
    min_x, max_x, min_y, max_y = _bbox_xy(vlist)
    w = (max_x - min_x) * scale
    d = (max_y - min_y) * scale
    placements = _grid_layout(copies, w, d)

    # Build N items by patching the proto's transform + adding 1-based UUIDs.
    new_items = []
    for idx, (dx, dy) in enumerate(placements):
        nums = base_floats[:9] + [base_dx + dx, base_dy + dy, base_dz]
        new_transform = " ".join(repr(x) for x in nums)
        # Replace transform="..." in proto; also rewrite the UUID so each
        # item is unique (the slicer dedupes by UUID).
        item = _re.sub(r'transform="[^"]+"', f'transform="{new_transform}"', proto)
        # Each item needs a fresh UUID; replace the last hex segment with idx.
        item = _re.sub(
            r'p:UUID="([0-9a-f]{8})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{4})-([0-9a-f]{12})"',
            lambda m_: f'p:UUID="{idx+1:08x}-{m_.group(2)}-{m_.group(3)}-{m_.group(4)}-{m_.group(5)}"',
            item, count=1,
        )
        new_items.append(item)
    # Replace just the FIRST <item/> with all N
    replacement = "\n  ".join(new_items)
    text = text.replace(proto, replacement, 1)
    return text.encode("utf-8")


def _patch_model_settings_for_copies(xml_bytes: bytes, copies: int, vlist, scale: float) -> bytes:
    """Patch `Metadata/model_settings.config` so its <plate> block has N
    <model_instance> entries. The first instance keeps the template's
    fields; subsequent instances increment `instance_id` + bump
    `identify_id` so the slicer tells them apart."""
    if copies <= 1:
        return xml_bytes
    import re as _re
    text = xml_bytes.decode("utf-8", errors="replace")
    # Find the existing <model_instance>...</model_instance>
    m = _re.search(r"(<model_instance>.*?</model_instance>)", text, _re.DOTALL)
    if not m:
        return xml_bytes
    proto = m.group(1)
    # Parse the existing identify_id so subsequent copies bump from there.
    id_m = _re.search(r'identify_id"\s+value="(\d+)"', proto)
    base_identify = int(id_m.group(1)) if id_m else 1

    new_instances = []
    for idx in range(copies):
        inst = _re.sub(
            r'instance_id"\s+value="\d+"',
            f'instance_id" value="{idx}"', proto, count=1,
        )
        inst = _re.sub(
            r'identify_id"\s+value="\d+"',
            f'identify_id" value="{base_identify + idx}"', inst, count=1,
        )
        new_instances.append(inst)
    text = text.replace(proto, "\n    ".join(new_instances), 1)
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
                              copies: int = 1) -> None:
    """Copy template 3MF, replace its 3D geometry with the STL's, and write
    to `out`. Preserves project_settings, machine, filament, etc.

    If `scale` != 1, bakes it into the build-item transform. If `color`
    is provided (e.g. "#FF0000"), patches the filament colour in
    project_settings.config so the slicer assigns it to the primary
    filament tray. If `copies` > 1, emits that many instance entries on
    a grid (see _grid_layout).
    """
    vlist, tris = parse_stl(stl)
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
                    zout.writestr(name, data)
                elif name == "Metadata/project_settings.config":
                    data = zin.read(name)
                    if color:
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
    env.setdefault("DISPLAY", ":1")
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
    p.add_argument("--bed",
                   help="curr_bed_type — one of 'cool', 'engineering', "
                        "'high_temp' (Smooth PEI), 'textured', 'supertack', or "
                        "the BambuBedType enum integer 1..5 (5 = SuperTack).")
    p.add_argument("--keep-graft", action="store_true",
                   help="keep the intermediate grafted 3mf for debugging")
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

    with tempfile.TemporaryDirectory(prefix="x2d_slice_") as td:
        graft = Path(td) / "graft.gcode.3mf"
        graft_stl_into_template(args.template, args.stl, graft,
                                 scale=args.scale, color=color_hex,
                                 bed_type=bed_type, copies=int(args.copies))
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
