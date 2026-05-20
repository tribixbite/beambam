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
    real_flushes: int = 0              # actual nozzle-swap events
    flush_volume_mm: float = 0.0       # cumulative purge length
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


def _count_gcode_toolchanges(gcode_bytes: bytes) -> dict[str, int]:
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


def _compute_phases(layer_filament: list[tuple[int, int, list[int]]],
                    sequence: list[int],
                    nozzle_sequence: list[int]) -> tuple[list[Phase], int]:
    """Walk layer_filament_lists in layer order and produce contiguous phases.
    For each phase, look at the slice of the sequence/nozzle_sequence that
    falls within it (heuristic: proportional split since the 3mf doesn't
    record explicit phase→sequence mapping) and count nozzle-swap events
    (same nozzle used by two different filaments in a row)."""
    # Sort layer ranges by start layer.
    sorted_ranges = sorted(layer_filament, key=lambda x: x[0])
    if not sorted_ranges:
        return [], 0

    total_layers = max(end for _, end, _ in sorted_ranges) + 1
    seq_len = len(sequence)
    swaps_total = 0
    phases: list[Phase] = []

    for start, end, filaments in sorted_ranges:
        # Map this layer range onto the sequence proportionally.
        if total_layers > 0 and seq_len > 0:
            seq_start = (start * seq_len) // total_layers
            seq_end = ((end + 1) * seq_len) // total_layers
        else:
            seq_start = seq_end = 0

        # Count swaps within this slice: same nozzle, different filament
        # vs the previous step on that nozzle.
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

        note = ""
        n_fil = len(filaments)
        if n_fil <= 1:
            note = f"single-color (filament {filaments[0] + 1 if filaments else '?'})"
        elif n_fil == 2:
            note = "2-color (pure parallel, no flush)" if phase_swaps == 0 else "2-color with flushes"
        elif n_fil >= 3:
            note = f"{n_fil}-color (1 nozzle MUST be shared → flushes unavoidable)"

        phases.append(Phase(
            layer_start=start,
            layer_end=end,
            filaments=[f + 1 for f in filaments],   # convert 0-based to 1-based filament IDs
            real_flushes=phase_swaps,
            note=note,
        ))

    return phases, swaps_total


def _compute_flush_volumes(phases: list[Phase],
                           sequence: list[int],
                           nozzle_sequence: list[int],
                           flush_matrix: list[list[list[float]]],
                           flush_multiplier: list[float]) -> None:
    """For each phase, sum the flush_matrix volume for every nozzle-swap
    event. Mutates phases in place."""
    if not flush_matrix or not sequence:
        return

    n_fil = len(flush_matrix[0]) if flush_matrix else 0
    total_layers = max((p.layer_end for p in phases), default=0) + 1
    seq_len = len(sequence)

    for phase in phases:
        if total_layers <= 0 or seq_len <= 0:
            continue
        seq_start = (phase.layer_start * seq_len) // total_layers
        seq_end = ((phase.layer_end + 1) * seq_len) // total_layers
        seen_on_nozzle: dict[int, int] = {}
        vol = 0.0
        for i in range(seq_start, min(seq_end, seq_len)):
            f = sequence[i]                                  # 1-based filament id
            n = nozzle_sequence[i] if i < len(nozzle_sequence) else 0
            prev = seen_on_nozzle.get(n)
            if prev is not None and prev != f and n < len(flush_matrix):
                fi_from = prev - 1                           # → 0-based for matrix
                fi_to = f - 1
                if 0 <= fi_from < n_fil and 0 <= fi_to < n_fil:
                    mult = flush_multiplier[n] if n < len(flush_multiplier) else 1.0
                    vol += flush_matrix[n][fi_from][fi_to] * mult
            seen_on_nozzle[n] = f
        phase.flush_volume_mm = vol


def _generate_hints(report: Report) -> list[str]:
    """Produce human-readable suggestions based on the analysis."""
    hints: list[str] = []
    tri_color = [p for p in report.phases if len(p.filaments) >= 3]
    if tri_color and len(report.nozzles) <= 2:
        worst = max(tri_color, key=lambda p: p.flush_volume_mm)
        layers = worst.layer_end - worst.layer_start + 1
        # ~0.0024 g/mm for 1.75mm filament (rough density estimate)
        waste_g = worst.flush_volume_mm * 1.75 ** 2 * 3.14159 / 4 * 1.24 / 1000
        hints.append(
            f"Tri-color section spans layers {worst.layer_start}-{worst.layer_end} "
            f"({layers} layers, {worst.real_flushes} flushes, "
            f"≈{worst.flush_volume_mm:.0f} mm = ≈{waste_g:.1f} g purge). "
            f"With only {len(report.nozzles)} nozzles, one color must share a nozzle. "
            f"Re-staging the model so the shared color appears only in non-tri-color "
            f"layers eliminates this entirely."
        )

    if report.plate.get("predicted_seconds"):
        mins = int(report.plate["predicted_seconds"]) // 60
        report.plate["predicted_human"] = f"{mins // 60}h {mins % 60}min" if mins >= 60 else f"{mins} min"

    if report.totals.get("flush_volume_mm", 0) > 5000:
        hints.append(
            f"Total purge volume ≈{report.totals['flush_volume_mm']:.0f} mm "
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
        gc = (_count_gcode_toolchanges(z.read(gcode_name))
              if gcode_name in names else {})

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
        "layer_count": int(si["plate_meta"].get("total_layer_num", "0")) or None,
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

    phases, swaps_total = _compute_phases(
        si["layer_filament_lists"], report.sequence, report.nozzle_sequence)
    _compute_flush_volumes(phases, report.sequence, report.nozzle_sequence,
                           report.flush_matrix_mm, report.flush_multiplier)
    report.phases = phases

    total_flush_mm = sum(p.flush_volume_mm for p in phases)
    # 1.75mm PLA ≈ 1.24 g/cc; volume = π·(0.875)² · length_mm = ~2.405 mm³/mm
    total_flush_g = total_flush_mm * 2.405 * 0.00124

    report.totals = {
        "sequence_length": len(report.sequence),
        "nozzle_swaps": swaps_total,
        "tool_calls": gc.get("tool_calls", {}),
        "tool_calls_total": gc.get("tool_calls_total", 0),
        "unload_sentinels": gc.get("unload_sentinels", 0),
        "m620_cycles": gc.get("m620_cycles", 0),
        "m620_flushes": gc.get("m620_flushes", 0),
        "flush_volume_mm": total_flush_mm,
        "flush_volume_g": total_flush_g,
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
                     f"purge={ph.flush_volume_mm:>6.0f}mm   {ph.note}")

    lines.append(_h("Totals"))
    t = r.totals
    tc = t.get("tool_calls", {})
    tc_str = " ".join(f"{k}={v}" for k, v in sorted(tc.items()))
    lines.append(f"  sequence: {t.get('sequence_length')} entries  "
                 f"nozzle swaps: {t.get('nozzle_swaps')}")
    lines.append(f"  gcode: {tc_str or '—'}   "
                 f"M620 cycles: {t.get('m620_cycles')}   "
                 f"M620.10 flushes: {t.get('m620_flushes')}")
    lines.append(f"  purge: {t.get('flush_volume_mm', 0):.0f} mm "
                 f"≈ {t.get('flush_volume_g', 0):.2f} g")

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
