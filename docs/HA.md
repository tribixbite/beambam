# beambam + Home Assistant (MQTT discovery)

`beambam ha` (alias `ha-publish`) publishes a printer to Home Assistant
through MQTT discovery. It reads state from a running `beambam daemon
--http` (the `/state.events` stream) and forwards HA commands to the
daemon's `/control/*` routes. Code: `runtime/ha/publisher.py`.

**Requires Home Assistant 2025.10 or newer.** Entity ids are suggested
with `default_entity_id`, which replaced `object_id` in 2025.10;
`object_id` stopped working in 2026.4.

Step-by-step install and dashboard examples: [`HA_SETUP.md`](./HA_SETUP.md).

## 1. Quick start

```bash
# MQTT broker (or use HA's Mosquitto add-on)
mosquitto -p 1883 &

# daemon: talks to the printer, serves HTTP for the publisher
beambam daemon --http 127.0.0.1:8765 &

# publisher: one HA device per [printer:NAME] section
beambam ha --broker 127.0.0.1:1883 --daemon-url http://127.0.0.1:8765 \
    --device-model X2D
```

In HA, add the MQTT integration pointed at the same broker. A "Bambu Lab
X2D" device appears under Settings → Devices & services → MQTT.

`--device-model` selects model-specific entities (two-nozzle sensors for
X2D / H2D / H2D Pro / H2C; door bit location for X1 vs newer models).

## 2. Identity

The device identifier, every `unique_id` and the topic prefix are built
from the printer **serial**, resolved in this order: `--device-serial`,
the credentials file section, then the daemon's last state
(`print.upgrade_state.sn`). If none is available `beambam ha` refuses to
start for that printer instead of inventing an id.

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
homeassistant/<component>/x2d_<serial>/<key>/config  discovery (retained)
x2d/<serial>/state                  merged printer state JSON (retained)
x2d/<serial>/availability           publisher online/offline (retained, last will)
x2d/<serial>/printer_availability   daemon hears the printer: online/offline (retained)
x2d/<serial>/snapshot               JPEG bytes (retained)
x2d/<serial>/light/set              ON | OFF
x2d/<serial>/print/set              PAUSE | RESUME | STOP | HOME | LEVEL
x2d/<serial>/temp/<bed|nozzle|chamber>/set   integer °C
x2d/<serial>/ams/<slot>/load        any payload; slot 1-16 = unit*4 + tray + 1
x2d/<serial>/sound/set              ON | OFF
x2d/<serial>/buzzer/set             SILENCE
homeassistant/status                (subscribed) HA birth message
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

- Broker: `--broker-username` / `--broker-password` (or `X2D_HA_USER` /
  `X2D_HA_PASS`).
- Daemon: `--daemon-token` must match the daemon's `--auth-token`; the
  publisher sends `Authorization: Bearer …` on every request.

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

An amqtt broker does not process discovery, so none of these prove that
HA accepts the payloads; the templates are checked with Jinja2 and the
payload keys against the HA MQTT documentation.

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
