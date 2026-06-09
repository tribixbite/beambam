# Bambu Handy API + printer-control protocol

Reverse-engineered from a live capture of Bambu Handy 3.21.0 (Android) by hooking
its Flutter dart:io BoringSSL `SSL_write` (Zygisk module → HPACK/MQTT decode; see
`README.md` Path #7, `decode_raw_h2.py`, `analyze_capture.py`). Responses below
were obtained by replaying the endpoints with beambam's own cloud token. Secrets
and personal identifiers are redacted / tokenised.

Two transports:
- **REST over HTTP/2** to `api.bambulab.com` (region `.cn` for China). Auth =
  `Authorization: Bearer <cloud access_token>` + `X-BBL-*` client headers. This
  is the MakerWorld surface + account/device metadata.
- **MQTT over TLS** to `{us,cn}.mqtt.bambulab.com:8883`. Auth = username
  `u_<uid>`, password `<cloud access_token>`. This is the **printer-control**
  surface — publish commands to `device/<serial>/request`, subscribe to
  `device/<serial>/report`. (Same topic layout as the LAN broker.)

All paths are `/v1/...`. `{id}` = numeric id, `{uuid}` = device/session uuid.

---

## What each service does

| service | role |
|---|---|
| **user-service** | account: login/refresh, profile attrs, FCM device token, message/inbox counts, **print-task history** (`my/tasks`, `my/task/{id}`, `my/task/printedplates`). |
| **design-service** | MakerWorld models ("designs"). A design has **instances** = the individual printable profiles; `instance/{id}/f3mf` is the .3mf download (GeeTest-walled, see README). favorites, likes, comments-rating share. |
| **design-user-service** | the MakerWorld *social* profile (`my/profile`, `my/preference`), filament inventory (`my/filament/v2`). |
| **design-recommend-service** | the "For You" recommendation feed. |
| **search-service** | search + discovery: full-text (`search/design`), typeahead (`suggest2`), faceted (`select/all`, `select/design2`), trending (`searchlist`, `recommand/youlike`), and **client config** (`cfg2` → the canonical filament-type list + color palette + sort/filter options), homepage tabs (`homepage/nav`). |
| **operation-service** | server-driven app content: homepage blocks (`apphomepage`), printer-model display ordering (`printer-model/sort-rule`). |
| **iot-service** | the **printer/device** cloud: bound-device list + status (`api/user/bind`, with `dev_access_code`, online, current `print_job`), firmware versions, **slicer presets** (`api/slicer/setting`, `api/slicer/resource`), camera tunnel codes (`api/user/ttcode` → TUTK/agora), device config packages, and the **per-app device certificate** (`api/user/applications/<token>/cert?aes256`) used by the MQTT security handshake. |
| **task-service** | aggregate task/badge counters (`user/taskv2/multi?taskNames=`). |
| **point-service** | MakerWorld gamification: points progress, design **boost** (a $-gift-card reward mechanic). |
| **comment-service** | model comments + ratings (`commentandrating`, `rating/share/info`). |
| **aftersale-service** | support tickets + unread badges. |
| **analysis-st** + `event.lunkuocorp.com` | **telemetry** (see `x2d_capture_analysis.md`): 9.4k events / 37 types per short session — model impressions, page enters, `mqtt_command_send`, the printer cert exchange, and `GeeTest_Success/Fail` (which independently confirms /f3mf is GeeTest-gated). |

### The cert / security flow (what "cert" means here)

To send **signed** printer commands, Handy installs a per-app X.509 certificate
into the printer and then signs sensitive commands with the matching private key
(baked into the SHIELD-protected app). Observed on `device/<serial>/request`:

1. `security.app_cert_list {type:"app"}` — ask the printer which app certs it holds.
2. `security.app_cert_install {app_cert:"-----BEGIN CERTIFICATE-----…"}` — install
   the app's cert (CN `GLOF<serial>.bambulab.com`, issued by the same CA).
3. `system.get_access_code {header:{sign_ver:"v1.0", sign_alg:"RSA_SHA256",
   sign_string:"<base64 sig>"}}` — a command carrying an **RSA-SHA256 signature**.

This is the per-installation cert/signature wall that blocks beambam's
**LAN-direct** `print.*` control (the private key lives inside SHIELD). The
**cloud** broker path is what Handy actually uses for control and is authed by
`u_<uid>`/token — see "Starting prints" below.

---

## REST endpoint catalog (with req/resp)

`★` = not yet in `cloud_client.py` at time of writing. Responses redacted.

### user-service
```
GET /v1/user-service/user/attr                 → {"allowed": true}            ★
GET /v1/user-service/latest/app                 (400 without extra args)      ★
POST /v1/user-service/user/devicetoken          body {source:"google", token, timeZoneOffset}  ★
GET /v1/user-service/my/tasks?limit=20          → print-task history (hits[])      (known)
GET /v1/user-service/my/task/{id}               → one task (signed .3mf + plate URLs) ★
GET /v1/user-service/my/task/printedplates?instanceId={id} → per-instance printed plates ★
POST /v1/user-service/my/message/device/taskstatus  body {deviceTaskInfo:[{deviceId, taskId}]} ★
GET /v1/user-service/my/message/count           → notification badge breakdown      (known)
```

### design-service / design-user-service
```
GET /v1/design-service/design/{id}              → full design; key: `instances[]`, `defaultInstanceId`
    args: trafficSource, visitHistory, ref_                                    ★(args)
GET /v1/design-service/design/{id}/remixed?ref_=…  → remixes                     ★(args)
GET /v1/design-service/instance/{id}/f3mf?type=preview|download  → {name, url(presigned .3mf)}  (GeeTest-walled)
GET /v1/design-service/my/design/favoriteslist?designId={id}  → {favoritesIds:[…]}  ★
GET /v1/design-user-service/my/preference       → profile + toggles (deviceLiveView, isModelSave, deviceNames…) ★
GET /v1/design-user-service/my/profile          → uid/handle/name/avatar/bio       (known)
```
Example — design instances are the **print profiles**:
```json
{"id":2829060,"title":"Sliceable magnetic corn","defaultInstanceId":3151930,
 "instances":[{"id":3151930,"title":"15x3mm magnets"},
              {"id":3159179,"title":"Scaled 80% - 10x3mm magnets"},
              {"id":3151907,"title":"10x3mm magnets"}]}
```

### search-service
```
GET /v1/search-service/cfg2?ref_=def            → client config:                   ★
    filaments: [PLA, PLA-AERO, PLA-CF, PETG, PETG-CF, TPU, TPU-AMS, ABS, ABS-GF, ASA,
                ASA-AERO, ASA-CF, BVOH, EVA, HIPS, PA, PA-CF, PA6-CF, PA-GF, PC, PCTG,
                PE, PE-CF, PET-CF, PHA, PP, PP-CF, PP-GF, PPA-CF, PPA-GF, PPS, PPS-CF, PVA]
    colors:    [#FFFFFF,#FFF144,#DCF478,#0ACC38,#057748,#0D6284,#0EE2A0,#76D9F4,#46A8F9,
                #2850E0,#443089,#A03CF7,#F330F9,#D4B1DD,#F95D73,#F72323,#7C4B00,#F98C36,
                #FCECD6,#D3C5A3,#AF7933,#898989,#BCBCBC,#161616]
GET /v1/search-service/suggest2?keyword=cube&include=  → typeahead buckets        ★
GET /v1/search-service/select/all?keyword=…&limit=&offset=  → faceted totals      ★
GET /v1/search-service/searchlist               → hot/custom trending word lists   ★
GET /v1/search-service/design/{id}/relate?scene=&limit=&offset=&ref_=  → related   ★
GET /v1/search-service/search/history   POST same  (scene=)  → recent searches     ★
GET /v1/search-service/select/design2?keyword=…  → faceted design search           (known)
```

### operation-service / iot-service / task / point / comment
```
GET /v1/operation-service/apphomepage           (400 without args)                 ★
GET /v1/operation-service/printer-model/sort-rule?ruleId=handy_printer_ranking →
    {"modelOrder":[{"devModelName":"N6","devProductName":"X2D"}, …]}              ★
GET /v1/iot-service/api/user/bind               → bound devices:                   (known)
    {"devices":[{"dev_id":"<serial>","name":"x2d","online":true,"print_status":"SUCCESS",
                 "print_job":1008388234,"dev_model_name":"N6-V2","dev_product_name":"X2D",
                 "dev_access_code":"<redacted>","nozzle_diameter":0.4,"dev_structure":"CoreXY"}]}
POST /v1/iot-service/api/user/ttcode            body {dev_id, protocols:["tutk","agora"]} → camera tunnel  (known)
GET /v1/iot-service/api/packages?names_with_type=["BambuHandy:DEVICE_CONFIG"] → cfg pkg url  ★
GET /v1/iot-service/api/user/applications/<token>/cert?aes256  → per-app device cert (security)  ★
GET /v1/iot-service/api/slicer/setting?version=&public=   → cloud slicer presets   (known)
GET /v1/task-service/user/taskv2/multi?taskNames=…  → badge/task counters          ★
GET /v1/point-service/boost/boostdesign?designId={id} → boost availability         ★
GET /v1/comment-service/rating/share/info       → {firstPrint, hasSharePoint, pointCnt}  ★
GET /v1/comment-service/commentandrating?designId=&type=&sort=&limit=&offset=  → comments  (known)
```

---

## MQTT printer-control protocol (captured)

Connection (outbound, from Handy):
```
CONNECT  proto=MQTT v3.1.1  clientid=android:<uid>:<device-id>  user=u_<uid>  pass=<access_token>
SUBSCRIBE device/<serial>/report  q1
```
Commands are JSON published to `device/<serial>/request`, each shaped
`{"<family>": {"sequence_id":"…","command":"…", …}, "user_id":"<uid>"}`. Observed:

| family.command | payload | purpose |
|---|---|---|
| `pushing` (push_target/version) | `{"pushing":{"sequence_id":2001,"version":1,"push_target":1}}` | begin state push |
| `pushing.pushall` | `{"pushing":{"version":1,"command":"pushall"}}` | request full state dump |
| `info.get_version` | `{"info":{"command":"get_version","timestamp":…}}` | firmware/module versions |
| `security.app_cert_list` | `{"security":{"command":"app_cert_list","type":"app"}}` | list installed app certs |
| `security.app_cert_install` | `{"security":{"command":"app_cert_install","app_cert":"-----BEGIN CERTIFICATE-----…"}}` | install app cert |
| `system.get_access_code` | `{"system":{"command":"get_access_code"},"header":{"sign_ver":"v1.0","sign_alg":"RSA_SHA256","sign_string":"<base64>"}}` | fetch LAN access code (RSA-signed) |

Note: only the `system`/`security` commands carry the RSA `header.sign_string`.
The print-control family (`print.*`) was **not** captured because no print was
started during the session (see below).

---

## Starting prints via beambam, and the Handy print features

**The path.** A MakerWorld print is `print.project_file` published to
`device/<serial>/request` (the firmware fetches the presigned .3mf and prints).
beambam already implements the LAN/local variants in `beambam/printer.py`
(`start_print`, `pause/resume/stop`, `gcode`, `ams_load`, `set_tray_metadata`)
and `beambam/print_job.py`; `cloud_client.py` notes the cloud broker accepts
`print.project_file` with `print_type=cloud`. The cloud broker is authed by
`u_<uid>`/token (no per-request cert in the captured non-security commands), so
the cloud path is the most promising for beambam to start MakerWorld prints
without the SHIELD-signed LAN handshake — **to be confirmed by capturing one
real `print.project_file` and checking for a `header.sign_string`.**

**Feature → mechanism map** (★ = needs a live capture of the exact payload):

| Handy feature | mechanism | beambam status |
|---|---|---|
| **Select profile** | choose a design **instance** (`design.instances[].id`, default `defaultInstanceId`), then `get_instance_download_url(instance_id)` for its .3mf | ✅ doable now (REST) |
| **Set quantity** | re-slice / plate repeat — likely a `project_file` param (or a slicer call); exact field ★ | needs capture |
| **Skip parts** | `print.skip_objects` (object id list) on a running print | not implemented; protocol known (pybambu) — add to printer.py |
| **Change filament color/type** | AMS: `print.ams_filament_setting` (color RRGGBBAA + `tray_info_idx` for type); for a print, the plate→AMS map is `project_file.ams_mapping`. Canonical type list + palette = `search-service/cfg2` (above) | partial: `set_tray_metadata` exists; `ams_mapping` ★ |
| **Live-view changes** | camera tunnel via `iot-service/api/user/ttcode` → TUTK / agora SDK stream | not implemented; tunnel codes available |

**Next step for the print features:** a focused live capture — start one real
MakerWorld print in Handy (+ optionally set quantity, skip an object, remap a
filament) while the Zygisk module records the MQTT. `decode_raw_h2.py`'s MQTT
parser (in `analyze_capture` workflow) already extracts the `print.project_file`
/ `print.skip_objects` / `print.ams_*` publishes verbatim — those become the
exact payloads to implement in `beambam/print_job.py`. This needs a real print
(consumes filament), so it's gated on the user running it.
