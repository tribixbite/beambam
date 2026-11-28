"""beambam.plate — multi-plate operations on a `.gcode.3mf`.

A Bambu .gcode.3mf bundle can contain N plates. The GUI lets the user
pick which to send to the printer. Without the GUI we ended up with no
plate-level control via `beambam print`. This module adds:

  beambam plate list <file>                 — show every plate + stats
  beambam plate select <file> N --out X     — emit a new .3mf with only plate N
  beambam plate skip <file> N --out X       — emit a new .3mf without plate N

Implementation:
  * Each plate stores its assets at `Metadata/plate_N.{gcode,gcode.md5,json,png,top_N.png,plate_N.gcode,...}`
  * `Metadata/slice_info.config` (XML) carries one `<plate>` element per
    plate with `<metadata key="index" value="N"/>`.
  * `Metadata/model_settings.config` (XML) carries one `<plate>` per plate
    with `plater_id == N` and `gcode_file == "Metadata/plate_N.gcode"`.

To `select` plate N: keep only plate N's assets + the matching <plate>
elements in both XMLs. To `skip` plate N: drop just plate N's assets and
remove its <plate> elements. We never touch the 3D/Objects/ models —
plates reference them by id.

Per-plate metadata surfaced by `list`:
  index | weight (g) | time (predicted s) | objects | filaments | gcode file

The output is human-readable by default and `--json` for tools.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


# Files inside the zip whose path encodes a plate index. Used to filter
# the zip when selecting / skipping plates.
_PLATE_FILE_PATTERNS = (
    re.compile(r"^Metadata/plate_(\d+)\.gcode(\.md5)?$"),
    re.compile(r"^Metadata/plate_(\d+)\.json$"),
    re.compile(r"^Metadata/plate_(\d+)(?:_small)?\.png$"),
    re.compile(r"^Metadata/plate_no_light_(\d+)\.png$"),
    re.compile(r"^Metadata/top_(\d+)\.png$"),
    re.compile(r"^Metadata/pick_(\d+)\.png$"),
)


# ----- data model ---------------------------------------------------------


@dataclass
class PlateInfo:
    index: int
    weight_g: float = 0.0
    predicted_seconds: int = 0
    objects: list[str] = field(default_factory=list)
    filaments: list[str] = field(default_factory=list)
    gcode_file: str = ""

    @property
    def predicted_human(self) -> str:
        s = self.predicted_seconds
        if s <= 0:
            return "—"
        if s < 3600:
            return f"{s // 60}m{s % 60:02d}s"
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"

    def to_dict(self) -> dict:
        return {
            "index":             self.index,
            "weight_g":          self.weight_g,
            "predicted_seconds": self.predicted_seconds,
            "predicted_human":   self.predicted_human,
            "objects":           list(self.objects),
            "filaments":         list(self.filaments),
            "gcode_file":        self.gcode_file,
        }


# ----- discovery ----------------------------------------------------------


def _plate_index_of_path(name: str) -> int | None:
    """Return the plate index N if `name` matches a per-plate asset path,
    else None. e.g. `Metadata/plate_3.gcode` → 3."""
    for pat in _PLATE_FILE_PATTERNS:
        m = pat.match(name)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                return None
    return None


def read_plates(path: Path) -> list[PlateInfo]:
    """Parse slice_info.config + model_settings.config from a .3mf and
    return one PlateInfo per declared plate. Plates that exist only in
    model_settings (not slice_info) are still listed but with zeroed
    stats — they're unsliced."""
    plates: dict[int, PlateInfo] = {}

    with zipfile.ZipFile(path) as z:
        # slice_info.config carries the stats per sliced plate.
        try:
            si = z.read("Metadata/slice_info.config")
        except KeyError:
            si = b""
        if si:
            try:
                root = ET.fromstring(si)
            except ET.ParseError:
                root = None
            if root is not None:
                for p_el in root.findall("plate"):
                    idx_str = ""
                    weight = 0.0
                    pred = 0
                    for meta in p_el.findall("metadata"):
                        key = meta.get("key") or ""
                        val = meta.get("value") or ""
                        if key == "index":
                            idx_str = val
                        elif key == "weight":
                            try:
                                weight = float(val)
                            except ValueError:
                                pass
                        elif key == "prediction":
                            try:
                                pred = int(float(val))
                            except ValueError:
                                pass
                    if not idx_str.isdigit():
                        continue
                    idx = int(idx_str)
                    objects = [o.get("name") or o.get("identify_id") or ""
                               for o in p_el.findall("object")]
                    filaments = []
                    for f in p_el.findall("filament"):
                        fid = f.get("id") or "?"
                        ftype = f.get("type") or ""
                        col = f.get("color") or ""
                        filaments.append(f"#{fid} {ftype} {col}".strip())
                    info = plates.setdefault(idx, PlateInfo(index=idx))
                    info.weight_g = weight
                    info.predicted_seconds = pred
                    info.objects = objects
                    info.filaments = filaments

        # model_settings.config carries the gcode_file reference.
        try:
            ms = z.read("Metadata/model_settings.config")
        except KeyError:
            ms = b""
        if ms:
            try:
                root = ET.fromstring(ms)
            except ET.ParseError:
                root = None
            if root is not None:
                for p_el in root.findall("plate"):
                    plater_id = None
                    gcode_file = ""
                    for meta in p_el.findall("metadata"):
                        key = meta.get("key") or ""
                        val = meta.get("value") or ""
                        if key == "plater_id":
                            try:
                                plater_id = int(val)
                            except ValueError:
                                pass
                        elif key == "gcode_file":
                            gcode_file = val
                    if plater_id is None:
                        continue
                    info = plates.setdefault(plater_id,
                                              PlateInfo(index=plater_id))
                    info.gcode_file = gcode_file

    return [plates[i] for i in sorted(plates)]


# ----- rewriters ----------------------------------------------------------


def _drop_plate_xml(xml_bytes: bytes, keep: set[int] | None = None,
                   drop: set[int] | None = None) -> bytes:
    """Rewrite a slice_info.config / model_settings.config XML by either
    keeping only <plate> elements whose index is in `keep`, OR dropping
    elements whose index is in `drop`. Pass exactly one of keep/drop.

    Plate index is read from `metadata[key=index]` (slice_info) or
    `metadata[key=plater_id]` (model_settings) — we check both keys."""
    if (keep is None) == (drop is None):
        raise ValueError("pass exactly one of keep=/drop=")
    if not xml_bytes:
        return xml_bytes
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return xml_bytes  # corrupt — leave untouched

    for p_el in list(root.findall("plate")):
        idx = None
        for meta in p_el.findall("metadata"):
            key = meta.get("key") or ""
            if key in ("index", "plater_id"):
                val = meta.get("value") or ""
                if val.isdigit():
                    idx = int(val)
                    break
        if idx is None:
            continue
        remove = (keep is not None and idx not in keep) or \
                 (drop is not None and idx in drop)
        if remove:
            root.remove(p_el)

    # Preserve the XML declaration if the input had one (Bambu's parser
    # tolerates either way, but matching the original is safer).
    decl = b'<?xml version="1.0" encoding="UTF-8"?>\n' \
        if xml_bytes.lstrip().startswith(b"<?xml") else b""
    return decl + ET.tostring(root, encoding="utf-8")


def filter_3mf(src: Path, dst: Path, *, keep: set[int] | None = None,
                drop: set[int] | None = None) -> None:
    """Stream-copy src.3mf to dst.3mf, removing per-plate assets for
    plates NOT in `keep` (if keep given) OR plates in `drop` (if drop
    given). Slice_info + model_settings XMLs are rewritten to match.

    Other entries (3D models, content types, _rels, etc.) are copied
    verbatim. Use `keep` for plate-select, `drop` for plate-skip.
    """
    if (keep is None) == (drop is None):
        raise ValueError("pass exactly one of keep= / drop=")
    try:
        same = src.resolve() == dst.resolve()
    except OSError:
        same = str(src) == str(dst)
    if same:
        raise ValueError(f"filter_3mf: src and dst are the same file ({src}); "
                         f"write to a different path")

    with zipfile.ZipFile(src) as zin:
        with zipfile.ZipFile(dst, "w",
                              compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                idx = _plate_index_of_path(info.filename)
                if idx is not None:
                    remove = (keep is not None and idx not in keep) or \
                             (drop is not None and idx in drop)
                    if remove:
                        continue
                raw = zin.read(info.filename)
                if info.filename == "Metadata/slice_info.config":
                    raw = _drop_plate_xml(raw, keep=keep, drop=drop)
                elif info.filename == "Metadata/model_settings.config":
                    raw = _drop_plate_xml(raw, keep=keep, drop=drop)
                # Preserve the original timestamp / external_attr so the
                # round-trip 3mf parses cleanly in BambuStudio.
                new_info = zipfile.ZipInfo(info.filename, info.date_time)
                new_info.external_attr = info.external_attr
                new_info.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(new_info, raw)


# ----- formatting --------------------------------------------------------


def format_table(plates: list[PlateInfo]) -> str:
    """Render a list of PlateInfo as a fixed-width table."""
    if not plates:
        return "(no plates)"
    lines = []
    header = f"{'idx':>3}  {'weight':>8}  {'time':>8}  objects"
    lines.append(header)
    lines.append("-" * len(header))
    for p in plates:
        obj_str = ", ".join(p.objects)[:60] or "—"
        lines.append(
            f"{p.index:>3}  {p.weight_g:>6.1f} g  "
            f"{p.predicted_human:>8}  {obj_str}"
        )
    return "\n".join(lines)


# ----- CLI handlers ------------------------------------------------------


def cmd_plate_list(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 1
    plates = read_plates(path)
    if args.json:
        print(json.dumps([p.to_dict() for p in plates], indent=2))
        return 0
    print(f"{len(plates)} plate(s) in {path.name}:\n")
    print(format_table(plates))
    return 0


def cmd_plate_select(args: argparse.Namespace) -> int:
    return _select_or_skip(args, mode="select")


def cmd_plate_skip(args: argparse.Namespace) -> int:
    return _select_or_skip(args, mode="skip")


def _select_or_skip(args: argparse.Namespace, *, mode: str) -> int:
    src = Path(args.file)
    if not src.is_file():
        print(f"file not found: {src}", file=sys.stderr)
        return 1
    plates = read_plates(src)
    indices = {p.index for p in plates}
    if not indices:
        print(f"{src.name}: no plates declared (slice_info.config empty?)",
              file=sys.stderr)
        return 1
    target = args.plate
    if target not in indices:
        print(f"plate {target} not in {src.name} "
              f"(available: {sorted(indices)})", file=sys.stderr)
        return 1

    if mode == "select":
        remaining_plates = {target}
        if remaining_plates == indices:
            print(f"{src.name}: plate {target} is already the only plate; "
                  f"copying file unchanged.", file=sys.stderr)
        keep, drop = remaining_plates, None
    else:  # skip
        if len(indices) == 1:
            print(f"refusing to skip the only plate ({target}) — would "
                  f"leave an empty 3MF.", file=sys.stderr)
            return 1
        keep, drop = None, {target}

    dst = Path(args.out)
    if dst.exists() and not args.force:
        print(f"output exists: {dst} (pass --force to overwrite)",
              file=sys.stderr)
        return 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if keep is not None:
            filter_3mf(src, dst, keep=keep)
        else:
            filter_3mf(src, dst, drop=drop)
    except ValueError as e:
        # e.g. --out names the input file (with --force): filter_3mf refuses
        # to overwrite the file it is reading; say so instead of a traceback.
        print(f"plate {mode}: {e}", file=sys.stderr)
        return 1
    new_plates = read_plates(dst)
    if mode == "select":
        action = f"selected plate {target}"
    else:
        action = f"skipped plate {target}"
    print(f"{action} from {src.name} → {dst} ({len(new_plates)} plate(s) "
          f"remaining: {[p.index for p in new_plates]})")
    return 0


def add_subparser(sub: "argparse._SubParsersAction"
                  ) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "plate",
        help="Multi-plate operations on a .gcode.3mf — list, select, skip.",
    )
    psub = p.add_subparsers(dest="plate_cmd", required=True)

    pl = psub.add_parser("list", help="List every plate with stats")
    pl.add_argument("file", help="Path to a .gcode.3mf")
    pl.add_argument("--json", action="store_true",
                    help="Emit JSON instead of a human table")
    pl.set_defaults(fn=cmd_plate_list)

    ps = psub.add_parser(
        "select",
        help="Write a new 3MF containing only the specified plate.",
    )
    ps.add_argument("file", help="Source .gcode.3mf")
    ps.add_argument("plate", type=int, help="Plate index to keep")
    ps.add_argument("--out", required=True, help="Output .gcode.3mf path")
    ps.add_argument("--force", action="store_true",
                    help="Overwrite --out if it exists")
    ps.set_defaults(fn=cmd_plate_select)

    pk = psub.add_parser(
        "skip",
        help="Write a new 3MF with the specified plate removed.",
    )
    pk.add_argument("file", help="Source .gcode.3mf")
    pk.add_argument("plate", type=int, help="Plate index to drop")
    pk.add_argument("--out", required=True, help="Output .gcode.3mf path")
    pk.add_argument("--force", action="store_true",
                    help="Overwrite --out if it exists")
    pk.set_defaults(fn=cmd_plate_skip)

    return p


# --------------------------------------------------------------------------- single-plate extraction
# `filter_3mf` (above) keeps a plate's assets for re-opening in Bambu Studio, but leaves the whole
# `<build>` and the cross-plate `<assemble>` in place, which the CLI slicer (bs-bionic) refuses:
# `code -6` ("can not find object for assemble item") on the assemble section, and `code -50`
# ("plate is empty") on a plate whose objects have no `<build>` item. `extract_plate` instead rewrites
# the project down to one minimal, self-consistent plate the slicer parses, and carries the web UI's
# per-object quantity. slice_service calls it before slicing a multi-plate (or plate-/quantity-chosen)
# project. See docs/specs/multi-plate-slicing.md.

_MODEL_PATH = "3D/3dmodel.model"
_SETTINGS_PATH = "Metadata/model_settings.config"
# Per object id, one (dx, dy) translation per wanted copy; an empty list drops the object.
QuantitySpec = dict[str, list[tuple[float, float]]]
_ITEM_RE = re.compile(r"<item\b[^>]*/>")
_BUILD_RE = re.compile(r"<build\b[^>]*>.*?</build>", re.S)
_NUMBERED_ASSET = re.compile(r"Metadata/(plate|top|pick|plate_no_light)_(\d+)\.(json|png)$")
# Plate-level metadata that points at a plate's rendered previews: meaningless once renumbered, so dropped.
_SKIP_PLATE_META = {"thumbnail_file", "thumbnail_no_light_file", "top_file", "pick_file"}


def plate_count(src: Path) -> int:
    """How many plates the project at `src` has (1 when it is not a Bambu project or names no plate)."""
    try:
        with zipfile.ZipFile(src) as z:
            ms = z.read(_SETTINGS_PATH).decode("utf-8", "ignore")
    except (KeyError, zipfile.BadZipFile, OSError):
        return 1
    return max(1, ms.count("<plate>"))


def _fmt_num(n: float) -> str:
    """Render a transform number without trailing noise (an integer stays integer)."""
    return str(int(n)) if n == int(n) else repr(n)


def _fresh_item_uuid(item: str, used: set[str]) -> str:
    """`item` with a new, unique p:UUID when it already carries one (duplicate copies must not share it)."""
    import uuid as _uuid

    m = re.search(r'p:UUID="([^"]*)"', item)
    if not m:
        return item
    while True:
        u = str(_uuid.uuid4())
        if u not in used:
            used.add(u)
            break
    return item[: m.start(1)] + u + item[m.end(1):]


_IDENTITY = [1.0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
_VERTEX_RE = re.compile(r'<vertex\b[^>]*\bx="([^"]+)"[^>]*\by="([^"]+)"[^>]*\bz="([^"]+)"')
_COMPONENT_RE = re.compile(r"<component\b[^>]*/>")
_OBJECT_RE = re.compile(r'<object\s+id="([^"]+)"[^>]*>(.*?)</object>', re.S)


def _parse_objects(xml: str) -> dict[str, tuple[list[tuple[float, float, float]], list[tuple[str | None, str, list[float]]]]]:
    """object id -> (vertices, components) from one `.model` file. A component is (path|None, object id, transform)."""
    out: dict[str, tuple[list[tuple[float, float, float]], list[tuple[str | None, str, list[float]]]]] = {}
    for m in _OBJECT_RE.finditer(xml):
        body = m.group(2)
        verts = [(float(a), float(b), float(c)) for a, b, c in _VERTEX_RE.findall(body)]
        comps: list[tuple[str | None, str, list[float]]] = []
        for c in _COMPONENT_RE.findall(body):
            cp = re.search(r'p:path="([^"]*)"', c)
            co = re.search(r'objectid="([^"]*)"', c)
            ct = re.search(r'transform="([^"]*)"', c)
            tr = [float(x) for x in ct.group(1).split()] if ct and len(ct.group(1).split()) == 12 else list(_IDENTITY)
            comps.append((cp.group(1) if cp else None, co.group(1) if co else "", tr))
        out[m.group(1)] = (verts, comps)
    return out


def _compose(a: list[float], b: list[float]) -> list[float]:
    """Compose two 3MF transforms (apply `a` then `b`), matching the web loader's `compose`."""
    out = [0.0] * 12
    for r in range(4):
        for c in range(3):
            s = b[9 + c] if r == 3 else 0.0
            for k in range(3):
                s += a[r * 3 + k] * b[k * 3 + c]
            out[r * 3 + c] = s
    return out


def _accumulate_bbox(objs_by_path: dict, zin: zipfile.ZipFile, path: str, oid: str, M: list[float], acc: list[float]) -> None:
    """Grow `acc` [minx, miny, maxx, maxy] over object `oid`'s vertices (and its components') under transform `M`."""
    path = path.lstrip("/")  # 3MF component paths are absolute ("/3D/…"); zip members have no leading slash
    objs = objs_by_path.get(path)
    if objs is None:
        try:
            objs = _parse_objects(zin.read(path).decode("utf-8", "ignore"))
        except KeyError:
            objs = {}
        objs_by_path[path] = objs
    node = objs.get(oid)
    if not node:
        return
    verts, comps = node
    m0, m1, m3, m4, m6, m7, m9, m10 = M[0], M[1], M[3], M[4], M[6], M[7], M[9], M[10]
    for (x, y, z) in verts:
        wx = x * m0 + y * m3 + z * m6 + m9
        wy = x * m1 + y * m4 + z * m7 + m10
        if wx < acc[0]:
            acc[0] = wx
        if wy < acc[1]:
            acc[1] = wy
        if wx > acc[2]:
            acc[2] = wx
        if wy > acc[3]:
            acc[3] = wy
    for (cpath, coid, ct) in comps:
        _accumulate_bbox(objs_by_path, zin, cpath or path, coid, _compose(ct, M), acc)


def _shelf_pack(rects: list[tuple[float, float]], bed: tuple[float, float, float, float], margin: float = 5.0) -> list[tuple[float, float]]:
    """Greedy shelf pack (tallest first); returns one (cx, cy) target centre per rect, in input order. Mirrors
    the web `shelfPack` so the daemon lays a plate out the way the preview does."""
    bed_w = bed[2] - bed[0]
    bed_h = bed[3] - bed[1]
    order = sorted(range(len(rects)), key=lambda i: -rects[i][1])
    places: list[tuple[float, float]] = [(0.0, 0.0)] * len(rects)
    x = margin
    y = margin
    shelf_h = 0.0
    for i in order:
        w = rects[i][0] + 2 * margin
        h = rects[i][1] + 2 * margin
        if x + w > bed_w and x > margin:
            x = margin
            y += shelf_h
            shelf_h = 0.0
        places[i] = (bed[0] + x + w / 2, bed[1] + y + h / 2)
        x += w
        if h > shelf_h:
            shelf_h = h
    return places


def _bed_of(zin: zipfile.ZipFile) -> tuple[float, float, float, float]:
    """The project's printable area as (minx, miny, maxx, maxy); 256×256 when absent. Used to arrange a plate's
    objects on the bed (conservative: fitting the source bed fits the X2D's larger one)."""
    try:
        s = json.loads(zin.read("Metadata/project_settings.config").decode("utf-8", "ignore"))
        xs: list[float] = []
        ys: list[float] = []
        for p in s.get("printable_area") or []:
            a, _, b = str(p).partition("x")
            xs.append(float(a))
            ys.append(float(b))
        if len(xs) >= 3:
            return (min(xs), min(ys), max(xs), max(ys))
    except (KeyError, ValueError, TypeError):
        pass
    return (0.0, 0.0, 256.0, 256.0)


def _set_item_xy(item: str, tx: float, ty: float) -> str:
    """A `<build>` item with its transform's translation set to (tx, ty) (rotation/scale/tz untouched)."""
    m = re.search(r'transform="([^"]*)"', item)
    if not m:
        return item
    nums = m.group(1).split()
    if len(nums) != 12:
        return item
    nums[9] = _fmt_num(tx)
    nums[10] = _fmt_num(ty)
    return item[: m.start(1)] + " ".join(nums) + item[m.end(1):]


def extract_plate(src: Path, dst: Path, plate: int = 1, spec: QuantitySpec | None = None) -> dict[str, object]:
    """Write a single-plate copy of `src` (its `plate`-th plate) to `dst`, applying `spec`'s per-object
    quantity ({object id: one offset per wanted copy; empty list drops the object}; an object with no entry
    is kept once). Strips the cross-plate `<assemble>` section, keeps only this plate's objects/build items,
    and **re-places every object on the bed from its real geometry** (a multi-plate project stores objects in
    a global grid off this single bed, which the slicer rejects as an empty plate). `spec`'s offsets are only
    counted (one per copy); the daemon arranges the copies itself. Returns {objects, copies}. Raises
    ValueError when the project has no such plate or the plate ends up empty."""
    spec = spec or {}
    with zipfile.ZipFile(src) as zin:
        try:
            raw_ms = zin.read(_SETTINGS_PATH).decode("utf-8")
            raw_model = zin.read(_MODEL_PATH).decode("utf-8")
        except KeyError as e:
            raise ValueError(f"not a Bambu project (missing {e})") from e

        root = ET.fromstring(raw_ms)
        plates = root.findall("plate")
        target = None
        for p in plates:
            pid = p.find('metadata[@key="plater_id"]')
            if pid is not None and pid.get("value") == str(plate):
                target = p
                break
        if target is None:
            if plate == 1 and plates:
                target = plates[0]
            else:
                raise ValueError(f"the project has no plate {plate}")

        order: list[str] = []
        for mi in target.findall("model_instance"):
            oid_el = mi.find('metadata[@key="object_id"]')
            if oid_el is None:
                continue
            oid = oid_el.get("value") or ""
            if oid and oid not in order:
                order.append(oid)

        def copies_of(oid: str) -> list[tuple[float, float]]:
            return list(spec[oid]) if oid in spec else [(0.0, 0.0)]

        kept = [oid for oid in order if copies_of(oid)]
        if not kept:
            raise ValueError("every object on this plate was dropped (quantity 0)")

        # New model_settings.config: wanted objects, one plate, renumbered instances, no assemble.
        new = ET.Element("config")
        for obj in root.findall("object"):
            if obj.get("id") in kept:
                new.append(obj)
        np = ET.SubElement(new, "plate")
        for md in target.findall("metadata"):
            k = md.get("key")
            if k in _SKIP_PLATE_META:
                continue
            e = ET.SubElement(np, "metadata")
            e.set("key", k or "")
            e.set("value", "1" if k == "plater_id" else (md.get("value") or ""))
        counts: dict[str, int] = {}
        for oid in kept:
            for inst in range(len(copies_of(oid))):
                mi = ET.SubElement(np, "model_instance")
                for key, val in (("object_id", oid), ("instance_id", str(inst))):
                    e = ET.SubElement(mi, "metadata")
                    e.set("key", key)
                    e.set("value", val)
        new_ms = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(new, encoding="unicode")

        # New <build>: each wanted object placed on the bed from its real geometry (a multi-plate project
        # keeps objects in a global grid, off this single bed — the -50 cause — so the source translation is
        # NOT trusted). Compute each object's world XY bbox, shelf-pack every copy on the bed, and set each
        # item's translation so the object's bbox centre lands on its packed slot. Fresh UUIDs on copies.
        used_uuids: set[str] = set(re.findall(r'p:UUID="([^"]*)"', raw_model))
        bed = _bed_of(zin)
        objs_by_path: dict = {}

        def rebuild(build_tag: str) -> str:
            by: dict[str, list[str]] = {}
            for it in _ITEM_RE.findall(build_tag):
                o = re.search(r'objectid="(\d+)"', it)
                if o:
                    by.setdefault(o.group(1), []).append(it)
            # Per kept object with a build item: its base item, current (tx, ty), bbox centre and footprint.
            geo: dict[str, tuple[str, float, float, float, float, float, float]] = {}
            for oid in kept:
                base = by.get(oid)
                if not base:
                    counts[oid] = 0  # on the plate but no <build> item: nothing to place it from
                    continue
                item = base[0]
                tm = re.search(r'transform="([^"]*)"', item)
                nums = tm.group(1).split() if tm else []
                M = [float(x) for x in nums] if len(nums) == 12 else list(_IDENTITY)
                acc = [math.inf, math.inf, -math.inf, -math.inf]
                _accumulate_bbox(objs_by_path, zin, _MODEL_PATH, oid, M, acc)
                if math.isinf(acc[0]):
                    geo[oid] = (item, M[9], M[10], M[9], M[10], 0.0, 0.0)  # no geometry: leave where it is
                else:
                    geo[oid] = (item, M[9], M[10], (acc[0] + acc[2]) / 2, (acc[1] + acc[3]) / 2,
                                acc[2] - acc[0], acc[3] - acc[1])
            # Pack every copy of every object onto the bed.
            entries: list[str] = []
            rects: list[tuple[float, float]] = []
            for oid in kept:
                if oid not in geo:
                    continue
                n = len(copies_of(oid))
                counts[oid] = n
                for _ in range(n):
                    entries.append(oid)
                    rects.append((geo[oid][5], geo[oid][6]))
            places = _shelf_pack(rects, bed, 5.0)
            out: list[str] = []
            seen: dict[str, int] = {}
            for idx, oid in enumerate(entries):
                item, btx, bty, cx, cy, _w, _h = geo[oid]
                px, py = places[idx]
                placed = _set_item_xy(item, btx + (px - cx), bty + (py - cy))
                i = seen.get(oid, 0)
                seen[oid] = i + 1
                if i > 0:
                    placed = _fresh_item_uuid(placed, used_uuids)
                out.append(placed)
            head = re.match(r"<build\b[^>]*>", build_tag).group(0)  # type: ignore[union-attr]
            return head + "\n  " + "\n  ".join(out) + "\n </build>"

        new_model = _BUILD_RE.sub(lambda m: rebuild(m.group(0)), raw_model)
        kept = [oid for oid in kept if counts.get(oid, 0) > 0]
        if not kept:
            raise ValueError("no object on this plate has placeable geometry")

        keep_n = str(plate)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zo:
            for it in zin.infolist():
                n = it.filename
                if n == _SETTINGS_PATH:
                    zo.writestr(it, new_ms.encode("utf-8"))
                    continue
                if n == _MODEL_PATH:
                    zo.writestr(it, new_model.encode("utf-8"))
                    continue
                am = _NUMBERED_ASSET.match(n)
                if am:
                    if am.group(2) != keep_n:
                        continue  # another plate's preview/result: drop it
                    zo.writestr(f"Metadata/{am.group(1)}_1.{am.group(3)}", zin.read(n))
                    continue
                zo.writestr(it, zin.read(n))
    dst.write_bytes(buf.getvalue())
    return {"objects": kept, "copies": {k: counts[k] for k in kept}}
