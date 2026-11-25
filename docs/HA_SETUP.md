# Home Assistant — install and dashboard guide

Adds a Bambu Lab printer to Home Assistant through MQTT discovery: about
60 entities per printer plus 16 per AMS unit (124 for an X2D with four
AMS units).

Architecture, topics and the entity list: [`HA.md`](./HA.md). Comparison
with ha-bambulab: [`HA_VS_BAMBULAB.md`](./HA_VS_BAMBULAB.md).

Entity ids below use the form `<domain>.x2d_<serial>_<key>`, with the
serial lower-cased by HA, e.g. `button.x2d_03abc0001234567_pause` for
serial `03ABC0001234567`.

---

## 1. Prerequisites

- **Home Assistant 2025.10 or newer.** Older releases ignore
  `default_entity_id`, so entity ids would be generated from the names.
- **An MQTT broker** reachable from the HA host and the beambam host.
  The simplest is HA's **Mosquitto broker** add-on; create a user for
  beambam (e.g. `mqtt_beambam`) in its configuration.
- **HA's MQTT integration** pointed at that broker (Settings → Devices &
  services → Add integration → MQTT; host `core-mosquitto` for the
  add-on). Leave its birth message enabled (the default): beambam
  republishes discovery when HA announces itself.
- **A host on the printer's LAN** for beambam (Linux, macOS, Windows,
  Termux). It does not have to be the HA host.
- For the **camera**: the daemon's HTTP port must be reachable from HA
  if you add the optional MJPEG camera below (bind `0.0.0.0`).

## 2. beambam setup

```bash
# 1) install (the [all] extra includes paho-mqtt for the publisher)
pip install "beambam[all]"

# 2) printer credentials, one section per printer
mkdir -p ~/.x2d
cat > ~/.x2d/credentials <<'EOF'
[printer:studio]
ip     = 192.168.1.42
code   = 12345678
serial = 03ABC0001234567
EOF
chmod 600 ~/.x2d/credentials

# 3) a bearer token for the daemon's HTTP API
openssl rand -hex 24 > ~/.x2d/bridge.token
chmod 600 ~/.x2d/bridge.token

# 4) start the multi-printer daemon (HTTP + MQTT)
beambam daemon \
    --http        0.0.0.0:8765 \
    --auth-token  "$(cat ~/.x2d/bridge.token)" \
    --queue \
    > ~/.x2d/bridge.log 2>&1 &

# 5) start the on-demand camera daemon
#    (lazy-spawns ffmpeg only when HA / a viewer asks; idles after 30s)
beambam camera \
    --bind 0.0.0.0:8766 \
    --idle-timeout 30 \
    > ~/.x2d/camera.log 2>&1 &

# 6) start the HA discovery publisher
beambam ha-publish \
    --broker             192.168.1.10 \
    --broker-port        1883 \
    --broker-username    mqtt_x2d \
    --broker-password    YOUR_BROKER_PASSWORD \
    --daemon-url         http://127.0.0.1:8765 \
    --daemon-token       "$(cat ~/.x2d/bridge.token)" \
    > ~/.x2d/ha-publisher.log 2>&1 &
```

`--broker` takes `host:port` (there is no separate port flag). The broker
user and password can also come from `X2D_HA_USER` / `X2D_HA_PASS`.
Run the three services under systemd, Termux:Boot or Task Scheduler for
a permanent install.

## 3. Find the printer in HA

Within a few seconds of starting `beambam ha`, Settings → Devices &
services → MQTT → Devices lists **Bambu Lab X2D (studio)**. The AMS
entities are added once the first full state report arrives.

If the device does not appear:

- Settings → Devices & services → MQTT → Configure → *Listen to a
  topic*: `homeassistant/#`. No `.../x2d_<serial>/.../config` messages
  means the publisher cannot reach the broker; check
  `~/.x2d/ha-publisher.log`.
- Listen to `x2d/<serial>/state`: the merged printer state that all
  entities read. Empty means the publisher cannot reach the daemon
  (wrong `--daemon-url` / `--daemon-token`) or the daemon has no state.
- `[x2d-ha] ... cannot resolve the printer serial` in the log: add
  `serial =` to the credentials section or pass `--device-serial`.

## 4. Optional configuration.yaml additions

### Slice and print an STL from HA

The daemon's `POST /slice-print` takes the STL as the raw request body.
HA's `rest_command` sends text templates, not file contents, so use a
`shell_command` with `curl` (included in HA OS). Put the header line in
a file so the token stays out of `configuration.yaml`:

```bash
# on the HA host
echo "Authorization: Bearer $(cat bridge.token)" > /config/x2d_auth_header
```

```yaml
# configuration.yaml
shell_command:
  x2d_slice_print: >-
    curl -sf -X POST -H @/config/x2d_auth_header
    -H "X-Filename: {{ filename }}"
    -H "X-Printer: {{ printer | default('studio') }}"
    -H "X-Slot: {{ slot | default(1) }}"
    -H "X-Bed-Type: {{ bed | default('auto') }}"
    -H "Content-Type: application/octet-stream"
    --data-binary "@/config/uploads/{{ filename }}"
    http://192.168.1.50:8765/slice-print
```

The daemon answers with a job id; `GET /slice-print/jobs/<pid>` reports
progress.

### Smoother camera (MJPEG)

The discovered `camera.x2d_<serial>_chamber_camera` shows a still frame
refreshed every 10 s (`X2D_HA_SNAPSHOT_PERIOD`). For live video add the
camera daemon's MJPEG stream:

```yaml
camera:
  - platform: mjpeg
    name: "X2D studio chamber"
    mjpeg_url: http://192.168.1.50:8766/cam.mjpeg
    still_image_url: http://192.168.1.50:8766/cam.jpg
```

## 5. Dashboard examples

### Slice and print button

1. Settings → Devices & services → Helpers → Create helper → Text:
   `input_text.x2d_slice_filename`.
2. Copy STL files to `/config/uploads/` (Samba or File editor add-on).
3. Add a script that passes the helper's value to the shell command
   (dashboard card actions do not render templates, scripts do):

```yaml
# configuration.yaml (or Settings → Automations & scenes → Scripts)
script:
  x2d_slice_selected:
    alias: Slice and print selected STL
    sequence:
      - action: shell_command.x2d_slice_print
        data:
          filename: "{{ states('input_text.x2d_slice_filename') }}"
          printer: studio
          slot: 1
          bed: auto
```

4. Add a card:

```yaml
type: vertical-stack
cards:
  - type: entities
    title: Print STL
    entities:
      - input_text.x2d_slice_filename
  - type: button
    name: Slice and print
    icon: mdi:printer-3d-nozzle
    tap_action:
      action: perform-action
      perform_action: script.x2d_slice_selected
```

### Print controls

```yaml
type: horizontal-stack
cards:
  - type: entity
    entity: sensor.x2d_03abc0001234567_stage
  - type: button
    entity: button.x2d_03abc0001234567_pause
  - type: button
    entity: button.x2d_03abc0001234567_resume
  - type: button
    entity: button.x2d_03abc0001234567_stop
  - type: button
    entity: light.x2d_03abc0001234567_chamber_light
```

### Temperatures and AMS

`number.x2d_studio_bed_set` (slider 0-110), `number.x2d_studio_nozzle_set`
(right nozzle, 0-320), `number.x2d_studio_nozzle_left_set` (left nozzle),
`number.x2d_studio_chamber_set` (slider 0-60). Each nozzle also has
`sensor.…_nozzle_{right,left}_temp` / `_target` and `_tray` (the tray whose
filament is in that nozzle, e.g. `B3`, or `none`).

For each AMS tray the printer reports (A1–D4):
- `sensor.x2d_studio_ams_b3_color` (hex string)
- `sensor.x2d_studio_ams_b3_material` (PLA/PETG/…, `empty`; attributes: name, remain)
- `button.x2d_studio_ams_b3_load` (load that tray)

and for each unit `sensor.…_ams_b_humidity` (%RH), `sensor.…_ams_b_temp`,
`binary_sensor.…_ams_b_damp`. `button.…_ams_unload` unloads the toolhead.
`sensor.x2d_studio_track_switch` reports the Filament Track Switch
(`set up` / `not set up` / `not installed`).

## 6. Multi-printer

Add another `[printer:NAME]` section to the credentials file and restart
`beambam daemon` and `beambam ha`. Each printer becomes its own HA device
with its own topics.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| HA shows "Unavailable" on every entity | publisher can't reach the broker | check `~/.x2d/ha-publisher.log`, verify `--broker` and credentials |
| Entities present but values all 0/empty | bridge daemon isn't pulling state | `~/.x2d/bridge.log`, verify `~/.x2d/credentials` matches the printer |
| Camera entity returns 503 | camera daemon not started OR not reachable | start `beambam camera --bind 0.0.0.0:8766`; verify firewall |
| `rest_command.x2d_slice_print` returns 401 | bearer token mismatch | regenerate `~/.x2d/bridge.token`, restart daemon, update `secrets.yaml` |
| Printer goes "Offline" intermittently | LAN MQTT drops; daemon reconnects | normal under WiFi pressure; `binary_sensor.online` will flap |
| Slice command says "spawned" but nothing prints | firmware silently rejects LAN `print.*` (not in trust list) | known limitation; see [`SIGNED_VS_UNSIGNED.md`](./SIGNED_VS_UNSIGNED.md). Workaround: print from touchscreen / Bambu Handy after slice completes |

## 8. What the bridge gives you on top of stock ha-bambulab

Per [`HA_VS_BAMBULAB.md`](./HA_VS_BAMBULAB.md) — only the deltas.

- Settable temperatures (number sliders), not just read-only sensors.
- Per-tray `button.ams_<tray>_load` for one-tap filament swap, `button.ams_unload`.
- `binary_sensor.timelapse_running`, `binary_sensor.hms_problem`,
  `binary_sensor.print_error`, `binary_sensor.ext_spool_filament`.
- Active tray sensor (which AMS slot is loaded right now).
- Print start_time / end_time timestamps.
- `light.chamber_light` (proper light-domain card support) +
  `switch.x2d_<id>_light` (legacy, for back-compat).
- `switch.prompt_sound` toggle.
- HTTP `/slice-print` endpoint for STL upload + slice + print one-shot.
- Multi-printer first-class.
