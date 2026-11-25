# Design — beambam native Home Assistant integration (HACS) + bridge gap-fix

Date: 2026-06-22
Status: approved (architecture), pending spec review
Author: beambam maintainers

## Goal

Make Home Assistant able to monitor **and fully control** Bambu Lab printers
(X1/P1/A1/H2D/X2D, Jan-2025+ firmware) using beambam, with **no separate
computer and no external MQTT broker required**, installable via **HACS**, and
including the two capabilities the current bridge lacks:

1. **Signed print / start-print with the extracted RSA key** (the `start_print`
   + `url_enc` + device-cert path), so a print can actually be started from HA
   on auth-control firmware (today the only "print" path is `/slice-print`,
   which uses the *unsigned* `print.*` that X-series firmware silently rejects —
   documented as a known limitation in `docs/HA_SETUP.md §7`).
2. **Nozzle and color mapping** — human-readable nozzle type/diameter and AMS
   slot color/material/brand, consuming the in-development `filament_match`
   work (owned by another agent — this integration *consumes*, never edits it).

## Two-layer scope (approved)

- **Layer A — Native HACS integration** (`custom_components/beambam/`): the
  primary deliverable. HA Core itself runs beambam.
- **Layer B — Existing MQTT-discovery bridge gap-fix** (`runtime/ha/publisher.py`
  + `beambam/serve_http.py`): keeps current external-host users working and is
  fully runnable/verifiable on this device (Termux + in-process amqtt broker).

Both layers share new beambam primitives (Layer C).

## Layer C — beambam core enhancements (v1.6.0, additive, backward-compatible)

### C1. Per-call signing-material paths
beambam hardcodes `~/.x2d/printer_sign_key.pem`, `printer_cert_id.txt`,
`printer_device_cert.pem` (`print_job.py:104-105,148-149,406`;
`device_cert.py:31-32`). One HA process driving multiple printers cannot share a
single `~/.x2d`. Add optional path params (defaults preserve current behavior):

- `start_print(client, gcode_filename, *, …, sign_key_path: Path|None = None,
  cert_id: str|None = None, device_cert_path: Path|None = None)`
- `_publish_signed_or_legacy(client, payload, *, sign_key_path=None,
  cert_id=None)` — when supplied, sign with these instead of the `~/.x2d`
  defaults; otherwise unchanged.
- `build_x2d_project_file(…, device_cert_path=None)` already accepts the param;
  `start_print` threads its `device_cert_path` through.

Edits are surgical and confined to the signing functions. `request_auto_nozzle_mapping`
and other nozzle code in `print_job.py` are **not** touched (other agent owns
them); commit promptly to minimize the conflict window.

### C2. New module `beambam/hwmap.py` (new file → no collision)
Pure-data, side-effect-free presentation helpers shared by both layers:

- `nozzle_type_name(code: str) -> str` — e.g. `"HS01"/"hardened_steel"` →
  `"Hardened Steel"`, `"stainless_steel"` → `"Stainless Steel"`. Unknown →
  title-cased passthrough.
- `nozzle_diameter_mm(raw: str|float) -> float|None` — normalize `"0.4"`→`0.4`.
- `material_name(code: str) -> str` — best-effort Bambu `GFx`/material-code →
  human name; unknown → passthrough.
- `ams_slot_summary(state: dict) -> list[dict]` — thin wrapper over
  `beambam.filament_match.ams_loaded_slots(state)` adding `color_hex`
  (`#RRGGBB`, alpha stripped) + `material_name` + `nozzle_temp_min/max`, so both
  layers render identical AMS info without duplicating logic.

### C3. `/control/start_print` HTTP route (`beambam/serve_http.py`)
New POST verb alongside the existing `/control/*` set. Body:
```json
{ "gcode": "name.gcode.3mf", "slot": 0, "use_ams": true,
  "bed_levelling": true, "flow_cali": false, "timelapse": false,
  "vib_cali": false, "local_path": "/abs/path/name.gcode.3mf",
  "bed_type": null, "bed_temp": null }
```
Looks up the live `X2DClient` for the printer (daemon already holds one per
printer), calls `beambam.print_job.start_print(cli, gcode, …)` (auto-signs via
C1 defaults, or `~/.x2d` material on the bridge host). Validation: `gcode`
required; `slot` 0..15; returns `{ok, gcode}` or a structured error (e.g. the
`PrintRefusal` message when AMS/bed checks fail). `local_path` optional — when
absent, bed params must be supplied or derivation is skipped.

### C4. Version + release
- `pyproject.toml` + `beambam/_version.py`: `1.5.1 → 1.6.0`.
- CHANGELOG entry.
- PyPI publish (OIDC trusted publishing via `.github/workflows/release.yml`,
  triggered by a GitHub release). beambam is **already on PyPI** (1.5.0, 1.5.1),
  so this is not a first-time publish. **Authorized**: bump → CHANGELOG →
  commit → create the GitHub release/tag, which triggers
  `.github/workflows/release.yml` (OIDC) to publish 1.6.0 to PyPI. Done after
  the integration is built + tested so the `manifest.json` `beambam>=1.6.0`
  requirement resolves. Verify with `gh run watch` post-release.

## Layer A — Native HACS integration `custom_components/beambam/`

### A1. Files & responsibilities
- `manifest.json` — `domain: beambam`, `name`, `version` (HACS-required on the
  component), `requirements: ["beambam>=1.6.0"]`, `codeowners`, `documentation`,
  `issue_tracker`, `iot_class: local_push`, `config_flow: true`,
  `zeroconf`/`ssdp` discovery blocks, `integration_type: device`.
- `const.py` — `DOMAIN`, config/option keys, defaults, signing-method enum,
  entity descriptions metadata; imports nozzle map from `beambam.hwmap`.
- `__init__.py` — `async_setup_entry` / `async_unload_entry`; builds the
  coordinator; writes per-entry signing material; forwards platforms; registers
  services; `async_reload_entry` on options update.
- `coordinator.py` — `BeambamCoordinator(DataUpdateCoordinator)`. Owns a
  `beambam.mqtt.X2DClient`; runs `connect()` in the executor; `on_state`
  callback marshals pushall JSON into the loop via
  `hass.loop.call_soon_threadsafe(self.async_set_updated_data, state)`. Periodic
  `pushall` poll (configurable, default 5 s) for liveness + availability.
  Exposes `async_publish(payload)`, `async_start_print(...)`,
  `async_send_gcode(line)`, `async_set_filament(...)` (executor-wrapped). Cloud
  mode routes control/print via `beambam.cloud_control`/`http_cloud`.
- `entity.py` — `BeambamEntity(CoordinatorEntity)` base: device_info
  (identifiers `{(DOMAIN, serial)}`, manufacturer "Bambu Lab", model, sw_version
  from firmware), availability from coordinator + last-state staleness.
- Platforms (read `coordinator.data["print"]`, mirror `runtime/ha/publisher.py`):
  - `sensor.py` — temps, progress, layers, remaining, wifi, filename, stage,
    fans, speed profile, HMS count, IP, firmware, start/end time, print
    length/weight, bed type, **nozzle diameter + nozzle type (via hwmap)**,
    AMS humidity, **per-slot color/material/brand (via hwmap/filament_match)**,
    active tray, queue depth.
  - `binary_sensor.py` — online, door, timelapse, hms_problem, print_error,
    per-AMS-unit humidity warn.
  - `switch.py` — prompt sound; (legacy) light-as-switch optional.
  - `light.py` — chamber light (brightness-less on/off).
  - `button.py` — pause, resume, stop, home, level, buzzer-silence, per-AMS-slot
    load, **start last/selected print**.
  - `number.py` — bed/nozzle/chamber setpoints.
  - `camera.py` — **live stream is a hard requirement.** A `BeambamCamera`
    entity with `CameraEntityFeature.STREAM`:
    - **RTSPS models (X1/X2D/H2D)**: `stream_source()` returns
      `rtsps://bblp:<code>@<ip>:322/streaming/live/1`; HA's bundled
      stream/go2rtc/ffmpeg renders live HLS/WebRTC on the dashboard.
      `async_camera_image()` grabs a single frame via ffmpeg (cached) for the
      still/thumbnail. ffmpeg ships with HA.
    - **Proprietary models (P1/A1)**: run beambam's
      `runtime/network_shim/lvl_local.stream_frames(ip, code, on_frame=…)` in an
      executor thread into a latest-frame buffer; serve live via
      `handle_async_mjpeg_stream` and stills via `async_camera_image()`.
    - Optional override: an explicit external camera-daemon URL
      (`/cam.mjpeg` + `/cam.jpg`) when the user already runs `beambam camera`.
    `image.py` provides a retained-snapshot fallback entity.
- `services.yaml` + handlers — `beambam.start_print` (file on printer or
  upload+print), `beambam.send_gcode`, `beambam.set_filament`,
  `beambam.refresh`. Field schemas in `services.yaml`.
- `config_flow.py` — see A2.
- `strings.json` + `translations/en.json` — flow + service + entity strings.
- `diagnostics.py` — redacted dump (creds/keys removed) of config + last state.

### A2. Config flow (supports all three signing methods)
- **Step `user`**: name, host/IP, access code, serial, model (auto-suggested
  from discovery when launched via zeroconf/ssdp). Validates by connecting an
  `X2DClient` + one `request_state` in the executor; aborts on
  `already_configured` (unique_id = serial).
- **Step `signing`** (menu): `none` | `paste` | `file` | `cloud`.
  - `paste` — multiline fields for sign-key PEM + cert_id (+ optional device
    cert PEM). Device cert auto-fetched from the printer
    (`device_cert.fetch_device_cert`) if omitted. Material written to
    `hass.config.path(".storage/beambam/<entry_id>/")` (0600).
  - `file` — paths to existing PEM/txt files on the HA host; validated +
    copied into the per-entry dir.
  - `cloud` — Bambu cloud email/password (or token); provisions device
    cert/cert_id and enables cloud-routed signed control/print + cloud-slice.
    Cloud creds stored in entry data (`.storage`, like other HA cloud
    integrations).
- **Options flow** — change snapshot/camera URL, poll interval, re-run the
  signing step.

### A3. Discovery
- zeroconf: Bambu mDNS service type(s); ssdp: Bambu SSDP signature. Both set
  `unique_id` from the advertised serial and pre-fill the `user` step so HA
  surfaces a "Discovered: Bambu Lab <model>" card → "Add".

## Layer B — Existing bridge gap-fix

- `runtime/ha/publisher.py`: add a **Start print** button entity (publishes to
  `<base>/print/set` payload `START`; the publisher forwards to the daemon's new
  `/control/start_print`, using a configured default file/slot or the last job),
  and **nozzle/color mapping** sensors (nozzle type via `beambam.hwmap`,
  per-slot color/material/brand) so the discovery bridge reaches parity with the
  native integration. `_on_message` gains a `START` branch.
- `beambam/serve_http.py`: the C3 `/control/start_print` route (shared).
- Docs reference `beambam boo` (canonical) instead of `daemon` (now an alias).

## Verification plan (honest about Termux limits)

1. **Layer C unit tests** (`tests/`): signing-path params route correctly
   (monkeypatch signer; assert the supplied key/cert_id are used, defaults
   unchanged); `hwmap` mappings; `/control/start_print` route via a fake client.
2. **Layer B end-to-end on-device**: run `beambam boo --http 127.0.0.1:PORT` +
   `beambam ha-publish` against an in-process **amqtt** broker (as
   `runtime/ha/test_ha.py` already does); assert the new discovery configs
   (start_print button, nozzle/color sensors) + a START command round-trips to
   `/control/start_print`. This is the live "run daemon and confirm it's
   discoverable" check.
3. **Layer A**: `pytest-homeassistant-custom-component` in a python3.12 venv —
   test `config_flow` (all signing branches), `async_setup_entry`, and entity
   state derived from a captured pushall JSON sample. If feasible, load the
   component into the proot-Ubuntu HA from `docs/HA.md §4` and confirm it appears
   under "Add Integration" / via discovery.
4. Report precisely what executed vs. what needs the user's live HA.

## Risks / non-goals

- **PyPI publish** of 1.6.0 is the gating step for HACS install; **authorized**
  via the GH Actions release flow (done after build + test).
- **Brand icons** live in the separate `home-assistant/brands` repo — tracked as
  a follow-up PR, not a blocker for HACS (custom repos work without brands).
- **Cloud-login in HA** adds a cloud-auth path + stores cloud creds in
  `.storage`; kept optional and isolated.
- **Camera** is a required live-stream feature (see A1); RTSPS models lean on
  HA's bundled stream stack, proprietary models on beambam's `lvl_local` pump.
- **Slicing** is out of scope for in-core HA (needs BambuStudio); the
  integration starts already-sliced jobs (LAN signed or upload+print) and can
  delegate slicing to cloud-slice (cloud mode) or an external host.
- Full "add it in your HA" end-to-end ultimately needs the user's instance.
