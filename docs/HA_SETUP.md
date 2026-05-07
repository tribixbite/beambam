# Home Assistant — install + dashboard guide

End-to-end recipe for adding the Bambu X2D bridge to Home Assistant.
Result: HA discovers the printer, exposes 60+ entities (sensors, binary
sensors, light, switches, buttons, number sliders, camera, image), and
gives you a one-tap **Slice & Print STL** action on the dashboard.

For the architecture write-up see [`HA.md`](./HA.md). Feature-by-feature
parity with `greghesp/ha-bambulab` is logged in
[`HA_VS_BAMBULAB.md`](./HA_VS_BAMBULAB.md).

---

## 1. Prerequisites

- **Home Assistant** 2024.1+ (Core, OS, Container, or Supervised).
- **MQTT broker** reachable from BOTH the HA host and the bridge host.
  Easiest: install the **Mosquitto broker** add-on from HA OS Add-on
  Store, then add a user (e.g. `mqtt_x2d` + a random password) under
  Settings → Add-ons → Mosquitto → Configuration.
- **HA's MQTT integration** enabled and pointed at that broker
  (Settings → Devices & Services → Add Integration → MQTT → broker host
  is `core-mosquitto` for the add-on, or your IP).
- **One Linux host** on the same LAN as the X2D — Termux phone, a
  Raspberry Pi, an x86 NUC. Doesn't have to be the HA host.
- For the **camera** (live MJPEG): the bridge's HTTP port must be
  reachable from HA. If HA is on a different machine, bind the bridge
  daemon and camera daemon to `0.0.0.0` (instructions below).

## 2. Bridge daemon setup

```bash
# 1) clone + install
git clone https://github.com/tribixbite/x2d ~/x2d && cd ~/x2d
./install.sh                                 # installs python3.12 deps

# 2) printer credentials (one INI section per printer)
mkdir -p ~/.x2d
cat > ~/.x2d/credentials <<'EOF'
[printer:studio]                              # any name you like
ip     = 192.168.1.42                         # your printer's LAN IP
code   = 12345678                             # access code (touchscreen)
serial = 03ABC0001234567                      # touchscreen → About
EOF

# 3) generate a bearer token for the bridge HTTP API
openssl rand -hex 24 > ~/.x2d/bridge.token
chmod 600 ~/.x2d/bridge.token

# 4) start the multi-printer daemon (HTTP + MQTT)
python3.12 x2d_bridge.py daemon \
    --http        0.0.0.0:8765 \
    --auth-token  "$(cat ~/.x2d/bridge.token)" \
    --queue \
    > ~/.x2d/bridge.log 2>&1 &

# 5) start the on-demand camera daemon
#    (lazy-spawns ffmpeg only when HA / a viewer asks; idles after 30s)
python3.12 x2d_bridge.py camera \
    --bind 0.0.0.0:8766 \
    --idle-timeout 30 \
    > ~/.x2d/camera.log 2>&1 &

# 6) start the HA discovery publisher
python3.12 x2d_bridge.py ha-publish \
    --broker             192.168.1.10 \
    --broker-port        1883 \
    --broker-username    mqtt_x2d \
    --broker-password    YOUR_BROKER_PASSWORD \
    --daemon-url         http://127.0.0.1:8765 \
    --daemon-token       "$(cat ~/.x2d/bridge.token)" \
    > ~/.x2d/ha-publisher.log 2>&1 &
```

For long-running systems use systemd / a Termux:Boot script — run
`./install.sh` again on first boot for the systemd templates.

## 3. Find the printer in HA

Within ~5 seconds of starting `ha-publish`:

1. Settings → Devices & Services → MQTT → Devices.
2. **"Bambu Lab X2D (studio)"** appears with all entities listed under
   it.

If it doesn't show up:

- Developer Tools → MQTT → Listen to `homeassistant/#` for ~10s. You
  should see `homeassistant/sensor/x2d_<serial>/...` discovery
  messages. If absent, the publisher can't reach the broker — check
  `~/.x2d/ha-publisher.log`.
- Watch state arrive on `x2d/<serial>/state` — that's the raw
  pushall JSON the entities key off.

## 4. configuration.yaml additions

Add these blocks. The HA broker auto-discovery covers everything else.

```yaml
# secrets.yaml
x2d_bridge_bearer: "Bearer 0123abcd0123abcd…"   # cat ~/.x2d/bridge.token

# configuration.yaml
rest_command:
  x2d_slice_print:
    url: "http://192.168.1.50:8765/slice-print"
    method: POST
    headers:
      authorization:  !secret x2d_bridge_bearer
      x-filename:     "{{ filename }}"
      x-printer:      "{{ printer | default('studio') }}"
      x-slot:         "{{ slot    | default(1) }}"
      x-bed-type:     "{{ bed     | default('auto') }}"
    content_type: "application/octet-stream"
    payload:  "{{ stl_bytes }}"
    timeout:  120

# (optional) Low-latency MJPEG camera platform alongside the auto-discovered
# MQTT camera. The MQTT camera is bandwidth-cheap (~6 KB/s); MJPEG is smoother
# (~5 Mbps).
camera:
  - platform: mjpeg
    name: "X2D Studio chamber"
    mjpeg_url: http://192.168.1.50:8766/cam.mjpeg
    still_image_url: http://192.168.1.50:8766/cam.jpg
```

## 5. Add to dashboard

### Camera card

Edit dashboard → Add card → Picture Glance.

- **Entity** = `camera.bambu_lab_x2d_studio_chamber_camera_live` (the
  auto-discovered MQTT camera; uses a few KB/s of bandwidth, retains
  last frame across restarts), OR `camera.x2d_studio_chamber` (the
  MJPEG one, smoother live, requires `0.0.0.0:8766` reachable from HA).

### Slice & Print STL widget

1. Settings → Devices & Services → Helpers → Create Helper → Text →
   name `x2d_slice_filename`, max 120 chars.
2. (One-time) Put STL files into `/config/uploads/` on the HA host
   (e.g. via the Samba add-on or HA's File Editor).
3. Add a Lovelace card:

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
      action: call-service
      service: rest_command.x2d_slice_print
      data:
        filename: "{{ states('input_text.x2d_slice_filename') }}"
        printer: studio
        slot: 1
        bed: auto
        # Read the file at action-time; HA assembles raw bytes.
        stl_bytes: !include_bin /config/uploads/{{ states('input_text.x2d_slice_filename') }}
```

### Print controls row

```yaml
type: tile
features:
  - type: button
    icon: mdi:pause
    entity: button.x2d_studio_pause
  - type: button
    icon: mdi:play
    entity: button.x2d_studio_resume
  - type: button
    icon: mdi:stop
    entity: button.x2d_studio_stop
  - type: button
    icon: mdi:lightbulb
    entity: light.x2d_studio_chamber_light
```

### Temperatures + AMS

`number.x2d_studio_bed_set` (slider 0-110), `number.x2d_studio_nozzle_set`
(slider 0-320), `number.x2d_studio_chamber_set` (slider 0-60).

For each AMS slot:
- `sensor.x2d_studio_ams_slot1_color` (hex string)
- `sensor.x2d_studio_ams_slot1_material` (PLA/PETG/...)
- `button.x2d_studio_ams_slot1_load` (one-tap load to extruder)

`sensor.x2d_studio_active_tray` shows which slot (1-4) is currently
loaded into the toolhead, or 0 when empty.

## 6. Multi-printer

Add another `[printer:NAME]` section to `~/.x2d/credentials` and
restart `ha-publish`. The publisher fans out one MQTT-discovery
"device" per printer; each gets all 60+ entities under its own
hierarchy. No changes to HA configuration needed.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| HA shows "Unavailable" on every entity | publisher can't reach the broker | check `~/.x2d/ha-publisher.log`, verify `--broker` and credentials |
| Entities present but values all 0/empty | bridge daemon isn't pulling state | `~/.x2d/bridge.log`, verify `~/.x2d/credentials` matches the printer |
| Camera entity returns 503 | camera daemon not started OR not reachable | start `x2d_bridge.py camera --bind 0.0.0.0:8766`; verify firewall |
| `rest_command.x2d_slice_print` returns 401 | bearer token mismatch | regenerate `~/.x2d/bridge.token`, restart daemon, update `secrets.yaml` |
| Printer goes "Offline" intermittently | LAN MQTT drops; daemon reconnects | normal under WiFi pressure; `binary_sensor.online` will flap |
| Slice command says "spawned" but nothing prints | firmware silently rejects LAN `print.*` (not in trust list) | known limitation; see [`SIGNED_VS_UNSIGNED.md`](./SIGNED_VS_UNSIGNED.md). Workaround: print from touchscreen / Bambu Handy after slice completes |

## 8. What the bridge gives you on top of stock ha-bambulab

Per [`HA_VS_BAMBULAB.md`](./HA_VS_BAMBULAB.md) — only the deltas.

- Settable temperatures (number sliders), not just read-only sensors.
- Per-slot `button.ams_slotN_load` for one-tap filament swap.
- `binary_sensor.timelapse_running`, `binary_sensor.hms_problem`,
  `binary_sensor.print_error`, `binary_sensor.door_open`.
- Active tray sensor (which AMS slot is loaded right now).
- Print start_time / end_time timestamps.
- `light.chamber_light` (proper light-domain card support) +
  `switch.x2d_<id>_light` (legacy, for back-compat).
- `switch.prompt_sound` toggle.
- HTTP `/slice-print` endpoint for STL upload + slice + print one-shot.
- Multi-printer first-class.
