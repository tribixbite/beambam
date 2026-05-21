"""beambam.filament_profiles — build Bambu-Studio-compatible flat
filament profile JSONs, parameterized by printer model + nozzle size.

The originals in `tools/gen_x2d_filament_profiles.py` were X2D-only:
every output filename, profile `name`, and `compatible_printers` entry
hard-coded `@BBL X2D 0.4 nozzle`. This module pulls the builder out so
the same recipe (vendor / material / temps / flow / density) emits a
flat profile for whichever Bambu model the caller targets — X1C, X1E,
P1S, P1P, A1, A1mini, H2D, H2S, X2D.

Public API:

    PRINTER_MODELS         frozen list of supported models
    NOZZLE_SIZES_MM        frozen list (0.2 / 0.4 / 0.6 / 0.8)
    profile_basename(...)  → "ZIRO Twinkle PLA Silk @BBL X1C 0.4 nozzle"
    profile_filename(...)  → "ZIRO Twinkle PLA Silk @BBL X1C 0.4 nozzle.json"
    setting_suffix(...)    → "_X1C04"
    compatible_printer(..) → "Bambu Lab X1C 0.4 nozzle"
    retarget_profile(p, m) → mutate `p` so it binds to `m`'s printer
    apply_material(p, m)   → flip PLA→PETG/TPU defaults on a PLA clone
    patch_temps(p, ...)    → set nozzle/bed temp envelopes
    build_profile(...)     → top-level "build me a profile" entry point

The shop-list recipes themselves live in `tools/gen_x2d_filament_profiles.py`
because they encode personal filament inventory, not library logic."""
from __future__ import annotations

import copy
from typing import Any

# Canonical list — keep in sync with the Bambu hardware lineup. Order
# is significant for `--printer-model` autocompletion; most-common
# first.
PRINTER_MODELS = (
    "X1C",     # X1-Carbon
    "X1E",     # X1E (industrial)
    "P1S",     # P1S
    "P1P",     # P1P
    "A1",      # A1 (single-extruder)
    "A1mini",  # A1 mini
    "H2D",     # H2D (dual-extruder)
    "H2S",     # H2S
    "X2D",     # X2D
)

NOZZLE_SIZES_MM = (0.2, 0.4, 0.6, 0.8)


# ---------- printer-binding strings -----------------------------------

def _model_display(model: str) -> str:
    """Bambu Studio displays "Bambu Lab X1C", "Bambu Lab A1 mini"
    (note the space + lowercase 'mini'). Map our canonical id to the
    UI display form."""
    if model == "A1mini":
        return "A1 mini"
    return model


def profile_basename(
    *, vendor: str, material_name: str, model: str, nozzle_mm: float,
) -> str:
    """The `name` field value Bambu Studio shows in its dropdown.

    Examples:
      profile_basename(vendor="ZIRO", material_name="Twinkle PLA Silk",
                        model="X1C", nozzle_mm=0.4)
        → "ZIRO Twinkle PLA Silk @BBL X1C 0.4 nozzle"
    """
    return f"{vendor} {material_name} @BBL {_model_display(model)} {nozzle_mm:.1f} nozzle"


def profile_filename(
    *, vendor: str, material_name: str, model: str, nozzle_mm: float,
) -> str:
    return profile_basename(
        vendor=vendor, material_name=material_name,
        model=model, nozzle_mm=nozzle_mm,
    ) + ".json"


def setting_suffix(*, model: str, nozzle_mm: float) -> str:
    """The compact tag Bambu uses inside `setting_id`. `0.4` becomes
    `04`, `0.6` becomes `06`, etc. — same convention Bambu Studio uses
    in its own GFSA00_08-style ids. The model goes through unmodified
    (A1mini stays compact)."""
    nozzle_pack = f"{int(round(nozzle_mm * 10)):02d}"
    return f"_{model}{nozzle_pack}"


def compatible_printer(*, model: str, nozzle_mm: float) -> str:
    """The string Bambu Studio matches in the profile's
    `compatible_printers` list. Different from the @BBL tag in `name`:
    here `A1mini` becomes `A1 mini` with a space."""
    return f"Bambu Lab {_model_display(model)} {nozzle_mm:.1f} nozzle"


# ---------- profile mutators ------------------------------------------

DD_BOWDEN_4 = lambda v: [str(v)] * 4
DD_BOWDEN_4V = lambda dds, ddh, bs, bh: [str(dds), str(ddh), str(bs), str(bh)]


def apply_material(profile: dict[str, Any], material: str) -> None:
    """Flip material-specific defaults on a PLA-template clone.

    The Bambu PLA template carries safe PLA defaults out of the box;
    PETG and TPU need a handful of fields overridden so the slicer
    picks the right bed temp, retraction, and flow."""
    if material == "PLA":
        return

    if material == "PETG":
        profile["filament_type"] = ["PETG"]
        profile["filament_density"] = ["1.27"]
        profile["filament_flow_ratio"] = DD_BOWDEN_4(0.95)
        profile["temperature_vitrification"] = ["80"]
        profile["eng_plate_temp"] = ["80"]
        profile["eng_plate_temp_initial_layer"] = ["80"]
        profile["supertack_plate_temp"] = ["0"]
        profile["supertack_plate_temp_initial_layer"] = ["0"]
        profile["cool_plate_temp"] = ["0"]
        profile["cool_plate_temp_initial_layer"] = ["0"]
        # Short retract on DD; process-default on Bowden. Forcing a
        # 4 mm Bowden retract over-retracts and leaves grind marks.
        profile["filament_retraction_length"] = ["0.4", "0.4", "nil", "nil"]
        profile["impact_strength_z"] = ["10"]
        return

    if material == "TPU":
        profile["filament_type"] = ["TPU"]
        profile["filament_density"] = ["1.21"]
        profile["filament_flow_ratio"] = DD_BOWDEN_4(1.0)
        profile["temperature_vitrification"] = ["60"]
        profile["eng_plate_temp"] = ["50"]
        profile["eng_plate_temp_initial_layer"] = ["50"]
        profile["supertack_plate_temp"] = ["0"]
        profile["supertack_plate_temp_initial_layer"] = ["0"]
        # Longer DD retract; Bowden default. TPU is springy — short
        # Bowden retract leaves blobs.
        profile["filament_retraction_length"] = ["0.8", "0.8", "nil", "nil"]
        # TPU should never push speed.
        profile["slow_down_min_speed"] = DD_BOWDEN_4(10)
        profile["impact_strength_z"] = ["35"]
        return

    raise ValueError(f"unknown material: {material!r}")


def patch_temps(
    profile: dict[str, Any],
    *,
    nozzle: int,
    n_low: int,
    n_high: int,
    bed: int,
    cool_plate: int | None = None,
) -> None:
    """Set nozzle + bed + plate temperature fields. Same field set
    Bambu's own templates write; we just fan a single nozzle figure
    across the four DD/Bowden variants."""
    profile["nozzle_temperature"] = DD_BOWDEN_4(nozzle)
    profile["nozzle_temperature_initial_layer"] = DD_BOWDEN_4(nozzle)
    profile["nozzle_temperature_range_low"] = [str(n_low)]
    profile["nozzle_temperature_range_high"] = [str(n_high)]
    profile["filament_tower_interface_print_temp"] = [str(nozzle)]
    profile["hot_plate_temp"] = [str(bed)]
    profile["hot_plate_temp_initial_layer"] = [str(bed)]
    profile["textured_plate_temp"] = [str(bed)]
    profile["textured_plate_temp_initial_layer"] = [str(bed)]
    if cool_plate is not None:
        profile["cool_plate_temp"] = [str(cool_plate)]
        profile["cool_plate_temp_initial_layer"] = [str(cool_plate)]


def retarget_profile(
    profile: dict[str, Any],
    *,
    model: str,
    nozzle_mm: float,
) -> None:
    """Flip every printer-binding field on a profile so it loads
    against `model` + `nozzle_mm` in Bambu Studio.

    Used by `build_profile()` after the material + temp patches, and
    callable on its own to retarget an existing flat profile loaded
    from disk (e.g. clone an X2D profile to an X1C with one call)."""
    if model not in PRINTER_MODELS:
        raise ValueError(
            f"unknown printer model {model!r}; "
            f"expected one of {PRINTER_MODELS}")
    if nozzle_mm not in NOZZLE_SIZES_MM:
        raise ValueError(
            f"unsupported nozzle size {nozzle_mm}; "
            f"expected one of {NOZZLE_SIZES_MM}")

    profile["compatible_printers"] = [
        compatible_printer(model=model, nozzle_mm=nozzle_mm),
    ]
    # `name` and `setting_id` may already point at a different
    # printer (when retargeting an existing profile). Replace the
    # `@BBL <m> <n> nozzle` tail and `_<M><NN>` suffix.
    name = profile.get("name", "")
    if "@BBL " in name:
        head = name.split("@BBL ", 1)[0].rstrip()
        profile["name"] = (head + " " +
                            f"@BBL {_model_display(model)} {nozzle_mm:.1f} nozzle")
    suffix = setting_suffix(model=model, nozzle_mm=nozzle_mm)
    sid = profile.get("setting_id", "")
    if sid:
        # `setting_id` may already carry a printer suffix (e.g.
        # `GFSL_ZIRO_TWINKLE_X2D04`). The suffix is the last
        # underscore-separated token after the vendor/material core
        # and looks like `_<MODEL_CHARS><NOZZLE_DIGITS>` — a run of
        # letters/digits followed by exactly 2 digits at end-of-string.
        # We use re.subn to distinguish "pattern matched (even if the
        # replacement is identical)" from "no match at all" — only the
        # latter triggers the append fallback.
        import re
        new_sid, n = re.subn(r"_[A-Za-z0-9]+\d{2}$", suffix, sid)
        if n == 0:
            new_sid = sid + suffix
        profile["setting_id"] = new_sid


# ---------- top-level builder -----------------------------------------

def build_profile(
    *,
    template: dict[str, Any],
    name: str,
    filename: str,
    vendor: str,
    material: str,            # "PLA" | "PETG" | "TPU"
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
    model: str = "X2D",
    nozzle_mm: float = 0.4,
) -> dict[str, Any]:
    """Deep-clone the template, apply material + temp + retarget +
    vendor-specific patches, return the finished profile dict.

    `template` is the parsed JSON dict of a Bambu flat filament profile
    (usually `flat-profiles/<model>_filament.json`). Callers can mix
    templates from any base model — `retarget_profile()` rewrites the
    binding fields after the patches."""
    p = copy.deepcopy(template)

    apply_material(p, material)
    patch_temps(
        p,
        nozzle=nozzle,
        n_low=nozzle_low,
        n_high=nozzle_high,
        bed=bed,
        cool_plate=cool_plate,
    )

    p["name"] = name
    p["filament_vendor"] = [vendor]
    p["filament_density"] = [f"{density:.2f}"]
    p["filament_max_volumetric_speed"] = DD_BOWDEN_4(max_vol)
    p["required_nozzle_HRC"] = [str(hrc)]
    p["filament_cost"] = [f"{cost:.2f}"]
    p["setting_id"] = setting_id
    p["filament_id"] = filament_id
    p["description"] = description
    if flow_ratio is not None:
        p["filament_flow_ratio"] = DD_BOWDEN_4(flow_ratio)
    if slow_down_layer_time is not None:
        p["slow_down_layer_time"] = [str(slow_down_layer_time)]

    # Retarget last so `name`/`compatible_printers`/`setting_id`
    # always match `model` + `nozzle_mm` even when the recipe's `name`
    # / `setting_id` were written against a different printer.
    retarget_profile(p, model=model, nozzle_mm=nozzle_mm)

    # Carry a filename hint for callers that want to write the profile
    # out. Not part of the Bambu schema — strip before writing if
    # importing into Bambu Studio.
    p["filename"] = filename
    return p
