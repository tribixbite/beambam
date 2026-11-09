"""Home Assistant MQTT auto-discovery publisher (items #50, #53, #54).

Bridges a running ``beambam daemon --http`` (its ``/state.events`` SSE
feed) to a Home Assistant MQTT broker. Requires Home Assistant **2025.10
or newer**: entity ids are suggested with ``default_entity_id``, which
replaced ``object_id`` in 2025.10 (``object_id`` stopped working in
2026.4).

Publishes:

* single-component discovery configs at
  ``<discovery_prefix>/<component>/x2d_<serial>/<key>/config`` (retained);
* the printer state at ``x2d/<serial>/state``: ONE JSON document, the
  deep-merge of every report the daemon relayed (the printer sends small
  delta reports between full ones, so a raw message is never "the
  state"); entities project fields out of it with ``value_template``;
* ``x2d/<serial>/availability`` — the publisher itself (MQTT last will);
* ``x2d/<serial>/printer_availability`` — whether the daemon still hears
  the printer (polled from the daemon's ``/healthz``). Printer-bound
  entities need both topics ``online`` (``availability_mode: all``).

Subscribes to:

* ``homeassistant/status`` (HA's birth message): on ``online`` it waits a
  random 0-5 s and republishes discovery, availability and state, as the
  HA MQTT docs recommend;
* the command topics below, forwarded to the daemon's HTTP API:

  * ``x2d/<serial>/light/set``        → POST /control/light
  * ``x2d/<serial>/print/set``        → POST /control/{pause,resume,stop}
  * ``x2d/<serial>/temp/<target>/set`` → POST /control/temp
  * ``x2d/<serial>/ams/<slot>/load``  → POST /control/ams_load (slot 1-16)
  * ``x2d/<serial>/sound/set``, ``x2d/<serial>/buzzer/set``

The device identifier is always the printer's serial (resolved by
``beambam ha`` from the credentials file or the daemon), so unique_ids do
not depend on whether the publisher was started with ``--printer``.

Entity templates use only filters and tests available in Home
Assistant's template sandbox (``.get`` on dicts, ``selectattr``,
``int(default, base)``, ``format``, ``to_json``, ``as_datetime``,
``now()`` / ``timedelta``). Jinja has no bitwise operators, so packed
fields are decoded with ``//`` and ``%``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import random
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import paho.mqtt.client as mqtt

from beambam.state import deep_merge

LOG = logging.getLogger("x2d.ha")

ORIGIN_NAME = "beambam"
SUPPORT_URL = "https://github.com/tribixbite/beambam"
#: The oldest Home Assistant release these discovery payloads work with.
MIN_HA_VERSION = "2025.10"


def _origin_version() -> str:
    """beambam's version for the discovery ``origin`` block (best effort:
    the publisher must still start from a bare source checkout)."""
    try:
        from beambam import __version__
        return str(__version__)
    except Exception:  # pragma: no cover - import side effects only
        return "unknown"


# ---------------------------------------------------------------------------
# Printer model helpers
# ---------------------------------------------------------------------------

def model_key(model: str) -> str:
    """Normalise a model string: ``"H2D Pro"`` -> ``"H2DPRO"``."""
    return re.sub(r"[^A-Z0-9]", "", model.upper())


#: Two-nozzle printers. Their top-level ``nozzle_temper`` is the ACTIVE
#: nozzle; per-nozzle values live in ``print.device.extruder.info[]``.
DUAL_NOZZLE_MODELS = frozenset({"X2D", "H2D", "H2DPRO", "H2C"})
#: Door state is bit 23 of ``home_flag`` on the X1 family and of the hex
#: string ``stat`` on newer models (same split as ha-bambulab).
X1_FAMILY = frozenset({"X1", "X1C", "X1E"})
#: bambulab.com firmware page slugs (from ha-bambulab's Upgrade.release_url).
FIRMWARE_PAGE = {"P1P": "p1", "P1S": "p1", "A1MINI": "a1-mini", "A1": "a1",
                 "X1C": "x1", "X1E": "x1e", "X2D": "x2"}

#: print.gcode_state values (lower-cased), from ha-bambulab's
#: GCODE_STATE_OPTIONS. Anything else renders None (HA: unknown).
GCODE_STATES = ["failed", "finish", "idle", "init", "offline", "pause",
                "prepare", "running", "slicing"]

#: AMS units are lettered A-D on the printer screen and in Bambu Studio.
AMS_LETTERS = "ABCD"
AMS_TRAYS_PER_UNIT = 4


# ---------------------------------------------------------------------------
# Template building blocks
# ---------------------------------------------------------------------------
# The published state always carries `print` (the publisher waits for it),
# so `value_json.print.<field>` is safe; deeper paths use `.get(…, {})` so a
# missing sub-document renders None instead of raising UndefinedError.

PR = "value_json.print"
DEV = "value_json.print.get('device', {})"


def _num(expr: str, digits: int | None = None) -> str:
    """Render ``expr`` as a number, or ``None`` when it is absent/null.

    ``None`` sets a numeric HA sensor to unknown; ``''`` would be ignored
    and leave a stale value behind. Numeric strings ("0.4") are accepted.
    """
    conv = "v | int(0)" if digits is None else f"(v | float(0)) | round({digits})"
    return ("{% set v = " + expr + " %}"
            "{{ " + conv + " if v is number or (v is string and "
            "v | float('x') != 'x') else None }}")


def _packed_temp(path: tuple[str, ...], *, target: bool,
                 fallback: str | None = None) -> str:
    """Temperature packed as ``target << 16 | current`` under ``print.device``.

    ``fallback`` names a top-level ``print`` field used when the packed
    value is absent (older firmware / single-nozzle printers).
    """
    getter = DEV + "".join(f".get('{k}', {{}})" for k in path[:-1]) \
        + f".get('{path[-1]}')"
    word = "(t // 65536) % 65536" if target else "t % 65536"
    out = "{% set t = " + getter + " %}{% if t is number %}{{ " + word + " }}"
    if fallback:
        out += ("{% elif " + PR + "." + fallback + " is number %}{{ "
                + PR + "." + fallback + " | round(1) }}")
    return out + "{% else %}None{% endif %}"


def _extruder_temp(nozzle_id: int, *, target: bool) -> str:
    """Current/target temperature of one nozzle of a two-nozzle printer.

    ``extruder.info[].id``: 0 = right nozzle, 1 = left nozzle.
    """
    word = "(e.temp // 65536) % 65536" if target else "e.temp % 65536"
    return ("{% set e = (" + DEV + ".get('extruder', {}).get('info') or []) "
            f"| selectattr('id', 'eq', {nozzle_id}) | first %}}"
            "{% if e is defined and e.temp is number %}{{ " + word + " }}"
            "{% else %}None{% endif %}")


#: Active nozzle: bits 4-7 of extruder.state (low 4 bits = nozzle count).
ACTIVE_NOZZLE_TPL = (
    "{% set s = " + DEV + ".get('extruder', {}).get('state') %}"
    "{% set i = (s // 16) % 16 if s is number else -1 %}"
    "{{ ['right', 'left'][i] if i in [0, 1] else None }}")

#: Chamber light mode ('on' / 'off' / 'flashing') from lights_report.
LIGHT_MODE = ("{% set m = (" + PR + ".lights_report or []) "
              "| selectattr('node', 'eq', 'chamber_light') "
              "| map(attribute='mode') | first | default('') %}")

#: Running print status, None when unrecognised.
STAGE_TPL = ("{% set s = " + PR + ".gcode_state | default('') | string | lower %}"
             "{{ s if s in " + repr(GCODE_STATES) + " else None }}")

#: HMS entries formatted like Bambu's wiki codes (AAAA_BBBB_CCCC_DDDD).
HMS_CODES = ("{% set ns = namespace(codes=[]) %}"
             "{% for h in (" + PR + ".hms or []) %}"
             "{% set ns.codes = ns.codes + ['%04X_%04X_%04X_%04X' | format("
             "h.attr // 65536, h.attr % 65536, h.code // 65536, h.code % 65536)] %}"
             "{% endfor %}")

#: Installed / offered firmware. The installed version is only reported in
#: the reply to `info.get_version` (merged at top-level `info.module`) or in
#: `upgrade_state.new_ver_list` (P1/A1); push_status reports neither on X2D.
FIRMWARE_SET = (
    "{% set ota = ((value_json.get('info') or {}).get('module') or []) "
    "| selectattr('name', 'eq', 'ota') | first | default({}) %}"
    "{% set up = " + PR + ".upgrade_state or {} %}"
    "{% set nv = (up.get('new_ver_list') or []) "
    "| selectattr('name', 'eq', 'ota') | first | default({}) %}"
    "{% set cur = ota.get('sw_ver') or nv.get('cur_ver') %}"
    "{% set new = (nv.get('new_ver') or up.get('ota_new_version_number')) "
    "if up.get('new_version_state') == 1 else none %}")


def _ams_unit_set(unit: int) -> str:
    """``{% set u = … %}``: the AMS unit whose id is ``unit`` (ids are strings
    on current firmware; ints accepted too), or ``{}``."""
    return ("{% set u = (" + PR + ".get('ams', {}).get('ams') or []) "
            f"| selectattr('id', 'in', ['{unit}', {unit}]) | first | default({{}}) %}}")


def _ams_tray_set(unit: int, tray: int) -> str:
    """``{% set t = … %}``: one tray of one AMS unit, or ``{}``."""
    return (_ams_unit_set(unit)
            + "{% set t = (u.get('tray') or []) "
            f"| selectattr('id', 'in', ['{tray}', {tray}]) | first | default({{}}) %}}")


def _ext_spool_set(spool_id: int) -> str:
    """``{% set t = … %}``: external spool ``spool_id`` (254 / 255).

    Two-nozzle printers list them in ``vir_slot``; older printers have a
    single ``vt_tray`` (id 254).
    """
    fallback = f"{PR}.vt_tray or {{}}" if spool_id == 254 else "{}"
    return ("{% set t = (" + PR + ".vir_slot or []) "
            f"| selectattr('id', 'in', ['{spool_id}', {spool_id}]) | first "
            f"| default({fallback}) %}}")


#: Colour / material / attributes of the tray bound to `t`.
TRAY_COLOR = ("{% set c = t.get('tray_color') or '' %}"
              "{{ '#' ~ c[:6] if c | length >= 6 else None }}")
TRAY_MATERIAL = "{{ t.get('tray_type') or None }}"
TRAY_ATTRS = ("{{ {'color': t.get('tray_color'), 'remain': t.get('remain'), "
              "'sub_brand': t.get('tray_sub_brands'), "
              "'filament_id': t.get('tray_info_idx'), "
              "'nozzle_temp_min': t.get('nozzle_temp_min'), "
              "'nozzle_temp_max': t.get('nozzle_temp_max')} | to_json }}")


# ---------------------------------------------------------------------------
# Entity catalogue
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Entity:
    """One HA entity.

    ``component`` is the HA platform (sensor / binary_sensor / switch /
    light / number / button / update / camera / image). ``key`` is the
    discovery-topic leaf and the unique_id suffix. ``value_template`` is
    evaluated by Home Assistant against the merged state JSON (for a light
    it becomes ``state_value_template``). ``extra`` is merged into the
    discovery payload last; ``__BASE__`` in a string value becomes the
    publisher's base topic. ``printer_bound=False`` keeps the entity
    available while the printer is offline (only the "Online" sensor).
    """
    component: str
    key: str
    name: str
    value_template: str = ""
    unit: str = ""
    device_class: str = ""
    state_class: str = ""
    icon: str = ""
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)
    category: str = ""
    printer_bound: bool = True


_ON_OFF = {"payload_on": "ON", "payload_off": "OFF"}
_DIAG = "diagnostic"


def _enum(options: list[str]) -> dict[str, Any]:
    return {"options": list(options)}


# Model-independent sensors.
SENSOR_ENTITIES: list[Entity] = [
    Entity("sensor", "nozzle_temp", "Nozzle temperature",
           # Top-level nozzle_temper is the active nozzle on two-nozzle models.
           _num(f"{PR}.nozzle_temper", 1), "°C", "temperature", "measurement"),
    Entity("sensor", "nozzle_target", "Nozzle target",
           _num(f"{PR}.nozzle_target_temper", 0), "°C", "temperature", "measurement"),
    Entity("sensor", "bed_temp", "Bed temperature",
           _packed_temp(("bed", "info", "temp"), target=False, fallback="bed_temper"),
           "°C", "temperature", "measurement"),
    Entity("sensor", "bed_target", "Bed target",
           _packed_temp(("bed", "info", "temp"), target=True, fallback="bed_target_temper"),
           "°C", "temperature", "measurement"),
    Entity("sensor", "chamber_temp", "Chamber temperature",
           _packed_temp(("ctc", "info", "temp"), target=False, fallback="chamber_temper"),
           "°C", "temperature", "measurement"),
    Entity("sensor", "progress", "Print progress",
           _num(f"{PR}.mc_percent"), "%", "", "measurement", "mdi:progress-clock"),
    Entity("sensor", "current_layer", "Current layer",
           _num(f"{PR}.layer_num"), "", "", "measurement", "mdi:layers"),
    Entity("sensor", "total_layers", "Total layers",
           _num(f"{PR}.total_layer_num"), "", "", "measurement", "mdi:layers-outline"),
    Entity("sensor", "remaining", "Time remaining",
           _num(f"{PR}.mc_remaining_time"), "min", "duration", "measurement",
           "mdi:clock-outline"),
    Entity("sensor", "wifi", "Wi-Fi signal",
           "{% set w = " + PR + ".wifi_signal | default('') | string | replace('dBm', '') "
           "| trim %}{{ w | int if w | int('x') != 'x' else None }}",
           "dBm", "signal_strength", "measurement", category=_DIAG),
    Entity("sensor", "filename", "Print job",
           "{{ " + PR + ".subtask_name or None }}", icon="mdi:file"),
    Entity("sensor", "stage", "Print status", STAGE_TPL,
           device_class="enum", icon="mdi:state-machine", extra=_enum(GCODE_STATES)),
    Entity("sensor", "chamber_fan_speed", "Chamber fan",
           _num(f"{PR}.big_fan1_speed"), "", "", "measurement", "mdi:fan"),
    Entity("sensor", "aux_fan_speed", "Aux fan",
           _num(f"{PR}.big_fan2_speed"), "", "", "measurement", "mdi:fan"),
    Entity("sensor", "cooling_fan_speed", "Cooling fan",
           _num(f"{PR}.cooling_fan_speed"), "", "", "measurement", "mdi:fan"),
    Entity("sensor", "heatbreak_fan_speed", "Heatbreak fan",
           _num(f"{PR}.heatbreak_fan_speed"), "", "", "measurement", "mdi:fan"),
    Entity("sensor", "speed_profile", "Speed profile",
           # spd_lvl: 1 silent, 2 standard, 3 sport, 4 ludicrous.
           "{% set l = " + PR + ".spd_lvl | default(0) | int(0) %}"
           "{{ {1: 'silent', 2: 'standard', 3: 'sport', 4: 'ludicrous'}.get(l) }}",
           device_class="enum", icon="mdi:speedometer",
           extra=_enum(["silent", "standard", "sport", "ludicrous"])),
    Entity("sensor", "hms_count", "HMS error count",
           "{{ (" + PR + ".hms or []) | length }}", "", "", "measurement",
           "mdi:alert-circle", category=_DIAG),
    Entity("sensor", "ip_address", "IP address",
           # net.info[0].ip is the IPv4 address as a little-endian integer.
           "{% set n = ((" + PR + ".net or {}).get('info') or [{}])[0].get('ip') %}"
           "{% if n is number and n > 0 %}{{ '%d.%d.%d.%d' | format(n % 256, "
           "(n // 256) % 256, (n // 65536) % 256, (n // 16777216) % 256) }}"
           "{% elif n is string and n %}{{ n }}{% else %}None{% endif %}",
           icon="mdi:ip-network", category=_DIAG),
    Entity("sensor", "firmware_version", "Firmware version",
           FIRMWARE_SET + "{{ cur or None }}", icon="mdi:chip", category=_DIAG),
    Entity("sensor", "printable_objects", "Printable objects",
           "{{ (" + PR + ".s_obj or []) | length }}", "", "", "measurement",
           "mdi:cube-outline"),
    Entity("sensor", "skipped_objects", "Skipped objects",
           "{{ (" + PR + ".skipped_objects or []) | length }}", "", "", "measurement",
           "mdi:cube-off-outline"),
    Entity("sensor", "total_usage_hours", "Total usage hours",
           "{% set v = " + PR + ".total_usage %}"
           "{{ (v | int(0)) // 3600 if v is number else None }}",
           "h", "duration", "total_increasing", "mdi:counter", category=_DIAG),
    Entity("sensor", "start_time", "Print start time",
           "{% set t = " + PR + ".gcode_start_time | default(0) | int(0) %}"
           "{{ as_datetime(t).isoformat() if t > 0 else None }}",
           "", "timestamp", "", "mdi:clock-start"),
    Entity("sensor", "end_time", "Print end time",
           # Estimated: now + remaining minutes, only while a job is active.
           "{% set r = " + PR + ".mc_remaining_time | default(0) | int(0) %}"
           "{% set s = " + PR + ".gcode_state | default('') | string | lower %}"
           "{{ (now() + timedelta(minutes=r)).isoformat() "
           "if r > 0 and s in ['running', 'pause', 'prepare'] else None }}",
           "", "timestamp", "", "mdi:clock-end"),
    Entity("sensor", "print_length", "Print length",
           _num(f"{PR}.print_length", 2), "m", "distance", "measurement",
           "mdi:tape-measure"),
    Entity("sensor", "print_weight", "Print weight",
           _num(f"{PR}.print_weight", 2), "g", "weight", "measurement",
           "mdi:weight-gram"),
    Entity("sensor", "bed_type", "Bed type",
           "{{ " + PR + ".bed_type or None }}", icon="mdi:checkerboard"),
    Entity("sensor", "active_tray", "Active AMS tray",
           # tray_now: global tray id 0-15, 254 external spool, 255 none.
           "{% set n = " + PR + ".get('ams', {}).get('tray_now') | default(255) | int(255) %}"
           "{{ n + 1 if n < 254 else 0 }}",
           "", "", "measurement", "mdi:tray-full"),
    Entity("sensor", "nozzle_diameter", "Nozzle diameter",
           _num(f"{PR}.nozzle_diameter", 2), "mm", "distance", "",
           "mdi:printer-3d-nozzle", category=_DIAG),
    Entity("sensor", "nozzle_type", "Nozzle type",
           "{{ " + PR + ".nozzle_type or None }}", icon="mdi:printer-3d-nozzle-alert",
           category=_DIAG),
    # The printer link as seen by the daemon (see printer_availability).
    Entity("binary_sensor", "online", "Online", "", "", "connectivity", "",
           "mdi:lan-connect",
           extra={"state_topic": "__BASE__/printer_availability",
                  "payload_on": "online", "payload_off": "offline"},
           category=_DIAG, printer_bound=False),
    Entity("binary_sensor", "timelapse_running", "Timelapse running",
           "{{ 'ON' if " + PR + ".get('ipcam', {}).get('timelapse') == 'enable' else 'OFF' }}",
           "", "running", "", "mdi:movie", extra=dict(_ON_OFF)),
    Entity("binary_sensor", "hms_problem", "HMS problem",
           "{{ 'ON' if (" + PR + ".hms or []) | length > 0 else 'OFF' }}",
           "", "problem", "", "mdi:alert-circle",
           extra={**_ON_OFF,
                  "json_attributes_topic": "__BASE__/state",
                  "json_attributes_template": HMS_CODES
                  + "{{ {'count': ns.codes | length, 'codes': ns.codes} | to_json }}"}),
    Entity("binary_sensor", "print_error", "Print error",
           "{{ 'ON' if " + PR + ".print_error | default(0) | int(0) != 0 else 'OFF' }}",
           "", "problem", "", "mdi:alert-octagon", extra=dict(_ON_OFF)),
]


def door_entity(model: str) -> Entity:
    """Door sensor: bit 23 (0x00800000) of ``home_flag`` (X1 family) or of
    the hex string ``stat`` (H2/X2 and newer). Live X2D, door closed:
    ``stat = "258000"``; ha-bambulab documents ``46A58008`` as open."""
    if model_key(model) in X1_FAMILY:
        tpl = ("{% set f = " + PR + ".home_flag %}"
               "{% if f is number %}{{ 'ON' if (f // 8388608) % 2 == 1 else 'OFF' }}"
               "{% else %}None{% endif %}")
    else:
        tpl = ("{% set s = " + PR + ".stat %}"
               "{% if s is string and s %}"
               "{{ 'ON' if ((s | int(0, 16)) // 8388608) % 2 == 1 else 'OFF' }}"
               "{% else %}None{% endif %}")
    return Entity("binary_sensor", "door_open", "Door", tpl,
                  "", "door", "", "mdi:door-open", extra=dict(_ON_OFF))


def nozzle_entities(model: str) -> list[Entity]:
    """Left/right nozzle current + target and the active nozzle (two-nozzle
    printers only; single-nozzle printers keep the top-level sensors)."""
    if model_key(model) not in DUAL_NOZZLE_MODELS:
        return []
    out: list[Entity] = []
    for side, nozzle_id in (("left", 1), ("right", 0)):
        title = side.capitalize()
        out.append(Entity("sensor", f"{side}_nozzle_temp", f"{title} nozzle temperature",
                          _extruder_temp(nozzle_id, target=False),
                          "°C", "temperature", "measurement"))
        out.append(Entity("sensor", f"{side}_nozzle_target", f"{title} nozzle target",
                          _extruder_temp(nozzle_id, target=True),
                          "°C", "temperature", "measurement"))
    out.append(Entity("sensor", "active_nozzle", "Active nozzle", ACTIVE_NOZZLE_TPL,
                      device_class="enum", icon="mdi:printer-3d-nozzle",
                      extra=_enum(["left", "right"])))
    return out


def firmware_update_entity(model: str) -> Entity:
    """Read-only firmware ``update`` entity (no ``command_topic``: beambam
    never starts a firmware install). Renders '' — ignored by HA — until
    the installed version is known."""
    attrs: dict[str, Any] = {"title": f"Bambu Lab {model} firmware"}
    slug = FIRMWARE_PAGE.get(model_key(model))
    release_url = (f"https://bambulab.com/en/support/firmware-download/{slug}"
                   if slug else "")
    payload = ("{'installed_version': cur, 'latest_version': new or cur"
               + (f", 'release_url': '{release_url}'" if release_url else "")
               + "}")
    return Entity("update", "firmware", "Firmware",
                  FIRMWARE_SET + "{% if cur %}{{ " + payload + " | to_json }}{% endif %}",
                  device_class="firmware", extra=attrs, category=_DIAG)


def ams_unit_entities(unit: int) -> list[Entity]:
    """Humidity (% and Bambu's A-E level), temperature, drying warning and
    the four trays of one AMS unit (id 0-3)."""
    letter = AMS_LETTERS[unit]
    u_set = _ams_unit_set(unit)
    # MQTT `humidity` is 1-5 with 5 = driest (Bambu forum / ha-bambulab,
    # which shows 6 - value). Level 1 = A (dry) ... 5 = E (wet).
    level = ("{% set h = u.get('humidity') | default(0) | int(0) %}"
             "{% set lvl = 6 - h if 1 <= h <= 5 else none %}")
    out = [
        Entity("sensor", f"ams_unit{unit}_humidity", f"AMS {letter} humidity",
               u_set + "{% set v = u.get('humidity_raw') %}"
               "{{ v | int if v | int('x') != 'x' and v | int > 0 else None }}",
               "%", "humidity", "measurement"),
        Entity("sensor", f"ams_unit{unit}_humidity_level", f"AMS {letter} humidity level",
               u_set + level + "{{ lvl }}", "", "", "measurement", "mdi:water-percent"),
        Entity("sensor", f"ams_unit{unit}_temp", f"AMS {letter} temperature",
               u_set + "{% set v = u.get('temp') %}"
               "{{ (v | float) | round(1) if v | float('x') != 'x' else None }}",
               "°C", "temperature", "measurement"),
        # Warning at level D or E. Bambu documents no drying threshold; this
        # only flags the two wettest levels of its own scale.
        Entity("binary_sensor", f"ams_unit{unit}_humidity_warn",
               f"AMS {letter} humidity warning",
               u_set + level + "{{ None if lvl is none else ('ON' if lvl >= 4 else 'OFF') }}",
               "", "moisture", "", "mdi:water-percent-alert", extra=dict(_ON_OFF)),
    ]
    for tray in range(AMS_TRAYS_PER_UNIT):
        slot = unit * AMS_TRAYS_PER_UNIT + tray + 1      # 1-16, as on /control/ams_load
        label = f"AMS {letter}{tray + 1}"
        t_set = _ams_tray_set(unit, tray)
        out += [
            Entity("sensor", f"ams_slot{slot}_color", f"{label} color",
                   t_set + TRAY_COLOR, icon="mdi:palette"),
            Entity("sensor", f"ams_slot{slot}_material", f"{label} material",
                   t_set + TRAY_MATERIAL, icon="mdi:printer-3d-nozzle",
                   extra={"json_attributes_topic": "__BASE__/state",
                          "json_attributes_template": t_set + TRAY_ATTRS}),
            Entity("button", f"ams_slot{slot}_load", f"{label} load",
                   icon="mdi:tray-arrow-up",
                   extra={"command_topic": f"__BASE__/ams/{slot}/load",
                          "payload_press": "ON"}),
        ]
    return out


def ext_spool_entities(model: str) -> list[Entity]:
    """External spool(s): id 254 everywhere, plus 255 on two-nozzle models."""
    ids = [254, 255] if model_key(model) in DUAL_NOZZLE_MODELS else [254]
    out: list[Entity] = []
    for sid in ids:
        key = "ext_spool" if sid == 254 else f"ext_spool_{sid}"
        name = "External spool" if len(ids) == 1 else f"External spool {sid}"
        t_set = _ext_spool_set(sid)
        out += [
            Entity("sensor", f"{key}_color", f"{name} color", t_set + TRAY_COLOR,
                   icon="mdi:palette"),
            Entity("sensor", f"{key}_material", f"{name} material", t_set + TRAY_MATERIAL,
                   icon="mdi:printer-3d-nozzle",
                   extra={"json_attributes_topic": "__BASE__/state",
                          "json_attributes_template": t_set + TRAY_ATTRS}),
        ]
    return out


# Switch / light / button / number entities.
CONTROL_ENTITIES: list[Entity] = [
    # Kept for dashboards that reference switch.<id>_light; the `light`
    # entity below is the preferred one.
    Entity("switch", "light", "Chamber light switch",
           LIGHT_MODE + "{{ 'on' if m in ['on', 'flashing'] else ('off' if m == 'off' else None) }}",
           icon="mdi:lightbulb",
           extra={"command_topic": "__BASE__/light/set",
                  "state_on": "on", "state_off": "off", **_ON_OFF}),
    Entity("light", "chamber_light", "Chamber light",
           LIGHT_MODE + "{{ 'ON' if m in ['on', 'flashing'] else ('OFF' if m == 'off' else None) }}",
           icon="mdi:lightbulb",
           extra={"command_topic": "__BASE__/light/set", **_ON_OFF}),
    Entity("button", "pause", "Pause print", icon="mdi:pause",
           extra={"command_topic": "__BASE__/print/set", "payload_press": "PAUSE"}),
    Entity("button", "resume", "Resume print", icon="mdi:play",
           extra={"command_topic": "__BASE__/print/set", "payload_press": "RESUME"}),
    Entity("button", "stop", "Stop print", icon="mdi:stop",
           extra={"command_topic": "__BASE__/print/set", "payload_press": "STOP"}),
    Entity("number", "bed_set", "Bed setpoint",
           _packed_temp(("bed", "info", "temp"), target=True, fallback="bed_target_temper"),
           "°C", "temperature", "", "mdi:thermometer",
           extra={"command_topic": "__BASE__/temp/bed/set",
                  "min": 0, "max": 110, "step": 1, "mode": "slider"}),
    Entity("number", "nozzle_set", "Nozzle setpoint",
           # Applies to the active nozzle on two-nozzle printers.
           _num(f"{PR}.nozzle_target_temper"),
           "°C", "temperature", "", "mdi:printer-3d-nozzle-heat",
           extra={"command_topic": "__BASE__/temp/nozzle/set",
                  "min": 0, "max": 320, "step": 1, "mode": "slider"}),
    Entity("number", "chamber_set", "Chamber setpoint",
           _packed_temp(("ctc", "info", "temp"), target=True,
                        fallback="chamber_target_temper"),
           "°C", "temperature", "", "mdi:thermometer-lines",
           extra={"command_topic": "__BASE__/temp/chamber/set",
                  "min": 0, "max": 60, "step": 1, "mode": "slider"}),
    Entity("button", "home", "Home all axes", icon="mdi:home",
           extra={"command_topic": "__BASE__/print/set", "payload_press": "HOME"}),
    Entity("button", "level", "Level bed", icon="mdi:format-line-style",
           extra={"command_topic": "__BASE__/print/set", "payload_press": "LEVEL"}),
    Entity("button", "buzzer_silence", "Silence buzzer", icon="mdi:bell-off",
           extra={"command_topic": "__BASE__/buzzer/set", "payload_press": "SILENCE"}),
    Entity("switch", "prompt_sound", "Prompt sound",
           "{% set s = " + PR + ".sound_enable %}"
           "{{ s if s in ['on', 'off'] else None }}",
           icon="mdi:volume-high",
           extra={"command_topic": "__BASE__/sound/set",
                  "state_on": "on", "state_off": "off", **_ON_OFF}),
]


def camera_entity(_snapshot_url: str, base_topic: str) -> Entity:
    """JPEG bytes the publisher polls from the daemon's /snapshot.jpg and
    republishes (retained) on ``<base>/snapshot`` (item #53)."""
    return Entity("image", "snapshot", "Chamber camera", icon="mdi:camera",
                  extra={"image_topic": f"{base_topic}/snapshot",
                         "content_type": "image/jpeg"})


def mqtt_camera_entity(base_topic: str) -> Entity:
    """The same JPEG bytes as a ``camera`` entity (picture-glance cards
    prefer the camera domain). Raw bytes, no base64."""
    return Entity("camera", "chamber_camera", "Chamber camera (live)", icon="mdi:camera",
                  extra={"topic": f"{base_topic}/snapshot"})


#: Entities published by earlier versions and now gone; their retained
#: discovery configs are cleared on connect so HA removes them.
#: queue_pending was never fed by the daemon; hms_active_count duplicated
#: hms_count; ams_humidity read the 1-5 index as a percentage (replaced by
#: ams_unit<N>_humidity).
RETIRED_ENTITIES: tuple[tuple[str, str], ...] = (
    ("sensor", "queue_pending"),
    ("sensor", "hms_active_count"),
    ("sensor", "ams_humidity"),
)

#: Command topics (relative to the base topic) the publisher listens on.
COMMAND_SUBSCRIPTIONS: tuple[str, ...] = (
    "light/set", "print/set", "buzzer/set", "sound/set", "temp/+/set", "ams/+/load",
)


def build_entities(model: str, base_topic: str, *,
                   ams_units: tuple[int, ...] = (),
                   snapshot_url: str = "") -> list[Entity]:
    """Every entity for one printer. AMS units are added once the state
    shows them (``ams_units``), so a printer without AMS gets no AMS
    entities."""
    out = list(SENSOR_ENTITIES)
    out.append(door_entity(model))
    out += nozzle_entities(model)
    out.append(firmware_update_entity(model))
    for unit in sorted(set(ams_units)):
        out += ams_unit_entities(unit)
    out += ext_spool_entities(model)
    out += CONTROL_ENTITIES
    out += [camera_entity(snapshot_url, base_topic), mqtt_camera_entity(base_topic)]
    return out


# ---------------------------------------------------------------------------
# State helpers (pure; unit-tested)
# ---------------------------------------------------------------------------

def ams_unit_ids(state: dict[str, Any]) -> tuple[int, ...]:
    """Ids (0-3) of the regular AMS units present in a merged state.

    # TODO: AMS HT units (ids 128-135, one tray each) are not exposed yet.
    """
    units = (state.get("print") or {}).get("ams") or {}
    out: set[int] = set()
    for unit in units.get("ams") or []:
        try:
            uid = int(unit.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= uid < len(AMS_LETTERS):
            out.add(uid)
    return tuple(sorted(out))


def firmware_version(state: dict[str, Any]) -> str | None:
    """Installed printer firmware, when the merged state knows it.

    Only the reply to ``info.get_version`` (``info.module[name=ota].sw_ver``)
    or ``upgrade_state.new_ver_list[name=ota].cur_ver`` (P1/A1) carry it.
    """
    for module in (state.get("info") or {}).get("module") or []:
        if isinstance(module, dict) and module.get("name") == "ota" and module.get("sw_ver"):
            return str(module["sw_ver"])
    upgrade = (state.get("print") or {}).get("upgrade_state") or {}
    for entry in upgrade.get("new_ver_list") or []:
        if isinstance(entry, dict) and entry.get("name") == "ota" and entry.get("cur_ver"):
            return str(entry["cur_ver"])
    return None


def serial_from_state(state: dict[str, Any]) -> str:
    """The printer serial as reported in ``print.upgrade_state.sn`` ('' if absent)."""
    upgrade = (state.get("print") or {}).get("upgrade_state") or {}
    return str(upgrade.get("sn") or "")


def _is_loopback_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("", "localhost", "::1") or host.startswith("127.")


def _bearer_request(url: str, token: str | None) -> urllib.request.Request:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return req


def fetch_serial_from_daemon(daemon_url: str, printer: str,
                             token: str | None = None, timeout: float = 5.0) -> str:
    """Ask a running daemon for a printer's serial (from its last state).
    Returns '' when the daemon is unreachable or has no state yet."""
    url = (f"{daemon_url.rstrip('/')}/state"
           f"?printer={urllib.parse.quote(printer)}")
    try:
        with urllib.request.urlopen(_bearer_request(url, token), timeout=timeout) as r:
            state = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    return serial_from_state(state) if isinstance(state, dict) else ""


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------

# TODO(device discovery): move to one `<prefix>/device/x2d_<serial>/config`
# message with `cmps` (HA >= 2024.11). Plan, per the HA MQTT docs
# ("Migration from single component to device discovery"): (1) publish
# {"migrate_discovery": true} to every current per-entity topic, (2) publish
# the device payload with the same unique_ids and device identifiers, with
# each entity's key as its `cmps` id and `p` = component, (3) after HA logs
# the migration, clear the per-entity topics with empty retained payloads.
# Not done yet because it can only be verified against a real Home
# Assistant instance (an amqtt broker does not process discovery).

class HAPublisher:
    """One printer's bridge between the daemon and an HA MQTT broker."""

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
                 client_id: str | None = None,
                 birth_topic: str = "homeassistant/status",
                 configuration_url: str | None = None,
                 legacy_device_ids: tuple[str, ...] = ()) -> None:
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.broker_username = broker_username
        self.broker_password = broker_password
        self.daemon_url = daemon_url.rstrip("/")
        self.daemon_token = daemon_token
        self.discovery_prefix = discovery_prefix.rstrip("/")
        self.birth_topic = birth_topic
        # Wire-side name (matches the daemon's [printer:NAME] / ?printer=).
        self.printer_name = printer_name or ""
        self._display_name = printer_name or "default"
        # The serial is the identity: unique_ids, topics, device identifiers.
        # `beambam ha` always resolves it; the name fallback only keeps
        # direct library use working.
        self.device_serial = device_serial or self._display_name
        self.device_model = device_model
        clean = re.sub(r"[^A-Za-z0-9_]", "_", self.device_serial)
        self.device_id = f"x2d_{clean}"
        self.base_topic = f"x2d/{clean}"
        # Device ids an older version may have published under (e.g. the
        # printer name); their retained configs are cleared on connect.
        self.legacy_device_ids = tuple(i for i in legacy_device_ids if i != self.device_id)
        if configuration_url is None:
            # Only a URL HA's browser can reach is useful as a device link.
            configuration_url = "" if _is_loopback_url(self.daemon_url) else self.daemon_url
        self.configuration_url = configuration_url
        self._stop = threading.Event()
        self._client_id = client_id or f"x2d-ha-{clean}-{os.getpid()}"

        # Merged printer state + what discovery was last built from.
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {}
        self._ams_units: tuple[int, ...] = ()
        self._firmware: str | None = None
        self._printer_online: bool | None = None
        self._connected = threading.Event()

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, self._client_id)
        if broker_username:
            self._client.username_pw_set(broker_username, broker_password or "")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.will_set(self._availability_topic(), "offline", qos=1, retain=True)
        self._threads: list[threading.Thread] = []
        # Snapshot poll cadence (s); HA's image card refreshes on each frame.
        self.snapshot_period = float(os.environ.get("X2D_HA_SNAPSHOT_PERIOD", "10"))
        # How often the daemon's /healthz is polled for printer availability.
        self.health_period = float(os.environ.get("X2D_HA_HEALTH_PERIOD", "5"))
        # Upper bound of the random delay before answering HA's birth message.
        self.birth_delay_max = 5.0

    # ----- topics ------------------------------------------------------
    def _state_topic(self) -> str:
        return f"{self.base_topic}/state"

    def _availability_topic(self) -> str:
        return f"{self.base_topic}/availability"

    def _printer_availability_topic(self) -> str:
        return f"{self.base_topic}/printer_availability"

    def _discovery_topic(self, entity: Entity, device_id: str | None = None) -> str:
        return (f"{self.discovery_prefix}/{entity.component}/"
                f"{device_id or self.device_id}/{entity.key}/config")

    def _resolve_extra(self, extra: dict[str, Any]) -> dict[str, Any]:
        return {k: (v.replace("__BASE__", self.base_topic)
                    if isinstance(v, str) and "__BASE__" in v else v)
                for k, v in extra.items()}

    # ----- entities ----------------------------------------------------
    @property
    def entities(self) -> list[Entity]:
        """Current entity set (grows when new AMS units appear)."""
        return build_entities(self.device_model, self.base_topic,
                              ams_units=self._ams_units,
                              snapshot_url=self.daemon_url + "/cam.jpg")

    # ----- HA discovery payload ----------------------------------------
    def _device_block(self) -> dict[str, Any]:
        name = (f"Bambu Lab {self.device_model} ({self._display_name})"
                if self._display_name != "default" else f"Bambu Lab {self.device_model}")
        block: dict[str, Any] = {
            "identifiers": [self.device_id],
            "name": name,
            "manufacturer": "Bambu Lab",
            "model": self.device_model,
            "serial_number": self.device_serial,
        }
        if self._firmware:
            block["sw_version"] = self._firmware
        if self.configuration_url:
            block["configuration_url"] = self.configuration_url
        return block

    @staticmethod
    def _origin_block() -> dict[str, Any]:
        return {"name": ORIGIN_NAME, "sw_version": _origin_version(),
                "support_url": SUPPORT_URL}

    def _discovery_payload(self, entity: Entity) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": entity.name,
            "unique_id": f"{self.device_id}_{entity.key}",
            # Same entity_id the removed `object_id` produced, so existing
            # dashboards keep working (only applies to new registrations).
            "default_entity_id": f"{entity.component}.{self.device_id}_{entity.key}",
            "device": self._device_block(),
            "origin": self._origin_block(),
        }
        if entity.printer_bound:
            payload["availability"] = [{"topic": self._availability_topic()},
                                       {"topic": self._printer_availability_topic()}]
            payload["availability_mode"] = "all"
        else:
            payload["availability_topic"] = self._availability_topic()
        if entity.component in ("sensor", "switch", "binary_sensor", "number",
                                "light", "update"):
            payload["state_topic"] = self._state_topic()
            if entity.value_template:
                key = ("state_value_template" if entity.component == "light"
                       else "value_template")
                payload[key] = entity.value_template
        if entity.unit:
            payload["unit_of_measurement"] = entity.unit
        if entity.device_class:
            payload["device_class"] = entity.device_class
        if entity.state_class:
            payload["state_class"] = entity.state_class
        if entity.icon:
            payload["icon"] = entity.icon
        if entity.category:
            payload["entity_category"] = entity.category
        if entity.extra:
            payload.update(self._resolve_extra(entity.extra))
        return payload

    # ----- publishing --------------------------------------------------
    def _publish_discovery(self) -> None:
        for ent in self.entities:
            self._client.publish(self._discovery_topic(ent),
                                 json.dumps(self._discovery_payload(ent)),
                                 qos=1, retain=True)

    def _clear_retired(self) -> None:
        for component, key in RETIRED_ENTITIES:
            self._client.publish(
                f"{self.discovery_prefix}/{component}/{self.device_id}/{key}/config",
                "", qos=1, retain=True)

    def _publish_state(self) -> None:
        with self._lock:
            if "print" not in self._state:
                return   # templates assume `print`; wait for a real report
            body = json.dumps(self._state, separators=(",", ":"))
        self._client.publish(self._state_topic(), body, qos=0, retain=True)

    def _publish_printer_availability(self) -> None:
        if self._printer_online is None:
            return
        self._client.publish(self._printer_availability_topic(),
                             "online" if self._printer_online else "offline",
                             qos=1, retain=True)

    def publish_all(self) -> None:
        """Discovery + availability + current state (connect / HA birth)."""
        self._publish_discovery()
        self._client.publish(self._availability_topic(), "online", qos=1, retain=True)
        self._publish_printer_availability()
        self._publish_state()

    def ingest_state(self, fragment: dict[str, Any]) -> None:
        """Fold one relayed report into the merged state and republish it.

        Republishes discovery first when the report reveals a new AMS unit
        or a (new) firmware version, so entities exist before HA renders
        the state that feeds them.
        """
        if not isinstance(fragment, dict) or not fragment:
            return
        with self._lock:
            deep_merge(self._state, fragment)
            units = tuple(sorted(set(self._ams_units) | set(ams_unit_ids(self._state))))
            firmware = firmware_version(self._state) or self._firmware
            changed = units != self._ams_units or firmware != self._firmware
            self._ams_units, self._firmware = units, firmware
        if changed and self._connected.is_set():
            self._publish_discovery()
        self._publish_state()

    def merged_state(self) -> dict[str, Any]:
        """A copy of the merged state (tests / diagnostics)."""
        with self._lock:
            return json.loads(json.dumps(self._state))

    # ----- mqtt callbacks ----------------------------------------------
    def _on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any,
                    reason_code: Any, _props: Any) -> None:
        rc = getattr(reason_code, "value", reason_code)
        if rc != 0:
            LOG.error("connect failed rc=%s", reason_code)
            return
        LOG.info("connected to %s:%d as %s",
                 self.broker_host, self.broker_port, self._client_id)
        self._connected.set()
        self._clear_retired()
        self.publish_all()
        for leaf in COMMAND_SUBSCRIPTIONS:
            client.subscribe(f"{self.base_topic}/{leaf}", qos=1)
        client.subscribe(self.birth_topic, qos=1)
        for legacy in self.legacy_device_ids:
            # Retained configs under an old device id arrive here and are
            # cleared in _on_message (removes the duplicate HA device).
            client.subscribe(f"{self.discovery_prefix}/+/{legacy}/+/config", qos=1)

    def _on_disconnect(self, _client: mqtt.Client, _userdata: Any, _flags: Any,
                       reason_code: Any, _props: Any) -> None:
        self._connected.clear()
        LOG.warning("disconnected rc=%s", reason_code)

    def _schedule_birth_republish(self) -> None:
        delay = random.uniform(0.0, self.birth_delay_max)
        LOG.info("HA came online; republishing discovery in %.1fs", delay)
        timer = threading.Timer(delay, self.publish_all)
        timer.daemon = True
        timer.start()

    def _is_legacy_config(self, topic: str) -> bool:
        parts = topic.split("/")
        return (len(parts) >= 4 and parts[-1] == "config"
                and parts[-3] in self.legacy_device_ids)

    def _on_message(self, _client: mqtt.Client, _userdata: Any, msg: Any) -> None:
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        if topic == self.birth_topic:
            if payload == "online":
                self._schedule_birth_republish()
            return
        if self._is_legacy_config(topic):
            if payload:
                LOG.info("clearing legacy discovery config %s", topic)
                self._client.publish(topic, "", qos=1, retain=True)
            return
        LOG.info("rx %s = %r", topic, payload)
        try:
            self._dispatch_command(topic, payload)
        except Exception as e:  # one bad command must not kill the loop
            LOG.exception("dispatch failed for %s: %s", topic, e)

    def _dispatch_command(self, topic: str, payload: str) -> None:
        base = self.base_topic
        if topic == f"{base}/light/set":
            state = "on" if payload.upper() in ("ON", "1", "TRUE") else "off"
            self._http_post("/control/light", {"state": state})
        elif topic == f"{base}/print/set":
            verb = payload.upper()
            if verb in ("PAUSE", "RESUME", "STOP"):
                self._http_post("/control/" + verb.lower(), {})
            elif verb == "HOME":
                self._http_post("/control/gcode", {"line": "G28"})
            elif verb == "LEVEL":
                self._http_post("/control/gcode", {"line": "G29"})
        elif topic == f"{base}/buzzer/set":
            if payload.upper() == "SILENCE":
                self._http_post("/control/gcode", {"line": "M300 S0 P0"})
        elif topic == f"{base}/sound/set":
            state = "on" if payload.upper() in ("ON", "1", "TRUE") else "off"
            self._http_post("/control/sound", {"state": state})
        elif m := re.fullmatch(re.escape(base) + r"/ams/(\d+)/load", topic):
            self._http_post("/control/ams_load", {"slot": int(m.group(1))})
        elif m := re.fullmatch(re.escape(base) + r"/temp/(bed|nozzle|chamber)/set", topic):
            self._http_post("/control/temp",
                            {"target": m.group(1), "value": int(float(payload))})

    # ----- HTTP forwarder ----------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.daemon_url}{path}?printer={urllib.parse.quote(self.printer_name)}"

    def _http_post(self, path: str, body: dict[str, Any]) -> None:
        req = urllib.request.Request(
            self._url(path), method="POST", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        if self.daemon_token:
            req.add_header("Authorization", f"Bearer {self.daemon_token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                LOG.info("forwarded %s -> %d", path, r.status)
        except urllib.error.HTTPError as e:
            LOG.warning("forward %s -> %d (%s)", path, e.code,
                        e.read()[:200].decode("utf-8", "replace"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            LOG.warning("forward %s failed: %s", path, e)

    # ----- background loops --------------------------------------------
    def _sse_loop(self) -> None:
        """Consume the daemon's /state.events and fold every event into
        the merged state (each event may be a partial delta report)."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                req = _bearer_request(self._url("/state.events"), self.daemon_token)
                with urllib.request.urlopen(req, timeout=30) as r:
                    backoff = 1.0
                    while not self._stop.is_set():
                        line = r.readline()
                        if not line:
                            break
                        if not line.startswith(b"data: "):
                            continue
                        try:
                            envelope = json.loads(line[6:].decode("utf-8", "replace"))
                        except json.JSONDecodeError:
                            continue
                        if isinstance(envelope, dict):
                            self.ingest_state(envelope.get("state") or {})
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                LOG.warning("SSE reconnect in %.1fs (%s)", backoff, e)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 1.5, 15.0)

    def _check_printer_health(self) -> bool:
        """True when the daemon's /healthz says it heard the printer within
        its --max-staleness window. A 503 or an unreachable daemon is False."""
        try:
            req = _bearer_request(self._url("/healthz"), self.daemon_token)
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
                return bool(body.get("healthy", r.status == 200))
        except urllib.error.HTTPError:
            return False          # 503 = stale printer link (or auth failure)
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _health_loop(self) -> None:
        while not self._stop.is_set():
            online = self._check_printer_health()
            if online != self._printer_online:
                LOG.info("printer link %s", "online" if online else "offline")
                self._printer_online = online
                self._publish_printer_availability()
            if self._stop.wait(self.health_period):
                break

    def _snapshot_loop(self) -> None:
        """Item #53: poll the daemon's /snapshot.jpg and republish the bytes
        (retained) to ``<base>/snapshot``. Failures keep the previous frame."""
        url = self.daemon_url + "/snapshot.jpg"
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(_bearer_request(url, self.daemon_token),
                                            timeout=8) as r:
                    body = r.read() if r.status == 200 else b""
                    if body[:3] == b"\xff\xd8\xff":  # JPEG SOI marker
                        self._client.publish(f"{self.base_topic}/snapshot",
                                             payload=body, qos=0, retain=True)
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                LOG.debug("snapshot poll failed: %s", e)
            if self._stop.wait(self.snapshot_period):
                break

    # ----- lifecycle ---------------------------------------------------
    def start(self) -> None:
        self._client.connect_async(self.broker_host, self.broker_port, keepalive=30)
        self._client.loop_start()
        for target, label in ((self._sse_loop, "sse"), (self._health_loop, "health"),
                              (self._snapshot_loop, "snap")):
            t = threading.Thread(target=target, daemon=True,
                                 name=f"x2d-ha-{label}-{self.printer_name}")
            t.start()
            self._threads.append(t)

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
            self._client.publish(self._availability_topic(), "offline", qos=1, retain=True)
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
    """Synchronous single-printer entry point (library use)."""
    logging.basicConfig(level=os.environ.get("X2D_HA_LOG", "INFO"),
                        format="[%(asctime)s] %(name)s %(levelname)s %(message)s")
    host, _, port_part = broker.rpartition(":")
    if not host:
        host, port = broker, 1883
    else:
        port = int(port_part)
    serial = device_serial or fetch_serial_from_daemon(daemon_url, printer, daemon_token)
    pub = HAPublisher(
        broker_host=host, broker_port=port,
        broker_username=broker_username, broker_password=broker_password,
        daemon_url=daemon_url, daemon_token=daemon_token,
        discovery_prefix=discovery_prefix, printer_name=printer or "",
        device_serial=serial, device_model=device_model)
    print(f"[x2d-ha] device_id={pub.device_id} base_topic={pub.base_topic} "
          f"discovery={discovery_prefix}", file=sys.stderr, flush=True)
    return pub.run_forever()
