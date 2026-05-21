#!/usr/bin/env python3
"""Generate flat X2D 0.4-nozzle filament profile JSONs from shop-list specs.

This script is now a thin shim — the profile-building logic lives in
`beambam.filament_profiles` so the same recipe can target any Bambu
model (X1C / X1E / P1S / P1P / A1 / A1mini / H2D / H2S / X2D). The
recipes themselves stay here because they encode personal shop
inventory (vendor + price + spool sizes), not library content.

To regenerate for a different printer, edit `MODEL` + `NOZZLE_MM`
below, or invoke `beambam.filament_profiles.build_profile()` directly
in your own script. Output filenames pick up the model automatically
via `profile_filename()`.

Run from repo root:
    python3 tools/gen_x2d_filament_profiles.py

Outputs go to flat-profiles/ (gitignored). Re-run any time to regenerate.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from beambam.filament_profiles import (  # noqa: E402
    DD_BOWDEN_4, DD_BOWDEN_4V,
    apply_material as _apply_material,
    build_profile as _lib_build_profile,
    patch_temps as _patch_temps,
)

OUT_DIR = REPO / "flat-profiles"
PLA_TEMPLATE = OUT_DIR / "x2d_filament.json"
SILK_TEMPLATE = OUT_DIR / "x2d_pla_silk.json"

# Target printer + nozzle for this batch. Change these to retarget the
# same recipes at a different Bambu model — beambam.filament_profiles
# handles the name / setting_id / compatible_printers swap.
MODEL = "X2D"
NOZZLE_MM = 0.4


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def build_profile(
    *,
    template: str,          # "pla" or "silk"
    name: str,
    filename: str,
    vendor: str,
    material: str,
    nozzle: int,
    nozzle_low: int,
    nozzle_high: int,
    bed: int,
    cool_plate: int | None,
    density: float,
    max_vol: int,
    hrc: int,
    setting_id: str,
    filament_id: str,
    description: str,
    flow_ratio: float | None = None,
    cost: float = 24.99,
    slow_down_layer_time: int | None = None,
) -> dict[str, Any]:
    """Thin wrapper around `beambam.filament_profiles.build_profile()`.

    Loads the requested template ("pla" or "silk"), then defers to the
    library for material/temp/retarget patches. `MODEL` + `NOZZLE_MM`
    at the top of this file decide which printer the output binds to."""
    src = PLA_TEMPLATE if template == "pla" else SILK_TEMPLATE
    tpl = _load(src)
    return _lib_build_profile(
        template=tpl,
        name=name,
        filename=filename,
        vendor=vendor,
        material=material,
        nozzle=nozzle,
        nozzle_low=nozzle_low,
        nozzle_high=nozzle_high,
        bed=bed,
        cool_plate=cool_plate,
        density=density,
        max_vol=max_vol,
        hrc=hrc,
        setting_id=setting_id,
        filament_id=filament_id,
        description=description,
        flow_ratio=flow_ratio,
        cost=cost,
        slow_down_layer_time=slow_down_layer_time,
        model=MODEL,
        nozzle_mm=NOZZLE_MM,
    )


# ---------------------------------------------------------------------------
# Recipes (numbers reference shop/filaments-2026-05.md rows)
# ---------------------------------------------------------------------------

RECIPES: list[dict[str, Any]] = [
    # 1) ZIRO Twinkle PLA Silk (Lavender) — sparkle + gradient
    dict(
        template="silk", filename="ZIRO Twinkle PLA Silk @BBL X2D 0.4 nozzle.json",
        name="ZIRO Twinkle PLA Silk @BBL X2D 0.4 nozzle",
        vendor="ZIRO", material="PLA",
        nozzle=210, nozzle_low=190, nozzle_high=220, bed=55, cool_plate=35,
        density=1.32, max_vol=8, hrc=4,
        setting_id="GFSL_ZIRO_TWINKLE_X2D04",
        filament_id="GFL_ZIRO_TWINKLE",
        cost=24.99,
        description="ZIRO Twinkle (Flower Series) sparkle+gradient PLA Silk. "
                    "Sparkle particles abrasive — hardened nozzle recommended. "
                    "Shop row 1 — Lavender.",
    ),
    # 2) XZN PETG (Silver) — tight 220–230 window
    dict(
        template="pla", filename="XZN PETG @BBL X2D 0.4 nozzle.json",
        name="XZN PETG @BBL X2D 0.4 nozzle",
        vendor="XZN", material="PETG",
        nozzle=225, nozzle_low=220, nozzle_high=230, bed=75, cool_plate=0,
        density=1.27, max_vol=12, hrc=3, flow_ratio=0.95,
        setting_id="GFSG_XZN_PETG_X2D04",
        filament_id="GFG_XZN_PETG",
        cost=18.99,
        description="XZN PETG. Tight 220-230 nozzle window — narrower than typical PETG. "
                    "Shop row 2 — Silver.",
    ),
    # 3) ZIRO TPU 95A Color-Change (Flex Opal)
    dict(
        template="pla", filename="ZIRO TPU 95A Color-Change @BBL X2D 0.4 nozzle.json",
        name="ZIRO TPU 95A Color-Change @BBL X2D 0.4 nozzle",
        vendor="ZIRO", material="TPU",
        nozzle=215, nozzle_low=200, nozzle_high=230, bed=55, cool_plate=0,
        density=1.21, max_vol=4, hrc=3,
        setting_id="GFSU_ZIRO_TPU95_X2D04",
        filament_id="GFU_ZIRO_TPU95",
        cost=29.99,
        slow_down_layer_time=8,
        description="ZIRO TPU 95A color-changing (Flex Opal). Direct-drive only, "
                    "disable retraction. 0.8 kg spool. Shop row 3.",
    ),
    # 4) eSUN PETG Basic — Black + Grey
    dict(
        template="pla", filename="eSUN PETG Basic @BBL X2D 0.4 nozzle.json",
        name="eSUN PETG Basic @BBL X2D 0.4 nozzle",
        vendor="eSUN", material="PETG",
        nozzle=250, nozzle_low=240, nozzle_high=260, bed=80, cool_plate=0,
        density=1.27, max_vol=12, hrc=3, flow_ratio=0.95,
        setting_id="GFSG_ESUN_PETG_BASIC_X2D04",
        filament_id="GFG_ESUN_PETG_BASIC",
        cost=19.99,
        description="eSUN PETG Basic. Runs hot (240-260) vs typical PETG. "
                    "Shop rows 4 (Black) + 5 (Grey).",
    ),
    # 5) Toksen3D PETG (Metal Wine Red) — low-bed PETG
    dict(
        template="pla", filename="Toksen3D PETG @BBL X2D 0.4 nozzle.json",
        name="Toksen3D PETG @BBL X2D 0.4 nozzle",
        vendor="Toksen3D", material="PETG",
        nozzle=240, nozzle_low=230, nozzle_high=255, bed=55, cool_plate=0,
        density=1.27, max_vol=12, hrc=3, flow_ratio=0.95,
        setting_id="GFSG_TOKSEN_PETG_X2D04",
        filament_id="GFG_TOKSEN_PETG",
        cost=21.99,
        description='Toksen3D "Metal Wine Red" PETG (metallic pigment, not metal-filled). '
                    "Unusual low bed (50-60); bump to 70 if first-layer delam. Shop row 6.",
    ),
    # 6) SUNLU PETG Glow (Blue / Green / Red / Yellow)
    dict(
        template="pla", filename="SUNLU PETG Glow @BBL X2D 0.4 nozzle.json",
        name="SUNLU PETG Glow @BBL X2D 0.4 nozzle",
        vendor="SUNLU", material="PETG",
        nozzle=250, nozzle_low=245, nozzle_high=260, bed=75, cool_plate=0,
        density=1.31, max_vol=10, hrc=4, flow_ratio=0.95,
        setting_id="GFSG_SUNLU_PETG_GLOW_X2D04",
        filament_id="GFG_SUNLU_PETG_GLOW",
        cost=22.49,
        description="SUNLU PETG Glow phosphor (abrasive — hardened nozzle). "
                    "Charge under bright light. Shop row 7 — Blue/Green/Red/Yellow.",
    ),
    # 7) OVERTURE PLA (Blue) — wide bed range
    dict(
        template="pla", filename="OVERTURE PLA @BBL X2D 0.4 nozzle.json",
        name="OVERTURE PLA @BBL X2D 0.4 nozzle",
        vendor="OVERTURE", material="PLA",
        nozzle=205, nozzle_low=190, nozzle_high=220, bed=50, cool_plate=35,
        density=1.20, max_vol=18, hrc=3,
        setting_id="GFSA_OVERTURE_PLA_X2D04",
        filament_id="GFA_OVERTURE_PLA",
        cost=17.99,
        description="OVERTURE PLA. Bed 25-60°C (room temp OK). Shop row 8 — Blue.",
    ),
    # 8) eSUN PLA+ — covers Light Brown / Haze Blue / Orange / Beige / Red / Black / White
    dict(
        template="pla", filename="eSUN PLA+ @BBL X2D 0.4 nozzle.json",
        name="eSUN PLA+ @BBL X2D 0.4 nozzle",
        vendor="eSUN", material="PLA",
        nozzle=215, nozzle_low=210, nozzle_high=230, bed=55, cool_plate=35,
        density=1.25, max_vol=20, hrc=3,
        setting_id="GFSA_ESUN_PLA_PLUS_X2D04",
        filament_id="GFA_ESUN_PLA_PLUS",
        cost=22.99,
        description="eSUN PLA+ — 215°C ideal. Shop rows 9-12 (Light Brown, Haze Blue, "
                    "Orange, Beige) plus user-confirmed Red + Black variants.",
    ),
    # 9) FLASHFORGE PLA (standard) — Orange
    dict(
        template="pla", filename="FLASHFORGE PLA @BBL X2D 0.4 nozzle.json",
        name="FLASHFORGE PLA @BBL X2D 0.4 nozzle",
        vendor="FLASHFORGE", material="PLA",
        nozzle=205, nozzle_low=190, nozzle_high=220, bed=55, cool_plate=35,
        density=1.24, max_vol=18, hrc=3,
        setting_id="GFSA_FF_PLA_X2D04",
        filament_id="GFA_FF_PLA",
        cost=18.99,
        description="FLASHFORGE standard PLA (mfg datasheet defaults). Shop row 13 — Orange.",
    ),
    # 10) FLASHFORGE Rapid PLA (high-speed) — Aurora Green
    dict(
        template="pla", filename="FLASHFORGE Rapid PLA @BBL X2D 0.4 nozzle.json",
        name="FLASHFORGE Rapid PLA @BBL X2D 0.4 nozzle",
        vendor="FLASHFORGE", material="PLA",
        nozzle=210, nozzle_low=200, nozzle_high=220, bed=55, cool_plate=35,
        density=1.24, max_vol=25, hrc=3,
        setting_id="GFSA_FF_RAPID_PLA_X2D04",
        filament_id="GFA_FF_RAPID_PLA",
        cost=21.99,
        description="FLASHFORGE Rapid PLA (high-speed). Volumetric ~22-28 mm³/s, "
                    "linear up to 500 mm/s rated. Shop row 14 — Aurora Green.",
    ),
    # 11) FLASHFORGE Rapid PLA Glow — Luminous Green
    dict(
        template="pla", filename="FLASHFORGE Rapid PLA Glow @BBL X2D 0.4 nozzle.json",
        name="FLASHFORGE Rapid PLA Glow @BBL X2D 0.4 nozzle",
        vendor="FLASHFORGE", material="PLA",
        nozzle=210, nozzle_low=200, nozzle_high=220, bed=55, cool_plate=35,
        density=1.27, max_vol=18, hrc=4,
        setting_id="GFSA_FF_RAPID_PLA_GLOW_X2D04",
        filament_id="GFA_FF_RAPID_PLA_GLOW",
        cost=22.99,
        description="FLASHFORGE Rapid PLA Glow. Hardened nozzle (phosphor abrasive). "
                    "Max volumetric 18 mm³/s vs 25 for non-glow. Shop row 15.",
    ),
    # 12) eSUN TPU 95A — Transparent Blue
    dict(
        template="pla", filename="eSUN TPU 95A @BBL X2D 0.4 nozzle.json",
        name="eSUN TPU 95A @BBL X2D 0.4 nozzle",
        vendor="eSUN", material="TPU",
        nozzle=240, nozzle_low=220, nozzle_high=250, bed=55, cool_plate=0,
        density=1.21, max_vol=4, hrc=3,
        setting_id="GFSU_ESUN_TPU95_X2D04",
        filament_id="GFU_ESUN_TPU95",
        cost=27.99,
        slow_down_layer_time=8,
        description="eSUN TPU 95A — 240°C ideal. Direct-drive only, disable retraction. "
                    "Shop row 16 — Transparent Blue.",
    ),
    # 13) FLASHFORGE Silk Dual (Blue → Silver gradient)
    dict(
        template="silk", filename="FLASHFORGE Silk Dual @BBL X2D 0.4 nozzle.json",
        name="FLASHFORGE Silk Dual @BBL X2D 0.4 nozzle",
        vendor="FLASHFORGE", material="PLA",
        nozzle=210, nozzle_low=200, nozzle_high=220, bed=55, cool_plate=35,
        density=1.30, max_vol=8, hrc=3,
        setting_id="GFSL_FF_SILK_DUAL_X2D04",
        filament_id="GFL_FF_SILK_DUAL",
        cost=24.99,
        description="FLASHFORGE Silk Dual gradient PLA. Continuous Blue→Silver — no swap. "
                    "Run ~25-100 mm/s actual (vs 500 rated) and low fan 30-50% for shine. "
                    "Shop row 17.",
    ),
    # 14b) eSUN PLA Silk — standard silk (non-metallic), e.g. Silk Blue
    dict(
        template="silk", filename="eSUN PLA Silk @BBL X2D 0.4 nozzle.json",
        name="eSUN PLA Silk @BBL X2D 0.4 nozzle",
        vendor="eSUN", material="PLA",
        nozzle=215, nozzle_low=200, nozzle_high=230, bed=55, cool_plate=35,
        density=1.23, max_vol=10, hrc=3,
        setting_id="GFSL_ESUN_PLA_SILK_X2D04",
        filament_id="GFL_ESUN_PLA_SILK",
        cost=22.99,
        description="eSUN PLA Silk (standard, non-metallic silk). 30-90 mm/s "
                    "recommended for sheen; dry before use.",
    ),
    # 14) eSUN Silk Metal PLA — Silk Gold (ASIN B07SDDD3RS; metallic-finish silk)
    dict(
        template="silk", filename="eSUN Silk Metal PLA @BBL X2D 0.4 nozzle.json",
        name="eSUN Silk Metal PLA @BBL X2D 0.4 nozzle",
        vendor="eSUN", material="PLA",
        nozzle=210, nozzle_low=200, nozzle_high=220, bed=55, cool_plate=35,
        density=1.23, max_vol=8, hrc=3,
        setting_id="GFSL_ESUN_SILK_METAL_X2D04",
        filament_id="GFL_ESUN_SILK_METAL",
        cost=23.99,
        description="eSUN Silk Metal PLA (ASIN B07SDDD3RS) — silky metallic finish, "
                    "+/-0.05mm tolerance. 30-90 mm/s recommended for sheen. "
                    "Dry before use. Slot 3 — Silk Gold.",
    ),
]


# Slot → profile assignments confirmed by the user (2026-05-21).
# Profile filenames must match a recipe `filename` above. `color` is optional
# and overrides the slot's current AMS color tag (8-char RRGGBBAA hex, alpha
# auto-appended FF if omitted from the value).
AMS_SLOT_ASSIGNMENTS: list[dict[str, Any]] = [
    # Slots 0, 8, 14 are Bambu RFID-tagged — not overridden (firmware re-reads
    # chip continuously and would revert the override).
    {"slot": 1,  "profile": "FLASHFORGE Rapid PLA @BBL X2D 0.4 nozzle.json",
     "color": "00C896"},   # Aurora Green
    {"slot": 2,  "profile": "eSUN PLA+ @BBL X2D 0.4 nozzle.json",
     "color": "7C4B00"},   # Light Brown
    {"slot": 3,  "profile": "eSUN Silk Metal PLA @BBL X2D 0.4 nozzle.json",
     "color": "D4AF37"},   # Silk Gold (metallic)
    {"slot": 4,  "profile": "eSUN PETG Basic @BBL X2D 0.4 nozzle.json",
     "color": "898989"},   # Solid Grey
    {"slot": 5,  "profile": "eSUN PLA+ @BBL X2D 0.4 nozzle.json",
     "color": "FFFFFF"},   # White (eSUN PLA+ Cold White)
    {"slot": 6,  "profile": "eSUN PLA+ @BBL X2D 0.4 nozzle.json",
     "color": "E03C3C"},   # Fire Engine Red (eSUN red)
    {"slot": 7,  "profile": "eSUN PLA+ @BBL X2D 0.4 nozzle.json",
     "color": "F98C36"},   # Orange
    {"slot": 9,  "profile": "eSUN PLA+ @BBL X2D 0.4 nozzle.json",
     "color": "000000"},   # Black
    {"slot": 10, "profile": "eSUN PLA+ @BBL X2D 0.4 nozzle.json",
     "color": "FCECD6"},   # Beige
    {"slot": 11, "profile": "OVERTURE PLA @BBL X2D 0.4 nozzle.json",
     "color": "1B73C5"},   # Overture Blue
    {"slot": 12, "profile": "eSUN PLA Silk @BBL X2D 0.4 nozzle.json",
     "color": "2850E0"},   # Silk Blue
    {"slot": 13, "profile": "Toksen3D PETG @BBL X2D 0.4 nozzle.json",
     "color": "722F37"},   # Metal Wine Red (was mis-tagged bright red)
    {"slot": 15, "profile": "eSUN PLA+ @BBL X2D 0.4 nozzle.json",
     "color": "46A8F9"},   # Haze Blue
]


def main() -> None:
    if not PLA_TEMPLATE.exists() or not SILK_TEMPLATE.exists():
        raise SystemExit(
            f"missing template(s): {PLA_TEMPLATE} or {SILK_TEMPLATE}"
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    valid_filenames = {r["filename"] for r in RECIPES}
    written = 0
    for recipe in RECIPES:
        profile = build_profile(**recipe)
        fname = profile.pop("filename")
        out_path = OUT_DIR / fname
        out_path.write_text(json.dumps(profile, indent=2) + "\n")
        written += 1
        print(f"  wrote {fname}")

    # Sanity-check the slot assignments reference existing recipes.
    for asn in AMS_SLOT_ASSIGNMENTS:
        if asn["profile"] not in valid_filenames:
            raise SystemExit(
                f"slot {asn['slot']} references unknown profile {asn['profile']!r}"
            )

    sync_path = OUT_DIR / "ams-sync.json"
    sync_path.write_text(
        json.dumps({"slots": AMS_SLOT_ASSIGNMENTS}, indent=2) + "\n"
    )
    print(f"  wrote {sync_path.name}  ({len(AMS_SLOT_ASSIGNMENTS)} slot assignments)")

    print(f"done: {written} profile(s) + 1 sync map in {OUT_DIR}")


if __name__ == "__main__":
    main()
