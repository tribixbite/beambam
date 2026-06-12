# MakerWorld cloud-slice / cloud-print API (reverse-engineered 2026-06-12)

Captured live by driving a real X2D print of MakerWorld model 2831282 through
Bambu Handy while the x2dcap Zygisk SSL_write hook recorded Handy's traffic, then
decoding the HTTP/2 with `analyze_capture.py` (REST) — see `extract_project_file.py`
for the MQTT side.

## The key finding

A **cloud-slice print is triggered by ONE signed REST call from Handy.** The cloud
then slices for the target printer, **signs and publishes the `print.project_file`
itself**, and the printer prints. Handy never publishes the project_file — which is
why subscribing to `device/<serial>/request` or hooking Handy's MQTT can't capture
it, and why beambam-built project_files hit `err 84033544` (the X2D dual-nozzle
task fields were missing). **Calling this endpoint subsumes the whole
project_file problem** — beambam wouldn't build or sign the MQTT command at all.

## Endpoint

```
POST https://api.bambulab.com/v1/user-service/my/task?ref_=def_MWPreparePrint_PrintNow_MWProfileSelect_UNKNOWN
```

Headers (the ones that matter):
- `authorization: Bearer <cloud access_token>`   (we have this)
- `content-type: application/json`
- `x-bbl-app-certification-id: CN=GLOF1000000000.bambulab.com:0123456789abcdef0123456789abcdef`
  — the app cert id (same cert as the MQTT signing; note the `CN=...:<hex>` order
  is the reverse of the MQTT `cert_id`'s `<hex>CN=...`).
- `x-bbl-device-security-sign: <base64 256-byte RSA-2048 signature>`
  — **SOLVED (2026-06-12).** It is a RAW PKCS#1 v1.5 (type 1) RSA signature of the
  **ASCII millisecond-timestamp string** — NOT a hash, NOT the body:
  `EM = 00 01 FF…FF 00 || ascii(str(now_ms))`, `sig = EM^d mod n`, header =
  base64(sig). Signed with the recovered app key (`~/.x2d/printer_sign_key.pem`).
  The cloud RSA-decrypts it to recover the timestamp (anti-replay) and trusts the
  app cert; body integrity rides on TLS. Reproduces real Handy signatures
  byte-for-byte. Implemented in `beambam/cloud_slice.py`
  (`device_security_sign` / `signed_headers` / `cloud_slice_print`).

Poll the task: `GET /v1/iot-service/api/user/task/{taskId}` + `GET /v1/user-service/my/task/{taskId}`.

## Full request body (verified, model 2831282 → X2D, PETG slot 9, Supertack)

```json
{
  "designId": 2831282,
  "modelId": "US7622359adc7574",
  "title": "0.2mm layer, 4 walls, 25% infill",      // the chosen instance/profile name
  "cover": "https://s3.us-west-2.amazonaws.com/or-cloud-model-prod/...plate_1.png?X-Amz-...",
  "deviceId": "<printer serial>",
  "nozzleDiameter": 0.4,
  "filamentSettingIds": ["GFSG99_15"],                // per-filament preset id(s)
  "instanceId": 3154751,
  "profileId": 777165123,
  "plateIndex": 1,
  "plateName": "",
  "bedType": "Supertack Plate",                       // human name, NOT the mqtt code
  "bedLeveling": true,
  "flowCali": true,
  "timelapse": true,
  "mode": "cloud_slice",
  "useAms": true,
  "amsMapping": [9],                                   // filament-idx -> GLOBAL slot
  "amsMapping2": [{"amsId": 2, "slotId": 1}],          // camelCase (cf. MQTT snake_case)
  "amsDetailMapping": [                                // the dual-nozzle remap
    {"ams": 9, "sourceColor": "FFFFFFFF", "targetColor": "161616FF",
     "filamentType": "PETG", "targetFilamentType": "PETG",
     "nozzleId": 1, "amsId": 2, "slotId": 1}
  ],
  "nozzleInfos": [                                     // X2D dual nozzle
    {"id": 1, "flowSize": "standard_flow", "diameter": 0.4},
    {"id": 0, "flowSize": "standard_flow", "diameter": 0.4}
  ],
  "hasFilamentSwitcher": 1,
  "isPublicProfile": true,
  "skipObjects": [],
  "repetitions": 1,
  "jobType": 1,
  "autoBedLeveling": 2, "extrudeCaliFlag": 2, "nozzleOffsetCali": 2,
  "extrudeCaliManualMode": 0, "primeVolumeMode": "Default",
  "enableArcFitting": 0, "matchFilamentMode": "Custom",
  "enableFilamentDynamicMap": 0,
  "slicer_settings_version": "02.07.00.06",
  "deviceAccessories": {"enclosureKit": false},
  "cfg": "6"
}
```

These camelCase fields (`amsMapping`, `amsMapping2`, `amsDetailMapping`,
`nozzleInfos`) match the `get_user_tasks` task-record shape — so
`normalize_task_params` already round-trips them. To build this payload beambam
needs: the design/instance/profile ids (from `get_design`/`get_design_instances`),
the printer serial, the plate name, and the AMS mapping (from `filament_match` +
the AMS loadout via the fixed `cloud_pull_state`).

## Flow

1. resolve model → instance/profile (`get_design_instances`)
2. `POST /v1/user-service/my/task` with the body above (+ the two `x-bbl-*` sign headers)
3. cloud creates the task, slices for the device (`mode:cloud_slice`), **signs +
   publishes `print.project_file`** to the printer
4. poll `GET /v1/iot-service/api/user/task/{id}` until printing

## Status

- Endpoint + full body schema: **DONE** (this doc).
- `x-bbl-device-security-sign` HTTP request signing: **SOLVED** (raw PKCS#1 v1.5
  RSA-sign of the ms-timestamp string; `beambam/cloud_slice.py`).
- So beambam now has everything to start a cloud-slice print of any MakerWorld
  model on the X2D with one REST call — no local slicing, no MQTT project_file,
  no OSS upload. Remaining: wire `build_cloud_slice_body` to resolve the
  design/instance/profile + AMS mapping (via `get_design_instances` +
  `filament_match` + `cloud_pull_state`) and a `beambam cloud-print-model` verb,
  then poll `get_task`.
