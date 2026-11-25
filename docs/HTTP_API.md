# Controlling the printer from another app (HTTP API)

`beambam daemon --http` keeps one connection to each printer and serves its state and controls over
HTTP. It is the way for a separate program — a web app's backend, a dashboard, a script, Home Assistant
(see [HA.md](HA.md)) — to read and drive the printer without starting `beambam` for every request.
pokefit uses it this way ([pokefit `docs/integration.md`](https://github.com/tribixbite/pokefit/blob/main/docs/integration.md)).

```bash
beambam daemon --http 127.0.0.1:8765 --quiet
# another machine on the LAN:
beambam daemon --http 0.0.0.0:8765 --auth-token "$(openssl rand -hex 16)" --quiet
```

A loopback bind needs no token. Any other bind requires one, sent as `Authorization: Bearer <token>`.
`?printer=NAME` picks a `[printer:NAME]` section of `~/.x2d/credentials`. Without it, the default printer is used.

## Browsers and other origins

A POST carrying an `Origin` header is refused (403) unless it comes from the daemon's own web UI or an
origin you allow:

```bash
beambam daemon --http 127.0.0.1:8765 --cors-origin https://pokefit.party --cors-origin http://localhost:5173
# or BEAMBAM_CORS_ORIGINS=https://pokefit.party,http://localhost:5173
```

Allowed origins get `Access-Control-Allow-Origin` and preflight (`OPTIONS`) answers. This stops any web
page open in your browser from stopping a print or starting one through a daemon on `127.0.0.1`. Programs that send no
`Origin` (curl, Home Assistant, a server-side app) are not affected.

## Reading

| Route | Answer |
|---|---|
| `GET /state` | The printer's merged report (`print`, plus `info` with firmware versions); the cached copy, so it answers in milliseconds |
| `GET /state.events` | Server-sent events: the report on every printer push |
| `GET /ams` | The AMS block of the report |
| `GET /healthz` | 200 while the printer reports, 503 when it went silent |
| `GET /doctor` | `beambam doctor`'s checks on the cached report |
| `POST /analyze` | Raw `.gcode.3mf` body → `beambam analyze` report (time, weight, purge, nozzle swaps) |

## Printing a sliced file

Printing takes two steps, as on the terminal: get the plan, show it to the user, and start only with their OK.

```bash
# 1. (daemon on another machine) send the file; the reply names its path there
curl -s -X POST --data-binary @fig.gcode.3mf -H 'X-Filename: fig.gcode.3mf' http://printer-host:8765/print/upload

# 2. the filament → tray plan against the live AMS; touches nothing
curl -s -X POST -d '{"file": "/path/fig.gcode.3mf", "slot": [13]}' http://127.0.0.1:8765/print/plan

# 3. start it: needs "confirm": true, and "accept_warnings": true if the plan had warnings
curl -s -X POST -d '{"file": "/path/fig.gcode.3mf", "slot": [13], "confirm": true, "accept_warnings": true}' \
     http://127.0.0.1:8765/print
# → 202 {"job": {...}, "poll": "/print/jobs/3f2a…"}

# 4. follow it: queued → uploading → starting → accepted | sent | rejected | failed
curl -s http://127.0.0.1:8765/print/jobs/3f2a…
```

The request body takes:
- `file`: a `.gcode.3mf` on the daemon's machine.
- `plate`: which plate (default 1).
- `slot`: one tray per used filament, or Bambu Studio's filament-indexed list, or text like `"4,0"`. Without it, trays are chosen automatically.
- `map`: `"1=B3,2=ext-left"`.
- `use_ams`, `bed_levelling`, `flow_cali`, `timelapse`, `vibration_cali`: on/off options.

The bed type and bed temperature always come from the file.

The plan reply is `beambam ams map --json`'s plan plus `ok` and `needs_confirmation`:
- `filaments`: each with its tray, `errors` and `warnings`.
- `errors` and `warnings`: flat lists of the same.
- `notes`.
- `ams_mapping` and `ams_mapping2`.

Errors refuse the print however it is asked:
- an empty or missing tray;
- a different material;
- a tray that cannot reach the filament's nozzle.

Warnings (brand, profile, colour) refuse unless `accept_warnings` is true. Only set it after the user has seen them.
One print is sent to a printer at a time.

## Controls

`POST /control/<verb>` takes a JSON body:
- `pause`, `resume`, `stop`;
- `light {"state": "on|off|flashing"}`;
- `temp {"target": "bed|nozzle|chamber", "value": 60, "idx": 1}` (`idx` 1 is the left nozzle);
- `ams_load {"tray": 5, "extruder": 0}`, where the tray is the global id: A1 = 0 … D4 = 15, 254/255 are the external spools. The old `{"slot": 1..16}` still works;
- `ams_unload`;
- `ams_set {"slot", "tray_type", "tray_color", …}`;
- `gcode {"line": "G28"}`;
- `sound {"state": "on|off"}`.

`POST /slice-print` slices and prints a raw STL/3MF body. It takes these headers:
- `X-Filename`;
- `X-Printer`;
- `X-Slot` (default: trays chosen automatically);
- `X-Bed-Type`;
- `X-No-AMS`;
- `X-Accept-Warnings: 1`, to accept mapping warnings the caller has shown.
