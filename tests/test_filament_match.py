"""tests/test_filament_match.py — model→AMS filament auto-match.

Functional prints (<3 colors) match by TYPE (color overruled); aesthetic
prints (>=3 colors) match by COLOR first.
"""
from __future__ import annotations

from beambam import filament_match as FM


# AMS loadout mirroring the real test printer (3 units, 9 loaded slots).
_SLOTS = [
    {"slot": 0, "type": "PLA",     "color": "FFFFFFFF"},   # white PLA
    {"slot": 1, "type": "PETG-CF", "color": "F72323FF"},   # red PETG-CF
    {"slot": 2, "type": "PLA",     "color": "7C4B00FF"},
    {"slot": 6, "type": "PLA",     "color": "46A8F9FF"},   # blue PLA
    {"slot": 9, "type": "PETG",    "color": "161616FF"},   # black PETG
    {"slot": 11, "type": "PLA",    "color": "2850E0FF"},   # blue PLA
]


def test_type_base_normalizes_variants():
    assert FM._type_base("PETG-CF") == "PETG"
    assert FM._type_base("PLA Matte") == "PLA"
    assert FM._type_base("TPU95A") == "TPU"
    assert FM._type_base("PETG") == "PETG"


def test_color_distance():
    assert FM._color_dist("#000000", "#000000") == 0
    assert FM._color_dist("#FFFFFF", "#000000") > FM._color_dist("#FFFFFF", "#EEEEEE")


def test_functional_matches_type_over_color():
    # Model wants WHITE PETG. White PLA (slot 0) is an exact color match but
    # wrong type; black PETG (slot 9) is the right type. Type must win.
    chosen = FM.match_slot("PETG", "#FFFFFF", _SLOTS, aesthetic=False)
    assert chosen["slot"] == 9
    assert chosen["type"] == "PETG"


def test_functional_prefers_exact_type_over_variant():
    # Both PETG (slot 9) and PETG-CF (slot 1) share base 'PETG'; exact 'PETG'
    # wins even though PETG-CF's red is no further from white than black is.
    chosen = FM.match_slot("PETG", "#FFFFFF", _SLOTS, aesthetic=False)
    assert chosen["slot"] == 9


def test_functional_overrules_color_when_no_type_match():
    # No ABS loaded → fall back to closest color (overruled), not crash/None.
    chosen = FM.match_slot("ABS", "#FFFFFF", _SLOTS, aesthetic=False)
    assert chosen is not None
    assert chosen["slot"] == 0          # white PLA = closest color to white


def test_aesthetic_matches_color_first():
    # Aesthetic: want a blue, type secondary → closest blue PLA, not the
    # exact-type-but-wrong-color slot.
    chosen = FM.match_slot("PLA", "#2850E0", _SLOTS, aesthetic=True)
    assert chosen["slot"] == 11         # exact blue


def test_is_aesthetic_threshold():
    assert FM.is_aesthetic([("PLA", "#fff")]) is False
    assert FM.is_aesthetic([("PLA", "#fff"), ("PLA", "#000")]) is False
    assert FM.is_aesthetic([("PLA", "#fff"), ("PLA", "#000"),
                            ("PLA", "#f00")]) is True


def test_model_filaments_parse():
    ps = {"filament_type": ["PETG"], "filament_colour": ["#FFFFFF"],
          "filament_settings_id": ["Bambu PETG HF @BBL A1"]}
    assert FM.model_filaments(ps) == [("PETG", "#FFFFFF")]


def test_ams_loaded_slots_from_state():
    state = {"print": {"ams": {"ams": [
        {"id": "0", "tray": [
            {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF"},
            {"id": "1", "tray_type": "", "tray_color": "808080FF"},   # empty
        ]},
        {"id": "2", "tray": [
            {"id": "1", "tray_type": "PETG", "tray_color": "161616FF"},
        ]},
    ]}}}
    slots = FM.ams_loaded_slots(state)
    assert {s["slot"] for s in slots} == {0, 9}      # empty tray skipped, global idx
    assert next(s for s in slots if s["slot"] == 9)["type"] == "PETG"


def test_auto_match_functional_real_case():
    fils = [("PETG", "#FFFFFF")]
    res = FM.auto_match(fils, _SLOTS)
    assert res["aesthetic"] is False and res["mode"] == "type-first"
    assert res["mapping"][0]["slot"] == 9


def test_auto_match_empty_ams_returns_none_mapping():
    res = FM.auto_match([("PETG", "#FFFFFF")], [])
    assert res["mapping"] == [None]
