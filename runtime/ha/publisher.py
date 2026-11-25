"""Home Assistant MQTT auto-discovery publisher (item #50).

Bridges between an x2d_bridge.py daemon's HTTP /state.events SSE feed
and a Home Assistant MQTT broker. Publishes:

* discovery configs at ``<discovery_prefix>/<component>/x2d_<serial>/<key>/config``
* per-entity state at ``x2d/<serial>/state`` (single JSON blob: the
  printer's merged report plus a ``beambam`` block of values derived in
  Python by :func:`derive_state` — dotted IP, both X2D nozzles, chamber,
  speed name, firmware, every AMS tray A1–D4 — so templates stay trivial)
* availability at ``x2d/<serial>/availability`` (online/offline)

Subscribes to:

* ``x2d/<serial>/light/set``       → POST /control/light
* ``x2d/<serial>/print/set``       → POST /control/{pause,resume,stop}
* ``x2d/<serial>/temp/<target>/set``→ POST /control/temp
* ``x2d/<serial>/ams/<label>/load`` → POST /control/ams_load (label a1..d4)
* ``x2d/<serial>/ams/unload``      → POST /control/ams_unload

The serial is the ``[printer:NAME]`` section name (or ``default`` for
the unnamed section). One HA Device per printer, with all entities
linked via the ``device.identifiers`` block.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import paho.mqtt.client as mqtt

LOG = logging.getLogger("x2d.ha")


# ---------------------------------------------------------------------------
# Entity catalogue
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Entity:
    """One HA entity. ``component`` is the HA platform (sensor / switch
    / camera / button / number). ``key`` becomes the discovery topic
    leaf and the unique_id suffix. ``value_template`` extracts the
    field from the printer's pushall JSON (Jinja-ish but evaluated by
    HA, not by us)."""
    component: str
    key: str
    name: str
    value_template: str = ""
    unit: str = ""
    device_class: str = ""
    state_class: str = ""
    icon: str = ""
    extra: dict = dataclasses.field(default_factory=dict)


# Sensor entities — one per scalar field in the printer's state.
SENSOR_ENTITIES: list[Entity] = [
    Entity("sensor", "nozzle_temp",   "Nozzle temperature",
           "{{ value_json.print.nozzle_temper | round(1) }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "nozzle_target", "Nozzle target",
           "{{ value_json.print.nozzle_target_temper | round(0) }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "bed_temp",      "Bed temperature",
           "{{ value_json.print.bed_temper | round(1) }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "bed_target",    "Bed target",
           "{{ value_json.print.bed_target_temper | round(0) }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "chamber_temp",  "Chamber temperature",
           "{{ value_json.beambam.chamber_temp }}",
           "°C", "temperature", "measurement"),
    # X2D / H2D: one sensor pair per nozzle (device.extruder.info, packed
    # current | target << 16). Single-nozzle printers report only "right".
    Entity("sensor", "nozzle_right_temp",   "Right nozzle temperature",
           "{{ value_json.beambam.nozzle_right_temp }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "nozzle_right_target", "Right nozzle target",
           "{{ value_json.beambam.nozzle_right_target }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "nozzle_left_temp",    "Left nozzle temperature",
           "{{ value_json.beambam.nozzle_left_temp }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "nozzle_left_target",  "Left nozzle target",
           "{{ value_json.beambam.nozzle_left_target }}",
           "°C", "temperature", "measurement"),
    Entity("sensor", "nozzle_right_tray",   "Right nozzle filament",
           "{{ value_json.beambam.nozzle_right_tray }}",
           "", "", "", "mdi:printer-3d-nozzle"),
    Entity("sensor", "nozzle_left_tray",    "Left nozzle filament",
           "{{ value_json.beambam.nozzle_left_tray }}",
           "", "", "", "mdi:printer-3d-nozzle"),
    Entity("sensor", "track_switch",        "Filament Track Switch",
           "{{ value_json.beambam.track_switch }}",
           "", "", "", "mdi:source-branch"),
    Entity("sensor", "progress",      "Print progress",
           "{{ value_json.print.mc_percent | int(0) }}",
           "%", "", "measurement", "mdi:progress-clock"),
    Entity("sensor", "current_layer", "Current layer",
           "{{ value_json.print.mc_current_layer | int(0) }}",
           "", "", "measurement", "mdi:layers"),
    Entity("sensor", "total_layers",  "Total layers",
           "{{ value_json.print.total_layer_num | int(0) }}",
           "", "", "measurement", "mdi:layers-outline"),
    Entity("sensor", "remaining",     "Time remaining",
           "{{ value_json.print.mc_remaining_time | int(0) }}",
           "min", "duration", "measurement", "mdi:clock-outline"),
    Entity("sensor", "wifi",          "Wi-Fi signal",
           "{{ value_json.print.wifi_signal | replace('dBm','') | trim }}",
           "dBm", "signal_strength", "measurement"),
    Entity("sensor", "filename",      "Print job",
           "{{ value_json.print.subtask_name | default('') }}",
           "", "", "", "mdi:file"),
    Entity("sensor", "stage",         "Stage",
           "{{ value_json.print.gcode_state | default(value_json.print.mc_print_sub_stage) | default('idle') }}",
           "", "", "", "mdi:state-machine"),
    # Parity with ha-bambulab: fan speeds, HMS, speed profile, timestamps,
    # firmware version, IP address, printable_objects.
    Entity("sensor", "chamber_fan_speed", "Chamber fan",
           "{{ value_json.print.big_fan1_speed | int(0) }}",
           "%", "", "measurement", "mdi:fan"),
    Entity("sensor", "aux_fan_speed",     "Aux fan",
           "{{ value_json.print.big_fan2_speed | int(0) }}",
           "%", "", "measurement", "mdi:fan"),
    Entity("sensor", "cooling_fan_speed", "Cooling fan",
           "{{ value_json.print.cooling_fan_speed | int(0) }}",
           "%", "", "measurement", "mdi:fan"),
    Entity("sensor", "heatbreak_fan_speed", "Heatbreak fan",
           "{{ value_json.print.heatbreak_fan_speed | int(0) }}",
           "%", "", "measurement", "mdi:fan"),
    Entity("sensor", "speed_profile",     "Speed profile",
           "{{ value_json.beambam.speed }}",
           "", "", "", "mdi:speedometer"),
    Entity("sensor", "hms_count",         "HMS error count",
           "{{ value_json.print.hms | length | default(0) }}",
           "", "", "measurement", "mdi:alert-circle"),
    Entity("sensor", "ip_address",        "IP address",
           "{{ value_json.beambam.ip }}",
           "", "", "", "mdi:ip-network"),
    # From the `info.get_version` reply the daemon requests on connect.
    Entity("sensor", "firmware_version",  "Firmware version",
           "{{ value_json.beambam.firmware }}",
           "", "", "", "mdi:chip"),
    Entity("sensor", "firmware_available", "Firmware available",
           "{{ value_json.beambam.firmware_available }}",
           "", "", "", "mdi:update"),
    Entity("sensor", "printable_objects", "Printable objects",
           "{{ value_json.print.s_obj | length | default(0) }}",
           "", "", "measurement", "mdi:cube-outline"),
    Entity("sensor", "skipped_objects",   "Skipped objects",
           "{{ value_json.print.skipped_objects | length | default(0) }}",
           "", "", "measurement", "mdi:cube-off-outline"),
    Entity("sensor", "total_usage_hours", "Total usage hours",
           "{{ (value_json.print.total_usage | int(0)) // 3600 }}",
           "h", "duration", "total_increasing", "mdi:counter"),
    # ha-bambulab parity additions (gap audit 2026-05-06): print
    # metadata, hardware geometry, AMS active tray, AMS humidity,
    # binary problem indicators.
    Entity("sensor", "start_time",        "Print start time",
           "{{ as_datetime(value_json.print.gcode_start_time | int(0)) "
           "if value_json.print.gcode_start_time | int(0) > 0 else '' }}",
           "", "timestamp", "", "mdi:clock-start"),
    Entity("sensor", "end_time",          "Print end time",
           "{{ as_datetime((value_json.print.gcode_start_time | int(0)) + "
           "((value_json.print.mc_remaining_time | int(0)) * 60)) "
           "if value_json.print.gcode_start_time | int(0) > 0 else '' }}",
           "", "timestamp", "", "mdi:clock-end"),
    Entity("sensor", "print_length",      "Print length",
           "{{ value_json.print.print_length | float(0) | round(2) }}",
           "m", "distance", "measurement", "mdi:tape-measure"),
    Entity("sensor", "print_weight",      "Print weight",
           "{{ value_json.print.print_weight | float(0) | round(2) }}",
           "g", "weight", "measurement", "mdi:weight-gram"),
    Entity("sensor", "bed_type",          "Bed type",
           "{{ value_json.print.bed_type | default('') }}",
           "", "", "", "mdi:checkerboard"),
    Entity("sensor", "active_tray",       "Active AMS tray",
           "{{ value_json.beambam.active_tray }}",
           "", "", "", "mdi:tray-full"),
    Entity("sensor", "nozzle_diameter",   "Nozzle diameter",
           "{{ value_json.print.nozzle_diameter | float(0.4) | round(2) }}",
           "mm", "distance", "", "mdi:printer-3d-nozzle"),
    Entity("sensor", "nozzle_type",       "Nozzle type",
           "{{ value_json.print.nozzle_type | default('') }}",
           "", "", "", "mdi:printer-3d-nozzle-alert"),

    # Binary sensors for door + online + new problem/timelapse indicators.
    Entity("binary_sensor", "online",     "Online",
           "{{ 'ON' if value_json.print is defined else 'OFF' }}",
           "", "connectivity", "", "mdi:lan-connect",
           extra={"payload_on": "ON", "payload_off": "OFF"}),
    # `hw_switch_state` is the external-spool filament sensor
    # (DeviceManager.cpp: m_ext_has_filament), not the door.
    Entity("binary_sensor", "ext_spool_filament", "External spool filament",
           "{{ 'ON' if value_json.print.hw_switch_state | default(0) == 1 else 'OFF' }}",
           "", "", "", "mdi:printer-3d-nozzle",
           extra={"payload_on": "ON", "payload_off": "OFF"}),
    Entity("binary_sensor", "timelapse_running", "Timelapse running",
           "{{ 'ON' if value_json.print.ipcam.timelapse | default('disable') == 'enable' else 'OFF' }}",
           "", "running", "", "mdi:movie",
           extra={"payload_on": "ON", "payload_off": "OFF"}),
    Entity("binary_sensor", "hms_problem", "HMS problem",
           "{{ 'ON' if (value_json.print.hms | length | default(0)) > 0 else 'OFF' }}",
           "", "problem", "", "mdi:alert-circle",
           extra={"payload_on": "ON", "payload_off": "OFF"}),
    Entity("binary_sensor", "print_error", "Print error",
           "{{ 'ON' if value_json.print.print_error | int(0) != 0 else 'OFF' }}",
           "", "problem", "", "mdi:alert-octagon",
           extra={"payload_on": "ON", "payload_off": "OFF"}),
]

# ---------------------------------------------------------------------------
# Values derived in Python (published as `beambam` in the state blob)
# ---------------------------------------------------------------------------

# DevPrintingSpeedLevel (DevDefs.h): 1 silent, 2 standard, 3 sport, 4 ludicrous.
SPEED_NAMES = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}

# Entities older releases published that no longer exist (or changed key).
# Their discovery topics get an empty retained payload on connect, which is
# HA's documented way to remove an MQTT-discovered entity.
RETIRED_KEYS: list[tuple[str, str]] = (
    [("sensor", f"ams_slot{n}_{f}") for n in range(1, 5) for f in ("color", "material")]
    + [("button", f"ams_slot{n}_load") for n in range(1, 5)]
    + [("binary_sensor", f"ams_unit{n}_humidity_warn") for n in range(4)]
    + [("sensor", "ams_humidity"), ("binary_sensor", "door_open")])


def _packed(v: Any) -> tuple[int | None, int | None]:
    """`current | target << 16` (DevUtil::get_flag_bits(temp, 0/16, 16))."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None, None
    return n & 0xFFFF, (n >> 16) & 0xFFFF


def _ip(v: Any) -> str:
    """`net.info[0].ip` is a little-endian uint32 (2315299008 → 192.168.0.138)."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return ""
    return ".".join(str((n >> s) & 0xFF) for s in (0, 8, 16, 24)) if n else ""


def _tray_label(ams_id: int, slot: int) -> str:
    if ams_id in (254, 255):
        return "External " + ("left" if ams_id == 254 else "right")
    if ams_id >= 128:                         # AMS HT units
        return f"HT{ams_id - 127}"
    return f"{chr(ord('A') + ams_id)}{slot + 1}" if 0 <= ams_id < 26 and 0 <= slot < 4 else ""


def derive_state(state: dict) -> dict:
    """Flat, display-ready values from a merged printer report.

    Everything model-specific lives here, in Python, where it is tested —
    HA templates then only read `value_json.beambam.<key>`. Keys are always
    present (None / "" when the printer does not report them) so a template
    never hits an undefined attribute."""
    from beambam.filament_mapping import read_topology, tray_letter_label
    p = state.get("print") or {}
    dev = p.get("device") or {}
    out: dict[str, Any] = {}

    # Chamber: X2D/H2D report it packed in device.ctc.info.temp (DevChamber.cpp);
    # older models use chamber_temper.
    cur, tar = _packed(((dev.get("ctc") or {}).get("info") or {}).get("temp"))
    if cur is None and isinstance(p.get("chamber_temper"), (int, float)):
        cur = p["chamber_temper"]
    out["chamber_temp"], out["chamber_target"] = cur, tar

    # Nozzles: extruder id 0 = right (main), 1 = left (deputy).
    exts = {int(e.get("id", -1)): e for e in ((dev.get("extruder") or {}).get("info") or [])
            if isinstance(e, dict)}
    for eid, side in ((0, "right"), (1, "left")):
        e = exts.get(eid)
        cur, tar = _packed(e.get("temp")) if e else (None, None)
        if e is None and eid == 0:            # single-nozzle printers
            cur = p.get("nozzle_temper")
            tar = p.get("nozzle_target_temper")
        out[f"nozzle_{side}_temp"], out[f"nozzle_{side}_target"] = cur, tar
        tray = "none"
        if e is not None:
            has_filament = bool((int(e.get("info") or 0) >> 1) & 1)   # DevExtder m_ext_has_filament
            snow = int(e.get("snow", 0xFFFF) or 0)
            if has_filament and snow != 0xFFFF:
                tray = _tray_label((snow >> 8) & 0xFF, snow & 0xFF) or "unknown"
        out[f"nozzle_{side}_tray"] = tray

    out["ip"] = _ip(((p.get("net") or {}).get("info") or [{}])[0].get("ip"))
    out["speed"] = SPEED_NAMES.get(int(p.get("spd_lvl") or 0), "unknown")
    lights = {(x or {}).get("node"): (x or {}).get("mode") for x in p.get("lights_report") or []}
    out["chamber_light"] = "on" if lights.get("chamber_light") == "on" else "off"

    # Firmware: `info.get_version` → module "ota".
    ota = next((m for m in ((state.get("info") or {}).get("module") or [])
                if isinstance(m, dict) and m.get("name") == "ota"), {})
    out["firmware"] = ota.get("sw_ver") or "unknown"
    new = ota.get("new_ver") or (p.get("upgrade_state") or {}).get("ota_new_version_number") or ""
    out["firmware_available"] = new if new and new != ota.get("sw_ver") else "up to date"

    topo = read_topology(state)
    out["track_switch"] = ("not installed" if not topo.switch_installed
                           else "set up" if topo.switch_ready else "not set up")
    tray_now = str((p.get("ams") or {}).get("tray_now", "255"))
    out["active_tray"] = (tray_letter_label(int(tray_now)) if tray_now.isdigit()
                          and int(tray_now) < 64 else "none")
    trays: dict[str, dict] = {}
    for t in topo.trays:
        if t.external or t.ams_id >= 128:
            continue
        trays[tray_letter_label(t.tray_id).lower()] = {
            "color": t.colour or "", "material": t.type or "empty",
            "name": t.name or t.profile_id or "", "remain": t.remain}
    out["ams"] = trays
    units: dict[str, dict] = {}
    for u in (p.get("ams") or {}).get("ams") or []:
        try:
            uid = int(u.get("id"))
        except (TypeError, ValueError):
            continue
        if uid >= 26:
            continue
        def _num(v: Any) -> float | None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        level = _num(u.get("humidity"))
        units[chr(ord("a") + uid)] = {
            # `humidity_raw` is %RH; `humidity` is the firmware's 1–5 level in
            # 20 % bands, 1 = driest (live X2D: level 2 at 37 %RH). ≥ 3 (over
            # 40 %RH) is "damp", the same threshold `beambam doctor` warns at.
            "humidity": _num(u.get("humidity_raw")),
            "humidity_level": level,
            "damp": "ON" if level is not None and level >= 3 else "OFF",
            "temp": _num(u.get("temp"))}
    out["ams_units"] = units
    return out


def ams_entities(trays: list[str], units: list[str]) -> list[Entity]:
    """Per-tray (a1..d4) and per-unit (a..d) entities for the AMS units the
    printer actually reports — published as they appear (`HAPublisher`
    re-runs discovery when a unit is added)."""
    out: list[Entity] = []
    for lab in trays:
        up = lab.upper()
        base = "{{ value_json.beambam.ams." + lab
        out.append(Entity("sensor", f"ams_{lab}_color", f"AMS {up} colour",
                          base + ".color }}", "", "", "", "mdi:palette"))
        out.append(Entity("sensor", f"ams_{lab}_material", f"AMS {up} material",
                          base + ".material }}", "", "", "", "mdi:printer-3d-nozzle",
                          extra={"json_attributes_topic": "__BASE__/state",
                                 "json_attributes_template":
                                     "{{ value_json.beambam.ams." + lab + " | tojson }}"}))
        out.append(Entity("button", f"ams_{lab}_load", f"AMS {up} load",
                          "", "", "", "", "mdi:tray-arrow-up",
                          extra={"command_topic": f"__BASE__/ams/{lab}/load",
                                 "payload_press": "ON"}))
    for u in units:
        base = "{{ value_json.beambam.ams_units." + u
        out.append(Entity("sensor", f"ams_{u}_humidity", f"AMS {u.upper()} humidity",
                          base + ".humidity }}", "%", "humidity", "measurement",
                          "mdi:water-percent"))
        out.append(Entity("sensor", f"ams_{u}_temp", f"AMS {u.upper()} temperature",
                          base + ".temp }}", "°C", "temperature", "measurement"))
        # On when the firmware's humidity level is ≥ 3 — the same warning
        # `beambam doctor` emits, so HA can automate "AMS is damp".
        out.append(Entity("binary_sensor", f"ams_{u}_damp", f"AMS {u.upper()} damp",
                          base + ".damp }}", "", "moisture", "", "mdi:water-percent-alert",
                          extra={"payload_on": "ON", "payload_off": "OFF"}))
    return out


# ----- v1.2.0: queue + diagnostic sensors --------------------------------
def queue_doctor_entities() -> list[Entity]:
    """Diagnostic sensors: queue depth + active-HMS count."""
    return [
        Entity(
            "sensor", "queue_pending", "Print queue pending",
            # State source: separate /queue endpoint, not the pushall blob.
            # Daemon publishes queue depth into the same x2d/<serial>/state
            # JSON under "queue_pending" — see daemon code.
            "{{ value_json.queue_pending | default(0) }}",
            "", "", "", "mdi:format-list-numbered"),
        Entity(
            "sensor", "hms_active_count", "HMS active errors",
            "{{ value_json.print.hms | default([]) | length }}",
            "", "", "", "mdi:alert-circle"),
    ]


# Switch / button / number entities.
CONTROL_ENTITIES: list[Entity] = [
    Entity("switch", "light", "Chamber light",
           "{{ value_json.beambam.chamber_light }}",
           "", "", "", "mdi:lightbulb",
           extra={"command_topic":  "__BASE__/light/set",
                  "state_on":       "on",  "state_off": "off",
                  "payload_on":     "ON",  "payload_off": "OFF"}),
    Entity("button", "pause", "Pause print",
           "", "", "", "", "mdi:pause",
           extra={"command_topic": "__BASE__/print/set",
                  "payload_press": "PAUSE"}),
    Entity("button", "resume", "Resume print",
           "", "", "", "", "mdi:play",
           extra={"command_topic": "__BASE__/print/set",
                  "payload_press": "RESUME"}),
    Entity("button", "stop", "Stop print",
           "", "", "", "", "mdi:stop",
           extra={"command_topic": "__BASE__/print/set",
                  "payload_press": "STOP"}),
    # Temp setpoints — HA `number` slider.
    Entity("number", "bed_set",     "Bed setpoint",
           "{{ value_json.print.bed_target_temper | int(0) }}",
           "°C", "temperature", "", "mdi:thermometer",
           extra={"command_topic": "__BASE__/temp/bed/set",
                  "min":  0, "max": 110, "step": 1, "mode": "slider"}),
    Entity("number", "nozzle_set",  "Right nozzle setpoint",
           "{{ value_json.beambam.nozzle_right_target | int(0) }}",
           "°C", "temperature", "", "mdi:printer-3d-nozzle-heat",
           extra={"command_topic": "__BASE__/temp/nozzle/set",
                  "min":  0, "max": 320, "step": 1, "mode": "slider"}),
    Entity("number", "nozzle_left_set", "Left nozzle setpoint",
           "{{ value_json.beambam.nozzle_left_target | int(0) }}",
           "°C", "temperature", "", "mdi:printer-3d-nozzle-heat",
           extra={"command_topic": "__BASE__/temp/nozzle_left/set",
                  "min":  0, "max": 320, "step": 1, "mode": "slider"}),
    Entity("number", "chamber_set", "Chamber setpoint",
           "{{ value_json.print.chamber_target_temper | int(0) }}",
           "°C", "temperature", "", "mdi:thermometer-lines",
           extra={"command_topic": "__BASE__/temp/chamber/set",
                  "min":  0, "max":  60, "step": 1, "mode": "slider"}),
    # Parity with ha-bambulab: home / level / buzzer / refresh buttons.
    Entity("button", "home",       "Home all axes",
           "", "", "", "", "mdi:home",
           extra={"command_topic": "__BASE__/print/set",
                  "payload_press": "HOME"}),
    Entity("button", "level",      "Level bed",
           "", "", "", "", "mdi:format-line-style",
           extra={"command_topic": "__BASE__/print/set",
                  "payload_press": "LEVEL"}),
    Entity("button", "ams_unload", "Unload filament",
           "", "", "", "", "mdi:tray-arrow-down",
           extra={"command_topic": "__BASE__/ams/unload",
                  "payload_press": "ON"}),
    Entity("button", "buzzer_silence",  "Silence buzzer",
           "", "", "", "", "mdi:bell-off",
           extra={"command_topic": "__BASE__/buzzer/set",
                  "payload_press": "SILENCE"}),
    # ha-bambulab parity: chamber light as a `light` entity (HA's lighting
    # cards prefer this domain over `switch`). The existing
    # `switch.<id>_light` stays for back-compat with dashboards that
    # already reference it.
    Entity("light", "chamber_light", "Chamber light",
           "", "", "", "", "mdi:lightbulb",
           extra={"command_topic": "__BASE__/light/set",
                  "state_topic": "__BASE__/state",
                  "state_value_template":
                      "{{ 'ON' if value_json.beambam.chamber_light == 'on' else 'OFF' }}",
                  "payload_on": "ON", "payload_off": "OFF"}),
    # ha-bambulab parity: prompt-sound toggle. Bambu firmware's print.sound_enable
    # field carries the current state; the bridge daemon's /control/sound
    # route dispatches the M300 toggle.
    Entity("switch", "prompt_sound", "Prompt sound",
           "{{ value_json.print.sound_enable | default('off') }}",
           "", "", "", "mdi:volume-high",
           extra={"command_topic": "__BASE__/sound/set",
                  "state_on": "on", "state_off": "off",
                  "payload_on": "ON", "payload_off": "OFF"}),
]


# Camera entity — JPEG bytes flow through `x2d/<id>/snapshot` on the
# MQTT broker. The publisher polls the bridge daemon's /snapshot.jpg
# (which proxies the camera daemon's /cam.jpg, item #53) every
# `X2D_HA_SNAPSHOT_PERIOD` seconds and republishes the bytes with
# `retain=True` so HA's image card always has SOMETHING to show even
# after a restart. ha-bambulab uses the same MQTT-bytes pattern; we
# match it for parity.
def camera_entity(_snapshot_url: str, base_topic: str) -> Entity:
    return Entity(
        "image", "snapshot", "Chamber camera",
        "", "", "", "", "mdi:camera",
        extra={"image_topic":   f"{base_topic}/snapshot",
                "content_type":  "image/jpeg"})


def mqtt_camera_entity(base_topic: str) -> Entity:
    """Same JPEG bytes, exposed as a `camera` entity rather than `image`.
    HA's picture-glance / picture-entity Lovelace cards prefer the
    camera domain; ha-bambulab also publishes one. Sharing the topic
    means no extra publisher load — viewers just pick whichever
    entity their dashboard references."""
    return Entity(
        "camera", "chamber_camera", "Chamber camera (live)",
        "", "", "", "", "mdi:camera",
        # `topic` carries raw JPEG bytes (publisher's _snapshot_loop
        # publishes binary directly; no base64 wrapping). HA's mqtt
        # camera platform accepts that natively.
        extra={"topic": f"{base_topic}/snapshot"})


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------

class HAPublisher:
    def __init__(self, *,
                 broker_host: str,
                 broker_port: int = 1883,
                 broker_username: str | None = None,
                 broker_password: str | None = None,
                 daemon_url: str = "http://127.0.0.1:8765",
                 daemon_token: str | None = None,
                 discovery_prefix: str = "homeassistant",
                 printer_name: str = "",
                 device_serial: str = "",
                 device_model: str = "X2D",
                 client_id: str | None = None) -> None:
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.broker_username = broker_username
        self.broker_password = broker_password
        self.daemon_url = daemon_url.rstrip("/")
        self.daemon_token = daemon_token
        self.discovery_prefix = discovery_prefix.rstrip("/")
        # Wire-side name (matches the bridge's `--printer NAME` /
        # ?printer=NAME query). Empty string → the default `[printer]`
        # section in ~/.x2d/credentials.
        self.printer_name = printer_name or ""
        # Display label for the HA Device card.
        self._display_name = printer_name or "default"
        # The device "identifier" — used in unique_id and topic prefixes.
        # Prefer the printer's serial when available; fall back to the
        # display name.
        self.device_serial = device_serial or self._display_name
        self.device_model = device_model
        clean = re.sub(r"[^A-Za-z0-9_]", "_", self.device_serial)
        self.device_id = f"x2d_{clean}"
        self.base_topic = f"x2d/{clean}"
        self._stop = threading.Event()
        self._client_id = client_id or f"x2d-ha-{clean}-{os.getpid()}"
        # Build entity list now so command-topic substitution can resolve
        # __BASE__ before any subscribe.
        self._entities = (list(SENSOR_ENTITIES)
                          + queue_doctor_entities()    # v1.2.0
                          + list(CONTROL_ENTITIES)
                          + [camera_entity(self.daemon_url + "/cam.jpg",
                                            self.base_topic),
                              mqtt_camera_entity(self.base_topic)])
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, self._client_id)
        if broker_username:
            self._client.username_pw_set(broker_username, broker_password or "")
        self._client.on_connect    = self._on_connect
        self._client.on_message    = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.will_set(self._availability_topic(), "offline",
                              qos=1, retain=True)
        # AMS entities follow the units the printer reports (see
        # _sync_ams_entities); keyed by entity key.
        self._ams_entities: dict[str, Entity] = {}
        self._connected = threading.Event()
        self._sse_thread: threading.Thread | None = None
        self._snapshot_thread: threading.Thread | None = None
        # Snapshot poll cadence (seconds) — once every 10s by default;
        # HA's image card refreshes within a couple seconds of receiving
        # the new MQTT bytes.
        self.snapshot_period = float(
            os.environ.get("X2D_HA_SNAPSHOT_PERIOD", "10"))

    # ----- topics ------------------------------------------------------
    def _state_topic(self) -> str:
        return f"{self.base_topic}/state"

    def _availability_topic(self) -> str:
        return f"{self.base_topic}/availability"

    def _discovery_topic(self, entity: Entity) -> str:
        return (f"{self.discovery_prefix}/{entity.component}/"
                f"{self.device_id}/{entity.key}/config")

    def _resolve_extra(self, extra: dict) -> dict:
        out = {}
        for k, v in extra.items():
            if isinstance(v, str) and "__BASE__" in v:
                out[k] = v.replace("__BASE__", self.base_topic)
            else:
                out[k] = v
        return out

    # ----- HA discovery payload ----------------------------------------
    def _device_block(self) -> dict:
        return {
            "identifiers":  [self.device_id],
            "name":          (f"Bambu Lab {self.device_model} ({self._display_name})"
                              if self._display_name != "default"
                              else f"Bambu Lab {self.device_model}"),
            "manufacturer": "Bambu Lab",
            "model":         self.device_model,
            "sw_version":    "x2d_bridge",
        }

    def _discovery_payload(self, entity: Entity) -> dict:
        payload: dict[str, Any] = {
            "name":          entity.name,
            "unique_id":     f"{self.device_id}_{entity.key}",
            "object_id":     f"{self.device_id}_{entity.key}",
            "device":        self._device_block(),
            "availability_topic": self._availability_topic(),
        }
        # State-bearing entities reuse the single full-state topic.
        if entity.component in ("sensor", "switch", "binary_sensor", "number"):
            payload["state_topic"] = self._state_topic()
            if entity.value_template:
                payload["value_template"] = entity.value_template
        if entity.component in ("camera", "image"):
            payload.pop("availability_topic", None)
        if entity.unit:           payload["unit_of_measurement"] = entity.unit
        if entity.device_class:   payload["device_class"]        = entity.device_class
        if entity.state_class:    payload["state_class"]         = entity.state_class
        if entity.icon:           payload["icon"]                = entity.icon
        if entity.extra:
            payload.update(self._resolve_extra(entity.extra))
        return payload

    # ----- mqtt callbacks ----------------------------------------------
    def _on_connect(self, client, _userdata, _flags, reason_code, _props):
        rc = getattr(reason_code, "value", reason_code)
        if rc != 0:
            LOG.error("connect failed rc=%s", reason_code)
            return
        LOG.info("connected to %s:%d as %s",
                 self.broker_host, self.broker_port, self._client_id)
        # Publish all discovery configs (retained).
        for ent in self._entities + list(self._ams_entities.values()):
            self._publish_discovery(client, ent)
        # Remove entities earlier releases published under other keys.
        for component, key in RETIRED_KEYS:
            client.publish(f"{self.discovery_prefix}/{component}/{self.device_id}/{key}/config",
                           "", qos=1, retain=True)
        # Mark online (also retained — survives broker restarts).
        client.publish(self._availability_topic(), "online",
                       qos=1, retain=True)
        # Subscribe to every command topic the entities advertise (only
        # command topics: state / attribute / image topics are our own
        # output), plus the AMS subtree whose entities come and go.
        seen: set[str] = {f"{self.base_topic}/ams/#"}
        client.subscribe(f"{self.base_topic}/ams/#", qos=1)
        for ent in self._entities:
            v = self._resolve_extra(ent.extra).get("command_topic")
            if isinstance(v, str) and v not in seen:
                client.subscribe(v, qos=1)
                seen.add(v)
        self._connected.set()

    def _publish_discovery(self, client, ent: Entity) -> None:
        client.publish(self._discovery_topic(ent),
                       json.dumps(self._discovery_payload(ent)),
                       qos=1, retain=True)

    def _sync_ams_entities(self, derived: dict) -> None:
        """Publish discovery for AMS trays / units that appeared, and an empty
        config (HA: delete) for ones that went away."""
        want = {e.key: e for e in ams_entities(sorted(derived.get("ams") or {}),
                                                sorted(derived.get("ams_units") or {}))}
        if want.keys() == self._ams_entities.keys():
            return
        gone = [e for k, e in self._ams_entities.items() if k not in want]
        added = [e for k, e in want.items() if k not in self._ams_entities]
        self._ams_entities = want
        if not self._connected.is_set():
            return                      # _on_connect publishes the full set
        for e in added:
            self._publish_discovery(self._client, e)
        for e in gone:
            self._client.publish(self._discovery_topic(e), "", qos=1, retain=True)

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props):
        self._connected.clear()
        LOG.warning("disconnected rc=%s", reason_code)

    def _on_message(self, _client, _userdata, msg) -> None:
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8", errors="replace").strip()
        except Exception:
            payload = ""
        LOG.info("rx %s = %r", topic, payload)
        # Dispatch by topic suffix.
        try:
            if topic == f"{self.base_topic}/light/set":
                state = "on" if payload.upper() in ("ON", "1", "TRUE") else "off"
                self._http_post("/control/light", {"state": state})
            elif topic == f"{self.base_topic}/print/set":
                v = payload.upper()
                if v in ("PAUSE", "RESUME", "STOP"):
                    self._http_post("/control/" + v.lower(), {})
                elif v == "HOME":
                    self._http_post("/control/gcode", {"line": "G28"})
                elif v == "LEVEL":
                    self._http_post("/control/gcode", {"line": "G29"})
            elif topic == f"{self.base_topic}/buzzer/set":
                if payload.upper() == "SILENCE":
                    # Beep_volume=0 silences the buzzer
                    self._http_post("/control/gcode", {"line": "M300 S0 P0"})
            elif topic == f"{self.base_topic}/ams/unload":
                self._http_post("/control/ams_unload", {})
            elif topic.endswith("/load") and "/ams/" in topic:
                # x2d/<id>/ams/<a1..d4>/load → POST /control/ams_load {tray}
                # (legacy x2d/<id>/ams/<1..16>/load → {slot}, 1-based).
                m = re.match(re.escape(self.base_topic)
                             + r"/ams/([a-p][1-4]|\d+)/load$", topic)
                if m and m.group(1).isdigit():
                    self._http_post("/control/ams_load", {"slot": int(m.group(1))})
                elif m:
                    lab = m.group(1)
                    tray = (ord(lab[0]) - ord("a")) * 4 + int(lab[1]) - 1
                    self._http_post("/control/ams_load", {"tray": tray})
            elif "/temp/" in topic and topic.endswith("/set"):
                # x2d/<id>/temp/<target>/set
                m = re.match(re.escape(self.base_topic)
                              + r"/temp/(bed|nozzle|nozzle_left|chamber)/set$", topic)
                if m:
                    body: dict[str, Any] = {"target": m.group(1),
                                            "value": int(float(payload))}
                    if m.group(1) == "nozzle_left":
                        body.update(target="nozzle", idx=1)
                    self._http_post("/control/temp", body)
            elif topic == f"{self.base_topic}/sound/set":
                # ha-bambulab parity: prompt-sound switch.
                state = "on" if payload.upper() in ("ON", "1", "TRUE") else "off"
                self._http_post("/control/sound", {"state": state})
        except Exception as e:
            LOG.exception("dispatch failed for %s: %s", topic, e)

    # ----- HTTP forwarder ----------------------------------------------
    def _http_post(self, path: str, body: dict) -> None:
        url = (f"{self.daemon_url}{path}"
               f"?printer={urllib.parse.quote(self.printer_name)}")
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        if self.daemon_token:
            req.add_header("Authorization", f"Bearer {self.daemon_token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                LOG.info("forwarded %s → %d", path, r.status)
        except urllib.error.HTTPError as e:
            LOG.warning("forward %s → %d (%s)", path, e.code,
                        e.read()[:200].decode("utf-8", "replace"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            LOG.warning("forward %s failed: %s", path, e)

    # ----- SSE consumer ------------------------------------------------
    def _sse_loop(self) -> None:
        url = (f"{self.daemon_url}/state.events"
               f"?printer={urllib.parse.quote(self.printer_name)}")
        backoff = 1.0
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(url)
                if self.daemon_token:
                    req.add_header("Authorization",
                                    f"Bearer {self.daemon_token}")
                with urllib.request.urlopen(req, timeout=15) as r:
                    backoff = 1.0
                    while not self._stop.is_set():
                        line = r.readline()
                        if not line:
                            break
                        if line.startswith(b"data: "):
                            try:
                                envelope = json.loads(line[6:].decode())
                            except json.JSONDecodeError:
                                continue
                            state = envelope.get("state") or {}
                            self._client.publish(
                                self._state_topic(),
                                json.dumps(self.state_payload(state)),
                                qos=0, retain=True)
            except (urllib.error.URLError, ConnectionError,
                    TimeoutError, OSError) as e:
                LOG.warning("SSE reconnect in %.1fs (%s)", backoff, e)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 1.5, 15.0)

    def state_payload(self, state: dict) -> dict:
        """The JSON published on the state topic: the report plus `beambam`."""
        try:
            derived = derive_state(state)
        except Exception as e:                       # never stop the feed
            LOG.warning("derive_state failed: %s", e)
            derived = {}
        self._sync_ams_entities(derived)
        return {**state, "beambam": derived}

    # ----- lifecycle ---------------------------------------------------
    def _snapshot_loop(self) -> None:
        """Item #53: poll the bridge daemon's /snapshot.jpg and
        republish the bytes to `x2d/<id>/snapshot` so HA's image card
        always has the latest frame. Cadence is `snapshot_period`
        seconds. Failures are non-fatal — HA keeps showing the
        previous frame."""
        url = self.daemon_url + "/snapshot.jpg"
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(url)
                if self.daemon_token:
                    req.add_header("Authorization",
                                    f"Bearer {self.daemon_token}")
                with urllib.request.urlopen(req, timeout=8) as r:
                    if r.status == 200:
                        body = r.read()
                        if body and body[:3] == b"\xff\xd8\xff":  # JFIF magic
                            self._client.publish(
                                f"{self.base_topic}/snapshot",
                                payload=body, qos=0, retain=True)
            except (urllib.error.URLError, urllib.error.HTTPError,
                    ConnectionError, TimeoutError, OSError) as e:
                LOG.debug("snapshot poll failed: %s", e)
            if self._stop.wait(self.snapshot_period):
                break

    def start(self) -> None:
        self._client.connect_async(self.broker_host, self.broker_port,
                                    keepalive=30)
        self._client.loop_start()
        self._sse_thread = threading.Thread(
            target=self._sse_loop, name=f"x2d-ha-sse-{self.printer_name}",
            daemon=True)
        self._sse_thread.start()
        self._snapshot_thread = threading.Thread(
            target=self._snapshot_loop,
            name=f"x2d-ha-snap-{self.printer_name}", daemon=True)
        self._snapshot_thread.start()

    def run_forever(self) -> int:
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(1)
        except KeyboardInterrupt:
            pass
        self.stop()
        return 0

    def stop(self) -> None:
        self._stop.set()
        try:
            self._client.publish(self._availability_topic(),
                                  "offline", qos=1, retain=True)
            self._client.disconnect()
            self._client.loop_stop()
        except Exception:
            pass


def run(*, broker: str,
        daemon_url: str,
        printer: str,
        device_serial: str = "",
        device_model: str = "X2D",
        discovery_prefix: str = "homeassistant",
        broker_username: str | None = None,
        broker_password: str | None = None,
        daemon_token: str | None = None) -> int:
    """Synchronous entry point used by ``x2d_bridge.py ha-publish``."""
    logging.basicConfig(
        level=os.environ.get("X2D_HA_LOG", "INFO"),
        format="[%(asctime)s] %(name)s %(levelname)s %(message)s")
    host, _, port_part = broker.rpartition(":")
    if not host:
        host = broker
        port = 1883
    else:
        port = int(port_part)
    pub = HAPublisher(
        broker_host=host, broker_port=port,
        broker_username=broker_username, broker_password=broker_password,
        daemon_url=daemon_url, daemon_token=daemon_token,
        discovery_prefix=discovery_prefix,
        printer_name=printer or "",
        device_serial=device_serial,
        device_model=device_model)
    print(f"[x2d-ha] device_id={pub.device_id} base_topic={pub.base_topic} "
          f"discovery={discovery_prefix}", file=sys.stderr, flush=True)
    return pub.run_forever()
