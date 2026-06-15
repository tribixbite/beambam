"""beambam.print_job — LAN print-command machinery.

Phase 5d sub-batch (`docs/BRIDGE_SPLIT_PLAN.md`). Hosts the
print-execution helpers extracted from x2d_bridge.py:

  PrintRefusal                       — exception raised on safety-gate refusal
  _md5_of(local_path)                — file-hash for firmware verification
  _filament_class(s)                 — coarse PLA/PETG/ABS family bucket
  _BED_NAME_TO_MQTT / _BED_TEMP_KEY_BY_MQTT — bed-type enum + temp-key maps
  _derive_print_params_from_3mf(...) — read bed/temp/filament from a sliced 3MF
  _validate_ams_slot(...)            — hard+soft AMS-slot compatibility check
  start_print(client, name, ...)     — build + publish the project_file MQTT

These are pure functions / static data — they own no global state. The
only external dep is `X2DClient` for the `client.publish(...)` and
`client.creds.serial` accesses in start_print.

x2d_bridge.py re-exports every name so existing callers
(`from x2d_bridge import start_print` in lan_print.py, beambam.simulate,
beambam.printer, tests/test_ams_mapping.py, etc.) keep working
unchanged.
"""
from __future__ import annotations

import hashlib
import json as _json
import time
import zipfile as _zf
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beambam.mqtt import X2DClient


class PrintRefusal(Exception):
    """A print command was refused for safety reasons (3MF mismatch,
    empty AMS slot, wrong filament class, etc.). Inherits from
    Exception (NOT BaseException like SystemExit) so worker threads in
    serve-mode and the queue dispatcher can catch+surface the error
    without tearing down the host process."""


def _md5_of(local_path: Path) -> str:
    """Hex MD5 of a local file. Bambu firmware (Jan-2025+) verifies this
    against the uploaded .3mf before queuing the print — without it
    project_file is silently rejected on X2D / H2D / refreshed X1C."""
    h = hashlib.md5()
    with local_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _inner_gcode_md5(local_path: Path, plate_idx: int = 1) -> str:
    """Hex MD5 of the INNER `Metadata/plate_<N>.gcode` inside a .gcode.3mf.

    This — NOT the md5 of the whole .3mf zip — is what the firmware's
    `print.project_file` `md5` field must carry (verified live on the X2D: the
    wrong md5 trips the file-verify stage → err_code 84033544). The slicer
    already stores it alongside the gcode as `Metadata/plate_<N>.gcode.md5`; we
    prefer that, falling back to hashing the extracted gcode. Uppercase hex to
    match the slicer's stored form + the captured BS-Windows LAN wire."""
    import zipfile as _zfm
    inner = f"Metadata/plate_{plate_idx}.gcode"
    try:
        with _zfm.ZipFile(local_path) as z:
            try:
                return z.read(inner + ".md5").decode("ascii", "ignore").strip().upper()
            except KeyError:
                return hashlib.md5(z.read(inner)).hexdigest().upper()
    except (_zfm.BadZipFile, KeyError, OSError) as e:
        raise PrintRefusal(
            f"cannot read {inner} from {local_path.name} for md5: {e}")


def _signing_user_id() -> str:
    """`user_id` embedded in the signed pre-image — read from the LOCAL cloud
    session file (no network). The signed scheme hashes
    `{"<family>":<cmd>,"user_id":"<uid>"}`; the uid is just a value in that
    blob, so reading it locally keeps a LAN print fully offline."""
    import json
    import os
    try:
        j = json.loads((Path.home() / ".x2d" / "cloud_session.json").read_text())
        if j.get("user_id"):
            return str(j["user_id"])
    except Exception:                                       # noqa: BLE001
        pass
    return os.environ.get("X2D_USER_ID", "")


def _publish_signed_or_legacy(client: "X2DClient", payload: dict) -> None:
    """Publish a `print.*` payload with the right signature for this firmware.

    Jan-2025+ X2D/H2D firmware verifies `print.*` against a Bambu-CA-issued cert.
    If the recovered per-install key is present (`~/.x2d/printer_sign_key.pem` +
    `printer_cert_id.txt`) we sign the command with it (`mqtt_sign`) and publish
    the raw wire — the only signature the printer accepts for print control. The
    legacy `X2DClient.publish` path signs with the leaked Bambu Connect cert,
    which the firmware rejects for `print.*` (err 84033545). Falls back to it
    only when the recovered key is absent (older firmware)."""
    key_path = Path.home() / ".x2d" / "printer_sign_key.pem"
    cid_path = Path.home() / ".x2d" / "printer_cert_id.txt"
    # Take the signed-raw path only when the recovered key is present AND the
    # client exposes a real paho `.client` (raw publish). Test/simulate clients
    # capture via their high-level `publish()` and have no `.client`.
    if (not (key_path.is_file() and cid_path.is_file())
            or getattr(client, "client", None) is None):
        client.publish(payload)
        return
    from beambam import mqtt_sign
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key = load_pem_private_key(key_path.read_bytes(), None)
    cert_id = cid_path.read_text().strip()
    family = next(iter(payload))                            # "print"
    command = payload[family]                               # already has seq+ts
    wire = mqtt_sign.signed_message(family, command, _signing_user_id(),
                                    signer=mqtt_sign.key_signer(key), cert_id=cert_id)
    topic = f"device/{client.creds.serial}/request"
    client.client.publish(topic, wire, qos=1)


def request_auto_nozzle_mapping(client: "X2DClient", group_info: list[dict], *,
                                version: int = 1, wait: float = 8.0):
    """Ask a dual-nozzle X2D/H2D to AUTO-compute the filament→nozzle mapping —
    the "automatic switch" (`get_auto_nozzle_mapping`, `DevMappingNozzle.cpp`).

    `group_info` is one entry per used nozzle group, derived from the .gcode.3mf
    (`filament_map` / `nozzle_diameter` / `nozzle_volume_type`):
    `{"id": group_id, "ext": extruder_id+1, "dia": float, "vol": "Standard"|"High Flow"|"TPU Flow"}`.
    The printer replies with a `mapping` array (one assigned nozzle per group)
    which becomes the project_file's `nozzle_mapping`. Signed (print.* tier);
    requires the recovered key + a connected client.

    Returns the `mapping` list, or None on timeout. Verified live on the X2D
    (`result:success`). NOTE: the *full* X2D dual-nozzle project_file field set
    that consumes this mapping is NOT yet reproduced — a beambam-built
    project_file still returns err 84033544 at the task-verify stage. The exact
    fields are only emitted by the cloud / BS-desktop closed networking lib and
    are absent from any captured Handy traffic (the cloud, not Handy, publishes
    the project_file). Capture the cloud's publish to `device/<serial>/request`
    to finish this.  # TODO(lan-print): complete the project_file field set.
    """
    import json
    import time
    key_path = Path.home() / ".x2d" / "printer_sign_key.pem"
    cid_path = Path.home() / ".x2d" / "printer_cert_id.txt"
    if not (key_path.is_file() and cid_path.is_file()
            and getattr(client, "client", None) is not None):
        return None
    from beambam import mqtt_sign
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key = load_pem_private_key(key_path.read_bytes(), None)
    cert_id = cid_path.read_text().strip()
    now = int(time.time() * 1000)
    cmd = {"command": "get_auto_nozzle_mapping", "sequence_id": str(now),
           "timestamp": now, "group_info": group_info, "version": int(version)}
    wire = mqtt_sign.signed_message("print", cmd, _signing_user_id(),
                                    signer=mqtt_sign.key_signer(key), cert_id=cert_id)
    result: dict = {}
    prev = client.client.on_message

    def _on(c, d, m):
        try:
            p = json.loads(m.payload).get("print", {})
        except Exception:                                  # noqa: BLE001
            return
        if isinstance(p, dict) and p.get("command") == "get_auto_nozzle_mapping" \
                and "mapping" in p:
            result["mapping"] = p["mapping"]

    client.client.on_message = _on
    client.client.subscribe(f"device/{client.creds.serial}/report", qos=1)
    time.sleep(0.8)
    client.client.publish(f"device/{client.creds.serial}/request", wire, qos=1)
    deadline = time.time() + wait
    while time.time() < deadline and "mapping" not in result:
        time.sleep(0.2)
    client.client.on_message = prev
    return result.get("mapping")


def _filament_class(s: str) -> str:
    """Collapse a Bambu-style filament_type string to its class prefix
    for compatibility comparison. PLA / PLA Silk / PLA-CF -> 'PLA';
    PETG / PETG-CF -> 'PETG'; ABS / ABS-GF -> 'ABS'. Different chemistries
    (PLA vs PETG vs ABS vs PA vs PA6 vs TPU vs ASA) still mismatch —
    that's the danger case the AMS slot validator must refuse on.

    Per BambuStudio's setting_id_to_type() (DeviceManager.cpp:2629) the
    AMS push-status reports a coarse class string while a 3MF's
    filament_type can be the longer family name; comparing class prefix
    avoids false-positive refusals on chemically identical spools."""
    token = s.replace("-", " ").replace("_", " ").strip().split(" ", 1)[0]
    return token.upper()


# ---------------------------------------------------------------------------
# Safety derivation: the .gcode.3mf is the slicer's contract — bed_type +
# bed_temp + filament_type/colour come from project_settings.config and
# MUST match what we send to the firmware. Trusting CLI defaults
# ("--bed-type textured_plate --bed-temp 65") when the slice was prepared
# for a different plate has two failure modes:
#   1) firmware silently drops the project_file command on bed_type vs
#      curr_bed_type mismatch (observed live on X2D firmware 01.02.x —
#      the print never starts and there is no HMS to surface the reason)
#   2) if firmware did accept, the wrong heat profile drives the wrong
#      adhesive (e.g. 65°C on SuperTack silicone-rubber every print is
#      damaging long-term; 35°C on PEI wastes a layer of warpage).
# `_derive_print_params_from_3mf` is the safe-by-default replacement for
# the CLI defaults — cmd_print refuses to send if user-supplied values
# disagree with the slice unless --force is passed.
# ---------------------------------------------------------------------------

# Mirrors `bed_type_to_gcode_string()` in BambuStudio's PrintConfig.hpp
# combined with `s_keys_map_BedType` in PrintConfig.cpp — the human-readable
# `curr_bed_type` enum_value seen in 3MFs to the lowercase MQTT enum string
# the firmware expects in `print.project_file.bed_type`.
_BED_NAME_TO_MQTT = {
    "Cool Plate":         "cool_plate",
    "Engineering Plate":  "eng_plate",
    "High Temp Plate":    "hot_plate",
    "Textured PEI Plate": "textured_plate",
    "Supertack Plate":    "supertack_plate",
}
_BED_TEMP_KEY_BY_MQTT = {
    "cool_plate":      "cool_plate_temp_initial_layer",
    "eng_plate":       "eng_plate_temp_initial_layer",
    "hot_plate":       "hot_plate_temp_initial_layer",
    "textured_plate":  "textured_plate_temp_initial_layer",
    "supertack_plate": "supertack_plate_temp_initial_layer",
}


def _derive_print_params_from_3mf(local_path: Path,
                                  filament_index: int = 0) -> dict:
    """Pull authoritative bed_type / bed_temp / filament expectations
    from the .gcode.3mf so cmd_print sends exactly what the slicer
    intended. `filament_index` selects the per-slot value when the
    project has multiple filaments; defaults to 0 (single-color)."""
    if not local_path.is_file():
        raise PrintRefusal(f"3MF not found locally: {local_path}")
    try:
        with _zf.ZipFile(local_path) as z:
            try:
                ps_bytes = z.read("Metadata/project_settings.config")
            except KeyError:
                raise PrintRefusal(
                    f"3MF {local_path.name} missing Metadata/"
                    f"project_settings.config — cannot derive print "
                    f"params; re-slice with x2d_slice.py.")
    except _zf.BadZipFile:
        raise PrintRefusal(f"{local_path} is not a valid 3MF (zip)")
    try:
        ps = _json.loads(ps_bytes.decode("utf-8", errors="replace"))
    except _json.JSONDecodeError as e:
        raise PrintRefusal(
            f"3MF project_settings.config malformed: {e}")

    bed_name = ps.get("curr_bed_type")
    bed_type = _BED_NAME_TO_MQTT.get(bed_name)
    if not bed_type:
        raise PrintRefusal(
            f"3MF curr_bed_type {bed_name!r} not in supported plates "
            f"{sorted(_BED_NAME_TO_MQTT)}. Re-slice with --bed and one "
            f"of cool|engineering|high_temp|textured|supertack.")

    temps = ps.get(_BED_TEMP_KEY_BY_MQTT[bed_type]) or []
    if not isinstance(temps, list) or filament_index >= len(temps):
        raise PrintRefusal(
            f"3MF {_BED_TEMP_KEY_BY_MQTT[bed_type]} missing index "
            f"{filament_index}: {temps!r}; refusing to derive "
            f"bed_temp.")
    raw = str(temps[filament_index]).strip()
    if not raw or not raw.isdigit():
        raise PrintRefusal(
            f"3MF {_BED_TEMP_KEY_BY_MQTT[bed_type]}[{filament_index}] "
            f"= {raw!r}; non-numeric or zero — refusing to send. "
            f"Re-slice (x2d_slice.py --bed bakes in PLA-compatible "
            f"defaults).")
    bed_temp = int(raw)

    fila_types  = ps.get("filament_type")     or []
    fila_colour = ps.get("filament_colour")   or []
    fila_ids    = ps.get("filament_ids")      or []
    return {
        "bed_type": bed_type,
        "bed_temp": bed_temp,
        "expected_filament_type":   (fila_types[filament_index]
                                     if filament_index < len(fila_types)
                                     else None),
        "expected_filament_colour": (fila_colour[filament_index]
                                     if filament_index < len(fila_colour)
                                     else None),
        "expected_filament_id":     (fila_ids[filament_index]
                                     if filament_index < len(fila_ids)
                                     else None),
    }


def _validate_ams_slot(state: dict, ams_global_slot: int,
                       expected: dict, *, force: bool) -> None:
    """Refuse to send a use_ams=True print when the named AMS slot is empty
    or carries an incompatible filament type. Bambu firmware does NOT
    validate this server-side — without this guard the toolhead will
    extrude air or attempt to extrude e.g. PETG with PLA hotend ramp.

    Hard checks (always enforced, --force NEVER bypasses):
      - slot index in 0..15
      - referenced AMS exists and that slot's tray_info_idx is non-empty
      - tray_type ∈ {expected_filament_type, ''} or compatible class

    Soft checks (warn if --force, refuse without it):
      - filament brand/id mismatch (e.g. Bambu PLA Silk ≠ Generic PLA Silk
        — same chemistry, different settings profile).
    """
    import sys
    if ams_global_slot < 0 or ams_global_slot > 15:
        raise PrintRefusal(
            f"AMS slot {ams_global_slot} out of range 0..15")
    ams_id  = ams_global_slot // 4
    slot_id = ams_global_slot %  4

    p = state.get("print", {}) if isinstance(state, dict) else {}
    ams_root = p.get("ams") or {}
    ams_list = ams_root.get("ams") or []
    target_ams = next(
        (a for a in ams_list if a.get("id") in (ams_id, str(ams_id))),
        None)
    if target_ams is None:
        raise PrintRefusal(
            f"Live state shows no AMS#{ams_id} attached to printer "
            f"(found AMS ids: {[a.get('id') for a in ams_list]}). "
            f"Refusing to send use_ams=True with slot {ams_global_slot}.")
    trays = target_ams.get("tray") or []
    target_tray = next(
        (t for t in trays if t.get("id") in (slot_id, str(slot_id))),
        None)
    if target_tray is None:
        raise PrintRefusal(
            f"AMS#{ams_id} has no slot {slot_id}. Refusing to send.")

    tray_info_idx = (target_tray.get("tray_info_idx") or "").strip()
    tray_type     = (target_tray.get("tray_type")     or "").strip()
    tray_color    = (target_tray.get("tray_color")    or "").strip()
    if not tray_info_idx:
        raise PrintRefusal(
            f"AMS#{ams_id} slot {slot_id} (global {ams_global_slot}) is "
            f"EMPTY (tray_info_idx is blank). use_ams=True with an empty "
            f"slot would run the toolhead dry. Load filament or pass "
            f"--no-ams to use the external spool. (NOT bypassable with "
            f"--force.)")

    exp_type = (expected.get("expected_filament_type") or "").strip()
    if not exp_type:
        # Per code-review #7: refuse rather than skip the filament-type
        # gate. A 3MF with no filament_type is malformed; sending it
        # blind risks loading wrong material into the wrong hotend ramp.
        raise PrintRefusal(
            f"3MF declared no filament_type — cannot validate AMS slot "
            f"{ams_global_slot} ({tray_type!r}) against print intent. "
            f"Re-slice with x2d_slice.py. (NOT bypassable with --force.)")
    if (tray_type and
            _filament_class(exp_type) != _filament_class(tray_type)):
        raise PrintRefusal(
            f"AMS slot {ams_global_slot} loaded with {tray_type!r} "
            f"(class {_filament_class(tray_type)}) but the 3MF expects "
            f"{exp_type!r} (class {_filament_class(exp_type)}). Hotend "
            f"ramp profile would be wrong — different filament classes "
            f"have different melt temps. Re-slice with the correct "
            f"filament_type or load matching material into slot "
            f"{ams_global_slot}. (NOT bypassable with --force.)")

    # Soft checks below — bypassable with --force, warn loudly otherwise.
    soft_warnings: list[str] = []
    exp_id = (expected.get("expected_filament_id") or "").strip()
    if exp_id and tray_info_idx and exp_id != tray_info_idx:
        soft_warnings.append(
            f"filament brand mismatch: 3MF profile id {exp_id!r} ≠ slot "
            f"{tray_info_idx!r} (same {tray_type or 'type'}, different "
            f"flow/temp profile)")
    exp_colour = (
        (expected.get("expected_filament_colour") or "")
        .lstrip("#").upper())
    slot_colour = tray_color.upper()
    if exp_colour and slot_colour and exp_colour[:6] != slot_colour[:6]:
        soft_warnings.append(
            f"colour mismatch: 3MF wants #{exp_colour[:6]} but slot has "
            f"#{slot_colour[:6]} (visual only)")

    if soft_warnings:
        msg = ("AMS slot soft mismatch(es):\n  - "
               + "\n  - ".join(soft_warnings))
        if force:
            sys.stderr.write(
                f"[print] WARN: {msg}\n"
                f"[print] continuing under --force\n")
        else:
            raise PrintRefusal(
                f"{msg}\nPass --force to ignore (or re-slice / "
                f"swap spool).")


def start_print(client: "X2DClient", gcode_filename: str, *,
                use_ams: bool = True,
                ams_slot: int | list[int] = 0,
                bed_levelling: bool = True, flow_cali: bool = False,
                timelapse: bool = False, vibration_cali: bool = False,
                bed_type: str | None = None,
                bed_temp: int | None = None,
                local_path: Path | None = None) -> None:
    """Submit a project_file print command to the printer.

    Payload now matches the full Jan-2025+ firmware-required shape captured
    from real cloud + Windows BambuStudio LAN sessions. Earlier reduced
    shapes (bambulabs_api 2.6.6 / orca-lan-bridge minimal) get silently
    dropped on X2D / H2D — the firmware's command handler does shape
    validation before logging, missing required keys = drop without HMS.

    Required fields the older shapes were missing (per drndos/openspoolman
    A1 cloud captures and DeepWiki's BambuStudio PrintParams reverse-engineering):
      * `dev_id`              — device serial; binds the publish to this printer
      * `task_id` / `subtask_id` / `subtask_name` / `job_id` — task identity
      * `project_id` / `profile_id` / `design_id` / `model_id` / `plate_idx`
      * `md5`                 — MD5 of the INNER `plate_<N>.gcode` (not the .3mf zip)
      * `timestamp`           — unix seconds
      * `job_type`            — 0 for LAN local, 1 for cloud
      * `bed_temp`            — int degrees C
      * `auto_bed_leveling`   — int (0/1/2), NOT `bed_leveling` bool
      * `extrude_cali_flag`   — int 0
      * `nozzle_offset_cali`  — int 0
      * `extrude_cali_manual_mode` — int 0
      * `ams_mapping2`        — newer `[{"ams_id": 0, "slot_id": N}]` form
      * `cfg`                 — string "0"

    URL scheme: the .gcode.3mf is FTP-uploaded into the printer's `cache/` dir
    and referenced as `ftp:///cache/<name>` — exactly what the Windows BS LAN
    flow does (verified from a captured wire diff). A root-level `ftp:///<name>`
    is NOT found by the firmware (file-verify stage → err 84033544)."""
    if local_path is None:
        local_path = Path.cwd() / gcode_filename
    md5_hex = _inner_gcode_md5(local_path) if local_path.is_file() else ""

    # Safety: derive bed_type / bed_temp from the .gcode.3mf when callers
    # don't pass them. The pre-2026-05 defaults ("textured_plate" / 65 °C)
    # would silently misheat SuperTack PLA (35 °C) or Cool Plate PLA
    # (35 °C) — SuperTack's silicone-rubber surface degrades faster at
    # sustained 60+ °C, and the firmware silently DROPS project_file
    # commands when MQTT bed_type disagrees with the slice's
    # curr_bed_type. The 3MF is the slicer's authoritative contract.
    # Per code-review #5/#6 we no longer fall through to a hardcoded PLA
    # bed-temp table — that masked malformed 3MFs and would have driven
    # PETG-on-hot_plate at 55 °C instead of 80 °C, detaching mid-print.
    # If we cannot derive AND the caller didn't supply, we REFUSE.
    if bed_type is None or bed_temp is None:
        if not local_path.is_file():
            raise PrintRefusal(
                f"start_print: bed_type/bed_temp not supplied AND no "
                f"local 3MF available at {local_path} to auto-derive. "
                f"Refusing to send a print command without a known "
                f"plate / heat profile. Pass bed_type/bed_temp "
                f"explicitly or wire local_path.")
        # If this raises (malformed 3MF, unknown bed enum, etc.) we
        # let it propagate — better to refuse loudly than fall back
        # to a guess.
        _d = _derive_print_params_from_3mf(local_path,
                                            filament_index=0)
        if bed_type is None:
            bed_type = _d["bed_type"]
        if bed_temp is None:
            bed_temp = _d["bed_temp"]
    # ams_mapping (legacy flat int list) + ams_mapping2 (newer
    # ams_id/slot_id form) together cover every firmware path. When
    # use_ams is false both must be empty.
    #
    # Multi-color / multi-extruder support: ams_slot can be a single int
    # (single-filament print, one mapping) OR a list of ints — one per
    # filament index in the 3MF (e.g. [1, 5] = filament 0 -> AMS0 slot 1
    # green, filament 1 -> AMS1 slot 1 white). The slot ordinal is the
    # global one (ams_id*4 + slot_id) so it works on multi-AMS setups
    # transparently. Verified payload shape against captured BS-Windows
    # multi-color X2D run (2-filament) by exporting a 2-color 3MF and
    # comparing dry-run output to the wire capture.
    if use_ams:
        slots = ([ams_slot] if isinstance(ams_slot, int)
                 else list(ams_slot))
        if not slots:
            raise ValueError(
                "use_ams=True requires at least one ams_slot")
        ams_mapping_legacy = list(slots)
        ams_mapping_v2 = [
            {"ams_id": s // 4, "slot_id": s % 4} for s in slots
        ]
    else:
        ams_mapping_legacy = []
        ams_mapping_v2 = []
    # Task identity numbers — must be numeric-looking 9-10 digit IDs
    # (not "0") so the firmware's command handler accepts them. Use
    # timestamp so each invocation gets a fresh ID and we don't collide
    # with stale state. Captured cloud BS-Windows payloads use the same
    # shape.
    job_id_int = int(time.time()) * 10
    job_id_str = str(job_id_int)
    # subtask_name is the printer-side label that appears on the
    # touchscreen. Bambu's Files-screen convention is `<basename>.gcode`
    # (NOT `.gcode.3mf` — the firmware strips the .3mf when listing).
    # Verified from the X2D's own `subtask_name` field for its prior
    # print: 'mira_frame.gcode' (no .3mf suffix).
    name_no_3mf = gcode_filename
    if name_no_3mf.endswith(".gcode.3mf"):
        name_no_3mf = name_no_3mf[: -len(".3mf")]
    elif name_no_3mf.endswith(".3mf"):
        name_no_3mf = name_no_3mf[: -len(".3mf")] + ".gcode"
    payload = {
        "print": {
            "sequence_id":              str(int(time.time())),
            "command":                  "project_file",
            "param":                    "Metadata/plate_1.gcode",
            "file":                     gcode_filename,
            "url":                      f"ftp:///cache/{gcode_filename}",
            "md5":                      md5_hex,
            # Task identity — firmware insists these are present even
            # for LAN. Numeric-looking strings; same value across the
            # trio.
            "task_id":                  job_id_str,
            "subtask_id":               job_id_str,
            "subtask_name":             name_no_3mf,
            "job_id":                   job_id_int,
            "project_id":               job_id_str,
            "profile_id":               "0",
            "design_id":                "0",
            "model_id":                 "0",
            "plate_idx":                1,         # int, NOT string
            "dev_id":                   client.creds.serial,
            # 0 = LAN local, 1 = cloud
            "job_type":                 0,
            "timestamp":                int(time.time()),
            # Plate / heating
            "bed_type":                 bed_type,
            "bed_temp":                 int(bed_temp),
            "auto_bed_leveling":        1 if bed_levelling else 0,
            # Calibration int flags (0 = don't calibrate)
            "extrude_cali_flag":        1 if flow_cali else 0,
            "nozzle_offset_cali":       0,
            "extrude_cali_manual_mode": 0,
            # Print-time toggles (kept for forward-compat with older
            # paths in firmware that still read these names)
            "flow_cali":                bool(flow_cali),
            "bed_leveling":             bool(bed_levelling),
            "vibration_cali":           bool(vibration_cali),
            "timelapse":                bool(timelapse),
            "layer_inspect":            False,
            # AMS mapping — both legacy and v2 forms; firmware reads
            # whichever
            "use_ams":                  bool(use_ams),
            "ams_mapping":              ams_mapping_legacy,
            "ams_mapping2":             ams_mapping_v2,
            "skip_objects":             None,
            "cfg":                      "0",
        }
    }
    _publish_signed_or_legacy(client, payload)
