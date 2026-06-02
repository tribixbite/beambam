#!/usr/bin/env python3
"""Register the 15 flat X2D filament profiles as cloud user presets.

Hits Bambu's reverse-engineered preset endpoint:
    POST  /v1/iot-service/api/slicer/setting       create  (returns PFUS<hex>)
    GET   /v1/iot-service/api/slicer/setting/<id>  read
    DELETE /v1/iot-service/api/slicer/setting/<id> remove

Body schema (top-level):
    name        — user-facing preset name
    type        — "filament" | "process" | "printer"
    version     — slicer version string (e.g. "01.10.00.69")
    base_id     — parent's setting_id (e.g. "GFSA00_08")
    setting     — dict of Bambu-serialized strings (per ConfigOption::serialize)

After a successful run, writes flat-profiles/cloud-setting-ids.json mapping
each profile filename to its returned PFUS<hex> setting_id.

Run from repo root: `python3 tools/register_cloud_presets.py`
Add --dry-run to preview without hitting the API.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from cloud_client import CloudClient, _request, REGIONS  # noqa: E402

FLAT_DIR = REPO / "flat-profiles"
OUT_MAP = FLAT_DIR / "cloud-setting-ids.json"
VERSION = "01.10.00.69"

# Map per-profile filename → parent's setting_id (Bambu library, X2D 0.4 nozzle).
# Picked from the closest material/process family. Determined by inspecting
# BambuStudio/resources/profiles/BBL/filament/*.json setting_id values.
BASE_ID_BY_PROFILE: dict[str, tuple[str, str]] = {
    # filename → (base_id, parent_preset_name)
    "ZIRO Twinkle PLA Silk @BBL X2D 0.4 nozzle.json":
        ("GFSA05_14", "Bambu PLA Silk @BBL X2D 0.4 nozzle"),
    "XZN PETG @BBL X2D 0.4 nozzle.json":
        ("GFSG99_15", "Generic PETG @BBL X2D 0.4 nozzle"),
    "ZIRO TPU 95A Color-Change @BBL X2D 0.4 nozzle.json":
        ("GFSU99_03", "Generic TPU @BBL X2D 0.4 nozzle"),
    "eSUN PETG Basic @BBL X2D 0.4 nozzle.json":
        ("GFSG96_14", "Generic PETG HF @BBL X2D 0.4 nozzle"),
    "Toksen3D PETG @BBL X2D 0.4 nozzle.json":
        ("GFSG99_15", "Generic PETG @BBL X2D 0.4 nozzle"),
    "SUNLU PETG Glow @BBL X2D 0.4 nozzle.json":
        ("GFSG96_14", "Generic PETG HF @BBL X2D 0.4 nozzle"),
    "OVERTURE PLA @BBL X2D 0.4 nozzle.json":
        ("GFSA00_08", "Bambu PLA Basic @BBL X2D 0.4 nozzle"),
    "eSUN PLA+ @BBL X2D 0.4 nozzle.json":
        ("GFSA00_08", "Bambu PLA Basic @BBL X2D 0.4 nozzle"),
    "FLASHFORGE PLA @BBL X2D 0.4 nozzle.json":
        ("GFSA00_08", "Bambu PLA Basic @BBL X2D 0.4 nozzle"),
    "FLASHFORGE Rapid PLA @BBL X2D 0.4 nozzle.json":
        ("GFSL95_16", "Generic PLA High Speed @BBL X2D 0.4 nozzle"),
    "FLASHFORGE Rapid PLA Glow @BBL X2D 0.4 nozzle.json":
        ("GFSL95_16", "Generic PLA High Speed @BBL X2D 0.4 nozzle"),
    "eSUN TPU 95A @BBL X2D 0.4 nozzle.json":
        ("GFSU99_03", "Generic TPU @BBL X2D 0.4 nozzle"),
    "FLASHFORGE Silk Dual @BBL X2D 0.4 nozzle.json":
        ("GFSA05_14", "Bambu PLA Silk @BBL X2D 0.4 nozzle"),
    "eSUN PLA Silk @BBL X2D 0.4 nozzle.json":
        ("GFSL96_06", "Generic PLA Silk @BBL X2D 0.4 nozzle"),
    "eSUN Silk Metal PLA @BBL X2D 0.4 nozzle.json":
        ("GFSL96_06", "Generic PLA Silk @BBL X2D 0.4 nozzle"),
}

# Fields from our flat profile JSON that go into the cloud `setting` dict.
# Their flat shape (list-of-strings, even singletons) is converted to the
# Bambu-serialize format per ConfigOption::serialize:
#   ConfigOptionStrings  ["x","y"]   → "\"x\";\"y\""   (quoted, semicolon-joined)
#   ConfigOptionFloats   ["1.27"]    → "1.27"          (unquoted; multi → "1;2")
#   ConfigOptionInts     ["230"]     → "230"           (same as floats)
#   ConfigOptionBool     ["1"]       → "1"
QUOTED_STRING_FIELDS = {
    # ConfigOptionStrings — values wrapped in literal double quotes.
    "filament_type", "filament_vendor", "filament_extruder_variant",
    "filament_z_hop_types", "filament_metal_stickiness",
    "filament_scarf_seam_type", "filament_settings_id", "compatible_printers",
    "cooling_slowdown_logic",
}
# These are the keys we care about for downstream behaviour. Everything else
# stays inherited from base_id.
INCLUDED_KEYS = (
    "filament_type", "filament_vendor", "filament_density",
    "filament_flow_ratio",
    "nozzle_temperature", "nozzle_temperature_initial_layer",
    "nozzle_temperature_range_low", "nozzle_temperature_range_high",
    "hot_plate_temp", "hot_plate_temp_initial_layer",
    "textured_plate_temp", "textured_plate_temp_initial_layer",
    "cool_plate_temp", "cool_plate_temp_initial_layer",
    "eng_plate_temp", "eng_plate_temp_initial_layer",
    "supertack_plate_temp", "supertack_plate_temp_initial_layer",
    "filament_max_volumetric_speed",
    "filament_retraction_length",
    "required_nozzle_HRC",
    "temperature_vitrification",
    "slow_down_min_speed",
    "filament_extruder_variant",
)


def serialize_value(key: str, values: list[str]) -> str:
    """Convert a flat-profile array to Bambu's serialize() string form."""
    if key in QUOTED_STRING_FIELDS:
        return ";".join(f"\"{v}\"" for v in values)
    return ";".join(values)


def build_setting_dict(profile: dict, parent_name: str) -> dict[str, str]:
    out: dict[str, str] = {
        "inherits": parent_name,
        "filament_settings_id": profile.get("name", ""),
        "updated_time": "0",
    }
    for k in INCLUDED_KEYS:
        v = profile.get(k)
        if isinstance(v, list) and v:
            out[k] = serialize_value(k, v)
    return out


def short_name(profile_name: str) -> str:
    """Strip ` @BBL X2D 0.4 nozzle` for the cloud preset display name."""
    return profile_name.replace(" @BBL X2D 0.4 nozzle", "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the request bodies but don't POST.")
    ap.add_argument("--overwrite", action="store_true",
                    help="Re-register profiles already present in "
                         "cloud-setting-ids.json (deletes existing first).")
    args = ap.parse_args()

    cli = CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `beambam cloud-login`", file=sys.stderr)
        return 1
    cli._ensure_fresh()
    base = REGIONS[cli.session.region]["iot"]
    hdr = {"Authorization": f"Bearer {cli.session.access_token}"}
    url = base + "/v1/iot-service/api/slicer/setting"

    existing: dict[str, dict] = {}
    if OUT_MAP.exists():
        try:
            existing = json.loads(OUT_MAP.read_text())
        except json.JSONDecodeError:
            existing = {}

    written: dict[str, dict] = dict(existing)

    for fname, (base_id, parent_name) in BASE_ID_BY_PROFILE.items():
        path = FLAT_DIR / fname
        if not path.exists():
            print(f"  SKIP missing: {fname}", file=sys.stderr)
            continue
        profile = json.loads(path.read_text())
        preset_name = short_name(profile.get("name", path.stem))

        # If we already have a setting_id and not --overwrite, skip.
        if fname in existing and not args.overwrite:
            print(f"  SKIP already-registered: {fname} "
                  f"→ {existing[fname]['setting_id']}")
            continue

        body = {
            "name": preset_name,
            "type": "filament",
            "version": VERSION,
            "base_id": base_id,
            "setting": build_setting_dict(profile, parent_name),
        }

        if args.dry_run:
            print(f"  DRY-RUN {fname}")
            print(f"    name      : {preset_name}")
            print(f"    base_id   : {base_id}")
            print(f"    setting keys: {list(body['setting'])}")
            continue

        # If --overwrite and we have a previous setting_id, delete first to
        # avoid name conflicts (the endpoint enforces unique names per user).
        if args.overwrite and fname in existing:
            old_id = existing[fname]["setting_id"]
            try:
                _request("DELETE", f"{url}/{old_id}", headers=hdr, timeout=10)
                print(f"  deleted previous {old_id} for {fname}")
            except Exception as e:                                  # noqa: BLE001
                print(f"  (could not delete {old_id}: {e!s:.100})")

        try:
            r = _request("POST", url, body=body, headers=hdr, timeout=15)
        except Exception as e:                                      # noqa: BLE001
            print(f"  FAIL {fname}: {e!s:.200}", file=sys.stderr)
            continue

        sid = str(r.get("setting_id", ""))
        if not sid:
            print(f"  FAIL {fname}: no setting_id in response: {r}",
                  file=sys.stderr)
            continue

        written[fname] = {
            "setting_id":   sid,
            "preset_name":  preset_name,
            "base_id":      base_id,
            "parent_name":  parent_name,
            "update_time":  r.get("update_time", ""),
        }
        print(f"  OK {fname:55s} → {sid}")
        # Gentle pacing — Bambu's cloud sometimes rate-limits bursts.
        time.sleep(0.3)

    if not args.dry_run:
        OUT_MAP.write_text(json.dumps(written, indent=2) + "\n")
        print(f"\nwrote {OUT_MAP} ({len(written)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
