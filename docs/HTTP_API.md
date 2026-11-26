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
beambam.boo/print and pokefit.party ask for it when the daemon's address is not this device, and keep it in
that browser.
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

A public https site may call a daemon on `127.0.0.1` from the same device: the address counts as secure, so
there is no mixed-content block. Chrome asks the user once to let the site reach apps on the device. Its older
Private Network Access preflight gets `Access-Control-Allow-Private-Network: true`, for allowed origins only.
This is how pokefit.party prints from a phone that runs beambam:

```bash
beambam daemon --http 127.0.0.1:8765 --cors-origin https://pokefit.party
```

## Reading

| Route | Answer |
|---|---|
| `GET /state` | The printer's merged report (`print`, plus `info` with firmware versions); the cached copy, so it answers in milliseconds |
| `GET /state.events` | Server-sent events: the report on every printer push |
| `GET /ams` | The AMS block of the report |
| `GET /healthz` | 200 while the printer reports, 503 when it went silent; `version`, `capabilities`, and `camera` (the camera proxy answers) |
| `GET /camera.mjpeg` | The chamber camera, live (`multipart/x-mixed-replace`, renders in an `<img>`), relayed from the camera proxy (`beambam cam start`, or `daemon --camera`); 503 with the reason when the proxy is down or 4 viewers already watch. A browser page from another site needs an allowed Origin (`--cors-origin`), else 403. beambam.boo and pokefit.party read it with `fetch` (so it carries `Authorization` and Chrome's Local Network Access for a LAN daemon) and split the parts by `Content-Length`; a plain `<img crossorigin="anonymous">` works for a loopback daemon without a token |
| `GET /snapshot.jpg` | One frame from the camera proxy |
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
The plan also carries `digest`. Send it back as `plan_digest` with `/print`, and beambam starts only that plan: if
the spools, the plate or the file changed since, `/print` answers 409 with `plan_changed: true` and the new
`plan`. That way `accept_warnings` never covers a warning the user did not see.
One print is sent to a printer at a time.

## Slicing a project

A caller that builds a Bambu Studio project but cannot slice it (a web page) can have the daemon's machine slice
it with the bundled headless Bambu Studio (`bs-bionic`):

```bash
curl -s -X POST --data-binary @figure.3mf -H 'X-Filename: figure.3mf' http://127.0.0.1:8765/slice
# → 202 {"job": {...}, "poll": "/slice/jobs/9c1e…"}
curl -s http://127.0.0.1:8765/slice/jobs/9c1e…
# → {"state": "done", "file": "/…/.x2d/uploads/sliced/9c1e…/figure.gcode.3mf",
#    "seconds": 6387, "grams": 18.7, "grams_per_filament": [...], "warnings": [...]}
```

- The project's own process and filament settings are kept.
- The printer profile is reloaded from bs-bionic's bundled profile of the same name (`printer_settings_id`). A
  project saved by a newer Bambu Studio can carry start G-code this build cannot parse.
- The headless CLI resets `has_filament_switcher` and `enable_filament_dynamic_map` to off unless they are given as
  options, whatever the project says; the daemon passes them on (`--has-filament-switcher=1`) when the project has
  them on. It also turns the switch on when the printer reports one: the slice is for the printer it will print
  on, as Bambu Studio does once it syncs the printer (a project saved without it would otherwise plan with
  "sliced without the Filament Track Switch").
- The print file is named after `X-Filename` (the printer shows it as the job).
- One slice runs at a time; another `POST /slice` meanwhile gets 409. Job states: `queued`, `slicing`, `done`,
  `failed` (with `message`).
- The finished `file` goes straight into `/print/plan` and `/print`.
- A refused slice names the setting Bambu objected to (from its log), e.g. `… (code -51): Too small line width`.

### Meshes and slice options

`/slice` also takes a bare mesh, `X-Filename: model.stl` (`.obj`, `.step` / `.stp`, or a 3MF with no print
settings, such as another slicer's geometry export). It is placed on the bed and sliced with bs-bionic's bundled
X2D 0.4 mm profiles, for the printer's Filament Track Switch when the printer has one.

Optional header `X-Slice-Options` (compact JSON, up to 4 KB) applies to projects and meshes alike:

```json
{"layer_height": 0.2, "wall_loops": 3, "infill": 20,
 "supports": "auto", "brim": "no_brim",
 "nozzles": [1, 2, 2, 1],
 "bed_type": "Supertack Plate",
 "spools": [{"id": "GFG02", "type": "PETG", "colour": "#F4EE2A"}, {"id": null, "type": null, "colour": "#FFFFFF"}],
 "filament_ids": ["GFA00"], "filament_types": ["PLA"]}
```

- `supports`: `auto` (Bambu adds them only where needed), `plate` (the same, standing on the plate only), `off`.
- `brim`: `auto_brim`, `outer_only`, `no_brim`. `layer_height` 0.08–0.28 mm; a layer as tall as one of the
  process's explicit line widths widens those lines to layer + 0.02 mm (Bambu refuses a line no wider than the
  layer).
- `nozzles`: the nozzle of each filament, 1 = left, 2 = right (Bambu's "Manual" grouping). For a project it is
  written into the project settings and every plate: Bambu Studio's CLI reads a plate's grouping before the
  project's. Read back the result from `/print/plan`: it names the nozzle each filament was really sliced on.
- `filament_ids` / `filament_types` (meshes): the bundled filament profile per filament, by the spool's
  `tray_info_idx` (e.g. `GFA00`), else by material, else Bambu PLA Basic.
- `bed_type`: the build plate, one of `Cool Plate`, `Engineering Plate`, `High Temp Plate`, `Textured PEI Plate`,
  `Supertack Plate` (Bambu's `curr_bed_type`; its bed temperatures and first layer follow).
- `spools`: per filament, the spool it prints from. A project's filament i takes the spool's bundled system preset
  (by `id`, else `type`) and its colour, written into the project settings (`beambam/project_sync.py`; the CLI's
  `--load-filaments` crashes on X2D projects); `id` and `type` null keeps the slot's preset and sets only the colour.
  A mesh takes the presets.
- Unknown keys or values out of range answer 400.

`POST /project/update` takes the same body, `X-Filename` and `X-Slice-Options` as `/slice` for a project and
answers the project with the options applied, as the `.3mf` to keep (`Content-Disposition: <name>_printer.3mf`):
what `/slice` would slice. 400 for a sliced file or a 3MF without print settings. `X-Beambam-Warnings` (a JSON list)
says what the spools could not change: a spool with no bundled profile, or a project for another nozzle size, keeps
the project's preset. A slice job's `warnings` say the same, and it reports `grams_by_filament` (by filament
number), `plates`, `plate_seconds` and `plate_grams` (a print starts plate 1 unless `/print/plan` and `/print` name another `plate`).

`/print/plan` also compares the plate: when the printer reports a known plate marker (`device.plate.cur_id`;
P0301 = SuperTack, `filament_mapping.PLATE_MARKERS`) and the file was sliced for another plate, `warnings` says so
(confirmed like a spool warning) and `bed` gives `{file, printer, printer_marker}`. Warnings the slicer writes into
the print file (e.g. a plate too hot for a filament) are added to the slice job's `warnings`.

`GET /healthz` lists `capabilities`; web clients check `slice-options`, `slice-mesh`, `camera`, `plate-check` and
`project-sync` before using these.

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
