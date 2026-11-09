"""beambam.analyze — dissect a .gcode.3mf and report what's actually
going to happen on the printer: filament/nozzle assignment, per-phase
toolchanges, real flush volume, AMS-tray-color requirements, and
optimization hints.

Used by the `beambam analyze` CLI subcommand and importable as a
library:

    from beambam.analyze import analyze_3mf, format_report
    report = analyze_3mf(Path("model.gcode.3mf"))
    print(format_report(report))                # human summary
    json.dumps(report, indent=2)                # machine output

This is the formalised version of the manual investigation the
bridge maintainer ran on the eevee 3-color print (2026-05). The
key insight: even though the slicer picks a flush-volume-optimal
nozzle assignment, the user-visible cost of a tri-color middle
section against 2 nozzles is unavoidable and worth surfacing
explicitly before the print starts."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Filament:
    id: int
    type: str
    tray_idx: str
    color: str
    used_m: float
    used_g: float
    group_id: str
    nozzle_diameter: float
    used_for_object: bool
    used_for_support: bool
    # Computed
    nozzle: int | None = None   # 0 or 1; None if dynamic-only
    dynamic: bool = False       # group_id contains both nozzles


@dataclass
class Nozzle:
    id: int
    extruder_id: int
    nozzle_diameter: float


@dataclass
class Phase:
    """A contiguous layer range where the same set of filaments is in use."""
    layer_start: int
    layer_end: int
    filaments: list[int]               # filament IDs (1-based per slice_info)
    real_flushes: int = 0              # filament changes that purge (same nozzle, new filament)
    # Purge VOLUME in mm³ (legacy name: Bambu's flush matrix is mm³, not mm).
    flush_volume_mm: float = 0.0
    flush_length_mm: float = 0.0       # the same purge as filament length (1.75 mm)
    flush_g: float = 0.0               # the same purge in grams (incoming filament's density)
    filament_changes: int = 0          # every filament load in this phase (purging or not)
    note: str = ""


@dataclass
class Report:
    file: dict[str, Any]
    slicer: dict[str, Any]
    printer: dict[str, Any]
    plate: dict[str, Any]
    objects: list[dict[str, Any]]
    filaments: list[Filament]
    nozzles: list[Nozzle]
    flush_matrix_mm: list[list[list[float]]] = field(default_factory=list)
    flush_multiplier: list[float] = field(default_factory=list)
    sequence: list[int] = field(default_factory=list)
    nozzle_sequence: list[int] = field(default_factory=list)
    optimal_assignment: list[int] = field(default_factory=list)
    phases: list[Phase] = field(default_factory=list)
    totals: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


def _parse_layer_ranges(s: str) -> list[tuple[int, int]]:
    """Parse `"105 136,68 90"` → [(105,136), (68,90)]."""
    out = []
    for part in s.split(","):
        nums = part.strip().split()
        if len(nums) == 2:
            out.append((int(nums[0]), int(nums[1])))
    return out


def _parse_slice_info(xml_bytes: bytes) -> dict[str, Any]:
    """Parse Metadata/slice_info.config into a structured dict."""
    root = ET.fromstring(xml_bytes)
    header = {
        item.get("key"): item.get("value")
        for item in root.findall(".//header/header_item")
    }
    plate = root.find("plate")
    if plate is None:
        raise ValueError("slice_info.config missing <plate> element")

    plate_meta = {
        m.get("key"): m.get("value")
        for m in plate.findall("metadata")
    }

    objects = [
        {"id": int(o.get("identify_id", "0")),
         "name": o.get("name", ""),
         "skipped": (o.get("skipped") or "false").lower() == "true"}
        for o in plate.findall("object")
    ]

    filaments = []
    for f in plate.findall("filament"):
        group_id = f.get("group_id", "")
        filaments.append(Filament(
            id=int(f.get("id", "0")),
            type=f.get("type", ""),
            tray_idx=f.get("tray_info_idx", ""),
            color=f.get("color", ""),
            used_m=float(f.get("used_m", "0")),
            used_g=float(f.get("used_g", "0")),
            group_id=group_id,
            nozzle_diameter=float(f.get("nozzle_diameter", "0.4")),
            used_for_object=(f.get("used_for_object") or "false").lower() == "true",
            used_for_support=(f.get("used_for_support") or "false").lower() == "true",
            dynamic=("," in group_id),
            nozzle=(int(group_id) if group_id.isdigit() else None),
        ))

    nozzles = [
        Nozzle(
            id=int(n.get("id", "0")),
            extruder_id=int(n.get("extruder_id", "0")),
            nozzle_diameter=float(n.get("nozzle_diameter", "0.4")),
        )
        for n in plate.findall("nozzle")
    ]

    layer_filament = []
    for lst in plate.findall("layer_filament_lists/layer_filament_list"):
        fil_str = lst.get("filament_list", "")
        rng_str = lst.get("layer_ranges", "")
        filament_ids_in_layer = [int(x) for x in fil_str.split() if x]
        for start, end in _parse_layer_ranges(rng_str):
            layer_filament.append((start, end, filament_ids_in_layer))

    warnings = [
        {"msg": w.get("msg"),
         "level": int(w.get("level", "0")),
         "error_code": w.get("error_code", "").strip()}
        for w in plate.findall("warning")
    ]

    return {
        "header": header,
        "plate_meta": plate_meta,
        "objects": objects,
        "filaments": filaments,
        "nozzles": nozzles,
        "layer_filament_lists": layer_filament,
        "warnings": warnings,
    }


def _parse_project_settings(json_bytes: bytes) -> dict[str, Any]:
    """Pull the flush matrix + bed config out of project_settings.config.

    project_settings.config is a flat JSON dict with strings everywhere
    (Bambu's slicer quirk — even numbers are quoted)."""
    d = json.loads(json_bytes)

    def _as_floats(key: str) -> list[float]:
        v = d.get(key, [])
        if not isinstance(v, list):
            return []
        return [float(x) for x in v]

    flush_vec = _as_floats("flush_volumes_vector")
    flush_matrix_flat = _as_floats("flush_volumes_matrix")
    flush_mult = _as_floats("flush_multiplier")

    # Reshape flush matrix: it's nozzle_count * filament_count^2 long.
    # For 3 filaments and 2 nozzles → 18 entries = 2 × 9.
    # Each 9-entry chunk is row-major (from-filament rows, to-filament cols).
    filaments = d.get("filament_colour", [])
    n_fil = len(filaments)
    nozzles_used: list[list[list[float]]] = []
    if n_fil and len(flush_matrix_flat) % (n_fil * n_fil) == 0:
        n_noz = len(flush_matrix_flat) // (n_fil * n_fil)
        for nz in range(n_noz):
            mat = []
            base = nz * n_fil * n_fil
            for r in range(n_fil):
                row = flush_matrix_flat[base + r * n_fil : base + (r + 1) * n_fil]
                mat.append(row)
            nozzles_used.append(mat)

    return {
        "filament_colour": filaments,
        "filament_type": d.get("filament_type", []),
        "filament_settings_id": d.get("filament_settings_id", []),
        "flush_volumes_vector": flush_vec,
        "flush_volumes_matrix": nozzles_used,
        "flush_multiplier": flush_mult,
        "bed_type_default": d.get("bed_type", ""),
        "prime_tower_width": d.get("prime_tower_width"),
    }


def _parse_plate_json(json_bytes: bytes) -> dict[str, Any]:
    d = json.loads(json_bytes)
    return {
        "bed_type": d.get("bed_type", ""),
        "bbox_all": d.get("bbox_all", []),
        "first_extruder": d.get("first_extruder", 0),
        "filament_colors": d.get("filament_colors", []),
        "filament_ids": d.get("filament_ids", []),
        "nozzle_diameter": d.get("nozzle_diameter"),
        "is_seq_print": d.get("is_seq_print", False),
    }


def _parse_filament_sequence(json_bytes: bytes, plate_idx: int = 1) -> dict[str, Any]:
    d = json.loads(json_bytes)
    plate = d.get(f"plate_{plate_idx}", {})
    return {
        # Bambu uses two keys interchangeably across versions
        "sequence": plate.get("filament_sequence") or plate.get("sequence") or [],
        "nozzle_sequence": plate.get("nozzle_sequence", []),
        "optimal_assignment": plate.get("optimal_assignment", []),
    }


def _count_gcode_toolchanges(gcode_bytes: bytes) -> dict[str, Any]:
    """Count T0..T9 toolchange directives + M620 flush cycles in the actual
    gcode. Doesn't parse — just regexes line starts.

    Bambu firmware also uses Tnnn with high numbers (T65279, T65535) as
    UNLOAD sentinels in start/end gcode — those are NOT printing extruder
    selects. We restrict to single-digit T0-T9 to match real printer
    nozzles (no consumer printer has 10+ extruders)."""
    text = gcode_bytes.decode("utf-8", errors="replace")
    t_counts: dict[str, int] = {}
    for m in re.finditer(r"(?m)^(T\d)(?:$|[ \t])", text):
        tag = m.group(1)
        t_counts[tag] = t_counts.get(tag, 0) + 1
    # Count unload sentinels separately so they're visible in the report
    unload_count = len(re.findall(r"(?m)^T(?:6[0-9]{4}|[1-9][0-9]{2,})(?:$|[ \t])", text))
    return {
        "tool_calls": t_counts,
        "tool_calls_total": sum(t_counts.values()),
        "unload_sentinels": unload_count,
        "m620_cycles": len(re.findall(r"(?m)^M620 ", text)),
        "m620_flushes": len(re.findall(r"(?m)^M620\.10", text)),
    }


# Cross-section of 1.75 mm filament (mm²): Bambu's own change_filament_gcode
# converts volume to length with this constant (`/2.4053`).
FILAMENT_AREA_MM2 = 2.4053
DEFAULT_DENSITY_G_CM3 = 1.24   # PLA; used only when the G-code header has none


@dataclass
class _GcodeChange:
    """One filament load in the printed G-code (not the initial load)."""
    layer: int            # 0-based layer, matching slice_info layer_ranges
    filament: int         # 1-based filament id
    flush_length_mm: float


def _parse_gcode_events(gcode_bytes: bytes) -> dict[str, Any]:
    """Read what the printer will actually do from the plate G-code.

    Bambu Studio marks every layer with `M73 L<n>` (1-based) and every
    filament change with `M620 S<k>A` (k 0-based; 255+ = unload sentinels)
    followed by `M620.10 A1 ... L<len>`, whose L is the purge length in mm
    of filament the firmware extrudes for that change (0 when the other
    nozzle takes over instead of purging). Loads before the first layer
    marker belong to the start G-code's initial load and are not changes.
    """
    text = gcode_bytes.decode("utf-8", errors="replace")
    header_layers = re.search(r"(?m)^; total layer number: (\d+)", text)
    densities_m = re.search(r"(?m)^; filament_density: ([\d.,\s]+)$", text)
    densities = ([float(x) for x in densities_m.group(1).split(",") if x.strip()]
                 if densities_m else [])
    changes: list[_GcodeChange] = []
    layer = -1
    pending: _GcodeChange | None = None
    for line in text.splitlines():
        if line.startswith("M73 L"):
            m = re.match(r"M73 L(\d+)", line)
            if m:
                layer = int(m.group(1)) - 1
        elif line.startswith("M620 S"):
            m = re.match(r"M620 S(\d+)A", line)
            if m and layer >= 0 and int(m.group(1)) < 255:
                pending = _GcodeChange(layer=layer, filament=int(m.group(1)) + 1,
                                       flush_length_mm=0.0)
                changes.append(pending)
        elif line.startswith("M620.10 A1") and pending is not None:
            m = re.search(r" L([\d.]+)", line)
            if m:
                pending.flush_length_mm = float(m.group(1))
            pending = None
    return {
        "layer_count": int(header_layers.group(1)) if header_layers else None,
        "densities": densities,
        "changes": changes,
    }


def _density(densities: list[float], filament: int) -> float:
    """g/cm³ of a 1-based filament id (header value, else PLA)."""
    return densities[filament - 1] if 0 < filament <= len(densities) else DEFAULT_DENSITY_G_CM3


def _change_grams(c: _GcodeChange, densities: list[float]) -> float:
    return c.flush_length_mm * FILAMENT_AREA_MM2 * _density(densities, c.filament) / 1000.0


def _phase_note(filaments: list[int], flushes: int) -> str:
    n_fil = len(filaments)
    if n_fil <= 1:
        return f"single-color (filament {filaments[0] + 1 if filaments else '?'})"
    if n_fil == 2:
        return "2-color (pure parallel, no flush)" if flushes == 0 else "2-color with flushes"
    return f"{n_fil}-color (1 nozzle MUST be shared → flushes unavoidable)"


def _phases_from_gcode(layer_filament: list[tuple[int, int, list[int]]],
                       changes: list[_GcodeChange],
                       densities: list[float]) -> tuple[list[Phase], int]:
    """Exact phases: every change is attributed to the layer it happens in."""
    phases: list[Phase] = []
    flushes_total = 0
    for start, end, filaments in sorted(layer_filament, key=lambda x: x[0]):
        here = [c for c in changes if start <= c.layer <= end]
        purging = [c for c in here if c.flush_length_mm > 0]
        length = sum(c.flush_length_mm for c in purging)
        flushes_total += len(purging)
        phases.append(Phase(
            layer_start=start, layer_end=end,
            filaments=[f + 1 for f in filaments],   # 0-based -> 1-based ids
            real_flushes=len(purging),
            flush_volume_mm=length * FILAMENT_AREA_MM2,
            flush_length_mm=length,
            flush_g=sum(_change_grams(c, densities) for c in purging),
            filament_changes=len(here),
            note=_phase_note(filaments, len(purging)),
        ))
    return phases, flushes_total


def _compute_phases(layer_filament: list[tuple[int, int, list[int]]],
                    sequence: list[int],
                    nozzle_sequence: list[int]) -> tuple[list[Phase], int]:
    """ESTIMATE, used only when the 3MF has no plate G-code: map each layer
    range onto filament_sequence proportionally (the sequence carries no
    layer numbers) and count swaps (same nozzle, different filament than
    its previous one). With uneven swap density per layer the swaps land
    in the wrong phase; `_phases_from_gcode` is exact."""
    sorted_ranges = sorted(layer_filament, key=lambda x: x[0])
    if not sorted_ranges:
        return [], 0

    total_layers = max(end for _, end, _ in sorted_ranges) + 1
    seq_len = len(sequence)
    swaps_total = 0
    phases: list[Phase] = []

    for start, end, filaments in sorted_ranges:
        if total_layers > 0 and seq_len > 0:
            seq_start = (start * seq_len) // total_layers
            seq_end = ((end + 1) * seq_len) // total_layers
        else:
            seq_start = seq_end = 0

        seen_on_nozzle: dict[int, int] = {}
        phase_swaps = 0
        for i in range(seq_start, min(seq_end, seq_len)):
            f = sequence[i]
            n = nozzle_sequence[i] if i < len(nozzle_sequence) else 0
            prev = seen_on_nozzle.get(n)
            if prev is not None and prev != f:
                phase_swaps += 1
            seen_on_nozzle[n] = f
        swaps_total += phase_swaps

        phases.append(Phase(
            layer_start=start,
            layer_end=end,
            filaments=[f + 1 for f in filaments],   # convert 0-based to 1-based filament IDs
            real_flushes=phase_swaps,
            note=_phase_note(filaments, phase_swaps),
        ))

    return phases, swaps_total


def _compute_flush_volumes(phases: list[Phase],
                           sequence: list[int],
                           nozzle_sequence: list[int],
                           flush_matrix: list[list[list[float]]],
                           flush_multiplier: list[float],
                           densities: list[float] | None = None) -> None:
    """ESTIMATE (no G-code): sum the flush matrix (mm³) for every swap in
    each phase's proportional share of the sequence. Mutates phases."""
    if not flush_matrix or not sequence:
        return

    n_fil = len(flush_matrix[0]) if flush_matrix else 0
    total_layers = max((p.layer_end for p in phases), default=0) + 1
    seq_len = len(sequence)
    dens = densities or []

    for phase in phases:
        if total_layers <= 0 or seq_len <= 0:
            continue
        seq_start = (phase.layer_start * seq_len) // total_layers
        seq_end = ((phase.layer_end + 1) * seq_len) // total_layers
        seen_on_nozzle: dict[int, int] = {}
        vol = 0.0
        grams = 0.0
        for i in range(seq_start, min(seq_end, seq_len)):
            f = sequence[i]                                  # 1-based filament id
            n = nozzle_sequence[i] if i < len(nozzle_sequence) else 0
            prev = seen_on_nozzle.get(n)
            if prev is not None and prev != f and n < len(flush_matrix):
                fi_from = prev - 1                           # → 0-based for matrix
                fi_to = f - 1
                if 0 <= fi_from < n_fil and 0 <= fi_to < n_fil:
                    mult = flush_multiplier[n] if n < len(flush_multiplier) else 1.0
                    v = flush_matrix[n][fi_from][fi_to] * mult
                    vol += v
                    grams += v * _density(dens, f) / 1000.0
            seen_on_nozzle[n] = f
        phase.flush_volume_mm = vol
        phase.flush_length_mm = vol / FILAMENT_AREA_MM2
        phase.flush_g = grams


def _generate_hints(report: Report) -> list[str]:
    """Produce human-readable suggestions based on the analysis."""
    hints: list[str] = []
    tri_color = [p for p in report.phases if len(p.filaments) >= 3]
    if tri_color and len(report.nozzles) <= 2:
        worst = max(tri_color, key=lambda p: p.flush_volume_mm)
        layers = worst.layer_end - worst.layer_start + 1
        hints.append(
            f"Tri-color section spans layers {worst.layer_start}-{worst.layer_end} "
            f"({layers} layers, {worst.real_flushes} flushes, "
            f"≈{worst.flush_volume_mm:.0f} mm³ = ≈{worst.flush_g:.1f} g purge). "
            f"With only {len(report.nozzles)} nozzles, one color must share a nozzle. "
            f"Re-staging the model so the shared color appears only in non-tri-color "
            f"layers eliminates this entirely."
        )

    if report.plate.get("predicted_seconds"):
        mins = int(report.plate["predicted_seconds"]) // 60
        report.plate["predicted_human"] = f"{mins // 60}h {mins % 60}min" if mins >= 60 else f"{mins} min"

    if report.totals.get("flush_volume_g", 0) > 6.0:
        hints.append(
            f"Total purge volume ≈{report.totals['flush_volume_mm']:.0f} mm³ "
            f"(~{report.totals.get('flush_volume_g', 0):.1f} g) — non-trivial cost. "
            f"Check that filament_map_mode is 'Auto For Flush' (slicer picks the "
            f"minimum-flush assignment) rather than a manual override."
        )

    return hints


def analyze_3mf(path: Path) -> Report:
    """Parse a .gcode.3mf and return a structured Report.

    Raises FileNotFoundError / zipfile.BadZipFile / ValueError on malformed
    input. All XML / JSON parsing errors propagate so callers can present
    the file as broken rather than half-analyzed."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)

    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()

    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        si = _parse_slice_info(z.read("Metadata/slice_info.config"))
        ps = (_parse_project_settings(z.read("Metadata/project_settings.config"))
              if "Metadata/project_settings.config" in names else {})
        plate_idx = int(si["plate_meta"].get("index", "1"))
        pl = (_parse_plate_json(z.read(f"Metadata/plate_{plate_idx}.json"))
              if f"Metadata/plate_{plate_idx}.json" in names else {})
        fs = (_parse_filament_sequence(z.read("Metadata/filament_sequence.json"),
                                       plate_idx=plate_idx)
              if "Metadata/filament_sequence.json" in names else {})
        gcode_name = f"Metadata/plate_{plate_idx}.gcode"
        gcode_raw = z.read(gcode_name) if gcode_name in names else b""
        gc = _count_gcode_toolchanges(gcode_raw) if gcode_raw else {}
        ev = _parse_gcode_events(gcode_raw) if gcode_raw else {}

    nozzle_diameters_raw = si["plate_meta"].get("nozzle_diameters", "")
    nozzle_diameters = [float(x) for x in nozzle_diameters_raw.split(",") if x]

    # Try to extract nozzle assignment from filament_sequence's optimal_assignment
    # (one entry per filament: which nozzle the slicer prefers).
    for i, fil in enumerate(si["filaments"]):
        if i < len(fs.get("optimal_assignment", [])):
            fil.nozzle = fs["optimal_assignment"][i]
        # dynamic flag already set in _parse_slice_info from group_id

    plate_info = {
        "index": plate_idx,
        "bed_type": pl.get("bed_type") or ps.get("bed_type_default", ""),
        "weight_g": float(si["plate_meta"].get("weight", "0")),
        "predicted_seconds": int(float(si["plate_meta"].get("prediction", "0"))),
        "layer_count": (int(si["plate_meta"].get("total_layer_num", "0"))
                        or ev.get("layer_count")),
        "filament_dynamic_map": (si["plate_meta"].get("enable_filament_dynamic_map", "")
                                  .lower() == "true"),
        "has_filament_switcher": (si["plate_meta"].get("has_filament_switcher", "")
                                   .lower() == "true"),
        "filament_maps": si["plate_meta"].get("filament_maps", ""),
        "prime_tower_width": ps.get("prime_tower_width"),
    }

    report = Report(
        file={"path": str(path), "size": len(raw), "sha256": sha},
        slicer={"version": si["header"].get("X-BBL-Client-Version"),
                "client": si["header"].get("X-BBL-Client-Type")},
        printer={"model_id": si["plate_meta"].get("printer_model_id"),
                 "nozzle_count": len(si["nozzles"]),
                 "nozzle_diameters": nozzle_diameters},
        plate=plate_info,
        objects=si["objects"],
        filaments=si["filaments"],
        nozzles=si["nozzles"],
        flush_matrix_mm=ps.get("flush_volumes_matrix", []),
        flush_multiplier=ps.get("flush_multiplier", []),
        sequence=fs.get("sequence", []),
        nozzle_sequence=fs.get("nozzle_sequence", []),
        optimal_assignment=fs.get("optimal_assignment", []),
        warnings=si["warnings"],
    )

    densities: list[float] = ev.get("densities") or []
    changes: list[_GcodeChange] = ev.get("changes") or []
    if changes:
        # Exact: what the G-code does, layer by layer.
        phases, swaps_total = _phases_from_gcode(si["layer_filament_lists"], changes, densities)
        flush_source = "gcode"
        flush_length = sum(c.flush_length_mm for c in changes)
        flush_g = sum(_change_grams(c, densities) for c in changes)
        n_changes = len(changes)
    else:
        phases, swaps_total = _compute_phases(
            si["layer_filament_lists"], report.sequence, report.nozzle_sequence)
        _compute_flush_volumes(phases, report.sequence, report.nozzle_sequence,
                               report.flush_matrix_mm, report.flush_multiplier, densities)
        flush_source = "estimate"
        flush_length = sum(p.flush_length_mm for p in phases)
        flush_g = sum(p.flush_g for p in phases)
        n_changes = max(len(report.sequence) - 1, 0)
    report.phases = phases

    report.totals = {
        "sequence_length": len(report.sequence),
        # Filament changes that purge (same nozzle, different filament); a
        # change the other nozzle takes over costs no purge.
        "nozzle_swaps": swaps_total,
        "filament_changes": n_changes,
        "tool_calls": gc.get("tool_calls", {}),
        "tool_calls_total": gc.get("tool_calls_total", 0),
        "unload_sentinels": gc.get("unload_sentinels", 0),
        "m620_cycles": gc.get("m620_cycles", 0),
        "m620_flushes": gc.get("m620_flushes", 0),
        # Purge in three units of one quantity. `flush_volume_mm` keeps its
        # historical key but is mm³ (it always summed Bambu's mm³ matrix).
        "flush_volume_mm": flush_length * FILAMENT_AREA_MM2,
        "flush_volume_mm3": flush_length * FILAMENT_AREA_MM2,
        "flush_length_mm": flush_length,
        "flush_volume_g": flush_g,
        # "gcode" = read from the plate G-code (exact); "estimate" = flush
        # matrix times a proportional guess of where the swaps fall.
        "flush_source": flush_source,
    }

    report.hints = _generate_hints(report)
    return report


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _h(s: str) -> str:
    return f"\n{s}\n{'─' * len(s)}"


def format_report(r: Report) -> str:
    """Render a Report as a human-readable terminal summary."""
    lines: list[str] = []
    lines.append(_h(f"beambam analyze — {r.file['path']}"))
    lines.append(f"size: {r.file['size']:,} B   sha256: {r.file['sha256'][:16]}…")
    lines.append(f"slicer: {r.slicer['client']} v{r.slicer['version']}")
    lines.append(f"printer: model_id={r.printer['model_id']}  "
                 f"nozzles={r.printer['nozzle_count']} × {r.printer['nozzle_diameters']}mm")

    lines.append(_h("Plate"))
    p = r.plate
    pred = p.get("predicted_seconds", 0)
    pred_human = f"{pred // 3600}h {(pred % 3600) // 60}min" if pred >= 3600 else f"{pred // 60}min"
    lines.append(f"  bed: {p.get('bed_type')}   weight: {p.get('weight_g')} g   "
                 f"predicted: {pred_human} ({pred}s)")
    lines.append(f"  layers: {p.get('layer_count') or 'unknown'}   "
                 f"prime tower: {p.get('prime_tower_width')}mm   "
                 f"dynamic_map: {p.get('filament_dynamic_map')}")
    if r.objects:
        lines.append(f"  objects ({len(r.objects)}):")
        for o in r.objects:
            tag = " (skipped)" if o["skipped"] else ""
            lines.append(f"    • id={o['id']:<4} {o['name']}{tag}")

    lines.append(_h(f"Filaments ({len(r.filaments)})"))
    for f in r.filaments:
        nz = ("dynamic" if f.dynamic else f"nozzle {f.nozzle}") if f.nozzle is not None else "—"
        lines.append(f"  • #{f.id} {f.type:<6} {f.color}  tray={f.tray_idx}  "
                     f"{f.used_m:.2f}m / {f.used_g:.2f}g  → {nz}")

    lines.append(_h(f"Phases ({len(r.phases)})"))
    for ph in r.phases:
        layers = ph.layer_end - ph.layer_start + 1
        fil_str = ",".join(str(f) for f in ph.filaments)
        lines.append(f"  layers {ph.layer_start:>3}-{ph.layer_end:<3} ({layers:>3}) "
                     f"filaments=[{fil_str}]  flushes={ph.real_flushes:>3}  "
                     f"purge={ph.flush_g:>5.1f} g   {ph.note}")

    lines.append(_h("Totals"))
    t = r.totals
    tc = t.get("tool_calls", {})
    tc_str = " ".join(f"{k}={v}" for k, v in sorted(tc.items()))
    lines.append(f"  filament changes: {t.get('filament_changes')}  "
                 f"purging: {t.get('nozzle_swaps')}  "
                 f"(sequence: {t.get('sequence_length')} entries)")
    lines.append(f"  gcode: {tc_str or '—'}   "
                 f"M620 cycles: {t.get('m620_cycles')}   "
                 f"M620.10 flushes: {t.get('m620_flushes')}")
    lines.append(f"  purge: {t.get('flush_length_mm', 0):.0f} mm of filament "
                 f"= {t.get('flush_volume_mm3', 0):.0f} mm³ "
                 f"≈ {t.get('flush_volume_g', 0):.2f} g  ({t.get('flush_source')})")

    if r.warnings:
        lines.append(_h("Slicer warnings"))
        for w in r.warnings:
            lines.append(f"  ⚠ level={w['level']} {w['msg']}  ({w['error_code']})")

    if r.hints:
        lines.append(_h("Hints"))
        for h in r.hints:
            lines.append(f"  → {h}")

    return "\n".join(lines)


def report_to_dict(r: Report) -> dict[str, Any]:
    """Convert a Report (with dataclasses) into a JSON-serializable dict."""
    return {
        "file": r.file,
        "slicer": r.slicer,
        "printer": r.printer,
        "plate": r.plate,
        "objects": r.objects,
        "filaments": [f.__dict__ for f in r.filaments],
        "nozzles": [n.__dict__ for n in r.nozzles],
        "flush_matrix_mm": r.flush_matrix_mm,
        "flush_multiplier": r.flush_multiplier,
        "sequence": r.sequence,
        "nozzle_sequence": r.nozzle_sequence,
        "optimal_assignment": r.optimal_assignment,
        "phases": [p.__dict__ for p in r.phases],
        "totals": r.totals,
        "warnings": r.warnings,
        "hints": r.hints,
    }


# ---------------------------------------------------------------------------
# CLI entry point — invoked from beambam analyze
# ---------------------------------------------------------------------------


def cli_main(file: str, json_out: bool = False) -> int:
    """Argparse glue. Returns exit code."""
    try:
        report = analyze_3mf(Path(file))
    except FileNotFoundError as e:
        print(f"error: file not found: {e}", flush=True)
        return 2
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as e:
        print(f"error: malformed 3mf: {e}", flush=True)
        return 2

    if json_out:
        print(json.dumps(report_to_dict(report), indent=2, default=str))
    else:
        print(format_report(report))
    return 0
