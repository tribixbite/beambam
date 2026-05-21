"""Tests for `beambam.filament_profiles` — the cross-printer flat
profile builder.

These tests exercise the printer-binding string helpers + the
retarget / build pipeline using a tiny inline template. No
filesystem access; no real Bambu Studio import roundtrip — that
remains a manual verification step."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.filament_profiles import (
    NOZZLE_SIZES_MM,
    PRINTER_MODELS,
    apply_material,
    build_profile,
    compatible_printer,
    patch_temps,
    profile_basename,
    profile_filename,
    retarget_profile,
    setting_suffix,
)


# --- minimal template approximating a Bambu PLA flat profile ----------

def _stub_template() -> dict:
    """Just enough fields to exercise the patcher functions. Real
    templates have ~50 fields but our patches only touch ~15."""
    return {
        "type":                 "filament",
        "name":                 "Bambu PLA Basic @BBL X2D 0.4 nozzle",
        "filament_type":        ["PLA"],
        "filament_vendor":      ["Bambu"],
        "filament_density":     ["1.24"],
        "filament_flow_ratio":  ["0.98", "0.98", "0.98", "0.98"],
        "filament_id":          "GFA00",
        "setting_id":           "GFSA00_08",
        "compatible_printers":  ["Bambu Lab X2D 0.4 nozzle"],
        "nozzle_temperature":   ["215", "215", "215", "215"],
    }


# --- printer-binding strings ------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("X1C",    "Bambu Lab X1C 0.4 nozzle"),
    ("P1S",    "Bambu Lab P1S 0.4 nozzle"),
    ("A1mini", "Bambu Lab A1 mini 0.4 nozzle"),  # space + lowercase
    ("X2D",    "Bambu Lab X2D 0.4 nozzle"),
    ("H2D",    "Bambu Lab H2D 0.4 nozzle"),
])
def test_compatible_printer_string_renders_correctly(model, expected):
    assert compatible_printer(model=model, nozzle_mm=0.4) == expected


def test_compatible_printer_picks_up_nozzle_size():
    """0.6 and 0.8 nozzles should produce different strings — Bambu's
    profile matcher is exact-string."""
    s4 = compatible_printer(model="X1C", nozzle_mm=0.4)
    s6 = compatible_printer(model="X1C", nozzle_mm=0.6)
    assert s4 != s6
    assert s6 == "Bambu Lab X1C 0.6 nozzle"


def test_setting_suffix_compact_form():
    """`_X1C04`, `_A1mini04`, `_X2D08` — model id verbatim + 2-digit
    nozzle. Matches Bambu's GFSA00_08 / GFSL_..._X2D04 convention."""
    assert setting_suffix(model="X1C", nozzle_mm=0.4) == "_X1C04"
    assert setting_suffix(model="A1mini", nozzle_mm=0.4) == "_A1mini04"
    assert setting_suffix(model="X2D", nozzle_mm=0.8) == "_X2D08"
    assert setting_suffix(model="X2D", nozzle_mm=0.2) == "_X2D02"


def test_profile_basename_and_filename():
    bn = profile_basename(vendor="ZIRO",
                          material_name="Twinkle PLA Silk",
                          model="X1C", nozzle_mm=0.4)
    assert bn == "ZIRO Twinkle PLA Silk @BBL X1C 0.4 nozzle"
    assert profile_filename(vendor="ZIRO",
                            material_name="Twinkle PLA Silk",
                            model="X1C", nozzle_mm=0.4) == bn + ".json"


# --- material patches -------------------------------------------------

def test_apply_material_pla_is_noop():
    p = _stub_template()
    snap = copy.deepcopy(p)
    apply_material(p, "PLA")
    assert p == snap


def test_apply_material_petg_flips_type_and_retraction():
    p = _stub_template()
    apply_material(p, "PETG")
    assert p["filament_type"] == ["PETG"]
    assert p["filament_density"] == ["1.27"]
    # The DD-short / Bowden-default retraction pattern is the
    # signature of the PETG override.
    assert p["filament_retraction_length"] == ["0.4", "0.4", "nil", "nil"]


def test_apply_material_tpu_sets_slow_down_min_speed():
    p = _stub_template()
    apply_material(p, "TPU")
    assert p["filament_type"] == ["TPU"]
    # TPU must throttle; check the explicit min-speed fan-out.
    assert p["slow_down_min_speed"] == ["10", "10", "10", "10"]


def test_apply_material_unknown_raises():
    with pytest.raises(ValueError, match="unknown material"):
        apply_material(_stub_template(), "PEEK")


# --- temp patch -------------------------------------------------------

def test_patch_temps_fans_nozzle_across_four_variants():
    p = _stub_template()
    patch_temps(p, nozzle=210, n_low=190, n_high=220, bed=55,
                cool_plate=35)
    assert p["nozzle_temperature"] == ["210", "210", "210", "210"]
    assert p["nozzle_temperature_initial_layer"] == ["210"] * 4
    assert p["nozzle_temperature_range_low"] == ["190"]
    assert p["nozzle_temperature_range_high"] == ["220"]
    assert p["hot_plate_temp"] == ["55"]
    assert p["cool_plate_temp"] == ["35"]


def test_patch_temps_skips_cool_plate_when_none():
    p = _stub_template()
    patch_temps(p, nozzle=240, n_low=230, n_high=260, bed=80,
                cool_plate=None)
    assert "cool_plate_temp" not in p


# --- retarget_profile -------------------------------------------------

def test_retarget_replaces_compatible_printers():
    p = _stub_template()  # starts as X2D 0.4
    retarget_profile(p, model="X1C", nozzle_mm=0.4)
    assert p["compatible_printers"] == ["Bambu Lab X1C 0.4 nozzle"]


def test_retarget_rewrites_name_at_BBL_tag():
    p = _stub_template()
    p["name"] = "Bambu PLA Basic @BBL X2D 0.4 nozzle"
    retarget_profile(p, model="P1S", nozzle_mm=0.6)
    assert p["name"] == "Bambu PLA Basic @BBL P1S 0.6 nozzle"


def test_retarget_swaps_setting_id_suffix_in_place():
    """The fix for the duplicate-suffix bug: retargeting a profile
    whose setting_id already ends with `_X2D04` to X1C 0.6 should give
    `_X1C06`, NOT `_X2D04_X1C06`."""
    p = _stub_template()
    p["setting_id"] = "GFSL_ZIRO_TWINKLE_X2D04"
    retarget_profile(p, model="X1C", nozzle_mm=0.6)
    assert p["setting_id"] == "GFSL_ZIRO_TWINKLE_X1C06"


def test_retarget_appends_suffix_when_setting_id_has_none():
    p = _stub_template()
    p["setting_id"] = "GFCUSTOM_BARE"
    retarget_profile(p, model="X1C", nozzle_mm=0.4)
    assert p["setting_id"] == "GFCUSTOM_BARE_X1C04"


def test_retarget_idempotent():
    """Retargeting to the same printer/nozzle twice produces the same
    profile — no suffix doubling, no stale `name` fragment."""
    p = _stub_template()
    retarget_profile(p, model="X1C", nozzle_mm=0.4)
    snap = copy.deepcopy(p)
    retarget_profile(p, model="X1C", nozzle_mm=0.4)
    assert p == snap


def test_retarget_rejects_unknown_model():
    with pytest.raises(ValueError, match="unknown printer model"):
        retarget_profile(_stub_template(), model="ENDER3", nozzle_mm=0.4)


def test_retarget_rejects_unsupported_nozzle_size():
    with pytest.raises(ValueError, match="unsupported nozzle size"):
        retarget_profile(_stub_template(), model="X1C", nozzle_mm=0.5)


# --- build_profile (top-level) ----------------------------------------

def test_build_profile_targets_requested_model_and_nozzle():
    p = build_profile(
        template=_stub_template(),
        name="ZIRO Twinkle PLA Silk @BBL X2D 0.4 nozzle",
        filename="ZIRO Twinkle PLA Silk @BBL X2D 0.4 nozzle.json",
        vendor="ZIRO", material="PLA",
        nozzle=210, nozzle_low=190, nozzle_high=220,
        bed=55, cool_plate=35,
        density=1.32, max_vol=8, hrc=4,
        setting_id="GFSL_ZIRO_TWINKLE_X2D04",
        filament_id="GFL_ZIRO_TWINKLE",
        description="ZIRO Twinkle (Flower Series) sparkle+gradient PLA Silk.",
        cost=24.99,
        model="X1C", nozzle_mm=0.4,   # retarget away from X2D
    )
    assert p["compatible_printers"] == ["Bambu Lab X1C 0.4 nozzle"]
    assert "@BBL X1C 0.4 nozzle" in p["name"]
    assert p["setting_id"].endswith("_X1C04")
    assert p["setting_id"].count("_X1C04") == 1
    # And the recipe-specific fields landed:
    assert p["filament_vendor"] == ["ZIRO"]
    assert p["filament_density"] == ["1.32"]
    assert p["nozzle_temperature"] == ["210"] * 4


def test_supported_models_includes_real_lineup():
    """Catches accidental deletion of a printer model — the landing
    page + docs both advertise this list, so it's load-bearing."""
    for m in ("X1C", "P1S", "A1", "A1mini", "X2D", "H2D"):
        assert m in PRINTER_MODELS, f"{m} missing from PRINTER_MODELS"
    assert 0.4 in NOZZLE_SIZES_MM
