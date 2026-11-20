"""beambam.filament_mapping — map a sliced plate's filaments to the printer's trays.

This is the beambam counterpart of BambuStudio's print-dialog mapping
(`SelectMachineDialog::do_ams_mapping` + `DevMappingUtil::ams_filament_mapping`
in `bs-bionic/src/slic3r/GUI/`), extended with the X2D/H2D dual-nozzle and
Filament Track Switch rules. Three inputs, one output:

  ProjectInfo  (read_project)   — what the .gcode.3mf plate needs: the filaments
                                  it actually USES, each with its project index,
                                  type, colour, profile id and assigned nozzle.
  Topology     (read_topology)  — what the printer has: every AMS tray and
                                  external spool, and which extruder(s) each can
                                  feed (AMS `info` bits + the Filament Track
                                  Switch, `print.aux` bit 29).
  slots / map  (user choice)    — nothing (auto), `--slot N`, or `--map F=T,…`.
        ↓
  PrintPlan    (plan_print)     — per-filament assignments with hard errors /
                                  soft warnings, plus the `ams_mapping` and
                                  `ams_mapping2` arrays for `print.project_file`.

Facts the code relies on (all from the BambuStudio source, cited inline):

  * `ams_mapping` is indexed by PROJECT FILAMENT (0-based), one entry per
    filament, -1 for filaments the plate does not use (`get_ams_mapping_result`).
    A plate that only uses filament 5 sends `[-1, -1, -1, -1, <tray>, …]`.
  * Tray ids: AMS / AMS Lite / AMS 2 Pro (types 1-3) = ams_id*4 + slot;
    AMS HT (type 4) = ams_id + slot (ams_id ≥ 128); external spools are
    255 (main = right extruder) and 254 (deputy = left extruder), sent as -1
    in `ams_mapping` and `{ams_id: 255|254, slot_id: 0}` in `ams_mapping2`.
  * `filament_map[i]` in project_settings: 1 = left nozzle, 2 = right nozzle
    (`FilamentMapNozzleId`); extruder ids: 0 = main/right, 1 = deputy/left.
  * AMS `info` hex: bits 0-3 type, bits 8-11 bound extruder; 0xE with the
    switch installed means "via the switch" and bits 24-27 give the switch
    input (0 = B, 1 = A) — such an AMS can feed BOTH extruders
    (`DevFilaSystem.cpp` ParseAmsInfo).
  * Auto-mapping (`DevMapping.cpp`): only same-type trays reachable from the
    filament's nozzle are candidates; repeatedly take the globally closest
    (CIE76 ΔE) unassigned filament/tray pair, preferring an exact profile-id
    match on ties; reuse an already-picked tray only when nothing else fits.

beambam differences, deliberately:
  * Ordering follows the user's rule in `beambam.filament_match`: fewer than
    3 colours → type first (exact type, then profile, then colour); 3+ colours
    → colour first. Material CLASS compatibility (PLA vs PETG …) is always a
    hard constraint — the gcode's temperatures are per filament.
  * Files sliced with `enable_filament_dynamic_map` (the switch's automatic
    nozzle mode) are refused: they need a printer-computed `nozzle_mapping`
    that beambam cannot send yet (see print_job.request_auto_nozzle_mapping).
"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beambam.filament_match import _hex_rgb, is_aesthetic

# Extruder ids as the firmware / BambuStudio use them (DevDefs.h).
MAIN_EXTRUDER = 0      # right nozzle
DEPUTY_EXTRUDER = 1    # left nozzle
EXT_SPOOL_MAIN = 255   # external spool feeding the main (right) extruder
EXT_SPOOL_DEPUTY = 254 # external spool feeding the deputy (left) extruder

# AMS types from the low nibble of `ams[].info` (DevAmsType).
_AMS_TYPE_NAMES = {1: "AMS", 2: "AMS Lite", 3: "AMS 2 Pro", 4: "AMS HT"}

# ΔE76 above which two colours count as visibly different (≈ 2.3 is the
# just-noticeable difference; 2.3 keeps "same colour, different hex
# rounding" quiet while flagging any real change).
COLOUR_DIFFERS_DE = 2.3

# Width of the mapping arrays in a Bambu Studio X2D LAN capture
# (~/.x2d/captured_project_file.json): 10 entries padded with -1 / 255.
MAPPING_WIDTH = 10


class MappingError(Exception):
    """The plate cannot be mapped (hard error; never bypassable)."""


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

def _srgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """sRGB (0-255) → CIE L*a*b* (D65), as BambuStudio's RGB2Lab."""
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.00000
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    """CIE76 colour difference between two hex colours ('#RRGGBB[AA]')."""
    la, lb = _srgb_to_lab(_hex_rgb(a)), _srgb_to_lab(_hex_rgb(b))
    return sum((p - q) ** 2 for p, q in zip(la, lb)) ** 0.5


def _norm_colour(c: str) -> str:
    """'f72323ff' / '#F72323' → '#F72323' (alpha dropped for display)."""
    s = (c or "").lstrip("#").upper()
    return f"#{s[:6]}" if len(s) >= 6 else ""


# ---------------------------------------------------------------------------
# Project side
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectFilament:
    """One filament the plate uses."""
    index: int            # 0-based project filament index (ams_mapping position)
    type: str             # e.g. "PLA"
    colour: str           # '#RRGGBB'
    profile_id: str       # e.g. "GFA00" ('' when the slice didn't record one)
    nozzle: int           # extruder id it was sliced for (MAIN/DEPUTY)
    used_g: float

    @property
    def number(self) -> int:
        """1-based number as Bambu Studio and the touchscreen show it."""
        return self.index + 1


@dataclass(frozen=True)
class ProjectInfo:
    path: Path
    plate: int
    total_filaments: int
    used: tuple[ProjectFilament, ...]
    nozzle_count: int
    has_switcher: bool        # sliced with the Filament Track Switch profile
    dynamic_nozzle_map: bool  # sliced for the switch's automatic nozzle mode


def _flag(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true")


def read_project(path: Path | str, plate: int = 1) -> ProjectInfo:
    """Read plate `plate` of a .gcode.3mf: used filaments, nozzles, switch flags."""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            ps = json.loads(z.read("Metadata/project_settings.config").decode("utf-8", "replace"))
            slice_info = (z.read("Metadata/slice_info.config").decode("utf-8", "replace")
                          if "Metadata/slice_info.config" in names else "")
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError) as e:
        raise MappingError(f"cannot read {path.name} as a sliced .gcode.3mf: {e}") from e
    if f"Metadata/plate_{plate}.gcode" not in names:
        plates = sorted(int(m.group(1)) for n in names
                        if (m := re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n)))
        raise MappingError(f"{path.name} has no sliced plate {plate} "
                           f"(sliced plates: {plates or 'none'})")

    types = ps.get("filament_type") or []
    colours = ps.get("filament_colour") or []
    ids = ps.get("filament_ids") or []
    fmap = ps.get("filament_map") or []
    nozzle_count = max(1, len(ps.get("nozzle_diameter") or [0]))

    def nozzle_of(i: int) -> int:
        if nozzle_count < 2:
            return MAIN_EXTRUDER
        v = str(fmap[i]) if i < len(fmap) else "1"
        return MAIN_EXTRUDER if v == "2" else DEPUTY_EXTRUDER

    # slice_info lists the filaments each plate really uses (1-based ids).
    used_ids: dict[int, float] = {}
    for block in re.findall(r"<plate>.*?</plate>", slice_info, re.S):
        m = re.search(r'key="index"\s+value="(\d+)"', block)
        if not m or int(m.group(1)) != plate:
            continue
        for fm in re.finditer(r"<filament\s+([^>]*)/>", block):
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', fm.group(1)))
            try:
                used_ids[int(attrs["id"]) - 1] = float(attrs.get("used_g") or 0)
            except (KeyError, ValueError):
                continue
    if not used_ids:
        # Older slices without slice_info: assume filament 1 only.
        used_ids = {0: 0.0}

    used = tuple(
        ProjectFilament(
            index=i,
            type=(types[i] if i < len(types) else "").strip(),
            colour=_norm_colour(colours[i] if i < len(colours) else ""),
            profile_id=(ids[i] if i < len(ids) else "").strip(),
            nozzle=nozzle_of(i),
            used_g=g,
        )
        for i, g in sorted(used_ids.items()))
    return ProjectInfo(
        path=path, plate=plate,
        total_filaments=max(len(types), max(used_ids) + 1),
        used=used, nozzle_count=nozzle_count,
        has_switcher=_flag(ps.get("has_filament_switcher")),
        dynamic_nozzle_map=_flag(ps.get("enable_filament_dynamic_map")),
    )


# ---------------------------------------------------------------------------
# Printer side
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Tray:
    """One AMS slot or external spool, as the mapping sees it."""
    tray_id: int                 # value for ams_mapping (BS tray index)
    ams_id: int                  # ams_mapping2.ams_id (255/254 for external)
    slot_id: int                 # ams_mapping2.slot_id
    type: str
    colour: str                  # '#RRGGBB' or ''
    profile_id: str              # tray_info_idx, e.g. "GFA00"
    name: str                    # sub-brand, e.g. "PLA Basic"
    extruders: frozenset[int]    # extruder ids this tray can feed
    switch_input: str | None     # "A" / "B" when routed through the switch
    unit_type: str               # "AMS 2 Pro", "External", …
    remain: int                  # % left, -1 unknown

    @property
    def external(self) -> bool:
        return self.ams_id in (EXT_SPOOL_MAIN, EXT_SPOOL_DEPUTY)

    @property
    def empty(self) -> bool:
        return not self.type

    def label(self) -> str:
        if self.external:
            return f"External ({'right' if self.ams_id == EXT_SPOOL_MAIN else 'left'})"
        if self.ams_id >= 128:
            return f"{self.unit_type} {self.ams_id - 127} (slot {self.tray_id})"
        return f"{tray_letter_label(self.tray_id)} (slot {self.tray_id})"


@dataclass(frozen=True)
class Topology:
    nozzle_count: int
    switch_installed: bool
    switch_ready: bool
    trays: tuple[Tray, ...]

    def tray(self, tray_id: int) -> Tray | None:
        return next((t for t in self.trays if t.tray_id == tray_id), None)


def tray_letter_label(tray_id: int) -> str:
    """Global slot → Bambu's touchscreen / Studio label: 0 → 'A1', 9 → 'C2'."""
    return f"{chr(ord('A') + tray_id // 4)}{tray_id % 4 + 1}"


def _bits(value: int, start: int, count: int) -> int:
    return (value >> start) & ((1 << count) - 1)


def _int_or(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _hexint(s: Any) -> int:
    try:
        return int(str(s), 16)
    except (TypeError, ValueError):
        return 0


def read_topology(state: dict) -> Topology:
    """Trays + reachability from a pushall state (`{"print": {...}}` or the inner dict)."""
    p = state.get("print", state) if isinstance(state, dict) else {}
    ext_info = ((p.get("device") or {}).get("extruder") or {}).get("info") or []
    nozzle_count = max(1, len(ext_info))
    switch_installed = bool(_bits(_hexint(p.get("aux")), 29, 1))

    trays: list[Tray] = []
    ams_ready = True
    for unit in ((p.get("ams") or {}).get("ams") or []):
        try:
            ams_id = int(unit.get("id"))
        except (TypeError, ValueError):
            continue
        info_s = unit.get("info")
        info = _hexint(info_s)
        unit_type_id = _bits(info, 0, 4) if info_s else 1
        ext = _bits(info, 8, 4)
        switch_input: str | None = None
        if not info_s:
            extruders = frozenset({MAIN_EXTRUDER})
        elif ext == 0xE and switch_installed:
            pos = _bits(info, 24, 4)
            extruders = frozenset({MAIN_EXTRUDER, DEPUTY_EXTRUDER}) if pos in (0, 1) else frozenset()
            switch_input = {0: "B", 1: "A"}.get(pos)
        elif ext == 0xE:
            extruders = frozenset()   # routed to a switch that isn't installed
        else:
            extruders = frozenset({ext})
        if not extruders or (switch_installed and switch_input is None):
            ams_ready = False
        for tr in unit.get("tray") or []:
            try:
                slot = int(tr.get("id"))
            except (TypeError, ValueError):
                continue
            tray_id = ams_id + slot if unit_type_id == 4 else ams_id * 4 + slot
            trays.append(Tray(
                tray_id=tray_id, ams_id=ams_id, slot_id=slot,
                type=(tr.get("tray_type") or "").strip(),
                colour=_norm_colour(tr.get("tray_color") or ""),
                profile_id=(tr.get("tray_info_idx") or "").strip(),
                name=(tr.get("tray_sub_brands") or "").strip(),
                extruders=extruders, switch_input=switch_input,
                unit_type=_AMS_TYPE_NAMES.get(unit_type_id, "AMS"),
                remain=_int_or(tr.get("remain"), -1),
            ))
    for v in p.get("vir_slot") or []:
        try:
            vid = int(v.get("id"))
        except (TypeError, ValueError):
            continue
        if vid not in (EXT_SPOOL_MAIN, EXT_SPOOL_DEPUTY):
            continue
        trays.append(Tray(
            tray_id=vid, ams_id=vid, slot_id=0,
            type=(v.get("tray_type") or "").strip(),
            colour=_norm_colour(v.get("tray_color") or ""),
            profile_id=(v.get("tray_info_idx") or "").strip(),
            name=(v.get("tray_sub_brands") or "").strip(),
            extruders=frozenset({MAIN_EXTRUDER if vid == EXT_SPOOL_MAIN or nozzle_count < 2
                                 else DEPUTY_EXTRUDER}),
            switch_input=None, unit_type="External", remain=-1,
        ))
    return Topology(nozzle_count=nozzle_count, switch_installed=switch_installed,
                    switch_ready=switch_installed and ams_ready, trays=tuple(trays))


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _material_class(t: str) -> str:
    """'PLA Silk' / 'PLA-CF' → 'PLA' (same rule as print_job._filament_class)."""
    return t.replace("-", " ").replace("_", " ").strip().split(" ", 1)[0].upper()


def nozzle_name(extruder: int, nozzle_count: int) -> str:
    if nozzle_count < 2:
        return "-"
    return "right" if extruder == MAIN_EXTRUDER else "left"


@dataclass
class Assignment:
    filament: ProjectFilament
    tray: Tray | None
    errors: list[str] = field(default_factory=list)    # hard: never bypassable
    warnings: list[str] = field(default_factory=list)  # soft: need confirm or --force

    @property
    def colour_delta(self) -> float | None:
        if self.tray is None or not self.tray.colour or not self.filament.colour:
            return None
        return delta_e(self.filament.colour, self.tray.colour)


@dataclass
class PrintPlan:
    project: ProjectInfo
    topology: Topology
    use_ams: bool
    assignments: list[Assignment]
    notes: list[str] = field(default_factory=list)     # informational

    @property
    def errors(self) -> list[str]:
        return [f"filament {a.filament.number}: {e}" for a in self.assignments for e in a.errors]

    @property
    def warnings(self) -> list[str]:
        return [f"filament {a.filament.number}: {w}" for a in self.assignments for w in a.warnings]

    def mapping_arrays(self) -> tuple[list[int], list[dict[str, int]]]:
        """`ams_mapping` (BS v0) and `ams_mapping2` (v1), filament-indexed."""
        width = max(MAPPING_WIDTH, self.project.total_filaments)
        v0 = [-1] * width
        v1 = [{"ams_id": 255, "slot_id": 255} for _ in range(width)]
        for a in self.assignments:
            if a.tray is None:
                continue
            i = a.filament.index
            v0[i] = -1 if a.tray.external else a.tray.tray_id
            v1[i] = {"ams_id": a.tray.ams_id, "slot_id": a.tray.slot_id}
        return v0, v1


def _candidates(f: ProjectFilament, topo: Topology, *, use_ams: bool) -> list[Tray]:
    """Loaded trays that can physically feed filament `f`'s nozzle with a
    compatible material. External spools only when the AMS is off or no AMS
    tray can reach that nozzle (BambuStudio's map_opt rule)."""
    want = _material_class(f.type)

    def fits(t: Tray) -> bool:
        # External spools may report no type; accept them unless they say otherwise.
        return f.nozzle in t.extruders and (
            _material_class(t.type) == want if t.type else t.external)

    ams = [t for t in topo.trays if not t.external and not t.empty and fits(t)]
    ext = [t for t in topo.trays if t.external and fits(t)]
    nozzle_has_ams = any(not t.external and f.nozzle in t.extruders for t in topo.trays)
    if not use_ams or not nozzle_has_ams:
        return ext
    return ams


def _score(f: ProjectFilament, t: Tray, *, aesthetic: bool) -> tuple:
    exact_type = t.type.upper() != f.type.upper()
    profile = not (f.profile_id and f.profile_id == t.profile_id)
    de = round(delta_e(f.colour, t.colour), 3) if f.colour and t.colour else 999.0
    # Profile id only breaks colour ties (as in DevMapping.cpp); type ranks
    # first for functional prints, colour first for aesthetic ones.
    return (de, exact_type, profile) if aesthetic else (exact_type, de, profile)


def auto_assign(project: ProjectInfo, topo: Topology, *, use_ams: bool = True
                ) -> dict[int, Tray | None]:
    """Greedy global assignment (BambuStudio's algorithm, beambam's ordering)."""
    aesthetic = is_aesthetic([(f.type, f.colour) for f in project.used])
    cands = {f.index: _candidates(f, topo, use_ams=use_ams) for f in project.used}
    result: dict[int, Tray | None] = {f.index: None for f in project.used}
    taken: set[int] = set()
    pending = list(project.used)
    while pending:
        best: tuple[tuple, ProjectFilament, Tray] | None = None
        for f in pending:
            for t in cands[f.index]:
                if t.tray_id in taken and not t.external:
                    continue
                s = _score(f, t, aesthetic=aesthetic)
                if best is None or s < best[0]:
                    best = (s, f, t)
        if best is None:
            # Nothing unused fits the rest: reuse the closest already-picked tray.
            for f in pending:
                options = sorted(cands[f.index], key=lambda t: _score(f, t, aesthetic=aesthetic))
                result[f.index] = options[0] if options else None
            break
        _, f, t = best
        result[f.index] = t
        taken.add(t.tray_id)
        pending.remove(f)
    return result


def parse_map(spec: str) -> dict[int, int]:
    """`--map "1=6,5=B3,2=ext-left"` → {filament_index: tray_id}.

    Filament numbers are 1-based (as Bambu Studio shows them). Tray forms:
    a global slot (`6`), the touchscreen label `B3` (AMS B, slot 3),
    `ext` / `ext-right` (255) or `ext-left` (254)."""
    out: dict[int, int] = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "=" not in part:
            raise MappingError(f"--map entry {part!r} is not FILAMENT=TRAY")
        left, right = (s.strip() for s in part.split("=", 1))
        try:
            fidx = int(left) - 1
        except ValueError as e:
            raise MappingError(f"--map: {left!r} is not a filament number") from e
        r = right.lower()
        if r in ("ext", "ext-right", "external"):
            tid = EXT_SPOOL_MAIN
        elif r == "ext-left":
            tid = EXT_SPOOL_DEPUTY
        elif (m := re.fullmatch(r"([a-p])([1-4])", r)):
            tid = (ord(m.group(1)) - ord("a")) * 4 + int(m.group(2)) - 1
        elif r.isdigit():
            tid = int(r)
        else:
            raise MappingError(f"--map: tray {right!r} not understood "
                               f"(use a global slot, a label like B3, ext-left or ext-right)")
        if fidx < 0:
            raise MappingError(f"--map: filament numbers start at 1, got {left}")
        out[fidx] = tid
    return out


def _check(a: Assignment, project: ProjectInfo, topo: Topology) -> None:
    """Fill in hard errors and soft warnings for one assignment."""
    f, t = a.filament, a.tray
    if t is None:
        a.errors.append(
            f"no loaded {f.type or 'compatible'} tray can feed the "
            f"{nozzle_name(f.nozzle, topo.nozzle_count)} nozzle — load one, or pick a tray "
            f"with --map {f.number}=<slot>")
        return
    if t.empty and not t.external:   # external spools don't report presence
        a.errors.append(f"{t.label()} is empty")
        return
    if f.nozzle not in t.extruders:
        if not t.extruders:
            why = "is routed to a Filament Track Switch the printer doesn't report as set up"
        else:
            why = ("only feeds the " + "/".join(nozzle_name(e, topo.nozzle_count)
                                                for e in sorted(t.extruders)) + " nozzle")
        a.errors.append(
            f"sliced for the {nozzle_name(f.nozzle, topo.nozzle_count)} nozzle but "
            f"{t.label()} {why} — re-slice with this filament on that nozzle, or choose "
            f"another tray")
    if f.type and t.type and _material_class(f.type) != _material_class(t.type):
        a.errors.append(f"sliced as {f.type} but {t.label()} holds {t.type} — different "
                        f"material, the gcode's temperatures would be wrong")
        return
    if f.type and t.type and t.type.upper() != f.type.upper():
        a.warnings.append(f"type {f.type} vs loaded {t.type} (same material class)")
    if f.profile_id and t.profile_id and f.profile_id != t.profile_id:
        a.warnings.append(f"profile {f.profile_id} vs loaded {t.profile_id} "
                          f"(different flow / temperature tuning)")
    de = a.colour_delta
    if de is not None and de > COLOUR_DIFFERS_DE:
        a.warnings.append(f"colour {f.colour} vs loaded {t.colour} (ΔE {de:.0f})")


def index_slots(project: ProjectInfo, slot: int | list[int]) -> dict[int, int]:
    """Interpret the historical `--slot` / `ams_slot` value against the plate.

    An int maps the plate's single used filament (an error if it uses
    several). A list is filament-indexed when it contains -1 or has one entry
    per project filament (Bambu Studio's own ams_mapping), otherwise it is
    one tray per USED filament, in filament order. Returns
    {filament_index: tray_id}."""
    used_idx = [f.index for f in project.used]
    if isinstance(slot, int):
        if len(used_idx) > 1:
            raise MappingError(
                f"--slot {slot} names one tray but plate {project.plate} uses "
                f"{len(used_idx)} filaments ({', '.join(str(i + 1) for i in used_idx)}); "
                f"use --map F=TRAY,… or leave it to auto-mapping")
        return {used_idx[0]: slot}
    slots = list(slot)
    if -1 in slots or (len(slots) == project.total_filaments and len(slots) != len(used_idx)):
        return {i: s for i, s in enumerate(slots) if s >= 0}
    if len(slots) == len(used_idx):
        return dict(zip(used_idx, slots))
    raise MappingError(
        f"{len(slots)} slots given but plate {project.plate} uses "
        f"{len(used_idx)} of {project.total_filaments} filaments")


def arrays_from_indexed(chosen: dict[int, int], total_filaments: int,
                        width: int = MAPPING_WIDTH) -> tuple[list[int], list[dict[str, int]]]:
    """Mapping arrays from {filament_index: tray_id} when no live state is at
    hand (simulate / offline callers): tray ids ≥ 128 are AMS HT units and
    254/255 external spools; everything else is ams*4 + slot."""
    width = max(width, total_filaments)
    v0 = [-1] * width
    v1 = [{"ams_id": 255, "slot_id": 255} for _ in range(width)]
    for i, tid in chosen.items():
        if tid in (EXT_SPOOL_MAIN, EXT_SPOOL_DEPUTY):
            v1[i] = {"ams_id": tid, "slot_id": 0}
        elif tid >= 128:
            v0[i], v1[i] = tid, {"ams_id": tid, "slot_id": 0}
        else:
            v0[i], v1[i] = tid, {"ams_id": tid // 4, "slot_id": tid % 4}
    return v0, v1


def plan_print(project: ProjectInfo, state: dict, *, use_ams: bool = True,
               slot: int | list[int] | None = None,
               explicit: dict[int, int] | None = None) -> PrintPlan:
    """Resolve and validate the mapping for one print.

    `slot` keeps the historical `--slot` / `ams_slot` meaning: an int maps the
    plate's single used filament (an error if it uses several); a list is
    either filament-indexed (Bambu Studio style, may contain -1) or one tray
    per used filament in order. `explicit` ({filament_index: tray_id}, from
    --map) overrides per filament; anything not chosen is auto-mapped."""
    topo = read_topology(state)
    notes: list[str] = []
    if project.dynamic_nozzle_map:
        raise MappingError(
            "this file was sliced for the Filament Track Switch's automatic nozzle "
            "mode (enable_filament_dynamic_map); beambam cannot send the printer-"
            "computed nozzle mapping it needs yet. Re-slice with a fixed filament → "
            "nozzle assignment, or start it from Bambu Studio / Handy.")
    if project.has_switcher != topo.switch_installed:
        notes.append(
            "the file was sliced " + ("with" if project.has_switcher else "without")
            + " the Filament Track Switch but the printer "
            + ("has" if topo.switch_installed else "does not have")
            + " one installed — Bambu Studio recommends re-slicing for this printer")
    if topo.switch_installed and not topo.switch_ready:
        notes.append("the Filament Track Switch is installed but not set up for every AMS "
                     "(run its setup on the printer)")

    chosen: dict[int, int] = index_slots(project, slot) if slot is not None else {}
    if explicit:
        unknown = sorted(i + 1 for i in explicit if i not in {f.index for f in project.used})
        if unknown:
            raise MappingError(f"--map names filament(s) {unknown} that plate "
                               f"{project.plate} does not use")
        chosen.update(explicit)

    auto = auto_assign(project, topo, use_ams=use_ams)
    assignments: list[Assignment] = []
    for f in project.used:
        if f.index in chosen:
            tid = chosen[f.index]
            t = topo.tray(tid)
            a = Assignment(f, t)
            if t is None:
                a.errors.append(f"tray {tid} does not exist on this printer")
            else:
                _check(a, project, topo)
        else:
            a = Assignment(f, auto[f.index])
            _check(a, project, topo)
        if not use_ams and a.tray is not None and not a.tray.external:
            a.errors.append(f"{a.tray.label()} is an AMS tray but --no-ams was given")
        assignments.append(a)
    ams_used = any(a.tray is not None and not a.tray.external for a in assignments)
    return PrintPlan(project=project, topology=topo, use_ams=use_ams and ams_used,
                     assignments=assignments, notes=notes)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def format_plan(plan: PrintPlan, *, color: bool = False) -> str:
    """Human table of the plan, for confirmation before a print starts."""
    def sgr(code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if color and s else s

    def swatch(hex_c: str) -> str:
        if not color or not hex_c:
            return ""
        r, g, b = _hex_rgb(hex_c)
        return f"\x1b[48;2;{r};{g};{b}m  \x1b[0m "

    topo, proj = plan.topology, plan.project
    sw = ("not installed" if not topo.switch_installed
          else "installed, set up" if topo.switch_ready else "installed, NOT set up")
    lines = [sgr("1", f"Filament mapping — {proj.path.name}, plate {proj.plate}"),
             f"  printer: {topo.nozzle_count} nozzle(s), Filament Track Switch {sw}", ""]
    for a in plan.assignments:
        f, t = a.filament, a.tray
        left = (f"{f.number:>2}. {swatch(f.colour)}{f.type:<6} {f.colour:<8} "
                f"{f.profile_id or '-':<6} {nozzle_name(f.nozzle, topo.nozzle_count):>5}")
        if t is None:
            right = sgr("1;31", "(no tray)")
        else:
            route = f" via switch {t.switch_input}" if t.switch_input else ""
            right = (f"{t.label():<15} {swatch(t.colour)}{t.type:<6} {t.colour:<8} "
                     f"{t.name or t.profile_id or ''}{route}")
        mark = (sgr("1;31", "✗") if a.errors else sgr("1;33", "!") if a.warnings
                else sgr("1;32", "✓"))
        lines.append(f"  {mark} {left}  →  {right}")
        for e in a.errors:
            lines.append("      " + sgr("31", "error: " + e))
        for w in a.warnings:
            lines.append("      " + sgr("33", "warning: " + w))
    for n in plan.notes:
        lines.append("  " + sgr("33", "note: " + n))
    return "\n".join(lines) + "\n"


def plan_to_json(plan: PrintPlan) -> dict[str, Any]:
    v0, v1 = plan.mapping_arrays()
    return {
        "file": str(plan.project.path), "plate": plan.project.plate,
        "use_ams": plan.use_ams,
        "switch": {"installed": plan.topology.switch_installed,
                   "ready": plan.topology.switch_ready},
        "filaments": [{
            "filament": a.filament.number, "type": a.filament.type,
            "colour": a.filament.colour, "profile_id": a.filament.profile_id,
            "nozzle": nozzle_name(a.filament.nozzle, plan.topology.nozzle_count),
            "tray": None if a.tray is None else {
                "tray_id": a.tray.tray_id, "label": a.tray.label(), "type": a.tray.type,
                "colour": a.tray.colour, "profile_id": a.tray.profile_id,
                "switch_input": a.tray.switch_input},
            "errors": a.errors, "warnings": a.warnings,
        } for a in plan.assignments],
        "notes": plan.notes,
        "ams_mapping": v0, "ams_mapping2": v1,
    }
