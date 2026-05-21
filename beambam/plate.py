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
import json
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
    if keep is not None:
        filter_3mf(src, dst, keep=keep)
    else:
        filter_3mf(src, dst, drop=drop)
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
