"""beambam.filament_match — auto-map a model's filament(s) to AMS slots.

The rule (per the user's spec):

  * Functional prints — fewer than `AESTHETIC_THRESHOLD` (3) distinct colors —
    match by **TYPE** first. Color is only a tiebreak; type wins even if no
    slot has a close color ("overruling color when no close color+type match").
    A snap latch in white PETG prints fine from black PETG.
  * Aesthetic / toy prints — `>=` 3 distinct colors — match by **COLOR** first
    (the look is the point); type is the tiebreak.

Inputs:
  * a model's required filaments — `[(type, color_hex), ...]` parsed from a
    .3mf's `Metadata/project_settings.config` (`model_filaments`)
  * the AMS loadout — `[{slot, type, color}, ...]` from a pushall state's
    `print.ams` (`ams_loaded_slots`)

Output: one chosen global AMS slot per required filament (`auto_match`), plus
the classification, for building the print's `ams_mapping`.
"""
from __future__ import annotations

import re
from typing import Any, Optional

AESTHETIC_THRESHOLD = 3


def _type_base(t: str) -> str:
    """Normalize a filament type to its material family for matching:
    'PETG-CF' -> 'PETG', 'PLA Matte' -> 'PLA', 'TPU95A' -> 'TPU'. Takes the
    leading alphabetic run (AMS `tray_type` + model `filament_type` are both
    clean, e.g. 'PETG-CF' / 'PETG')."""
    m = re.match(r"[A-Za-z]+", (t or "").strip())
    return m.group(0).upper() if m else ""


def _hex_rgb(h: str) -> tuple[int, int, int]:
    """'#F72323' / 'F72323FF' -> (247, 35, 35). Tolerates missing '#' and an
    alpha suffix; non-hex -> mid-grey so it sorts as 'far from everything'."""
    s = (h or "").lstrip("#")
    if len(s) < 6:
        return (128, 128, 128)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return (128, 128, 128)


def _color_dist(a: str, b: str) -> float:
    ra, rb = _hex_rgb(a), _hex_rgb(b)
    return sum((x - y) ** 2 for x, y in zip(ra, rb)) ** 0.5


def ams_loaded_slots(state: dict) -> list[dict[str, Any]]:
    """Extract loaded AMS trays from a pushall state's `print.ams` (or a raw
    `ams` block). Returns `[{slot, type, color, brand}]` with `slot` the GLOBAL
    index (unit_id*4 + tray_id). Empty trays (no `tray_type`) are skipped."""
    ams = state.get("print", {}).get("ams") if "print" in state else state.get("ams")
    if not isinstance(ams, dict):
        return []
    out: list[dict[str, Any]] = []
    for unit in ams.get("ams", []) or []:
        try:
            uid = int(unit.get("id"))
        except (TypeError, ValueError):
            continue
        for tray in unit.get("tray", []) or []:
            ttype = (tray.get("tray_type") or "").strip()
            if not ttype:
                continue
            try:
                tid = int(tray.get("id"))
            except (TypeError, ValueError):
                continue
            out.append({
                "slot": uid * 4 + tid,
                "type": ttype,
                "color": (tray.get("tray_color") or "").strip(),
                "brand": tray.get("tray_sub_brands") or tray.get("tray_id_name") or "",
            })
    return out


def model_filaments(project_settings: dict) -> list[tuple[str, str]]:
    """Parse `[(type, color_hex)]` from a .3mf `project_settings.config` dict.
    Pairs `filament_type[i]` with `filament_colour[i]` (falls back to
    `filament_multi_colour`)."""
    types = project_settings.get("filament_type") or []
    colors = (project_settings.get("filament_colour")
              or project_settings.get("filament_multi_colour") or [])
    out: list[tuple[str, str]] = []
    for i, t in enumerate(types):
        out.append((t, colors[i] if i < len(colors) else ""))
    return out


def is_aesthetic(filaments: list[tuple[str, str]],
                 threshold: int = AESTHETIC_THRESHOLD) -> bool:
    """A print is 'aesthetic' (match by color) when it uses >= `threshold`
    distinct colors; otherwise 'functional' (match by type)."""
    distinct = {c.upper() for _, c in filaments if c}
    return len(distinct) >= threshold


def match_slot(req_type: str, req_color: str, slots: list[dict[str, Any]], *,
               aesthetic: bool) -> Optional[dict[str, Any]]:
    """Pick the best loaded slot for ONE required filament.

    functional (type-first): prefer a slot whose base material matches, then an
    exact type match, then the closest color; if NOTHING matches the type, the
    closest color is chosen anyway (color 'overruled' as specified).
    aesthetic (color-first): closest color wins, base-type match breaks ties."""
    if not slots:
        return None
    base = _type_base(req_type)
    if aesthetic:
        return min(slots, key=lambda s: (
            round(_color_dist(req_color, s["color"]), 3),
            _type_base(s["type"]) != base,
        ))
    return min(slots, key=lambda s: (
        _type_base(s["type"]) != base,                         # base family first
        s["type"].strip().upper() != req_type.strip().upper(),  # then exact type
        round(_color_dist(req_color, s["color"]), 3),          # then closest color
    ))


def auto_match(filaments: list[tuple[str, str]],
               slots: list[dict[str, Any]]) -> dict[str, Any]:
    """Map every required filament to a loaded AMS slot per the type/color rule.

    Returns `{aesthetic: bool, mode: str, mapping: [{req_type, req_color,
    slot, slot_type, slot_color} | None]}`. `mapping[i]` is None only when the
    AMS is empty. The first mapping's `slot` is the single-filament answer."""
    aesthetic = is_aesthetic(filaments)
    mapping = []
    for req_type, req_color in filaments:
        chosen = match_slot(req_type, req_color, slots, aesthetic=aesthetic)
        mapping.append(None if chosen is None else {
            "req_type": req_type, "req_color": req_color,
            "slot": chosen["slot"], "slot_type": chosen["type"],
            "slot_color": chosen["color"],
        })
    return {
        "aesthetic": aesthetic,
        "mode": "color-first" if aesthetic else "type-first",
        "mapping": mapping,
    }
