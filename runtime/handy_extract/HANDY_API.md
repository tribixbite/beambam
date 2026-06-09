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

## Printer-control commands are RSA-signed (the real wall) — captured 2026-06-09

A live capture of a stop+restart confirmed that **every actionable command** the
app sends to `device/<serial>/request` carries an RSA-SHA256 signature; only
passive/read-only ones go unsigned:

| command | signed? |
|---|---|
| `print.stop`, `print.clean_print_error` | **yes** |
| `system.get_access_code`, `system.uiop` | **yes** |
| `liveview.prepare` (live-view start) | **yes** |
| `camera.ipcam_get_media_info` | no (read-only) |
| `pushing.*`, `info.get_version`, `security.app_cert_*` | no (setup/passive) |

Signature envelope (the `header` sibling of the command):
```json
{"print": {"command": "stop", "sequence_id": "2009", "timestamp": 1780996597017},
 "user_id": "<uid>",
 "header": {"sign_ver": "v1.0", "sign_alg": "RSA_SHA256",
            "sign_string": "<base64 256-byte RSA sig>",
            "cert_id": "77bcfb…CN=GLOF1000000000.bambulab.com",
            "payload_len": 98}}
```
- `sign_string` = RSA-SHA256 over the first `payload_len` bytes of the command
  (the `print:{…}` object serialised), using the **app's private key**.
- `cert_id` references the X.509 app cert the app installed into the printer via
  `security.app_cert_install` (CN = `GLOF<printerSerial>.bambulab.com`).
- This applies on the **cloud** broker too — a valid `u_<uid>`/token MQTT session
  is necessary but **not sufficient**; the printer rejects unsigned control
  commands. This is the same per-installation cert/signature wall that blocks
  beambam's LAN-direct `print.*` (#65/#66/#68).

**Implication for beambam:** knowing the protocol is not enough — to issue ANY
control command (start, stop, skip, filament, live-view) beambam must produce a
valid RSA-SHA256 signature with the app's private key. That key lives inside
SHIELD. The tractable path, given we already run a Zygisk module inside the app:
**hook the BoringSSL signer** (`EVP_DigestSign*` / `RSA_sign` in libflutter.so,
invoked right before each MQTT publish) to either (a) dump the RSA private key
from the `EVP_PKEY`, or (b) expose a local signing-oracle so beambam asks the
running app to sign its payloads. Until then, beambam can read everything (status
via cloud, model downloads via /f3mf) but cannot command the printer. This is the
single blocker for all five requested print features.

## The Handy print features (protocol map)

All five features are MQTT commands to `device/<serial>/request`, and (per the
section above) **all of them are RSA-signed** — so each is "doable" only once
beambam can sign. The mechanism for each:

| Handy feature | command / mechanism | needs signing? |
|---|---|---|
| **Select profile** | pick a design **instance** (`design.instances[].id`, default `defaultInstanceId`) → `get_instance_download_url(instance_id)` for its .3mf | no — pure REST, ✅ works today |
| **Start print** | `print.project_file` (firmware fetches the presigned .3mf). beambam has the LAN builder in `beambam/print_job.py`; cloud uses `print_type=cloud` | **yes** (signed like stop/clean — confirmed by analogy; project_file not yet captured because the restart reused the existing subtask) |
| **Set quantity** | a `project_file` param (plate repeat / `subtask` count) | yes (part of project_file) |
| **Skip parts** | `print.skip_objects` (object-id list) on a running print | yes |
| **Change filament color/type** | `print.ams_filament_setting` (color RRGGBBAA + `tray_info_idx` for type) and the plate→AMS map `project_file.ams_mapping`. Type list + palette = `search-service/cfg2` | yes (`set_tray_metadata` in beambam already builds the payload — still needs a signature on this firmware) |
| **Live-view changes** | `liveview.prepare` (signed) + camera tunnel `iot-service/api/user/ttcode` → TUTK/agora; settings via `camera.ipcam_*` | mixed: `ipcam_get_media_info` read-only/unsigned, `liveview.prepare` signed |

**Therefore the prerequisite for ALL of these is a signing capability.** The next
capture target is not another print payload — it's the **signer**: extend the
Zygisk module to hook libflutter.so's BoringSSL `EVP_DigestSign*` / `RSA_sign`,
called right before each MQTT publish, and either dump the RSA private key from
the `EVP_PKEY` or expose it as a local signing oracle. With that, beambam's
existing `beambam/printer.py` / `print_job.py` payload builders just need an
`header:{sign_alg, sign_string, cert_id}` wrapper to become live printer control.
(The exact `project_file` body is still worth a one-off capture — start a NEW
print, e.g. "Print again" on a completed job — to confirm quantity/ams_mapping
fields; but it will be signed.)
