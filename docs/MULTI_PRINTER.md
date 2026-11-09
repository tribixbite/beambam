# Multi-printer setup

The bridge daemon, MCP server, web UI, HA publisher, and queue manager
all support multiple printers from one process. Add a `[printer:NAME]`
section per printer in `~/.x2d/credentials`; everything else
auto-discovers them.

## Credentials

```ini
# ~/.x2d/credentials

[printer:studio]
ip     = 192.168.1.42
code   = 12345678
serial = 03ABC0001234567

[printer:garage]
ip     = 192.168.1.43
code   = 87654321
serial = 03DEF0007654321
```

The plain `[printer]` (no name) is also valid — it's reported as
the empty string in API responses. You can mix `[printer]` plus
`[printer:NAME]` sections; the unnamed one becomes the default.

## What runs against multiple printers

* **`beambam daemon`** — spawns one X2DClient per section, all
  sharing one HTTP server on `--http`. State, /state.events, /metrics,
  /healthz, /control/* are all `?printer=NAME` routed.
* **`beambam status` / `pause` / etc.** — pass `--printer NAME`
  (or set `X2D_PRINTER`) to target a specific section.
* **`beambam ha-publish`** — without `--printer`, spawns one
  HAPublisher thread per section in the same process; each gets its
  own HA Device with namespaced topics. Failures are isolated
  per-printer.
* **`beambam serve`** (the `libbambu_networking.so` shim
  endpoint) — registers each printer with `DeviceManager::on_machine_alive`
  so the BambuStudio GUI's Device-tab dropdown lists all of them.
* **Web UI** — printer dropdown in the header re-targets every card.
* **Print queue** — each job is enqueued for a specific printer;
  the per-printer auto-dispatch loops are independent.

## Routing in the API

```bash
# Pull state for one named printer
curl http://127.0.0.1:8765/state?printer=studio

# Pause the studio printer (not garage)
curl -X POST -H 'Content-Type: application/json' -d '{}' \
    'http://127.0.0.1:8765/control/pause?printer=studio'

# Subscribe to live SSE for garage
curl -N http://127.0.0.1:8765/state.events?printer=garage

# /metrics has per-printer labels for every gauge
curl http://127.0.0.1:8765/metrics | grep printer=
```

## Print-control signing is per printer

LAN print control (`pause`/`resume`/`stop`/`gcode`, and the X2D/H2D
start-print `url_enc`) is signed with a per-install key recovered by
`beambam key`, plus the printer's device cert from `beambam device-cert`.
These secrets are **keyed by serial** so a second printer never signs
with the first one's key or encrypts its file to the wrong device:

```
~/.x2d/
  printer_sign_key.pem          # legacy global (still read as a fallback)
  <serial-A>/printer_sign_key.pem
  <serial-A>/printer_cert_id.txt
  <serial-A>/printer_device_cert.pem
  <serial-B>/printer_sign_key.pem
  ...
```

`beambam key` / `beambam device-cert` write into `~/.x2d/<serial>/`
(resolved from the target printer's credentials); the signing path reads
`~/.x2d/<serial>/<file>` first and falls back to the global file, so an
existing single-printer install keeps working unchanged. Run each
setup command once per printer, e.g.
`beambam device-cert --printer garage` then `beambam key --printer garage`.

## SSDP auto-discovery

If you don't want to write the credentials file by hand, the bridge's
`serve` mode listens for Bambu's SSDP NOTIFYs on UDP 2021 multicast
and shows you what's on the LAN:

```bash
beambam serve --debug 2>&1 | grep ssdp
```

(Then add the matching `[printer:NAME]` section with the discovered
IP + serial.)

## Test harnesses

```bash
PYTHONPATH=. python3.12 runtime/ha/test_multi_printer.py  # 20/20 PASS
```

That harness drives two daemons + two HAPublishers + one MQTT broker
and verifies command isolation, distinct device_ids, disjoint topic
sets, and graceful per-printer teardown.
