# beambam + Home Assistant (MQTT discovery)

The bridge ships an MQTT auto-discovery publisher (`beambam ha-publish`) that exposes the printer to Home Assistant as a single
Device with 32 entities. Tested against real Home Assistant Core
2025.1.4 — see §4 for the live-test artefacts.

**Requires Home Assistant 2025.10 or newer.** Entity ids are suggested
with `default_entity_id`, which replaced `object_id` in 2025.10;
`object_id` stopped working in 2026.4.

Step-by-step install and dashboard examples: [`HA_SETUP.md`](./HA_SETUP.md).

## 1. Quick start

```bash
# MQTT broker (or use HA's Mosquitto add-on)
mosquitto -p 1883 &

# 2. Start the x2d bridge daemon (already running for the web UI)
beambam daemon --http 127.0.0.1:8765 &

# 3. Run the HA discovery publisher
beambam ha-publish \
    --broker      127.0.0.1:1883 \
    --daemon-url  http://127.0.0.1:8765 \
    --device-serial $(grep '^serial' ~/.x2d/credentials | head -1 | cut -d= -f2 | tr -d ' ') \
    --device-model X2D
```

In Home Assistant, go to **Settings → Integrations → MQTT** and point
it at the same broker. Within seconds, a "Bambu Lab X2D" Device
appears under MQTT: ~60 printer entities plus 4 per AMS tray and 3 per AMS
unit (an X2D with 4× AMS 2 Pro gets 122). AMS entities follow what the
printer reports — a unit that appears later gets its entities then.

`--device-model` selects model-specific entities (two-nozzle sensors for
X2D / H2D / H2D Pro / H2C; door bit location for X1 vs newer models).

| Component | Count | Examples |
|---|---|---|
| `sensor`  | 40+ | nozzle_temp, nozzle_{right,left}_{temp,target}, nozzle_{right,left}_tray, bed_temp, chamber_temp, progress, layers, remaining, wifi, ip_address, speed_profile, firmware_version, firmware_available, track_switch, active_tray, fans, ams_{a1..d4}_{color,material}, ams_{a..d}_{humidity,temp} |
| `binary_sensor` | 6+ | online, ext_spool_filament, timelapse_running, hms_problem, print_error, ams_{a..d}_damp |
| `number`  | 4  | bed_set, nozzle_set (right), nozzle_left_set, chamber_set |
| `switch` / `light` | 3 | light + chamber_light (chamber LED), prompt_sound |
| `button`  | 8+ | pause, resume, stop, home, level, buzzer_silence, ams_unload, ams_{a1..d4}_load |
| `image` / `camera` | 2 | snapshot (chamber camera, refreshed via daemon /cam.jpg) |

X2D-specific values (both nozzles and the chamber from the packed
`device.*.info.temp` words, the dotted IP, the speed name, firmware from the
`get_version` reply, each tray's colour/material, AMS humidity in %RH) are
computed in Python by `runtime/ha/publisher.derive_state` and published in the
state JSON under `beambam`, so the templates only read
`value_json.beambam.<key>`. Upgrading from an older release deletes the
entities that were renamed (`ams_slotN_*`, `ams_humidity`, `ams_unitN_humidity_warn`,
`door_open` — which was really the external-spool filament sensor) by
publishing empty discovery configs.

All of them are linked to one HA Device:

Before this change a publisher started with `--printer NAME` and no
`--device-serial` used the name (`x2d_<name>`). On connect the publisher
now clears any retained configs left under `x2d_<name>`, which removes
that duplicate device from HA.

## 3. Entities

An X2D with four AMS units gets 124 entities; a printer without AMS
gets 60. AMS entities are created when the state first shows the units.

| Group | Entities |
|---|---|
| Temperatures | nozzle (active nozzle), nozzle target, bed, bed target, chamber; on two-nozzle models also left/right nozzle current + target and `active_nozzle` (enum `left`/`right`) |
| Job | progress, current/total layer, time remaining, print status (enum), print job name, start time, estimated end time, length, weight, printable/skipped objects, bed type, speed profile (enum) |
| Fans | chamber, aux, cooling, heatbreak |
| AMS, per unit (A-D) | humidity %, humidity level (1 = A dry ... 5 = E wet), temperature, humidity warning (level D or E) |
| AMS, per tray (16) | color, material (attributes: remain %, sub-brand, filament id, nozzle range), load button |
| External spool | color + material for id 254 (and 255 on two-nozzle models) |
| Binary sensors | online (printer link), door, timelapse running, HMS problem (attributes: HMS codes), print error |
| Controls | `light.chamber_light`, `switch.light` (legacy), prompt sound switch, pause/resume/stop/home/level/silence buttons, bed/nozzle/chamber setpoint sliders (on two-nozzle models the nozzle slider is the right nozzle, see §8) |
| Diagnostics | Wi-Fi signal, IP address, firmware version, nozzle diameter/type, HMS count, total usage hours, read-only firmware `update` entity |
| Camera | `image.snapshot` and `camera.chamber_camera` (same JPEG, polled from the daemon's `/snapshot.jpg`) |

Notes on the printer fields (checked against a live X2D, firmware
01.02.00.83, and ha-bambulab):

- On two-nozzle printers the top-level `nozzle_temper` is the active
  nozzle. Per-nozzle values are `print.device.extruder.info[]`: low 16
  bits current, high 16 bits target; `id` 1 = left, 0 = right. Bed and
  chamber come from `device.bed.info.temp` / `device.ctc.info.temp`
  (same packing), falling back to `bed_temper` / `chamber_temper`.
- The door is bit 23 (`0x00800000`) of the hex string `print.stat` (of
  `home_flag` on the X1 family). `hw_switch_state` is the filament
  sensor, not the door.
- AMS `humidity` is an index with 5 = driest; `humidity_raw` is the
  percentage. Bambu documents no humidity at which to dry filament; the
  warning only flags its two wettest levels.
- The installed firmware version appears only in the reply to
  `info.get_version`, which the daemon does not request today, so the
  firmware sensor, the `update` entity and the device's `sw_version`
  stay unknown on X2D until it does.

## 4. Topics

```
homeassistant/<component>/x2d_<serial>/<key>/config   discovery (retained)
x2d/<serial>/state                                    merged report + `beambam` block (retained)
x2d/<serial>/availability                             online/offline (retained)
x2d/<serial>/light/set         payload: ON | OFF
x2d/<serial>/print/set         payload: PAUSE | RESUME | STOP
x2d/<serial>/temp/<bed|nozzle|nozzle_left|chamber>/set  payload: integer °C
x2d/<serial>/ams/<a1..d4>/load payload: any (legacy: ams/<1..16>/load)
x2d/<serial>/ams/unload        payload: any
x2d/<serial>/snapshot          payload: JPEG bytes (item #53)
```

- **State.** The printer sends full reports and, in between, small delta
  reports. The publisher deep-merges every event into one document and
  publishes that, so a delta never blanks fields it does not carry.
- **Availability.** Every entity except "Online" needs both
  `availability` and `printer_availability` to be `online`
  (`availability_mode: all`). `printer_availability` follows the
  daemon's `/healthz` (polled every 5 s, `X2D_HA_HEALTH_PERIOD`), which
  turns unhealthy when the printer has been silent for longer than the
  daemon's `--max-staleness` (default 30 s).
- **HA restarts.** When HA publishes `online` on its birth topic
  (`--ha-status-topic`, default `homeassistant/status`) the publisher
  waits a random 0-5 s and republishes discovery, availability and
  state, as the HA MQTT documentation recommends.
- **Removed entities.** `sensor.*_queue_pending` (never populated),
  `sensor.*_hms_active_count` (duplicate of `hms_count`) and
  `sensor.*_ams_humidity` (read the 1-5 index as a percentage) are
  removed from HA by clearing their retained configs.

Every discovery payload carries `origin` (`beambam`, version, support
URL) and a device block with `serial_number`, `model` and, when the
daemon URL is not loopback (or `--configuration-url` is given),
`configuration_url`.

## 5. Authentication

# Bridge daemon + publisher
beambam daemon --http 127.0.0.1:18555 --quiet &
beambam ha-publish --broker 127.0.0.1:21883 \
    --daemon-url http://127.0.0.1:18555 \
    --device-serial $(grep ^serial ~/.x2d/credentials | head -1 | cut -d= -f2 | tr -d ' ') &

# In proot Ubuntu: install + start HA
proot-distro login ubuntu --shared-tmp -- bash -c '
  cd /root/ha && source venv/bin/activate
  hass -c /root/ha-config --log-no-color
'
```

After ~60 s of HA boot, inspect HA's persisted registries:

```bash
cat $PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu/root/ha-config/.storage/core.entity_registry \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
    print(len([e for e in d["data"]["entities"] if "x2d_" in e["unique_id"]]))'
# Expect: 32
```

Sample of `core.restore_state` after live X2D state was processed by
HA's Jinja templates (saved to `docs/ha-live-proof/`):

```
sensor.x2d_<id>_ams_slot2_color    state="#F95D73"   (live AMS color, RGB-only — alpha stripped)
sensor.x2d_<id>_ams_slot2_material state="PLA"
sensor.x2d_<id>_ams_slot3_color    state="#A03CF7"
number.x2d_<id>_bed_set             state="0"        (bed target temperature)
number.x2d_<id>_nozzle_set          state="0"        (nozzle target temperature)
```

These are real values pulled from the running printer through the
full SSE → publisher → MQTT → HA pipeline.

## 5. Authentication for non-loopback brokers

Pass `--broker-username` / `--broker-password` (or set
`X2D_HA_USER` / `X2D_HA_PASS`) to the publisher. The publisher
authenticates as a regular MQTT client; HA's MQTT integration is
configured separately on the HA side.

For the bridge daemon's HTTP control routes, pass `--daemon-token`
matching `--auth-token` on the daemon side. The publisher will attach
`Authorization: Bearer …` to every `/control/<verb>` POST.

## 6. Multi-printer

Without `--printer`, `beambam ha` starts one publisher per
`[printer:NAME]` section, in one process. Each printer is its own HA
device with its own topics. A printer whose publisher fails to start
does not stop the others. `--printer NAME` drives a single printer;
`--device-serial` is only accepted together with `--printer` when more
than one printer is configured.

## 7. Tests

- `tests/test_ha_discovery.py` (normal pytest suite): payload structure,
  every template rendered with Jinja2 against an anonymised live X2D
  state (`tests/fixtures/x2d_state_ha.json`; render tests are skipped
  if `jinja2` is not installed — it is in the `dev` extra), and an
  end-to-end run with an in-process amqtt broker covering merged state,
  birth-message republish, printer availability and legacy cleanup.
- `runtime/ha/test_ha.py`, `test_multi_printer.py`, `test_snapshot.py`:
  standalone scripts (`python runtime/ha/test_ha.py`) for the command
  forwarding, multi-printer and snapshot paths.

# one process drives both
beambam ha-publish \
    --broker 127.0.0.1:1883 \
    --daemon-url http://127.0.0.1:8765
```

## 8. Not done

- Device-based discovery (`homeassistant/device/<id>/config` with
  `cmps`) with the documented `migrate_discovery` migration — needs a
  real HA instance to verify (TODO in `runtime/ha/publisher.py`).
- AMS HT units (ids 128+), AMS drying entities, controllable fans.
- A left-nozzle setpoint. The nozzle slider sends `set_nozzle_temp`
  with `extruder_index` 0; on two-nozzle printers it is labelled
  "Right nozzle setpoint" and shows that nozzle's target, because
  `extruder.info` id 0 is the right nozzle (as in ha-bambulab). That
  mapping of the command's index has not been tested on a printer.
