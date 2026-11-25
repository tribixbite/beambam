# beambam's HA publisher vs. ha-bambulab

[ha-bambulab](https://github.com/greghesp/ha-bambulab) is the
community Home Assistant integration for Bambu Lab printers (a HACS
custom component that connects to the printer itself). beambam's
publisher is a separate process that feeds HA through MQTT discovery.

Checked 2026-11-09 against ha-bambulab `main` and its release notes.

## 1. X2D support in ha-bambulab

The earlier version of this page said ha-bambulab does not support the
X2D. That is no longer true:

- ha-bambulab's code lists the X2D among its two-nozzle printers
  (`Printers.X2D` in `pybambu/models.py`: per-nozzle temperatures from
  `device.extruder.info`, active nozzle, door from `stat`).
- v2.2.26 (2026-09-12) added the X2D to its device lists and fixed X2D
  camera stream URLs.
- v2.3.0 (2026-11-08) added the X2D filament track switch, AMS nozzle
  routing and per-extruder filament sensors.

|  ha-bambulab key                  | x2d-bridge equivalent      | Status | Notes |
|-----------------------------------|----------------------------|--------|-------|
| `nozzle_temp`                     | `sensor.nozzle_temp`       | ✅ parity | |
| `bed_temp`                        | `sensor.bed_temp`          | ✅ parity | |
| `chamber_temp`                    | `sensor.chamber_temp`      | ✅ parity | |
| `target_nozzle_temp`              | `sensor.nozzle_target` + `number.nozzle_set` | ✅ better | settable via slider |
| `target_bed_temp`                 | `sensor.bed_target` + `number.bed_set` | ✅ better | settable via slider |
| `target_chamber_temp`             | `sensor.chamber_temp` + `number.chamber_set` | ✅ better | settable via slider |
| `print_progress`                  | `sensor.progress`          | ✅ parity | |
| `current_layer`                   | `sensor.current_layer`     | ✅ parity | |
| `total_layers`                    | `sensor.total_layers`      | ✅ parity | |
| `remaining_time`                  | `sensor.remaining`         | ✅ parity | minutes, `device_class: duration` |
| `wifi_signal`                     | `sensor.wifi`              | ✅ parity | dBm, signed |
| `subtask_name` / `gcode_file`     | `sensor.filename`          | ✅ parity | |
| `stage` / `print_status`          | `sensor.stage`             | ✅ parity | |
| `chamber_fan_speed`               | `sensor.chamber_fan_speed` | ✅ parity | |
| `aux_fan_speed`                   | `sensor.aux_fan_speed`     | ✅ parity | |
| `cooling_fan_speed`               | `sensor.cooling_fan_speed` | ✅ parity | |
| `heatbreak_fan_speed`             | `sensor.heatbreak_fan_speed` | ✅ parity | |
| `speed_profile`                   | `sensor.speed_profile`     | ✅ parity | |
| `hms` / `hms_errors`              | `sensor.hms_count`         | ✅ parity | error count; full HMS list via `x2d://state` MCP resource |
| `ip_address`                      | `sensor.ip_address`        | ✅ parity | dotted quad from the little-endian `print.net.info[0].ip` |
| `firmware_update` (binary)        | `sensor.firmware_available` | ✅ read-only | new version or `up to date` (`get_version` / `upgrade_state`); installing still needs the printer or Handy |
| `online`                          | `binary_sensor.online`     | ✅ parity | + retained `availability` topic with offline LWT |
| `door_open`                       | (not reported over LAN)    | ➖ N/A | `hw_switch_state` is the external-spool filament sensor → `binary_sensor.ext_spool_filament` |
| `printable_objects`               | `sensor.printable_objects` | ✅ parity | count of `print.s_obj[]` |
| `skipped_objects`                 | `sensor.skipped_objects`   | ✅ parity | |
| `total_usage_hours`               | `sensor.total_usage_hours` | ✅ parity | hours, `total_increasing` |
| `tray` (per-AMS-slot color)       | `sensor.ams_{a1..d4}_color` | ✅ parity | every unit, RGB hex |
| `tray.tray_type` (per-slot mat)   | `sensor.ams_{a1..d4}_material` | ✅ parity | `empty` for an empty slot |
| `humidity`                        | `sensor.ams_{a..d}_humidity` | ✅ parity | %RH (`humidity_raw`) |
| `humidity_index`                  | `binary_sensor.ams_{a..d}_damp` | ✅ | level ≥ 3 (over 40 %RH) |
| `drying`/`drying_temperature`/`drying_filament`/`drying_duration`/`remaining_drying_time` | (planned) | ➖ planned | AMS dryer entities |
| `tool_module`                     | (X-series specific)        | ➖ N/A | X2D has 2 nozzles, both reported as `nozzle_temp` |
| `left_nozzle_*` / `right_nozzle_*`| `sensor.nozzle_{left,right}_{temp,target,tray}`, `number.nozzle_left_set` | ✅ parity | from `device.extruder.info[]` (packed temp words) |
| `pause` / `resume` / `stop` (button) | `button.pause/resume/stop` | ✅ parity | |
| `home` (implicit via gcode)       | `button.home`              | ✅ parity | |
| `level` (implicit via gcode)      | `button.level`             | ✅ parity | |
| `buzzer_silence`                  | `button.buzzer_silence`    | ✅ parity | M300 S0 P0 |
| `buzzer_beeping` / `buzzer_fire_alarm` | (planned)             | ➖ planned | low-priority diagnostic |
| `refresh` (force pushall)         | (omitted by design)         | ➖ N/A | bridge daemon does pushall every 5 s automatically |
| `light` (chamber LED)             | `switch.light`             | ✅ parity | |
| `camera` switch (enable/disable)  | (omitted)                   | ➖ N/A | LAN-mode liveview is firmware-side |
| `ftp` switch                      | (omitted by design)         | ➖ N/A | bridge always uses FTPS for upload |
| `prompt_sound` switch             | (planned)                  | ➖ planned | |
| `aux_fan` / `chamber_fan` / `cooling_fan` / `secondary_aux_fan` (controllable) | (planned) | ➖ planned | exposed as sensors today; controllable later |
| `target_*_temperature` (numbers)  | `number.*_set`             | ✅ parity | |
| `cover_image` / `pick_image` (P1P-specific) | (omitted)         | ➖ N/A | not on X2D |
| `p1p_camera`                      | `image.snapshot`           | ✅ parity | `mqtt.image` platform |
| `firmware_update` (update entity) | (planned)                  | ➖ planned | LAN can't trigger update on X-series |

## 2. Entity coverage

ha-bambulab exposes more entities than beambam. Rough comparison for an
X2D:

| Area | beambam | ha-bambulab |
|---|---|---|
| Nozzle / bed / chamber temperatures and targets | yes, incl. left/right nozzle and active nozzle | yes |
| Temperature setpoints | bed, chamber, one nozzle slider (right nozzle on two-nozzle models) | yes |
| Job progress, layers, remaining time, start/end time, status | yes | yes, plus print stage detail and cover image |
| Fans | read-only sensors | controllable fan entities |
| AMS trays (16) + external spools | color, material (+ remain/filament attributes), load button | richer tray entities, filament track switch, nozzle routing |
| AMS unit humidity / temperature | %, A-E level, temperature, warning | yes, plus drying control and status |
| AMS HT | no | yes |
| HMS | count, problem flag with codes as attributes | errors with descriptions |
| Door, online, timelapse, print error | yes | yes |
| Firmware | read-only `update` entity; version unknown until the daemon requests `get_version` | update entity with install action |
| Camera | MQTT snapshot image/camera every 10 s; MJPEG/WebRTC via the camera daemon | camera entity |
| Chamber light, prompt sound, pause/resume/stop, home, level, buzzer | yes | yes (set differs) |

## 3. Differences in how they run

| | beambam | ha-bambulab |
|---|---|---|
| Where it runs | any host on the printer LAN; HA only needs an MQTT broker | inside Home Assistant |
| Command signing on authorization-control firmware | yes | no (needs Developer Mode) |
| Same printer data for other tools | web UI, MCP server, Prometheus `/metrics`, CLI from the same daemon | HA only |
| Install | `pip install "beambam[all]"` + three services | HACS |

1. Keep your existing HA install. Disable the `bambu_lab` integration
   in HA (Settings → Integrations → Bambu Lab → Disable).
2. Install the X2D bridge on whatever device sees the printer (a
   Termux phone, a Raspberry Pi, an x86 box):
   ```bash
   git clone https://github.com/tribixbite/x2d
   cd x2d && ./install.sh
   ```
3. Set up `~/.x2d/credentials` (one INI section per printer; same
   `ip / code / serial` triple ha-bambulab asks for).
4. Start the bridge daemon + HA publisher pointed at the same MQTT
   broker HA already uses:
   ```bash
   beambam daemon --http 127.0.0.1:8765 &
   beambam ha-publish \
       --broker localhost:1883 \
       --daemon-url http://127.0.0.1:8765 \
       --device-serial $(grep ^serial ~/.x2d/credentials | head -1 | cut -d= -f2 | tr -d ' ')
   ```
5. HA's MQTT integration auto-discovers a new "Bambu Lab X2D" device
   with all entities. Your existing dashboards using
   `sensor.bambu_lab_*` entities will need to be re-pointed at
   `sensor.x2d_<serial>_*` (or you can rename the new entities to
   the old IDs in HA → Settings → Devices → Bambu Lab X2D).

1. Disable the Bambu Lab integration in HA (or keep it for monitoring;
   the two can run side by side).
2. Follow [`HA_SETUP.md`](./HA_SETUP.md).
3. Entities are new: `sensor.x2d_<serial>_*` instead of ha-bambulab's
   ids, so dashboards and automations must be re-pointed.

## 5. When to prefer ha-bambulab

- You do not need to send commands, or you run the printer in
  Developer Mode.
- You want its wider entity set (AMS drying, AMS HT, fan control,
  firmware install) and its Lovelace cards.
