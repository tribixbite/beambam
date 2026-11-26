"""beambam.project_sync — give a Bambu Studio project the printer's loaded filaments.

Bambu Studio's "Sync filaments from AMS": project filament i becomes the i-th
spool — its system filament preset (by Bambu filament id, else material), its
material and its colour. Filaments past the spools stay as they are, so the
objects painted with them still have a filament.

Why in Python: the headless CLI crashes when `--load-filaments` is given with a
project made for the X2D (rc -6, `std::length_error: vector`, right after
"Initializing StaticPrintConfigs"; checked 2026-11-26), so the project's own
settings are rewritten instead, as pokefit (`presets.add_filament`,
`slicer._filaments_from`, `projectsync.adopt`) does.

Facts this rests on (Bambu Studio `PresetBundle.cpp` load_vendor_configs_from_json,
checked against a project the GUI saved, 2026-11-26):
* A system preset's values are its `inherits` parent's (fully resolved), then
  each `include` template's keys, then its own keys.
* Per-filament project keys are filament-major blocks: length n·k, filament i
  at [i·k, (i+1)·k). On the X2D k is the number of extruder variants
  (`filament_extruder_variant`, e.g. "Direct Drive Standard", "Bowden High
  Flow"), and a preset's per-variant arrays are matched to them by name.
* The project stores a preset's "nil" entries verbatim (they mean "use the
  process/printer value"), so they are written through, not kept from the
  previous filament.
* `different_settings_to_system` / `inherits_group` are [print, filaments…,
  printer]; a slot that is a system preset has "" in both.
"""
from __future__ import annotations

import json
import re
import statistics
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from beambam.slice_options import BED_TYPES, _profiles, find_filament_profile

SETTINGS_MEMBER = "Metadata/project_settings.config"
# Identity/bookkeeping keys of a preset file: never copied into project settings.
META_KEYS = frozenset({
    "name", "inherits", "include", "from", "version", "type", "instantiation", "setting_id", "is_custom_defined",
    "compatible_printers", "compatible_printers_condition", "compatible_prints", "compatible_prints_condition",
    "filament_id", "base_id", "user_id", "updated_time", "description", "alias", "renamed_from"})
# Project keys that describe the filament list's layout or bookkeeping, set here explicitly (not from the preset).
LAYOUT_KEYS = frozenset({"filament_extruder_variant", "filament_self_index", "filament_settings_id",
                         "filament_ids", "filament_colour"})
# Lists whose length is a multiple of the filament count without being per-filament blocks.
NOT_PER_FILAMENT = frozenset({"flush_volumes_matrix", "different_settings_to_system", "inherits_group"})
# One spool: (Bambu filament id e.g. "GFG02" | None, material e.g. "PETG" | None, colour "#RRGGBB").
Spool = tuple[str | None, str | None, str]
_COLOUR = re.compile(r"#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?")
_MAX_DEPTH = 16                     # inherits/include chain length that means a loop


def _as_list(v: Any) -> list[Any]:
    return list(v) if isinstance(v, list) else [v]


@lru_cache(maxsize=4)
def _filament_files(root: Path) -> dict[str, Path]:
    """Bundled filament profile files by preset name (`inherits` / `include` refer to names, which are
    almost always the file stem; the file's own `name` wins where it is not)."""
    folder = _profiles(root, "filament")
    out: dict[str, Path] = {f.stem: f for f in folder.glob("*.json")}
    for f in folder.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("name"), str) and d["name"] != f.stem:
            out.setdefault(d["name"], f)
    return out


def _read(path: Path) -> dict[str, Any]:
    d = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        raise ValueError(f"{path.name} is not a filament profile")
    return d


def flatten_profile(root: Path, path: Path, depth: int = 0) -> dict[str, Any]:
    """A bundled filament profile's full values: its `inherits` parent's, then each `include` template's,
    then its own (the order Bambu Studio loads them). ValueError for a missing parent or a loop."""
    if depth > _MAX_DEPTH:
        raise ValueError(f"filament profile chain too deep at {path.name}")
    own = _read(path)
    files = _filament_files(root)

    def named(name: str) -> Path:
        p = files.get(name)
        if p is None:
            raise ValueError(f"filament profile {own.get('name', path.stem)!r} refers to missing {name!r}")
        return p

    parent = str(own.get("inherits") or "")
    values: dict[str, Any] = dict(flatten_profile(root, named(parent), depth + 1)) if parent else {}
    includes = own.get("include") or []
    for inc in _as_list(includes):
        if inc:
            # Bambu applies only the template keys that differ from the built-in defaults; a bundled template
            # sets nothing else, and the GUI-saved project matched this full overlay (module docstring).
            values.update(flatten_profile(root, named(str(inc)), depth + 1))
    values.update(own)
    return values


def _resolved_id(values: dict[str, Any]) -> str:
    """The Bambu filament id of flattened values ("" when the chain sets none); `filament_id` is set on a
    family's @base preset and inherited, so the flattened dict carries it."""
    fid = values.get("filament_id")
    fid = fid[0] if isinstance(fid, list) and fid else fid
    return str(fid or "")


def _variant_alias(name: str) -> str:
    """The variant a preset without this one's own values uses: newer Bambu Studio adds "E3D High Flow"
    nozzle variants that the bundled (older) profiles lack; the E3D high-flow nozzle is filled from the same
    drive's "High Flow" values. Other names are their own alias."""
    return re.sub(r"\s+E3D\b", "", name)


def _fit(new: Any, old: list[Any], new_variants: list[str] | None = None,
         old_variants: list[str] | None = None) -> list[Any] | None:
    """Preset value `new` reshaped to the project block `old` (one entry per project extruder variant), or
    None when it cannot be.

    Per-variant arrays (len == len(new_variants)) are matched by variant name first, since two variant lists
    of the same length need not be in the same order; a variant the preset lacks takes its alias's value
    (`_variant_alias`), else keeps the old value. Otherwise a value of the block's length is taken as is and a
    single value is repeated for every variant.
    """
    vals = _as_list(new)
    k = len(old)
    if new_variants and old_variants and len(vals) == len(new_variants) and len(old_variants) == k:
        by = {str(v): x for v, x in zip(new_variants, vals)}
        return [by.get(str(v), by.get(_variant_alias(str(v)), o)) for v, o in zip(old_variants, old)]
    if len(vals) == k:
        return vals
    if len(vals) == 1:
        return vals * k
    return None


def _per_filament(settings: dict[str, Any], key: str, n: int) -> bool:
    """Is `settings[key]` a list of n filament-major blocks?"""
    val = settings.get(key)
    return isinstance(val, list) and len(val) >= n and len(val) % n == 0 and key not in NOT_PER_FILAMENT


def _colour(c: str) -> str:
    """'#RRGGBB' (upper case) from '#RRGGBB', 'RRGGBB' or an AMS '#RRGGBBAA'; ValueError otherwise."""
    m = _COLOUR.fullmatch(str(c).strip())
    if not m:
        raise ValueError(f"colour must be #RRGGBB, got {c!r}")
    return f"#{m.group(1).upper()}"


def _rgb(c: str) -> tuple[int, int, int]:
    h = str(c).lstrip("#")[:6].ljust(6, "0")
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return 128, 128, 128           # a project colour that is not hex: mid grey, as Bambu's default


def _nearest(colours: list[str], c: str) -> int:
    """Index of the colour in `colours` closest to `c` (RGB distance; first on a tie)."""
    r, g, b = _rgb(c)
    return min(range(len(colours)),
               key=lambda i: (_rgb(colours[i])[0] - r) ** 2 + (_rgb(colours[i])[1] - g) ** 2
               + (_rgb(colours[i])[2] - b) ** 2)


def _flush_matrix(mat: list[Any], old_colours: list[str], new_colours: list[str]) -> list[str]:
    """`flush_volumes_matrix` (extruders × n × n, row-major) for the new colours: each pair keeps the purge
    volume between the old slots whose colours are closest, and a pair whose closest old slot is the same
    (no measured volume between them) gets the median off-diagonal volume (pokefit `_filaments_from`)."""
    n = len(old_colours)
    if not mat or n == 0 or len(mat) % (n * n):
        return list(mat)
    # A slot whose colour did not change keeps its own row (two slots of one colour stay distinct).
    near = [a if c.upper()[:7] == old_colours[a].upper()[:7] else _nearest(old_colours, c)  # alpha is not colour
            for a, c in enumerate(new_colours)]
    m = [float(x) for x in mat]
    out: list[str] = []
    for e in range(len(m) // (n * n)):
        base = e * n * n
        off = [m[base + i * n + j] for i in range(n) for j in range(n) if i != j]
        typical = statistics.median(off) if off else 0.0
        for a in range(n):
            for b in range(n):
                if a == b:
                    v = 0.0
                elif near[a] != near[b]:
                    v = m[base + near[a] * n + near[b]]
                else:
                    v = typical
                out.append(f"{v:g}")
    return out


def _filaments(nums: list[int]) -> str:
    """ "filament 3" / "filaments 1, 2 and 11" for a note."""
    if len(nums) == 1:
        return f"filament {nums[0]}"
    return f"filaments {', '.join(map(str, nums[:-1]))} and {nums[-1]}"


def adopt_filaments(settings: dict[str, Any], root: Path, spools: list[Spool],
                    warnings: list[str] | None = None) -> dict[str, Any]:
    """`settings` (a project's Metadata/project_settings.config) with project filament i set to `spools[i]`:
    the bundled system preset for the spool's filament id / material (`slice_options.filament_profile`), and
    the spool's colour. A spool with neither id nor material keeps the slot's preset and takes only the
    colour. Filaments past len(spools) stay as they are; spools past the project's filaments are ignored (a
    filament no object uses would only add purge volumes). Returns new settings; `settings` is not changed.
    A spool no bundled X2D 0.4 mm profile matches, or any spool of a project for another nozzle size (the
    bundled presets are the 0.4 mm ones), changes only the colour, and `warnings` says so.
    ValueError for a bad colour or a project with no filament list."""
    notes = warnings if warnings is not None else []
    printer = str(settings.get("printer_settings_id") or "")
    other_nozzle = bool(printer) and not printer.endswith("0.4 nozzle")
    if other_nozzle and any(fid or material for fid, material, _ in spools):
        notes.append(f"the project is for the {printer}: beambam has the 0.4 mm nozzle's filament presets only, "
                     "so its filaments keep their presets (the spools' colours are used)")
    names = settings.get("filament_settings_id")
    if not isinstance(names, list) or not names:
        raise ValueError("the project has no filament list (filament_settings_id)")
    n = len(names)
    out: dict[str, Any] = {k: (list(v) if isinstance(v, list) else v) for k, v in settings.items()}
    old_colours = [str(c) for c in _as_list(settings.get("filament_colour") or [])]
    old_colours = (old_colours + ["#808080"] * n)[:n]
    new_colours = list(old_colours)

    raw_variants = settings.get("filament_extruder_variant")
    variants_all: list[Any] = raw_variants if isinstance(raw_variants, list) else []
    vk = len(variants_all) // n if len(variants_all) % n == 0 else 0   # variants per filament (0 = none)

    # Filament numbers per note, so a full AMS of one unprofiled filament gives one note, not sixteen.
    kept: dict[tuple[str, str], list[int]] = {}         # (spool's material or id, project preset) -> filaments
    stood_in: dict[tuple[str, str], list[int]] = {}     # (spool's id, preset used instead) -> filaments

    for i, (fid, material, colour) in enumerate(spools[:n]):
        new_colours[i] = _colour(colour)
        if (not fid and not material) or other_nozzle:
            continue
        name_path = find_filament_profile(root, fid, material)
        if name_path is None:
            kept.setdefault((str(material or fid), str(names[i])), []).append(i + 1)
            continue
        values = flatten_profile(root, name_path)
        name = str(values.get("name") or name_path.stem)
        if fid and _resolved_id(values) != fid:
            # Matched by material only (the bundled build lacks this filament's X2D profile, e.g. PLA Pure):
            # the plan will still flag the id, so say which preset stood in.
            stood_in.setdefault((fid, name), []).append(i + 1)
        old_variants = [str(v) for v in variants_all[i * vk:(i + 1) * vk]] if vk else None
        new_variants = [str(v) for v in _as_list(values["filament_extruder_variant"])] \
            if values.get("filament_extruder_variant") else None
        for key, val in values.items():
            if key in META_KEYS or key in LAYOUT_KEYS or not _per_filament(out, key, n):
                continue
            k = len(out[key]) // n
            block = out[key][i * k:(i + 1) * k]
            fitted = _fit(val, block, new_variants, old_variants if old_variants and len(old_variants) == k else None)
            if fitted is not None:
                out[key][i * k:(i + 1) * k] = fitted
        out["filament_settings_id"][i] = name
        if _per_filament(out, "filament_ids", n) and len(out["filament_ids"]) == n:
            out["filament_ids"][i] = _resolved_id(values) or out["filament_ids"][i]
        for key in ("different_settings_to_system", "inherits_group"):
            v = out.get(key)
            if isinstance(v, list) and len(v) == n + 2:
                v[1 + i] = ""          # a system preset: no parent, no differences from it

    for (what, preset), nums in kept.items():
        notes.append(f"{_filaments(nums)}: no bundled Bambu profile for {what}: "
                     f"{'it keeps' if len(nums) == 1 else 'they keep'} the project's preset ({preset}); "
                     "only the colour changed")
    for (fid, preset), nums in stood_in.items():
        notes.append(f"{_filaments(nums)}: no bundled X2D profile for {fid}: "
                     f"{'it slices' if len(nums) == 1 else 'they slice'} with {preset} (same material), "
                     "not that filament's own temperatures and flow")
    out["filament_colour"] = new_colours
    mat = settings.get("flush_volumes_matrix")
    if isinstance(mat, list) and mat:
        out["flush_volumes_matrix"] = _flush_matrix(mat, old_colours, new_colours)
    return out


def adopt_project(src: Path, dst: Path, root: Path, spools: list[Spool], bed_type: str | None = None) -> dict[str, Any]:
    """Copy the project `src` to `dst` with its filaments set to `spools` (`adopt_filaments`) and, with
    `bed_type` (one of BED_TYPES), its plate type. Every other zip member is copied unchanged. Returns the new
    settings. ValueError for a bad bed type or a file that is not a Bambu Studio project."""
    if bed_type is not None and bed_type not in BED_TYPES:
        raise ValueError(f"bed type must be one of {', '.join(BED_TYPES)}")
    try:
        with zipfile.ZipFile(src) as zin:
            if SETTINGS_MEMBER not in zin.namelist():
                raise ValueError(f"{src.name} has no {SETTINGS_MEMBER}: not a Bambu Studio project")
            settings = json.loads(zin.read(SETTINGS_MEMBER).decode("utf-8-sig"))
    except zipfile.BadZipFile as e:
        raise ValueError(f"{src.name} is not a 3MF (zip) file") from e
    if not isinstance(settings, dict):
        raise ValueError(f"{src.name}: {SETTINGS_MEMBER} is not a settings object")
    new = adopt_filaments(settings, root, spools)
    if bed_type is not None:
        new["curr_bed_type"] = bed_type
    tmp = dst.with_name(dst.name + ".part")
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item)
                if item.filename == SETTINGS_MEMBER:
                    data = json.dumps(new, indent=4).encode("utf-8")
                zout.writestr(item, data)       # the member's own ZipInfo keeps its name, date and compression
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    return new
