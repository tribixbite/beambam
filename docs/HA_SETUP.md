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

# 4) daemon: printer connection + HTTP API
beambam daemon \
    --http       0.0.0.0:8765 \
    --auth-token "$(cat ~/.x2d/bridge.token)" \
    > ~/.x2d/bridge.log 2>&1 &

# 5) camera daemon (starts ffmpeg only while someone watches)
beambam camera --bind 0.0.0.0:8766 --idle-timeout 30 \
    > ~/.x2d/camera.log 2>&1 &

# 6) Home Assistant publisher (one HA device per credentials section)
beambam ha \
    --broker           192.168.1.10:1883 \
    --broker-username  mqtt_beambam \
    --broker-password  YOUR_BROKER_PASSWORD \
    --daemon-url       http://127.0.0.1:8765 \
    --daemon-token     "$(cat ~/.x2d/bridge.token)" \
    --device-model     X2D \
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

- Sliders: `number.x2d_<serial>_bed_set` (0-110),
  `number.x2d_<serial>_nozzle_set` (0-320; right nozzle on two-nozzle
  printers), `number.x2d_<serial>_chamber_set` (0-60).
- Two-nozzle printers: `sensor.x2d_<serial>_left_nozzle_temp`,
  `..._right_nozzle_temp`, the matching `_target` sensors and
  `sensor.x2d_<serial>_active_nozzle`.
- Per tray (slot 1-16 = AMS A1 ... D4):
  `sensor.x2d_<serial>_ams_slot<N>_color`, `..._material` (attributes:
  remaining %, sub-brand, filament id) and
  `button.x2d_<serial>_ams_slot<N>_load`.
- Per unit (0-3 = A-D): `sensor.x2d_<serial>_ams_unit<U>_humidity` (%),
  `..._humidity_level`, `..._temp`,
  `binary_sensor.x2d_<serial>_ams_unit<U>_humidity_warn`.
- `sensor.x2d_<serial>_active_tray`: loaded slot 1-16, or 0 for none /
  external spool.

## 6. Multi-printer

Add another `[printer:NAME]` section to the credentials file and restart
`beambam daemon` and `beambam ha`. Each printer becomes its own HA device
with its own topics.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Every entity "Unavailable" | publisher not connected to the broker, or the daemon has not heard the printer for 30 s | check `~/.x2d/ha-publisher.log`; `binary_sensor.x2d_<serial>_online` shows the printer link |
| Entity ids generated from names (`sensor.bambu_lab_x2d_...`) | Home Assistant older than 2025.10 | upgrade HA, then delete and re-add the device |
| Two devices for one printer after upgrading | old configs under `x2d_<name>` | restart `beambam ha` once; it clears them |
| Firmware sensor / update entity unknown | the daemon never requests `info.get_version` | known gap, see [`HA.md`](./HA.md) §3 |
| Camera entity shows no image | camera daemon not running or not reachable from the daemon | start `beambam camera`; check `http://<host>:8765/snapshot.jpg` |
| `shell_command` fails with 401 | token mismatch | regenerate `bridge.token`, restart the daemon, rewrite `/config/x2d_auth_header` |
| Slice job reports success but nothing prints | the printer rejected the LAN print command | see [`SIGNED_VS_UNSIGNED.md`](./SIGNED_VS_UNSIGNED.md) |
