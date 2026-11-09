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

Commands to printers on Bambu's authorization-control firmware are the
difference that remains. Bambu's
[third-party integration page](https://wiki.bambulab.com/en/software/third-party-integration)
says third-party control needs either authorization or **Developer
Mode**, which runs only in LAN-only mode (no Bambu cloud). beambam signs
its commands instead (see [`SIGNED_VS_UNSIGNED.md`](./SIGNED_VS_UNSIGNED.md)),
so its HA controls work without Developer Mode. Monitoring works with
either.

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

## 4. Switching from ha-bambulab

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
