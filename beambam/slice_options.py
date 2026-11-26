"""beambam.slice_options — print settings a web client asks a slice for, and the profiles a bare mesh slices with.

`POST /slice` takes them as the `X-Slice-Options` header (compact JSON), for a
Bambu Studio project or a bare mesh (STL / OBJ / STEP, or a 3MF with no print
settings):

    {"layer_height": 0.2, "wall_loops": 3, "infill": 20,
     "supports": "auto" | "plate" | "off", "brim": "auto_brim" | "outer_only" | "no_brim",
     "nozzles": [1, 2, ...],                       # per filament: 1 = left, 2 = right
     "bed_type": "Supertack Plate",                # the build plate (BED_TYPES)
     "spools": [{"id": "GFG02", "type": "PETG", "colour": "#FF0000"}, ...],  # per filament: the spool it
                                                   # prints from; a project takes its preset and colour
                                                   # (project_sync.adopt_filaments), a mesh its preset
     "filament_ids": ["GFA00"], "filament_types": ["PLA"]}   # mesh files: the spools' presets

Facts these rest on (checked on the X2D's bundled CLI, 2026-11-25/26):
* The CLI takes a project's nozzle grouping from the plate (model_settings
  `filament_map_mode` / `filament_maps`) before project_settings, so a manual
  map is written in both. A mesh slice takes it as `--filament-map-mode Manual
  --filament-map 2,1`.
* Bambu refuses any line no wider than the layer (code -51, "Too small line
  width"): a taller layer widens those lines to layer + 0.02 mm.
* A user process profile (`inherits` a system one, `compatible_printers` set)
  overlays the chosen keys; a user filament profile crashes this CLI, so mesh
  filaments use the bundled system profile of the spool's filament id/type.
"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

SUPPORT_MODES = ("auto", "plate", "off")
BRIM_TYPES = ("auto_brim", "outer_only", "no_brim")
# Bambu Studio's `curr_bed_type` values (PrintConfig.cpp), in its own order.
BED_TYPES = ("Cool Plate", "Engineering Plate", "High Temp Plate", "Textured PEI Plate", "Supertack Plate")
LAYER_RANGE = (0.08, 0.28)          # mm: Bambu's range for a 0.4 mm nozzle
LINE_MARGIN = 0.02                  # mm a widened line stays wider than the layer
MAX_HEADER = 4096                   # bytes of X-Slice-Options accepted
MESH_SUFFIXES = (".stl", ".obj", ".step", ".stp")
# The printer a bare mesh is sliced for (the slicer's own Bambu profiles).
MESH_PRINTER = "Bambu Lab X2D 0.4 nozzle"
DEFAULT_PROCESS = "0.20mm Standard @BBL X2D"
DEFAULT_FILAMENT = "Bambu PLA Basic @BBL X2D 0.4 nozzle"


def _whole(v: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or not lo <= v <= hi:
        raise ValueError(f"{name} must be a whole number {lo}..{hi}")
    return int(v)


def _strings(v: Any, name: str) -> tuple[str, ...]:
    if not isinstance(v, list) or not 0 < len(v) <= 64 or not all(isinstance(x, str) and len(x) < 80 for x in v):
        raise ValueError(f"{name} must be a list of short strings, one per filament")
    return tuple(v)


_HEX = re.compile(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?")


def _spools(v: Any) -> tuple[tuple[str | None, str | None, str], ...]:
    """`spools`: a list (1..64) of {"id": "GFG02" | null, "type": "PETG" | null, "colour": "#RRGGBB"}."""
    if not isinstance(v, list) or not 0 < len(v) <= 64:
        raise ValueError("spools must be a list of {id, type, colour}, one per filament")
    out = []
    for x in v:
        if not isinstance(x, dict) or set(x) - {"id", "type", "colour"}:
            raise ValueError("each spool must be {id, type, colour}")
        fid, ftype, colour = x.get("id"), x.get("type"), x.get("colour")
        for name, val in (("id", fid), ("type", ftype)):
            if val is not None and not (isinstance(val, str) and re.fullmatch(r"[A-Za-z0-9 +._-]{1,40}", val)):
                raise ValueError(f"spool {name} must be a short name like GFG02 / PETG")
        if not (isinstance(colour, str) and _HEX.fullmatch(colour)):
            raise ValueError("spool colour must be #RRGGBB")
        out.append((fid or None, ftype or None, colour[:7].upper()))
    return tuple(out)


@dataclass(frozen=True)
class SliceOptions:
    """Validated slice options; every field None/empty = the file's own setting."""
    layer_height: float | None = None
    wall_loops: int | None = None
    infill: int | None = None
    supports: str | None = None
    brim: str | None = None
    nozzles: tuple[int, ...] | None = None
    bed_type: str | None = None
    # Per filament: (Bambu filament id | None, material | None, colour "#RRGGBB") of the spool it prints from.
    spools: tuple[tuple[str | None, str | None, str], ...] = field(default_factory=tuple)
    filament_ids: tuple[str, ...] = field(default_factory=tuple)
    filament_types: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_header(cls, raw: str | None) -> "SliceOptions":
        """Parse the `X-Slice-Options` header (absent or empty = no options). ValueError on anything off."""
        if not raw or not raw.strip():
            return cls()
        if len(raw.encode()) > MAX_HEADER:
            raise ValueError(f"X-Slice-Options is longer than {MAX_HEADER} bytes")
        try:
            d = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"X-Slice-Options is not JSON: {e}") from e
        if not isinstance(d, dict):
            raise ValueError("X-Slice-Options must be a JSON object")
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SliceOptions":
        known = {"layer_height", "wall_loops", "infill", "supports", "brim", "nozzles", "bed_type", "spools",
                 "filament_ids", "filament_types"}
        unknown = sorted(set(d) - known)
        if unknown:
            raise ValueError(f"unknown slice option(s): {', '.join(unknown)}")
        lh = d.get("layer_height")
        if lh is not None:
            if isinstance(lh, bool) or not isinstance(lh, (int, float)) or not LAYER_RANGE[0] <= lh <= LAYER_RANGE[1]:
                raise ValueError(f"layer_height must be {LAYER_RANGE[0]}..{LAYER_RANGE[1]} mm")
            lh = round(float(lh), 3)
        sup = d.get("supports")
        if sup is not None and sup not in SUPPORT_MODES:
            raise ValueError(f"supports must be one of {', '.join(SUPPORT_MODES)}")
        brim = d.get("brim")
        if brim is not None and brim not in BRIM_TYPES:
            raise ValueError(f"brim must be one of {', '.join(BRIM_TYPES)}")
        bed = d.get("bed_type")
        if bed is not None and bed not in BED_TYPES:
            raise ValueError(f"bed_type must be one of {', '.join(BED_TYPES)}")
        noz = d.get("nozzles")
        if noz is not None:
            if (not isinstance(noz, list) or not 0 < len(noz) <= 64
                    or any(isinstance(n, bool) or n not in (1, 2) for n in noz)):
                raise ValueError("nozzles must be a list of 1 (left) / 2 (right), one per filament")
            noz = tuple(int(n) for n in noz)
        return cls(
            layer_height=lh,
            wall_loops=_whole(d["wall_loops"], "wall_loops", 1, 10) if d.get("wall_loops") is not None else None,
            infill=_whole(d["infill"], "infill", 0, 100) if d.get("infill") is not None else None,
            supports=sup, brim=brim, nozzles=noz, bed_type=bed,
            spools=_spools(d["spools"]) if d.get("spools") is not None else (),
            filament_ids=_strings(d["filament_ids"], "filament_ids") if d.get("filament_ids") else (),
            filament_types=_strings(d["filament_types"], "filament_types") if d.get("filament_types") else (),
        )

    def filament(self, i: int) -> tuple[str | None, str | None]:
        """(filament id, material) filament `i` slices with: the explicit lists first, then its spool."""
        spool = self.spools[i] if i < len(self.spools) else (None, None, "")
        fid = self.filament_ids[i] if i < len(self.filament_ids) else spool[0]
        ftype = self.filament_types[i] if i < len(self.filament_types) else spool[1]
        return fid or None, ftype or None

    def process_keys(self, current: dict[str, Any] | None = None) -> dict[str, str]:
        """The process settings these options set (Bambu keys, string values). `current`: the settings they apply
        to, whose explicit line widths a taller layer must clear."""
        out: dict[str, str] = {}
        if self.layer_height is not None:
            out["layer_height"] = f"{self.layer_height:g}"
            for key, w in _line_widths(current or {}).items():
                if w <= self.layer_height:
                    out[key] = f"{round(self.layer_height + LINE_MARGIN, 3):g}"
        if self.wall_loops is not None:
            out["wall_loops"] = str(self.wall_loops)
        if self.infill is not None:
            out["sparse_infill_density"] = f"{self.infill}%"
        if self.supports == "off":
            out["enable_support"] = "0"
        elif self.supports in ("auto", "plate"):
            out["enable_support"] = "1"
            out["support_on_build_plate_only"] = "1" if self.supports == "plate" else "0"
            if not str((current or {}).get("support_type", "")).endswith("(auto)"):
                out["support_type"] = "tree(auto)"
        if self.brim is not None:
            out["brim_type"] = self.brim
        if self.bed_type is not None:
            out["curr_bed_type"] = self.bed_type
        return out


def _line_widths(st: dict[str, Any]) -> dict[str, float]:
    """The explicit line widths of a process (mm; 0 = automatic and % values left out)."""
    out: dict[str, float] = {}
    for key, v in st.items():
        if key.endswith("line_width") and isinstance(v, str) and not v.endswith("%"):
            try:
                w = float(v)
            except ValueError:
                continue
            if w > 0:
                out[key] = w
    return out


def nozzle_map(nozzles: tuple[int, ...], n_filaments: int) -> list[str]:
    """`filament_map` for `n_filaments` filaments: the chosen nozzles, filaments past them on the left (Bambu's
    default), entries past the project's filaments dropped."""
    return ([str(x) for x in nozzles] + ["1"] * n_filaments)[:n_filaments]


def apply_to_project(src: Path, dst: Path, opts: SliceOptions, profiles: Path | None = None,
                     warnings: list[str] | None = None) -> dict[str, Any]:
    """Copy project `src` to `dst` with `opts` applied: the spools' filaments (`project_sync.adopt_filaments`,
    which needs the slicer's Bambu profiles folder `profiles`), project_settings, and a manual nozzle map on every plate as well.
    Returns the new project settings; what the spools could not change is appended to `warnings`. What POST
    /slice slices and POST /project/update hands back."""
    with zipfile.ZipFile(src) as zin:
        settings = json.loads(zin.read("Metadata/project_settings.config"))
        if opts.spools:
            if profiles is None:
                raise ValueError("spools need the slicer's Bambu profiles folder")
            from beambam.project_sync import adopt_filaments
            settings = adopt_filaments(settings, profiles, list(opts.spools), warnings)
        settings.update(opts.process_keys(settings))
        n = len(settings.get("filament_settings_id") or []) or (len(opts.nozzles) if opts.nozzles else 1)
        fmap = nozzle_map(opts.nozzles, n) if opts.nozzles else None
        if fmap:
            settings["filament_map_mode"] = "Manual"
            settings["filament_map"] = fmap
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item)
                if item.filename == "Metadata/project_settings.config":
                    data = json.dumps(settings, indent=4).encode()
                elif item.filename == "Metadata/model_settings.config" and (fmap or opts.bed_type):
                    text = data.decode("utf-8", "replace")
                    if fmap:
                        text = plate_map(text, fmap)
                    if opts.bed_type:
                        text = plate_bed_type(text, opts.bed_type)
                    data = text.encode()
                zout.writestr(item, data)
    return settings


_PLATE_MAP = re.compile(r'\s*<metadata key="(filament_map_mode|filament_maps)" value="[^"]*"\s*/>')


def plate_map(model_settings: str, fmap: list[str]) -> str:
    """model_settings.config with every `<plate>` set to the manual map `fmap` (any earlier map replaced)."""
    meta = (f'\n    <metadata key="filament_map_mode" value="Manual"/>'
            f'\n    <metadata key="filament_maps" value="{" ".join(fmap)}"/>')

    def one(m: re.Match[str]) -> str:
        body = _PLATE_MAP.sub("", m.group(2))
        return f"{m.group(1)}{meta}{body}{m.group(3)}"
    return re.sub(r"(<plate>)(.*?)(</plate>)", one, model_settings, flags=re.S)


# --------------------------------------------------------------------------- bare meshes

def plate_bed_type(model_settings: str, bed_type: str) -> str:
    """model_settings.config with every plate's own `bed_type` set to `bed_type`: Bambu applies a plate's
    config over the project's, so the project's `curr_bed_type` alone does not change a plate that names one."""
    return re.sub(r'(<metadata key="(?:bed_type|curr_bed_type)" value=")[^"]*("\s*/>)',
                  lambda m: f"{m.group(1)}{bed_type}{m.group(2)}", model_settings)


def made_for_another_printer(settings: dict[str, Any]) -> str | None:
    """The printer a project was made for when it is not MESH_PRINTER's model (an X2D project with another
    nozzle size is the same printer), else None."""
    name = str(settings.get("printer_settings_id") or "")
    model = MESH_PRINTER.rsplit(" ", 2)[0]                       # "Bambu Lab X2D"
    return name if name and not name.startswith(model) else None


def retarget(settings: dict[str, Any], opts: SliceOptions) -> SliceOptions:
    """Options for slicing a project made for another printer on MESH_PRINTER: its layer height and its
    filaments' ids and materials, unless the caller gave them."""
    from dataclasses import replace

    def floats(v: Any) -> float | None:
        try:
            return round(float(v), 3)
        except (TypeError, ValueError):
            return None
    lh = opts.layer_height if opts.layer_height is not None else floats(settings.get("layer_height"))
    if lh is not None and not LAYER_RANGE[0] <= lh <= LAYER_RANGE[1]:
        lh = None
    if opts.spools:                     # the printer's spools stand in for the project's filaments
        return replace(opts, layer_height=lh)
    return replace(opts, layer_height=lh,
                   filament_ids=opts.filament_ids or tuple(str(x) for x in settings.get("filament_ids") or ()),
                   filament_types=opts.filament_types or tuple(str(x) for x in settings.get("filament_type") or ()))


def has_print_settings(path: Path) -> bool:
    """A 3MF carrying Bambu/Orca print settings (else it is geometry only and slices like a mesh)."""
    try:
        with zipfile.ZipFile(path) as z:
            return "Metadata/project_settings.config" in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def _profiles(profiles: Path, kind: str) -> Path:
    """`kind` ("machine", "process", "filament") of the slicer's Bambu profiles: `profiles` is its
    `resources/profiles/BBL` folder (`slice_service.find_slicer`)."""
    return profiles / kind


@lru_cache(maxsize=4)
def _filament_index(profiles: Path) -> tuple[tuple[str, str, str], ...]:
    """(profile name, filament_id, filament_type) of each bundled filament profile for MESH_PRINTER, its id and
    type resolved through `inherits`."""
    folder = _profiles(profiles, "filament")
    by_name: dict[str, dict[str, Any]] = {}
    for f in folder.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("name"), str):
            by_name[d["name"]] = d

    def resolve(d: dict[str, Any], key: str, depth: int = 0) -> str:
        v = d.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            return str(v)
        parent = by_name.get(str(d.get("inherits") or ""))
        return resolve(parent, key, depth + 1) if parent and depth < 8 else ""
    out = []
    for name, d in by_name.items():
        if name.endswith(f"@BBL X2D 0.4 nozzle") and MESH_PRINTER in (d.get("compatible_printers") or [MESH_PRINTER]):
            out.append((name, resolve(d, "filament_id"), resolve(d, "filament_type").upper()))
    return tuple(sorted(out))


def filament_profile(profiles: Path, filament_id: str | None, filament_type: str | None) -> Path:
    """The bundled profile a mesh filament slices with: the spool's own filament id, else Bambu's or a generic
    profile of its material, else Bambu PLA Basic."""
    return find_filament_profile(profiles, filament_id, filament_type) or \
        _profiles(profiles, "filament") / f"{DEFAULT_FILAMENT}.json"


def find_filament_profile(profiles: Path, filament_id: str | None, filament_type: str | None) -> Path | None:
    """The bundled X2D 0.4 mm profile for a spool's filament id, else Bambu's or a generic profile of its
    material; None when neither matches (no silent fallback: a project keeps its own preset then)."""
    index = _filament_index(profiles)
    folder = _profiles(profiles, "filament")
    want_type = (filament_type or "").upper()
    for name, fid, _ in index:
        if filament_id and fid == filament_id:
            return folder / f"{name}.json"
    if want_type:
        for prefer in (f"Bambu {want_type} Basic", f"Generic {want_type}", f"Bambu {want_type}"):
            for name, _, ftype in index:
                if ftype == want_type and name.startswith(prefer):
                    return folder / f"{name}.json"
        for name, _, ftype in index:
            if ftype == want_type:
                return folder / f"{name}.json"
    return None


def process_profile(profiles: Path, layer_height: float | None) -> Path:
    """The bundled X2D 0.4 mm process closest to `layer_height` (default 0.20 mm Standard)."""
    folder = _profiles(profiles, "process")
    if layer_height is None:
        return folder / f"{DEFAULT_PROCESS}.json"
    best: tuple[float, int, Path] | None = None
    for f in folder.glob("*@BBL X2D.json"):          # no nozzle suffix = the 0.4 mm nozzle's
        m = re.match(r"(\d+\.\d+)mm (\w+)", f.stem)
        if not m:
            continue
        # Same height: Standard before the others.
        key = (abs(float(m.group(1)) - layer_height), 0 if m.group(2) == "Standard" else 1, f)
        if best is None or key[:2] < best[:2]:
            best = key
    return best[2] if best else folder / f"{DEFAULT_PROCESS}.json"


# Process settings a project made for another printer keeps when it is sliced for MESH_PRINTER (the rest are
# printer-specific: speeds, accelerations, line widths come from the bundled process).
PORTABLE_KEYS = ("wall_loops", "sparse_infill_density", "sparse_infill_pattern", "top_shell_layers",
                 "bottom_shell_layers", "enable_support", "support_type", "support_on_build_plate_only",
                 "brim_type", "seam_position", "ironing_type", "only_one_wall_top")


def mesh_process(profiles: Path, opts: SliceOptions, out_dir: Path, keep: dict[str, Any] | None = None) -> Path:
    """A user process profile: the bundled one nearest the layer height, with `keep` (a project's portable
    settings) and then the options on top."""
    base = process_profile(profiles, opts.layer_height)
    sysd = json.loads(base.read_text(encoding="utf-8"))
    user = {"type": "process", "name": "beambam process", "from": "User", "inherits": sysd["name"],
            "print_settings_id": "beambam process",
            "compatible_printers": sysd.get("compatible_printers") or [MESH_PRINTER]}
    user.update({k: str(v) for k, v in (keep or {}).items() if k in PORTABLE_KEYS and isinstance(v, (str, int, float))})
    user.update(opts.process_keys({**sysd, **user}))
    path = out_dir / "beambam_process.json"
    path.write_text(json.dumps(user, indent=1), encoding="utf-8")
    return path


def flattened(profile: Path) -> Path:
    """The `*_full` copy of a system profile (profile_flatten), when there is one. `--load-filaments` reads the
    file it is given as it is, without its `inherits` chain: the source profile of Bambu PLA Basic has no
    density or filament id of its own (both live in `@base`), so a mesh sliced with it reported 0 g and
    printed with the CLI's defaults for every inherited setting."""
    full = profile.parent.parent / f"{profile.parent.name}_full" / profile.name
    return full if full.is_file() else profile


def mesh_args(profiles: Path, opts: SliceOptions, out_dir: Path, n_filaments: int = 1,
              keep: dict[str, Any] | None = None, arrange: bool = True) -> list[str]:
    """The CLI arguments that slice a bare mesh, or a project made for another printer (`keep`: its portable
    process settings; `arrange` False keeps its placement), for MESH_PRINTER with `opts` (process, filaments,
    nozzle map)."""
    machine = _profiles(profiles, "machine") / f"{MESH_PRINTER}.json"
    n = max(n_filaments, len(opts.filament_ids), len(opts.filament_types), len(opts.spools), 1)
    fils = [flattened(filament_profile(profiles, *opts.filament(i))) for i in range(n)]
    args = (["--arrange", "1"] if arrange else []) + [
            "--load-settings", f"{machine};{mesh_process(profiles, opts, out_dir, keep)}",
            "--load-filaments", ";".join(str(f) for f in fils)]
    if opts.nozzles:
        args += ["--filament-map-mode", "Manual", "--filament-map", ",".join(nozzle_map(opts.nozzles, n))]
    return args
