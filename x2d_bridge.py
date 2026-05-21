#!/usr/bin/env python3
"""x2d_bridge — local LAN client / status daemon for Bambu Lab X2D, P2S,
and other Bambu printers that require RSA-SHA256 signed MQTT messages
(Jan-2025+ firmware).

Purpose
-------
The Bambu Network Plugin .so (which BambuStudio dlopens to talk to printers)
is x86_64 / arm64-mac only — there's no aarch64 Linux build, so on Termux
the GUI's "connect" / "AMS sync" / "print" actions don't work.

This script gives you a working LAN client without the plugin:

    x2d_bridge.py status                    # one-shot device state pull
    x2d_bridge.py upload  out.gcode.3mf     # FTPS:990 implicit-TLS push
    x2d_bridge.py print   out.gcode.3mf     # upload + start print w/ AMS slot
    x2d_bridge.py daemon                    # long-running monitor on stdout
    x2d_bridge.py daemon --http :8765       # status JSON at /state, etc.

Authentication
--------------
Three values are required. They are read from (in order):
  1. CLI flags                  --ip / --code / --serial
  2. `~/.x2d/credentials`       INI file with [printer] ip=… code=… serial=…
  3. environment variables      X2D_IP, X2D_CODE, X2D_SERIAL

The three values are: the printer's LAN IP, its 8-character access code
(visible on the printer screen under Settings → Network), and the printer's
serial number (printed on the device sticker / Settings → About).

The MQTT signing certificate is the publicly-leaked Bambu Connect cert
embedded in `BAMBU_CERT_PEM` below (cert_id GLOF1000000000-…). Without it,
recent firmware rejects every command with `err_code 84033543 "mqtt
message verify failed"`.

Side notes
----------
* No Bambu cloud calls. No telemetry. Talks only to the LAN IP you give.
* `bambulabs_api` is NOT a dependency — it ships unsigned MQTT and gets
  rejected on signed-only firmwares.
* `paho-mqtt` and `cryptography` are required (`pip install paho-mqtt
  cryptography`). On Termux: `pkg install python-cryptography &&
  pip install paho-mqtt`.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import json
import os
import shutil
import ssl
import sys
import time
from dataclasses import dataclass


# Force UTF-8 stdout / stderr on platforms whose default codec can't
# encode the non-ASCII characters in our help text + emoji status
# glyphs (e.g. → ✓ ✗ in `beambam doctor` output). On Windows the default
# is cp1252 which crashes with UnicodeEncodeError. Python 3.7+ supports
# stream.reconfigure(); we no-op on older interpreters / on streams
# that aren't real TTY wrappers (pytest capture etc.).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
del _stream
import ftplib
from ftplib import FTP_TLS
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable

import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# Repo root — bin/ + bs-bionic/ + runtime/ all live under here. Used by
# `fetch --open` to find bambu-studio + by `serve` for default sock path.
X2D_ROOT_PATH = Path(os.environ.get("X2D_ROOT", str(Path(__file__).resolve().parent)))


# ---------------------------------------------------------------------------
# Credentials resolution — implementation moved to beambam.config in v1.2.0.
# Signed-MQTT cert + signing — implementation moved to beambam.mqtt.
# Re-exported here so legacy callers `from x2d_bridge import ...` still work.
# ---------------------------------------------------------------------------

from beambam.config import Creds  # noqa: E402  — late import after stdlib block
from beambam.mqtt import BAMBU_CERT_ID, sign_payload  # noqa: E402

# Soft-import the private key the same way beambam.mqtt does, so legacy
# callers that grep for `BAMBU_PRIVATE_KEY_PEM` (or pass it to other tools)
# still find it. None when the user hasn't supplied a cert.
try:
    from bambu_cert import BAMBU_PRIVATE_KEY_PEM
except ModuleNotFoundError:
    BAMBU_PRIVATE_KEY_PEM = None


def _signing_key():
    """Deprecated alias — use beambam.mqtt.sign_payload directly. Kept
    so legacy callers (network_shim, downstream importers) still resolve."""
    from beambam.mqtt import _load_private_key
    return _load_private_key()


# ---------------------------------------------------------------------------
# MQTT client + metrics — moved to beambam.mqtt in v1.3.0 (Phase 4 of
# the bridge split). Re-exported here so external callers
# (`from x2d_bridge import X2DClient`) keep working unchanged.
# ---------------------------------------------------------------------------
from beambam.mqtt import (  # noqa: E402
    X2DClient,
    metric_inc as _metric_inc,
    metric_global_inc as _metric_global_inc,
    metrics_snapshot as _metrics_snapshot,
)

# `_threading` and `_collections` aliases used elsewhere in this module —
# the original imports lived with the now-moved metrics block.
import threading as _threading  # noqa: E402
import collections as _collections  # noqa: E402, F401


# ---------------------------------------------------------------------------
# FTPS upload (port 990 implicit TLS, anon-NULL cert acceptance)
# ---------------------------------------------------------------------------

# _ImplicitFTPTLS — moved to beambam.ftps in v1.2.0. Re-exported here for
# anything that still does `from x2d_bridge import _ImplicitFTPTLS`.
from beambam.ftps import _ImplicitFTPTLS  # noqa: E402


# upload_file / download_file / list_files moved to beambam.ftps in v1.2.0.
# Re-exported here for `from x2d_bridge import upload_file` legacy callers.
from beambam.ftps import download_file, list_files, upload_file  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Print start
# ---------------------------------------------------------------------------

# _next_seq / _print_cmd / _system_cmd / _camera_cmd moved to
# beambam/cli/_helpers.py (Phase 5b infrastructure). Re-exported here.
from beambam.cli._helpers import (  # noqa: E402
    _next_seq,
    _print_cmd,
    _system_cmd,
    _camera_cmd,
)


def _md5_of(local_path: Path) -> str:
    """Hex MD5 of a local file. Bambu firmware (Jan-2025+) verifies this
    against the uploaded .3mf before queuing the print — without it
    project_file is silently rejected on X2D / H2D / refreshed X1C."""
    import hashlib
    h = hashlib.md5()
    with local_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def start_print(client: X2DClient, gcode_filename: str, *,
                use_ams: bool = True,
                ams_slot: int | list[int] = 0,
                bed_levelling: bool = True, flow_cali: bool = False,
                timelapse: bool = False, vibration_cali: bool = False,
                bed_type: str | None = None,
                bed_temp: int | None = None,
                local_path: Path | None = None) -> None:
    """Submit a project_file print command to the printer.

    Payload now matches the full Jan-2025+ firmware-required shape captured
    from real cloud + Windows BambuStudio LAN sessions. Earlier reduced
    shapes (bambulabs_api 2.6.6 / orca-lan-bridge minimal) get silently
    dropped on X2D / H2D — the firmware's command handler does shape
    validation before logging, missing required keys = drop without HMS.

    Required fields the older shapes were missing (per drndos/openspoolman
    A1 cloud captures and DeepWiki's BambuStudio PrintParams reverse-engineering):
      * `dev_id`              — device serial; binds the publish to this printer
      * `task_id` / `subtask_id` / `subtask_name` / `job_id` — task identity
      * `project_id` / `profile_id` / `design_id` / `model_id` / `plate_idx`
      * `md5`                 — MD5 of the uploaded .gcode.3mf bytes
      * `timestamp`           — unix seconds
      * `job_type`            — 0 for LAN local, 1 for cloud
      * `bed_temp`            — int degrees C
      * `auto_bed_leveling`   — int (0/1/2), NOT `bed_leveling` bool
      * `extrude_cali_flag`   — int 0
      * `nozzle_offset_cali`  — int 0
      * `extrude_cali_manual_mode` — int 0
      * `ams_mapping2`        — newer `[{"ams_id": 0, "slot_id": N}]` form
      * `cfg`                 — string "0"

    URL scheme: X2D's printer profile maps the FTP root (`/`) to the SD-mount
    `sdcard/` prefix internally. We pass `ftp:///<name>` (firmware translates),
    matching what the Windows BS LAN flow does."""
    if local_path is None:
        local_path = Path.cwd() / gcode_filename
    md5_hex = _md5_of(local_path) if local_path.is_file() else ""

    # Safety: derive bed_type / bed_temp from the .gcode.3mf when callers
    # don't pass them. The pre-2026-05 defaults ("textured_plate" / 65 °C)
    # would silently misheat SuperTack PLA (35 °C) or Cool Plate PLA
    # (35 °C) — SuperTack's silicone-rubber surface degrades faster at
    # sustained 60+ °C, and the firmware silently DROPS project_file
    # commands when MQTT bed_type disagrees with the slice's
    # curr_bed_type. The 3MF is the slicer's authoritative contract.
    # Per code-review #5/#6 we no longer fall through to a hardcoded PLA
    # bed-temp table — that masked malformed 3MFs and would have driven
    # PETG-on-hot_plate at 55 °C instead of 80 °C, detaching mid-print.
    # If we cannot derive AND the caller didn't supply, we REFUSE.
    if bed_type is None or bed_temp is None:
        if not local_path.is_file():
            raise PrintRefusal(
                f"start_print: bed_type/bed_temp not supplied AND no local "
                f"3MF available at {local_path} to auto-derive. Refusing to "
                f"send a print command without a known plate / heat profile. "
                f"Pass bed_type/bed_temp explicitly or wire local_path.")
        # If this raises (malformed 3MF, unknown bed enum, etc.) we let it
        # propagate — better to refuse loudly than fall back to a guess.
        _d = _derive_print_params_from_3mf(local_path, filament_index=0)
        if bed_type is None:
            bed_type = _d["bed_type"]
        if bed_temp is None:
            bed_temp = _d["bed_temp"]
    # ams_mapping (legacy flat int list) + ams_mapping2 (newer ams_id/slot_id
    # form) together cover every firmware path. When use_ams is false both
    # must be empty.
    #
    # Multi-color / multi-extruder support: ams_slot can be a single int
    # (single-filament print, one mapping) OR a list of ints — one per
    # filament index in the 3MF (e.g. [1, 5] = filament 0 -> AMS0 slot 1
    # green, filament 1 -> AMS1 slot 1 white). The slot ordinal is the
    # global one (ams_id*4 + slot_id) so it works on multi-AMS setups
    # transparently. Verified payload shape against captured BS-Windows
    # multi-color X2D run (2-filament) by exporting a 2-color 3MF and
    # comparing dry-run output to the wire capture.
    if use_ams:
        slots = [ams_slot] if isinstance(ams_slot, int) else list(ams_slot)
        if not slots:
            raise ValueError("use_ams=True requires at least one ams_slot")
        ams_mapping_legacy = list(slots)
        ams_mapping_v2 = [
            {"ams_id": s // 4, "slot_id": s % 4} for s in slots
        ]
    else:
        ams_mapping_legacy = []
        ams_mapping_v2 = []
    # Task identity numbers — must be numeric-looking 9-10 digit IDs (not
    # "0") so the firmware's command handler accepts them. Use timestamp
    # so each invocation gets a fresh ID and we don't collide with stale
    # state. Captured cloud BS-Windows payloads use the same shape.
    job_id_int = int(time.time()) * 10
    job_id_str = str(job_id_int)
    # subtask_name is the printer-side label that appears on the
    # touchscreen. Bambu's Files-screen convention is `<basename>.gcode`
    # (NOT `.gcode.3mf` — the firmware strips the .3mf when listing).
    # Verified from the X2D's own `subtask_name` field for its prior
    # print: 'mira_frame.gcode' (no .3mf suffix).
    name_no_3mf = gcode_filename
    if name_no_3mf.endswith(".gcode.3mf"):
        name_no_3mf = name_no_3mf[: -len(".3mf")]
    elif name_no_3mf.endswith(".3mf"):
        name_no_3mf = name_no_3mf[: -len(".3mf")] + ".gcode"
    payload = {
        "print": {
            "sequence_id":              str(int(time.time())),
            "command":                  "project_file",
            "param":                    "Metadata/plate_1.gcode",
            "file":                     gcode_filename,
            "url":                      f"ftp:///{gcode_filename}",
            "md5":                      md5_hex,
            # Task identity — firmware insists these are present even for
            # LAN. Numeric-looking strings; same value across the trio.
            "task_id":                  job_id_str,
            "subtask_id":               job_id_str,
            "subtask_name":             name_no_3mf,
            "job_id":                   job_id_int,
            "project_id":               job_id_str,
            "profile_id":               "0",
            "design_id":                "0",
            "model_id":                 "0",
            "plate_idx":                1,         # int, NOT string
            "dev_id":                   client.creds.serial,
            "job_type":                 0,         # 0 = LAN local, 1 = cloud
            "timestamp":                int(time.time()),
            # Plate / heating
            "bed_type":                 bed_type,
            "bed_temp":                 int(bed_temp),
            "auto_bed_leveling":        1 if bed_levelling else 0,
            # Calibration int flags (0 = don't calibrate)
            "extrude_cali_flag":        1 if flow_cali else 0,
            "nozzle_offset_cali":       0,
            "extrude_cali_manual_mode": 0,
            # Print-time toggles (kept for forward-compat with older paths
            # in firmware that still read these names)
            "flow_cali":                bool(flow_cali),
            "bed_leveling":             bool(bed_levelling),
            "vibration_cali":           bool(vibration_cali),
            "timelapse":                bool(timelapse),
            "layer_inspect":            False,
            # AMS mapping — both legacy and v2 forms; firmware reads whichever
            "use_ams":                  bool(use_ams),
            "ams_mapping":              ams_mapping_legacy,
            "ams_mapping2":             ams_mapping_v2,
            "skip_objects":             None,
            "cfg":                      "0",
        }
    }
    client.publish(payload)


# ---------------------------------------------------------------------------
# Optional HTTP status endpoint (so other tools can poll a JSON URL)
# ---------------------------------------------------------------------------

def _is_loopback(host: str) -> bool:
    """True if the host is a loopback address (auth not required).
    Anything else (LAN IP, 0.0.0.0) is treated as exposed and gates
    on bearer-token auth when one is configured."""
    return host in {"127.0.0.1", "::1", "localhost", ""}


def _format_prometheus_metrics(states: dict[str, dict | None],
                               last_ts_by_name: dict[str, float]) -> bytes:
    """Render counters + per-printer gauges in Prometheus text exposition
    format (item #38). Stateless render — pulls counters from
    _metrics_snapshot and gauges from the live state cache."""
    counters, glob = _metrics_snapshot()
    lines: list[str] = []

    # Global counters (no printer label)
    lines.append("# HELP x2d_ssdp_notifies_total Total SSDP NOTIFY broadcasts received")
    lines.append("# TYPE x2d_ssdp_notifies_total counter")
    lines.append(f"x2d_ssdp_notifies_total {glob.get('ssdp_notifies_total', 0)}")

    # Per-printer counters
    counter_help = {
        "messages_total":         ("counter", "MQTT state push messages received"),
        "mqtt_connects_total":    ("counter", "MQTT connect successes"),
        "mqtt_disconnects_total": ("counter", "MQTT connect failures (rc!=0)"),
    }
    for cname, (ctype, chelp) in counter_help.items():
        lines.append(f"# HELP x2d_{cname} {chelp}")
        lines.append(f"# TYPE x2d_{cname} {ctype}")
        for serial, kvs in counters.items():
            v = kvs.get(cname, 0)
            lines.append(f'x2d_{cname}{{serial="{serial}"}} {v}')

    # Per-printer last_message_ts as a gauge
    lines.append("# HELP x2d_last_message_ts Unix-epoch seconds of last printer push")
    lines.append("# TYPE x2d_last_message_ts gauge")
    for name, ts in last_ts_by_name.items():
        lines.append(f'x2d_last_message_ts{{printer="{name}"}} {ts}')

    # Per-printer gauges from latest state
    gauge_paths = [
        ("bed_temp",          ("print", "bed_temper")),
        ("bed_temp_target",   ("print", "bed_target_temper")),
        ("nozzle_temp",       ("print", "nozzle_temper")),
        ("nozzle_temp_target",("print", "nozzle_target_temper")),
        ("mc_percent",        ("print", "mc_percent")),
        ("mc_remaining_min",  ("print", "mc_remaining_time")),
        ("layer_num",         ("print", "layer_num")),
        ("total_layer_num",   ("print", "total_layer_num")),
    ]
    for gname, path in gauge_paths:
        lines.append(f"# HELP x2d_{gname} Printer state field")
        lines.append(f"# TYPE x2d_{gname} gauge")
        for printer, state in states.items():
            if not state:
                continue
            v = state
            for key in path:
                if not isinstance(v, dict) or key not in v:
                    v = None
                    break
                v = v[key]
            if v is None or not isinstance(v, (int, float)):
                continue
            lines.append(f'x2d_{gname}{{printer="{printer}"}} {v}')

    # AMS slot humidity (per slot) — common scrape target
    lines.append("# HELP x2d_ams_humidity AMS slot humidity rating (0=dry, 5=wet)")
    lines.append("# TYPE x2d_ams_humidity gauge")
    for printer, state in states.items():
        if not state:
            continue
        ams_list = (state.get("print", {}).get("ams", {}).get("ams") or [])
        for ams in ams_list:
            try:
                ams_id = ams.get("id", "?")
                hum = float(ams.get("humidity", 0))
                lines.append(
                    f'x2d_ams_humidity{{printer="{printer}",ams_id="{ams_id}"}} {hum}')
            except (ValueError, TypeError, AttributeError):
                continue

    body = "\n".join(lines) + "\n"
    return body.encode("utf-8")


_ACCESS_LOG_PATH = Path.home() / ".x2d" / "access.log"
_ACCESS_LOG_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB
_access_log_lock = _threading.Lock()


def _write_access_log(record: dict) -> None:
    """Append one JSON line to ~/.x2d/access.log; rotate to access.log.1
    when the active file exceeds 1 MiB. Single rotation slot — older
    rotated logs are overwritten. Match the bridge.log rotation scheme
    used by run_gui_clean.sh so operators see the same shape everywhere.
    """
    path = _ACCESS_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with _access_log_lock:
        try:
            if path.exists() and path.stat().st_size + len(line) > _ACCESS_LOG_MAX_BYTES:
                rotated = path.with_suffix(path.suffix + ".1")
                try:
                    if rotated.exists():
                        rotated.unlink()
                except OSError:
                    pass
                try:
                    path.rename(rotated)
                except OSError:
                    pass
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


_AUTH_COOKIE_NAME = "x2d_token"


def _parse_cookie(header: str, name: str) -> str:
    """Extract a single cookie value by name from a Cookie: header.
    Returns "" if not present. Tolerant of quotes and surrounding spaces."""
    if not header:
        return ""
    for part in header.split(";"):
        kv = part.strip().split("=", 1)
        if len(kv) == 2 and kv[0].strip() == name:
            v = kv[1].strip()
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            return v
    return ""


def _check_bearer(handler, expected: str | None, host: str) -> bool:
    """Return True if the request is authorized. Loopback binds with
    no token configured stay open (single-user local case). Any
    non-loopback bind requires a token; missing/wrong token → 401
    with WWW-Authenticate. Sends the response on rejection so the
    caller just returns.

    Token may be presented in EITHER `Authorization: Bearer <token>` OR
    a `x2d_token=<token>` cookie. The cookie path is what the in-browser
    web UI (#48) uses so SSE/EventSource works (EventSource doesn't
    allow custom headers from JS). Static asset routes that don't need
    auth (login page bootstrap) bypass this check via the `bypass_auth`
    handler attr — see `do_GET`.
    """
    if not expected:
        if not _is_loopback(host):
            handler.send_response(401)
            handler.send_header("WWW-Authenticate", 'Bearer realm="x2d", '
                                'error="invalid_request", '
                                'error_description="--auth-token required for non-loopback binds"')
            handler.end_headers()
            return False
        return True
    presented = ""
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        presented = auth[len("Bearer "):].strip()
    if not presented:
        cookie_hdr = handler.headers.get("Cookie", "")
        presented = _parse_cookie(cookie_hdr, _AUTH_COOKIE_NAME)
    if not presented:
        handler.send_response(401)
        handler.send_header("WWW-Authenticate", 'Bearer realm="x2d"')
        handler.end_headers()
        return False
    # Constant-time compare so we don't leak token length via timing.
    import hmac
    if not hmac.compare_digest(presented, expected):
        handler.send_response(401)
        handler.send_header("WWW-Authenticate", 'Bearer realm="x2d", '
                            'error="invalid_token"')
        handler.end_headers()
        return False
    return True


_WEB_DIR_DEFAULT = Path(__file__).resolve().parent / "web"


def _serve_http(bind: str,
                get_state: Callable[[str], dict | None],
                get_last_ts: Callable[[str], float] | None = None,
                max_staleness: float = 30.0,
                auth_token: str | None = None,
                printer_names: list[str] | None = None,
                clients: dict | None = None,
                web_dir: Path | None = None,
                queue_mgr=None,
                timelapse_rec=None,
                get_hub: Callable[[str], "StateHub | None"] | None = None) -> None:
    """Multi-printer HTTP server (item #36).

    `get_state` and `get_last_ts` now take a printer name (empty string
    for the default plain `[printer]` section). The HTTP layer parses
    `?printer=NAME` from the query string and forwards it. Routes:

      GET  /printers          → list of configured printer names (JSON)
      GET  /state             → state of default printer
      GET  /state?printer=lab → state of named "lab" printer
      GET  /healthz           → health of default printer
      GET  /healthz?printer=lab → health of named "lab" printer
      GET  /metrics           → Prometheus exposition (#38)
      GET  /                  → web UI (#46) — serves web/index.html
      GET  /index.html        → ditto
      GET  /index.js          → web UI client script
      GET  /index.css         → web UI styles
      GET  /state.events      → SSE: state JSON pushed every 1s (#46)
      POST /control/pause     → MQTT publish pause (#46)
      POST /control/resume    → MQTT publish resume (#46)
      POST /control/stop      → MQTT publish stop (#46)
      POST /control/light     → {"state":"on|off|flashing"} (#46)
      POST /control/temp      → {"target":"bed|nozzle|chamber","value":int,"idx":int?} (#46)
      POST /control/ams_load  → {"slot":int} (#46)
      POST /control/sound     → {"state":"on|off"} (HA prompt-sound)
      POST /slice-print       → upload STL/3MF and spawn slice-print
                                 (HA-friendly; raw bytes body + headers)
      GET  /slice-print/jobs/<pid> → check spawned slice-print status

    `clients` (optional) maps printer name → live X2DClient so the
    POST /control/* routes can publish without re-dialing MQTT each time.
    Without it the control routes return 503.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import urllib.parse
    import urllib.request
    import urllib.error
    import re

    host_part, _, port_part = bind.rpartition(":")
    host = host_part or "127.0.0.1"
    port = int(port_part)
    names = list(printer_names) if printer_names else [""]
    web_dir = web_dir or _WEB_DIR_DEFAULT
    clients = clients or {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # silence default stderr access log
            return

        def log_request(self, code='-', size='-'):
            # Item #39: emit one JSON line per request to
            # ~/.x2d/access.log with 1 MiB rotation. Replaces wsgi-style
            # apache combined-log; structured logs are easier to grep
            # and feed into log aggregators.
            try:
                _write_access_log({
                    "ts":          time.time(),
                    "method":      self.command or "?",
                    "path":        self.path,
                    "status":      int(code) if str(code).isdigit() else 0,
                    "size":        int(size) if str(size).isdigit() else None,
                    "duration_ms": round((time.time() - getattr(self, "_x2d_start", time.time())) * 1000, 2),
                    "printer":     getattr(self, "_x2d_printer", None),
                    "authed":      getattr(self, "_x2d_authed", None),
                    "client":      self.client_address[0] if self.client_address else None,
                })
            except Exception:
                # Never let logging take down the response.
                pass

        def _parse_printer(self) -> str:
            url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(url.query)
            return (qs.get("printer", [""])[0] or "")

        # ---- web UI helpers (#46) ---------------------------------
        _STATIC_MIME = {
            ".html": "text/html; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".svg":  "image/svg+xml",
            ".png":  "image/png",
            ".ico":  "image/x-icon",
        }
        _WEB_ALLOWED = {
            "/":           "index.html",
            "/index.html": "index.html",
            "/index.js":   "index.js",
            "/index.css":  "index.css",
            "/login.html": "login.html",
            "/login.js":   "login.js",
        }
        # The login flow needs to render BEFORE the user has a token,
        # so we serve these without the bearer/cookie check. Same for
        # /auth/info which the JS uses to detect "auth disabled" mode
        # (loopback + no token configured) and skip the login redirect.
        _AUTH_BYPASS_PATHS = {"/login.html", "/login.js", "/auth/info"}

        def _serve_static(self, fname: str) -> None:
            path = (web_dir / fname).resolve()
            try:
                # Refuse traversal beyond the web dir.
                path.relative_to(web_dir.resolve())
            except ValueError:
                self.send_response(403); self.end_headers(); return
            if not path.exists() or not path.is_file():
                self.send_response(404); self.end_headers(); return
            data = path.read_bytes()
            ctype = self._STATIC_MIME.get(path.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _proxy_snapshot(self) -> None:
            """Fetch /cam.jpg from the upstream camera daemon and stream
            it back to the caller. Returns 503 with a plain-text reason
            if the camera daemon is unreachable; HA's image platform
            handles the failure gracefully (renders the previous
            frame). The upstream URL is `$X2D_CAMERA_URL` or
            `http://127.0.0.1:8766` by default."""
            cam_base = os.environ.get(
                "X2D_CAMERA_URL", "http://127.0.0.1:8766").rstrip("/")
            url = cam_base + "/cam.jpg"
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=5) as r:
                    body = r.read()
                    ctype = r.headers.get("Content-Type", "image/jpeg")
            except (urllib.error.URLError, ConnectionError,
                    TimeoutError, OSError) as e:
                msg = (f"camera daemon unreachable at {url} ({e}); "
                       "start `x2d_bridge.py camera --bind 127.0.0.1:8766`")
                body = msg.encode()
                self.send_response(503)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _serve_state_events(self, printer: str) -> None:
            """Server-Sent Events stream of printer state pushes.

            When a StateHub is wired in for this printer (daemon mode),
            the handler subscribes to the hub and writes each push as a
            `data:` line the moment it arrives — no 1Hz polling, no
            content-equality comparison. A `: keepalive` comment fires
            every 15 s of idle so intermediate proxies don't drop the
            connection.

            Without a hub (one-shot HTTP without a daemon, tests), the
            handler falls back to the legacy 1 s polling path."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            hub = get_hub(printer) if get_hub is not None else None
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                if hub is not None:
                    sub = hub.subscribe()
                    try:
                        # last_state is replayed by subscribe() when set,
                        # so a fresh client gets current state in the
                        # first event rather than waiting for the next
                        # MQTT push.
                        while True:
                            state = sub.get(timeout=15.0)
                            if state is None:
                                # Idle 15 s → keepalive comment. Doesn't
                                # show up as an SSE message client-side,
                                # but keeps proxies happy.
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                                continue
                            body = json.dumps({"printer": printer,
                                                "state":   state or {},
                                                "ts":      time.time()},
                                               separators=(",", ":"))
                            self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                            self.wfile.flush()
                    finally:
                        hub.unsubscribe(sub)
                else:
                    last_sent: str | None = None
                    ticks_since_send = 0
                    while True:
                        state = get_state(printer)
                        body = json.dumps({"printer": printer,
                                            "state":   state or {},
                                            "ts":      time.time()},
                                           separators=(",", ":"))
                        if body != last_sent or ticks_since_send >= 15:
                            line = f"data: {body}\n\n".encode("utf-8")
                            self.wfile.write(line)
                            self.wfile.flush()
                            last_sent = body
                            ticks_since_send = 0
                        else:
                            ticks_since_send += 1
                        time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Client disconnected or socket failed — exit cleanly so
                # the worker thread terminates.
                return

        def do_GET(self):
            self._x2d_start = time.time()
            self._x2d_printer = None
            cookie_token = _parse_cookie(self.headers.get("Cookie", ""),
                                          _AUTH_COOKIE_NAME)
            self._x2d_authed = (auth_token is not None) and (
                self.headers.get("Authorization", "").startswith("Bearer ")
                or bool(cookie_token))
            url = urllib.parse.urlparse(self.path)
            path = url.path
            # Item #48: /auth/info is a public probe so the JS can tell
            # whether the daemon is open (loopback + no token) or gated.
            # /login.html + /login.js are served WITHOUT the gate so the
            # user can reach the password prompt before having a token.
            if path == "/auth/info":
                payload = {
                    "auth_required": auth_token is not None,
                    "cookie_name":   _AUTH_COOKIE_NAME,
                }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path in self._AUTH_BYPASS_PATHS \
                    and path in self._WEB_ALLOWED:
                self._serve_static(self._WEB_ALLOWED[path])
                return
            if not _check_bearer(self, auth_token, host):
                return
            # /auth/check: token validated above; report success so the
            # login page knows it can persist + redirect.
            if path == "/auth/check":
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Web UI static assets (#46) — open once the bearer/cookie
            # check above passes.
            if path in self._WEB_ALLOWED:
                self._serve_static(self._WEB_ALLOWED[path])
                return
            if path == "/state.events":
                printer = self._parse_printer()
                self._x2d_printer = printer
                if printer not in names:
                    self.send_response(404)
                    self.end_headers()
                    return
                self._serve_state_events(printer)
                return
            if path == "/snapshot.jpg":
                # Item #53: proxy the latest /cam.jpg from the camera
                # daemon. URL is configurable via $X2D_CAMERA_URL.
                self._proxy_snapshot()
                return
            if path == "/queue":
                # Item #55: snapshot of the multi-printer queue.
                if queue_mgr is None:
                    self._send_json({"jobs": []}); return
                jobs = [j.to_dict() for j in queue_mgr.list()]
                self._send_json({"jobs": jobs})
                return
            # Item #58: AMS color → filament profile match.
            if path == "/colorsync/match":
                qs = urllib.parse.parse_qs(url.query)
                color = (qs.get("color", [""])[0] or "").strip()
                material = (qs.get("material", [""])[0] or "").strip()
                if not color:
                    self._send_json({"error":
                        "expected ?color=RRGGBB[AA]&material=…"},
                        status=400); return
                from runtime.colorsync.mapper import match as _cs_match
                m = _cs_match(color, material=material or None)
                if m is None:
                    self._send_json({"error":
                        f"no match for color={color!r}"},
                        status=404); return
                from dataclasses import asdict as _asdict
                self._send_json(_asdict(m))
                return
            if path == "/colorsync/state":
                from runtime.colorsync.mapper import state_for as _cs_state
                printers_out: dict = {}
                for p in names:
                    printers_out[p] = _cs_state(get_state(p))
                self._send_json({"printers": printers_out})
                return
            # Item #56: timelapse browser — listing + per-frame +
            # stitched MP4 fetch.
            if path == "/timelapses":
                if timelapse_rec is None:
                    self._send_json({"jobs": []}); return
                self._send_json({"jobs": timelapse_rec.list_jobs()})
                return
            tl_match = re.match(
                r"^/timelapses/([^/]+)/([^/]+)(?:/(.+))?$", path)
            if tl_match and timelapse_rec is not None:
                printer = urllib.parse.unquote(tl_match.group(1))
                job_id  = urllib.parse.unquote(tl_match.group(2))
                tail    = tl_match.group(3) or ""
                if tail == "":
                    self._send_json({
                        "printer": printer, "job_id": job_id,
                        "frames": timelapse_rec.list_frames(printer, job_id),
                        "mp4_ready":
                            timelapse_rec.mp4_path(printer, job_id) is not None,
                    })
                    return
                if tail == "timelapse.mp4":
                    p = timelapse_rec.mp4_path(printer, job_id)
                    if not p:
                        self.send_response(404); self.end_headers(); return
                    body = p.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                # Frame: NNNN.jpg
                fp = timelapse_rec.frame_path(printer, job_id, tail)
                if fp is None:
                    self.send_response(404); self.end_headers(); return
                body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/printers":
                body = json.dumps({"printers": names}, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/metrics":
                # Item #38: Prometheus text exposition format.
                states_snap = {n: get_state(n) for n in names}
                last_ts_snap = {n: (get_last_ts(n) if get_last_ts else 0.0)
                                for n in names}
                body = _format_prometheus_metrics(states_snap, last_ts_snap)
                self.send_response(200)
                self.send_header("Content-Type",
                                 "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Cloud-side routes (item #67) — sidestep the LAN credentials
            # check because the cloud session is keyed on the user's Bambu
            # account, not on a specific printer in ~/.x2d/credentials.
            # Each cloud route returns 401 if cloud-login hasn't been run.
            if path == "/cloud/status":
                self._send_json(_http_cloud_status()); return
            if path == "/cloud/printers":
                code, payload = _http_cloud_printers()
                self._send_json(payload, status=code); return
            if path == "/cloud/state":
                qs = urllib.parse.parse_qs(url.query)
                serial  = (qs.get("serial") or [""])[0] or None
                timeout = float((qs.get("timeout") or ["15"])[0])
                code, payload = _http_cloud_state(serial, timeout)
                self._send_json(payload, status=code); return
            printer = self._parse_printer()
            self._x2d_printer = printer
            if printer not in names:
                err = json.dumps({"error": f"unknown printer {printer!r}",
                                  "available": names}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            if path == "/state":
                state = get_state(printer)
                body = json.dumps(state or {}, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/history":
                # List every print-history snapshot harvested into
                # ~/.x2d/snapshots/ (populated by `beambam fcm-harvest`).
                # Each entry has print_id + jpg_size + sidecar metadata.
                snap_dir = Path.home() / ".x2d" / "snapshots"
                hits = []
                if snap_dir.is_dir():
                    for js in sorted(snap_dir.glob("*.json"),
                                     key=lambda p: p.stat().st_mtime, reverse=True):
                        try:
                            meta = json.loads(js.read_text())
                        except json.JSONDecodeError:
                            continue
                        pid = meta.get("print_id") or js.stem
                        jpg = snap_dir / f"{pid}.jpg"
                        if jpg.is_file():
                            meta["jpg_size"] = jpg.stat().st_size
                            meta["jpg_url"] = f"/history/{pid}.jpg"
                            hits.append(meta)
                body = json.dumps({"total": len(hits), "hits": hits}, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith("/history/") and path.endswith(".jpg"):
                # Serve a single harvested finish-snapshot JPG by print_id.
                # Path format: /history/<print_id>.jpg.
                pid = path[len("/history/"):-len(".jpg")]
                if not pid.isdigit():
                    self.send_response(400); self.end_headers(); return
                snap_dir = Path.home() / ".x2d" / "snapshots"
                jpg = snap_dir / f"{pid}.jpg"
                if not jpg.is_file():
                    self.send_response(404); self.end_headers(); return
                body = jpg.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith("/slice-print/jobs/"):
                # GET status of a spawned slice-print job.
                # Looks at the upload dir's <stem>.log file + the PID's
                # process status. Returns running / done(rc) / orphaned.
                pid_str = path[len("/slice-print/jobs/"):]
                if not pid_str.isdigit():
                    self.send_response(400); self.end_headers(); return
                pid = int(pid_str)
                # Process state via /proc (Linux only — Termux is Linux).
                state = "unknown"
                exit_code: int | None = None
                proc_stat = Path(f"/proc/{pid}/stat")
                if proc_stat.is_file():
                    try:
                        st = proc_stat.read_text().split()
                        # state field is the 3rd token; R/S = running/sleep,
                        # Z = zombie (already exited; parent is HTTP daemon
                        # so it will reap when waitpid is called).
                        state = "running" if st[2] in ("R", "S", "D") else "exited"
                    except Exception:
                        state = "unknown"
                else:
                    state = "exited"
                # Surface the latest log tail too (last 4 KiB) so HA
                # users can see slice progress.
                upload_dir = Path.home() / ".x2d" / "uploads"
                log_tail = ""
                for log_path in upload_dir.glob("*.log"):
                    # We can't bind pid -> log without bookkeeping; users
                    # passing the path in the spawn response can read the
                    # log directly. Surface the most-recent one as a hint.
                    pass
                self._send_json({
                    "ok":        True,
                    "pid":       pid,
                    "state":     state,
                    "exit_code": exit_code,
                    "log_hint":  ("Read the `log` path returned at spawn "
                                  "time for full output."),
                }, status=200)
                return
            elif path == "/ams":
                # Surface the AMS sub-tree of the printer's last cached
                # state for HA / web-UI consumers. Returns the raw shape
                # `state["print"]["ams"]` produces (ams.ams list with
                # tray subarrays). 404 if the printer has never reported
                # an AMS payload (e.g. fresh boot, no AMS hardware).
                state = get_state(printer) or {}
                ams_block = (state.get("print", {}) or {}).get("ams")
                if ams_block is None:
                    self._send_json(
                        {"printer": printer,
                         "error": "no AMS payload seen yet — printer may "
                                  "be booting or has no AMS hardware"},
                        status=404,
                    )
                else:
                    self._send_json(
                        {"printer": printer, "ams": ams_block}, status=200)
            elif path == "/doctor":
                # Run every doctor check_* on the printer's cached state
                # and return the list of Check dataclasses as JSON.
                # Severity counts in the summary so HA can drive a single
                # "doctor_status" sensor (pass / warn / fail).
                from beambam import doctor as _doctor
                from dataclasses import asdict
                state = get_state(printer) or {}
                checks = _doctor.run_all_checks(state)
                summary = {s: 0 for s in ("pass", "warn", "fail", "info")}
                for c in checks:
                    summary[c.severity] = summary.get(c.severity, 0) + 1
                worst = ("fail" if summary["fail"] else
                         "warn" if summary["warn"] else "pass")
                self._send_json({
                    "printer": printer,
                    "worst":   worst,
                    "summary": summary,
                    "checks":  [asdict(c) for c in checks],
                }, status=200)
            elif path == "/healthz":
                # 200 if we've heard from the printer recently;
                # 503 if MQTT silently disconnected. Used as a Home
                # Assistant binary_sensor or a uptime-monitor poll
                # target. JSON body for diagnostics.
                last = get_last_ts(printer) if get_last_ts else 0.0
                age = time.time() - last if last else float("inf")
                healthy = age <= max_staleness
                payload = {
                    "printer":           printer,
                    "healthy":           healthy,
                    "last_message_ts":   last,
                    "last_message_age_s": None if last == 0.0 else round(age, 2),
                    "max_staleness_s":   max_staleness,
                }
                body = json.dumps(payload, indent=2).encode()
                self.send_response(200 if healthy else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def _read_body_json(self) -> dict | None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            if length > 64 * 1024:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _publish_via_client(self, printer: str, payload: dict) -> tuple[int, dict]:
            cli = clients.get(printer)
            if cli is None:
                return 503, {"error": "no live MQTT client for printer "
                              f"{printer!r}; run with --http on the daemon"}
            try:
                cli.publish(payload)
            except Exception as e:
                return 502, {"error": f"publish failed: {e}",
                              "payload": payload}
            return 200, {"ok": True, "printer": printer, "payload": payload}

        def do_POST(self):
            self._x2d_start = time.time()
            self._x2d_printer = None
            self._x2d_authed = (auth_token is not None) and bool(
                self.headers.get("Authorization", "").startswith("Bearer "))
            if not _check_bearer(self, auth_token, host):
                return
            url = urllib.parse.urlparse(self.path)
            path = url.path
            # Item #57: AI assistant — POST chat.
            if path == "/assistant/chat":
                body = self._read_body_json() or {}
                msg = (body.get("message") or "").strip()
                if not msg:
                    self._send_json({"error":
                        "expected {message: str, provider?: str, history?: [...]}"},
                        status=400); return
                try:
                    from runtime.assistant.router import route as _route
                except ImportError as e:
                    self._send_json({"error": f"assistant import failed: {e}"},
                                      status=500); return
                result = _route(msg,
                                  provider=body.get("provider", "auto"),
                                  history=body.get("history") or [])
                self._send_json({
                    "reply":      result.reply,
                    "provider":   result.provider,
                    "tool_calls": result.tool_calls,
                    "transcript": [
                        {"role": t.role, "content": t.content,
                         "name":  t.name,
                         "tool_calls": t.tool_calls}
                        for t in result.transcript
                    ],
                })
                return
            if path == "/analyze":
                # POST a raw .gcode.3mf body, get the analyze Report as
                # JSON. Web UI / HA dashboards use this to surface
                # weight / time / purge waste / per-filament usage for
                # a file the user uploaded but hasn't sent to the
                # printer yet. Content-Type is irrelevant — we treat
                # the body as octet bytes and write to a tmp file.
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    self._send_json(
                        {"error": "expected raw .gcode.3mf body "
                                  "(Content-Length > 0)"}, status=400)
                    return
                # Cap body at 64 MiB — Bambu .3mf bundles are typically
                # 1-20 MiB; anything bigger is almost certainly an
                # attack or a misuploaded video.
                if length > 64 * 1024 * 1024:
                    self._send_json(
                        {"error": f"body too large ({length} B); cap is "
                                  "64 MiB"}, status=413)
                    return
                raw = self.rfile.read(length)
                import tempfile
                from dataclasses import asdict
                from beambam.analyze import analyze_3mf
                with tempfile.NamedTemporaryFile(
                        suffix=".gcode.3mf", delete=False) as tf:
                    tf.write(raw)
                    tmp_path = Path(tf.name)
                try:
                    report = analyze_3mf(tmp_path)
                except Exception as e:
                    self._send_json(
                        {"error": f"analyze failed: {e}"}, status=400)
                    return
                finally:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                self._send_json(asdict(report), status=200)
                return
            # Item #56: stitch a timelapse → MP4 (POST is the right
            # verb because it's a long-running, side-effecting op).
            tl_match = re.match(
                r"^/timelapses/([^/]+)/([^/]+)/stitch$", path)
            if tl_match and timelapse_rec is not None:
                printer = urllib.parse.unquote(tl_match.group(1))
                job_id  = urllib.parse.unquote(tl_match.group(2))
                body = self._read_body_json() or {}
                fps = int(body.get("fps", 30))
                result = timelapse_rec.stitch(printer, job_id, fps=fps)
                self._send_json(result, status=200 if result["ok"] else 500)
                return
            # Cloud-side POST routes (item #67).
            if path == "/cloud/login":
                body = self._read_body_json() or {}
                code, resp = _http_cloud_login(
                    email=body.get("email") or "",
                    password=body.get("password") or "",
                    region=body.get("region") or None,
                    email_code=body.get("email_code") or None,
                    tfa_code=body.get("tfa_code") or None)
                self._send_json(resp, status=code); return
            if path == "/cloud/logout":
                code, resp = _http_cloud_logout()
                self._send_json(resp, status=code); return
            if path == "/cloud/publish":
                body = self._read_body_json() or {}
                serial = body.get("serial") or ""
                payload = body.get("payload")
                timeout = float(body.get("timeout", 10.0))
                if not serial or not isinstance(payload, dict):
                    self._send_json({"error":
                        "expected {serial: str, payload: dict, timeout?: float}"},
                        status=400); return
                code, resp = _http_cloud_publish(serial, payload, timeout)
                self._send_json(resp, status=code); return
            if not (path.startswith("/control/")
                     or path.startswith("/queue/")
                     or path == "/assistant/chat"
                     or path == "/slice-print"):
                self.send_response(404); self.end_headers(); return
            # Item #55: queue mutations (POST /queue/<verb>)
            if path.startswith("/queue/"):
                if queue_mgr is None:
                    self._send_json({"error": "queue not enabled on this daemon"},
                                      status=503)
                    return
                qverb = path[len("/queue/"):]
                body = self._read_body_json() or {}
                if qverb == "add":
                    if "gcode" not in body:
                        self._send_json({"error":
                            "expected {gcode, printer, slot?, label?}"},
                            status=400); return
                    job = queue_mgr.add(
                        printer=body.get("printer", ""),
                        gcode=body["gcode"],
                        slot=int(body.get("slot", 1)),
                        label=body.get("label", ""))
                    self._send_json({"ok": True, "job": job.to_dict()})
                    return
                elif qverb == "cancel":
                    job_id = body.get("id", "")
                    ok = queue_mgr.cancel(job_id)
                    self._send_json({"ok": ok})
                    return
                elif qverb == "remove":
                    job_id = body.get("id", "")
                    ok = queue_mgr.remove(job_id)
                    self._send_json({"ok": ok})
                    return
                elif qverb == "move":
                    job_id = body.get("id", "")
                    ok = queue_mgr.move(
                        job_id,
                        dest_printer=body.get("dest_printer"),
                        position=(body.get("position")
                                   if body.get("position") is not None
                                   else None))
                    self._send_json({"ok": ok})
                    return
                self._send_json({"error": f"unknown queue verb {qverb!r}",
                                  "supported": ["add", "cancel", "remove", "move"]},
                                  status=404)
                return
            # POST /slice-print — HA-friendly STL upload + slice-print
            # one-shot. Body is the raw model bytes (octet-stream); the
            # filename/printer/slot/bed_type ride in headers because HA's
            # rest_command can't natively assemble multipart bodies.
            if path == "/slice-print":
                fname = self.headers.get("X-Filename") or "upload.stl"
                fname = re.sub(r"[^a-zA-Z0-9._-]", "_", fname)[:120]
                if not fname.lower().endswith((".stl", ".3mf", ".step", ".obj")):
                    fname += ".stl"
                cl = int(self.headers.get("Content-Length", 0))
                if cl <= 0 or cl > 200 * 1024 * 1024:
                    self._send_json({"error":
                        "Content-Length must be 1..200 MiB"}, status=400)
                    return
                body = self.rfile.read(cl)
                if len(body) != cl:
                    self._send_json({"error":
                        "short read; expected %d, got %d" % (cl, len(body))},
                                      status=400); return
                upload_dir = Path.home() / ".x2d" / "uploads"
                upload_dir.mkdir(parents=True, exist_ok=True)
                stamped = f"{int(time.time())}_{fname}"
                stl_path = upload_dir / stamped
                stl_path.write_bytes(body)
                # Header-driven kwargs — every header is optional.
                printer_name = (self.headers.get("X-Printer")
                                 or self._parse_printer())
                slot = int(self.headers.get("X-Slot") or 1)
                bed_type = self.headers.get("X-Bed-Type") or "auto"
                no_ams = (self.headers.get("X-No-AMS") or "").lower() in (
                    "1", "true", "yes", "on")
                # Resolve our own argv0 to allow distro/dev installs.
                cmd_argv = [sys.executable, str(Path(__file__).resolve()),
                            "slice-print", str(stl_path),
                            "--printer", printer_name,
                            "--slot",    str(slot),
                            "--bed-type", bed_type]
                if no_ams:
                    cmd_argv.append("--no-ams")
                log_path = upload_dir / (stl_path.stem + ".log")
                import subprocess as _sp
                try:
                    proc = _sp.Popen(
                        cmd_argv,
                        stdout=open(log_path, "wb"),
                        stderr=_sp.STDOUT,
                        close_fds=True)
                except OSError as e:
                    self._send_json({"error": f"spawn failed: {e}"},
                                      status=500); return
                self._send_json({
                    "ok":     True,
                    "job_id": proc.pid,
                    "stl":    str(stl_path),
                    "log":    str(log_path),
                    "status": "spawned",
                    "poll":   f"/slice-print/jobs/{proc.pid}",
                }, status=202)
                return
            verb = path[len("/control/"):]
            printer = self._parse_printer()
            self._x2d_printer = printer
            if printer not in names:
                self._send_json({"error": f"unknown printer {printer!r}",
                                  "available": names}, status=404)
                return
            body = self._read_body_json()
            if body is None:
                self._send_json({"error": "body must be JSON ≤64 KiB"},
                                  status=400)
                return
            if verb == "pause":
                payload = _print_cmd("pause", param="")
            elif verb == "resume":
                payload = _print_cmd("resume", param="")
            elif verb == "stop":
                payload = _print_cmd("stop", param="")
            elif verb == "light":
                state = (body or {}).get("state", "")
                if state not in ("on", "off", "flashing"):
                    self._send_json({"error":
                        "state must be on/off/flashing"}, status=400)
                    return
                payload = _system_cmd(
                    "ledctrl", led_node="chamber_light", led_mode=state,
                    led_on_time=int(body.get("on_time", 500)),
                    led_off_time=int(body.get("off_time", 500)),
                    loop_times=int(body.get("loops", 0)),
                    interval_time=int(body.get("interval", 0)))
            elif verb == "temp":
                target = (body or {}).get("target", "")
                value = body.get("value")
                if target not in ("bed", "nozzle", "chamber") \
                        or not isinstance(value, (int, float)):
                    self._send_json({"error":
                        "expected target=bed|nozzle|chamber + value=int"},
                                      status=400)
                    return
                if target == "bed":
                    payload = _print_cmd("set_bed_temp", temp=int(value))
                elif target == "nozzle":
                    payload = _print_cmd(
                        "set_nozzle_temp",
                        extruder_index=int(body.get("idx", 0)),
                        target_temp=int(value))
                else:  # chamber
                    payload = _print_cmd(
                        "gcode_line", param=f"M141 S{int(value)}\n")
            elif verb == "ams_load":
                slot = body.get("slot")
                if not isinstance(slot, int) or not 1 <= slot <= 16:
                    self._send_json({"error":
                        "slot must be int 1..16"}, status=400)
                    return
                # MachineObject::command_ams_change_filament — DeviceManager.cpp:1700
                payload = _print_cmd(
                    "ams_change_filament",
                    target=int(slot) - 1,         # 0-indexed in mqtt
                    curr_temp=int(body.get("curr_temp", 215)),
                    tar_temp=int(body.get("tar_temp", 215)))
            elif verb == "gcode":
                line = (body or {}).get("line", "")
                if not isinstance(line, str) or not line.strip():
                    self._send_json({"error":
                        "expected {\"line\": \"<g-code>\"}"}, status=400)
                    return
                payload = _print_cmd(
                    "gcode_line",
                    param=line if line.endswith("\n") else line + "\n")
            elif verb == "sound":
                # ha-bambulab parity: prompt-sound switch. Bambu's
                # firmware accepts `print:print_option` with a
                # `sound_enable` bit; expose as a simple on/off.
                state = (body or {}).get("state", "")
                if state not in ("on", "off", "ON", "OFF", True, False):
                    self._send_json({"error":
                        "state must be on/off"}, status=400)
                    return
                enable = state in ("on", "ON", True)
                payload = _print_cmd(
                    "print_option", sound_enable=bool(enable))
            else:
                self._send_json({"error": f"unknown control verb {verb!r}",
                                  "supported": ["pause", "resume", "stop",
                                                "light", "temp", "ams_load",
                                                "gcode", "sound"]},
                                  status=404)
                return
            status, resp = self._publish_via_client(printer, payload)
            self._send_json(resp, status=status)

    server = ThreadingHTTPServer((host, port), Handler)
    auth_state = "auth required" if auth_token else \
                 ("OPEN — loopback only" if _is_loopback(host) else "OPEN — exposed; pass --auth-token to require Bearer")
    print(f"[x2d-bridge] HTTP listening on http://{host}:{port}/state "
          f"(+ /healthz + /printers, max-staleness {max_staleness}s; "
          f"{auth_state}; printers={names})",
          file=sys.stderr)
    server.serve_forever()


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

# cmd_status / cmd_health / cmd_watch / cmd_tail / cmd_notify moved to
# beambam/cli/info.py (Phase 5c). Re-exported along with the
# _TailDispatcher class + _tail_print helper used by tail's unit tests.
from beambam.cli.info import (  # noqa: E402, F401
    cmd_status,
    cmd_health,
    cmd_watch,
    cmd_tail,
    cmd_notify,
    _TailDispatcher,
    _tail_print,
)


def cmd_upload(args: argparse.Namespace) -> int:
    creds = Creds.resolve(args)
    upload_file(creds, Path(args.file), remote_name=args.remote)
    print(f"uploaded {args.file} -> {creds.ip}:/{args.remote or Path(args.file).name}")
    return 0


class PrintRefusal(Exception):
    """A print command was refused for safety reasons (3MF mismatch,
    empty AMS slot, wrong filament class, etc.). Inherits from
    Exception (NOT BaseException like SystemExit) so worker threads in
    serve-mode and the queue dispatcher can catch+surface the error
    without tearing down the host process."""


def _filament_class(s: str) -> str:
    """Collapse a Bambu-style filament_type string to its class prefix
    for compatibility comparison. PLA / PLA Silk / PLA-CF -> 'PLA';
    PETG / PETG-CF -> 'PETG'; ABS / ABS-GF -> 'ABS'. Different chemistries
    (PLA vs PETG vs ABS vs PA vs PA6 vs TPU vs ASA) still mismatch —
    that's the danger case the AMS slot validator must refuse on.

    Per BambuStudio's setting_id_to_type() (DeviceManager.cpp:2629) the
    AMS push-status reports a coarse class string while a 3MF's
    filament_type can be the longer family name; comparing class prefix
    avoids false-positive refusals on chemically identical spools."""
    token = s.replace("-", " ").replace("_", " ").strip().split(" ", 1)[0]
    return token.upper()


# ---------------------------------------------------------------------------
# Safety derivation: the .gcode.3mf is the slicer's contract — bed_type +
# bed_temp + filament_type/colour come from project_settings.config and
# MUST match what we send to the firmware. Trusting CLI defaults
# ("--bed-type textured_plate --bed-temp 65") when the slice was prepared
# for a different plate has two failure modes:
#   1) firmware silently drops the project_file command on bed_type vs
#      curr_bed_type mismatch (observed live on X2D firmware 01.02.x —
#      the print never starts and there is no HMS to surface the reason)
#   2) if firmware did accept, the wrong heat profile drives the wrong
#      adhesive (e.g. 65°C on SuperTack silicone-rubber every print is
#      damaging long-term; 35°C on PEI wastes a layer of warpage).
# `_derive_print_params_from_3mf` is the safe-by-default replacement for
# the CLI defaults — cmd_print refuses to send if user-supplied values
# disagree with the slice unless --force is passed.
# ---------------------------------------------------------------------------

# Mirrors `bed_type_to_gcode_string()` in BambuStudio's PrintConfig.hpp
# combined with `s_keys_map_BedType` in PrintConfig.cpp — the human-readable
# `curr_bed_type` enum_value seen in 3MFs to the lowercase MQTT enum string
# the firmware expects in `print.project_file.bed_type`.
_BED_NAME_TO_MQTT = {
    "Cool Plate":         "cool_plate",
    "Engineering Plate":  "eng_plate",
    "High Temp Plate":    "hot_plate",
    "Textured PEI Plate": "textured_plate",
    "Supertack Plate":    "supertack_plate",
}
_BED_TEMP_KEY_BY_MQTT = {
    "cool_plate":      "cool_plate_temp_initial_layer",
    "eng_plate":       "eng_plate_temp_initial_layer",
    "hot_plate":       "hot_plate_temp_initial_layer",
    "textured_plate":  "textured_plate_temp_initial_layer",
    "supertack_plate": "supertack_plate_temp_initial_layer",
}


def _derive_print_params_from_3mf(local_path: Path,
                                  filament_index: int = 0) -> dict:
    """Pull authoritative bed_type / bed_temp / filament expectations
    from the .gcode.3mf so cmd_print sends exactly what the slicer
    intended. `filament_index` selects the per-slot value when the
    project has multiple filaments; defaults to 0 (single-color)."""
    if not local_path.is_file():
        raise PrintRefusal(f"3MF not found locally: {local_path}")
    import zipfile as _zf, json as _json
    try:
        with _zf.ZipFile(local_path) as z:
            try:
                ps_bytes = z.read("Metadata/project_settings.config")
            except KeyError:
                raise PrintRefusal(
                    f"3MF {local_path.name} missing Metadata/"
                    f"project_settings.config — cannot derive print params; "
                    f"re-slice with x2d_slice.py.")
    except _zf.BadZipFile:
        raise PrintRefusal(f"{local_path} is not a valid 3MF (zip)")
    try:
        ps = _json.loads(ps_bytes.decode("utf-8", errors="replace"))
    except _json.JSONDecodeError as e:
        raise PrintRefusal(f"3MF project_settings.config malformed: {e}")

    bed_name = ps.get("curr_bed_type")
    bed_type = _BED_NAME_TO_MQTT.get(bed_name)
    if not bed_type:
        raise PrintRefusal(
            f"3MF curr_bed_type {bed_name!r} not in supported plates "
            f"{sorted(_BED_NAME_TO_MQTT)}. Re-slice with --bed and one of "
            f"cool|engineering|high_temp|textured|supertack.")

    temps = ps.get(_BED_TEMP_KEY_BY_MQTT[bed_type]) or []
    if not isinstance(temps, list) or filament_index >= len(temps):
        raise PrintRefusal(
            f"3MF {_BED_TEMP_KEY_BY_MQTT[bed_type]} missing index "
            f"{filament_index}: {temps!r}; refusing to derive bed_temp.")
    raw = str(temps[filament_index]).strip()
    if not raw or not raw.isdigit():
        raise PrintRefusal(
            f"3MF {_BED_TEMP_KEY_BY_MQTT[bed_type]}[{filament_index}] "
            f"= {raw!r}; non-numeric or zero — refusing to send. "
            f"Re-slice (x2d_slice.py --bed bakes in PLA-compatible defaults).")
    bed_temp = int(raw)

    fila_types  = ps.get("filament_type")     or []
    fila_colour = ps.get("filament_colour")   or []
    fila_ids    = ps.get("filament_ids")      or []
    return {
        "bed_type": bed_type,
        "bed_temp": bed_temp,
        "expected_filament_type":   fila_types[filament_index]  if filament_index < len(fila_types)  else None,
        "expected_filament_colour": fila_colour[filament_index] if filament_index < len(fila_colour) else None,
        "expected_filament_id":     fila_ids[filament_index]    if filament_index < len(fila_ids)    else None,
    }


def _validate_ams_slot(state: dict, ams_global_slot: int,
                       expected: dict, *, force: bool) -> None:
    """Refuse to send a use_ams=True print when the named AMS slot is empty
    or carries an incompatible filament type. Bambu firmware does NOT
    validate this server-side — without this guard the toolhead will
    extrude air or attempt to extrude e.g. PETG with PLA hotend ramp.

    Hard checks (always enforced, --force NEVER bypasses):
      - slot index in 0..15
      - referenced AMS exists and that slot's tray_info_idx is non-empty
      - tray_type ∈ {expected_filament_type, ''} or compatible class

    Soft checks (warn if --force, refuse without it):
      - filament brand/id mismatch (e.g. Bambu PLA Silk ≠ Generic PLA Silk
        — same chemistry, different settings profile).
    """
    if ams_global_slot < 0 or ams_global_slot > 15:
        raise PrintRefusal(f"AMS slot {ams_global_slot} out of range 0..15")
    ams_id  = ams_global_slot // 4
    slot_id = ams_global_slot %  4

    p = state.get("print", {}) if isinstance(state, dict) else {}
    ams_root = p.get("ams") or {}
    ams_list = ams_root.get("ams") or []
    target_ams = next((a for a in ams_list if a.get("id") in (ams_id, str(ams_id))), None)
    if target_ams is None:
        raise PrintRefusal(
            f"Live state shows no AMS#{ams_id} attached to printer "
            f"(found AMS ids: {[a.get('id') for a in ams_list]}). "
            f"Refusing to send use_ams=True with slot {ams_global_slot}.")
    trays = target_ams.get("tray") or []
    target_tray = next((t for t in trays if t.get("id") in (slot_id, str(slot_id))), None)
    if target_tray is None:
        raise PrintRefusal(
            f"AMS#{ams_id} has no slot {slot_id}. Refusing to send.")

    tray_info_idx = (target_tray.get("tray_info_idx") or "").strip()
    tray_type     = (target_tray.get("tray_type")     or "").strip()
    tray_color    = (target_tray.get("tray_color")    or "").strip()
    if not tray_info_idx:
        raise PrintRefusal(
            f"AMS#{ams_id} slot {slot_id} (global {ams_global_slot}) is "
            f"EMPTY (tray_info_idx is blank). use_ams=True with an empty "
            f"slot would run the toolhead dry. Load filament or pass "
            f"--no-ams to use the external spool. (NOT bypassable with --force.)")

    exp_type = (expected.get("expected_filament_type") or "").strip()
    if not exp_type:
        # Per code-review #7: refuse rather than skip the filament-type
        # gate. A 3MF with no filament_type is malformed; sending it
        # blind risks loading wrong material into the wrong hotend ramp.
        raise PrintRefusal(
            f"3MF declared no filament_type — cannot validate AMS slot "
            f"{ams_global_slot} ({tray_type!r}) against print intent. "
            f"Re-slice with x2d_slice.py. (NOT bypassable with --force.)")
    if tray_type and _filament_class(exp_type) != _filament_class(tray_type):
        raise PrintRefusal(
            f"AMS slot {ams_global_slot} loaded with {tray_type!r} (class "
            f"{_filament_class(tray_type)}) but the 3MF expects {exp_type!r} "
            f"(class {_filament_class(exp_type)}). Hotend ramp profile "
            f"would be wrong — different filament classes have different "
            f"melt temps. Re-slice with the correct filament_type or load "
            f"matching material into slot {ams_global_slot}. "
            f"(NOT bypassable with --force.)")

    # Soft checks below — bypassable with --force, warn loudly otherwise.
    soft_warnings: list[str] = []
    exp_id = (expected.get("expected_filament_id") or "").strip()
    if exp_id and tray_info_idx and exp_id != tray_info_idx:
        soft_warnings.append(
            f"filament brand mismatch: 3MF profile id {exp_id!r} ≠ slot "
            f"{tray_info_idx!r} (same {tray_type or 'type'}, different "
            f"flow/temp profile)")
    exp_colour = (expected.get("expected_filament_colour") or "").lstrip("#").upper()
    slot_colour = tray_color.upper()
    if exp_colour and slot_colour and exp_colour[:6] != slot_colour[:6]:
        soft_warnings.append(
            f"colour mismatch: 3MF wants #{exp_colour[:6]} but slot has "
            f"#{slot_colour[:6]} (visual only)")

    if soft_warnings:
        msg = "AMS slot soft mismatch(es):\n  - " + "\n  - ".join(soft_warnings)
        if force:
            sys.stderr.write(f"[print] WARN: {msg}\n[print] continuing under --force\n")
        else:
            raise PrintRefusal(
                f"{msg}\nPass --force to ignore (or re-slice / swap spool).")


def cmd_print(args: argparse.Namespace) -> int:
    local = Path(args.file)
    use_ams = not args.no_ams
    force = bool(getattr(args, "force", False))

    # Step 0: --dry-run analyzes the file and refuses on excessive purge
    # waste BEFORE touching creds / network / FTPS. Lets `uvx beambam` users
    # sanity-check a print on a workstation that isn't on the printer's LAN.
    if getattr(args, "dry_run", False):
        from beambam.analyze import analyze_3mf, format_report
        report = analyze_3mf(local)
        print(format_report(report))
        flush_g = float(report.totals.get("flush_volume_g", 0.0))
        max_g = float(getattr(args, "max_flush_g", 10.0))
        if flush_g > max_g:
            sys.stderr.write(
                f"\n[print --dry-run] REFUSED: predicted purge "
                f"{flush_g:.1f} g exceeds --max-flush-g {max_g:.1f} g. "
                f"Re-slice with fewer color swaps OR raise the threshold.\n")
            return 2
        sys.stderr.write(
            f"\n[print --dry-run] OK: predicted purge {flush_g:.1f} g "
            f"<= --max-flush-g {max_g:.1f} g\n")
        return 0

    creds = Creds.resolve(args)

    # Step 1: derive authoritative bed_type / bed_temp / filament
    # expectations from the 3MF before we touch the network. If the
    # 3MF is unreadable we fail loud here, well before publish.
    try:
        derived = _derive_print_params_from_3mf(local, filament_index=0)
    except PrintRefusal as e:
        raise SystemExit(str(e))

    # Step 2: reconcile with user-supplied --bed-type / --bed-temp.
    # Defaults are sentinels (None) so we can tell "user didn't pass"
    # from "user explicitly passed a contradicting value".
    user_bed_type = getattr(args, "bed_type", None)
    user_bed_temp = getattr(args, "bed_temp", None)
    bed_type = derived["bed_type"]
    bed_temp = derived["bed_temp"]
    if user_bed_type and user_bed_type != bed_type:
        if not force:
            raise SystemExit(
                f"--bed-type {user_bed_type!r} contradicts 3MF "
                f"curr_bed_type ({bed_type!r}). Re-slice with the right "
                f"plate, OR pass --force if you genuinely want to "
                f"override (NOT recommended — heat profile WILL be wrong).")
        sys.stderr.write(
            f"[print] WARN: bed_type override {bed_type!r} -> "
            f"{user_bed_type!r} under --force\n")
        bed_type = user_bed_type
    if user_bed_temp is not None and user_bed_temp != bed_temp:
        if not force:
            raise SystemExit(
                f"--bed-temp {user_bed_temp} contradicts 3MF-derived "
                f"value ({bed_temp}°C for {bed_type}). Re-slice or pass "
                f"--force.")
        sys.stderr.write(
            f"[print] WARN: bed_temp override {bed_temp} -> "
            f"{user_bed_temp} under --force\n")
        bed_temp = user_bed_temp

    sys.stderr.write(
        f"[print] derived from 3MF: bed_type={bed_type!r} bed_temp={bed_temp}°C "
        f"filament_type={derived['expected_filament_type']!r} "
        f"filament_id={derived['expected_filament_id']!r} "
        f"colour={derived['expected_filament_colour']!r}\n")

    # Step 3: upload (if requested) BEFORE we open the MQTT client so a
    # transient FTPS failure doesn't leave a stale subscription.
    if not args.no_upload:
        upload_file(creds, local, remote_name=args.remote)

    # Step 4: open the MQTT client and validate live AMS state.
    cli = X2DClient(creds)
    cli.connect()
    name = args.remote or local.name
    try:
        if use_ams:
            try:
                live = cli.request_state(timeout=15.0)
            except TimeoutError:
                raise SystemExit(
                    "could not pull live printer state to validate AMS slot "
                    "before sending. Either the printer dropped, or "
                    "credentials are wrong. Re-run when the bridge can "
                    "reach the printer (status command works first).")
            try:
                _validate_ams_slot(live, args.slot, derived, force=force)
            except PrintRefusal as e:
                raise SystemExit(str(e))
            # Per code-review #4 (race window): warn the user not to
            # change spools between validation and the actual print
            # start. The window is small (~1s) but real.
            sys.stderr.write(
                "[print] AMS state validated; do not change AMS slots "
                "until the printer transitions to PREPARE/RUNNING.\n")

        try:
            start_print(cli, name,
                        use_ams=use_ams, ams_slot=args.slot,
                        bed_levelling=not args.no_bed_level,
                        flow_cali=args.flow_cali,
                        timelapse=args.timelapse,
                        vibration_cali=args.vib_cali,
                        bed_type=bed_type,
                        bed_temp=bed_temp,
                        local_path=local)
        except PrintRefusal as e:
            raise SystemExit(str(e))
        print(f"start_print queued: {name} "
              f"(slot={args.slot}, ams={use_ams}, bed={bed_type}@{bed_temp}°C)")
    finally:
        cli.disconnect()
    return 0


# ---------------------------------------------------------------------------
# `serve` mode — Unix-domain socket server that the libbambu_networking.so
# shim talks to. See runtime/network_shim/PROTOCOL.md for the wire format.
#
# One ServeServer process accepts many shim connections (one per
# bambu-studio instance). Each connection runs in its own reader thread.
# Printer-side MQTT clients are shared globally keyed by dev_id, so two
# shims pointing at the same printer don't double-subscribe.
# ---------------------------------------------------------------------------

ABI_VERSION = 1
SHIM_VERSION = "0.1.0"


class _OpError(Exception):
    """Op handler failure — surfaces as `{ok:false, error:{code, message}}`."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class _PrinterSession:
    """One live MQTT connection to a single printer plus a fan-out of state
    pushes to every shim that asked for it. Reference-counted so the
    underlying X2DClient closes only when the last shim disconnects."""

    def __init__(self, dev_id: str, dev_ip: str, code: str):
        from threading import Lock as _Lock
        self.dev_id = dev_id
        self.dev_ip = dev_ip
        self.code = code
        self._refcount = 0
        self._lock = _Lock()
        self._listeners: list[Callable[[dict], None]] = []
        self._connect_listeners: list[Callable[[int, str, str], None]] = []
        # Item #29: cache the most recent state push so a fresh shim
        # subscriber can replay it immediately and DeviceManager populates
        # MachineObject (AMS, temps, lights, etc.) without waiting up to
        # 30s for the next push.
        self._latest_state: dict | None = None
        self.client = X2DClient(
            Creds(ip=dev_ip, code=code, serial=dev_id),
            on_state=self._dispatch_state,
        )

    def _dispatch_state(self, payload: dict) -> None:
        with self._lock:
            self._latest_state = payload
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(payload)
            except Exception as e:  # one bad subscriber shouldn't poison others
                print(f"[serve] state listener raised: {e}", file=sys.stderr)

    def latest_state(self) -> dict | None:
        with self._lock:
            return self._latest_state

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(fn)
            except ValueError:
                pass

    def add_connect_listener(self, fn: Callable[[int, str, str], None]) -> None:
        with self._lock:
            self._connect_listeners.append(fn)

    def remove_connect_listener(self, fn: Callable[[int, str, str], None]) -> None:
        with self._lock:
            try:
                self._connect_listeners.remove(fn)
            except ValueError:
                pass

    def _emit_connect(self, status: int, msg: str = "") -> None:
        with self._lock:
            listeners = list(self._connect_listeners)
        for fn in listeners:
            try:
                fn(status, self.dev_id, msg)
            except Exception as e:
                print(f"[serve] connect listener raised: {e}", file=sys.stderr)

    def acquire(self) -> None:
        with self._lock:
            first = self._refcount == 0
            self._refcount += 1
        if first:
            try:
                self.client.connect(timeout=8.0)
                self._emit_connect(0, "connected")  # ConnectStatusOk
                self.client.publish(
                    {"pushing": {"sequence_id": _next_seq(), "command": "pushall"}}
                )
            except Exception as e:
                self._emit_connect(1, str(e))  # ConnectStatusFailed
                raise _OpError(-2, f"connect failed: {e}") from e

    def release(self) -> None:
        with self._lock:
            self._refcount -= 1
            now_zero = self._refcount <= 0
        if now_zero:
            try:
                self.client.disconnect()
            finally:
                self._emit_connect(2, "lost")  # ConnectStatusLost


class ServeServer:
    def __init__(self, sock_path: Path):
        self.sock_path = sock_path
        self._printers: dict[str, _PrinterSession] = {}
        self._printers_lock = __import__("threading").Lock()
        self._stop = Event()
        self._ssdp_listeners: list[Callable[[dict], None]] = []
        self._ssdp_lock = __import__("threading").Lock()
        # Cache of {dev_id: parsed_dict} so we can re-emit the most-recent
        # SSDP notify to a newly-connecting shim without waiting for the
        # printer's next 30-second broadcast.
        self._ssdp_cache: dict[str, dict] = {}
        self._ssdp_thread: Thread | None = None
        # Item #40: serial → (code, name) map loaded from ~/.x2d/credentials
        # so the SSDP loop can recognise our own printers when their NOTIFY
        # arrives and open the MQTT subscription proactively.
        self._known_creds: dict[str, tuple[str, str]] = self._load_known_creds()
        # Refcount holder: any session opened proactively from SSDP (item #40)
        # gets one persistent acquire() so the connection survives across
        # shim subscribe/unsubscribe cycles. Released on serve_forever exit.
        self._proactive_sessions: dict[str, _PrinterSession] = {}

    @staticmethod
    def _load_known_creds() -> dict[str, tuple[str, str]]:
        """Read every [printer] / [printer:NAME] section in
        ~/.x2d/credentials and return {serial: (code, name)}. Quietly
        returns {} if the file is missing or malformed — the bridge stays
        usable for unrecognised printers via the lazy shim path."""
        path = Path.home() / ".x2d" / "credentials"
        if not path.exists():
            return {}
        cp = configparser.ConfigParser()
        try:
            cp.read(path)
        except configparser.Error:
            return {}
        out: dict[str, tuple[str, str]] = {}
        for section in cp.sections():
            if section == "printer":
                name = ""
            elif section.startswith("printer:"):
                name = section.split(":", 1)[1]
            else:
                continue
            serial = cp.get(section, "serial", fallback="").strip()
            code = cp.get(section, "code", fallback="").strip()
            if serial and code:
                out[serial] = (code, name)
        return out

    # --- SSDP discovery -----------------------------------------------

    def add_ssdp_listener(self, fn: Callable[[dict], None]) -> None:
        with self._ssdp_lock:
            self._ssdp_listeners.append(fn)
            cache = list(self._ssdp_cache.values())
        # Replay the cache so a fresh shim sees existing devices immediately.
        for parsed in cache:
            try:
                fn(parsed)
            except Exception as e:
                print(f"[serve] ssdp replay raised: {e}", file=sys.stderr)

    def remove_ssdp_listener(self, fn: Callable[[dict], None]) -> None:
        with self._ssdp_lock:
            try:
                self._ssdp_listeners.remove(fn)
            except ValueError:
                pass

    def _ensure_ssdp_thread(self) -> None:
        if self._ssdp_thread and self._ssdp_thread.is_alive():
            return
        t = Thread(target=self._ssdp_loop, name="ssdp", daemon=True)
        t.start()
        self._ssdp_thread = t

    def _seed_appconfig_for_ssdp(self, parsed: dict) -> None:
        """Item #17: when we see the FIRST SSDP NOTIFY of the bridge's
        lifetime, ensure the user's BambuStudio.conf has a Bambu vendor
        preset selected. Without this, freshly-installed users land on
        the missing_connection.html fallback even though their printer
        is broadcasting itself.

        Idempotent: a marker file at ~/.x2d/.ssdp_seeded prevents
        re-patching across bridge restarts. Atomic write so a crash
        mid-write doesn't corrupt the user's AppConfig."""
        import os as _os
        marker = Path.home() / ".x2d" / ".ssdp_seeded"
        if marker.exists():
            return
        appconf = Path.home() / ".config" / "BambuStudioInternal" / "BambuStudio.conf"
        if not appconf.exists() or appconf.stat().st_size == 0:
            # No AppConfig yet — install.sh will seed it on next install
            # run. We can't sensibly create one out of nothing here.
            return
        try:
            data = json.loads(appconf.read_text())
        except (json.JSONDecodeError, OSError):
            return  # Don't touch a config we can't parse.
        presets = data.setdefault("presets", {})
        current = presets.get("printer", "")
        # If already on a Bambu vendor preset, leave alone.
        if current.lower().startswith("bambu lab"):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            return
        # Patch the same gate keys install.sh #11 sets — defaults to the
        # X2D since that's what this toolkit is for. The upstream BBL
        # profile catalogue ships full X2D variants (machine, filaments,
        # 0.20mm Standard process), so the GUI lands directly on the
        # right model without the user having to pick.
        data.setdefault("vendors", {})["BBL"] = "1"
        models = data.get("models") or []
        if not any(m.get("vendor") == "BBL" for m in models):
            models.append({
                "vendor": "BBL",
                "model": "Bambu Lab X2D",
                "nozzle_diameter": '"0.4"',
            })
            data["models"] = models
        presets["printer"]   = "Bambu Lab X2D 0.4 nozzle"
        presets["filament"]  = "Bambu PLA Basic @BBL X2D"
        presets.setdefault("print", "0.20mm Standard @BBL X2D")
        if not isinstance(presets.get("filaments"), list) or not presets["filaments"]:
            presets["filaments"] = ["Bambu PLA Basic @BBL X2D"]
        # Atomic write
        tmp = appconf.with_suffix(appconf.suffix + ".tmp-x2d")
        tmp.write_text(json.dumps(data, indent=4))
        _os.replace(tmp, appconf)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        print(f"[serve] ssdp seed: patched {appconf} (printer→{presets['printer']}, "
              f"triggered by {parsed.get('dev_name', '?')} @ {parsed.get('dev_ip', '?')})",
              file=sys.stderr)

    def _seed_access_code(self, parsed: dict) -> None:
        """Write access_code / user_access_code / ip_address keyed by
        dev_id into BambuStudio.conf so the GUI auto-binds on SSDP.
        Re-runs on every NOTIFY (cheap and idempotent: same code +
        dev_id only flips the file when the IP changes).

        Looks up the access code in self._known_creds (populated from
        ~/.x2d/credentials at startup). If the SSDP'd dev_id isn't in
        creds, do nothing — we don't have the access code for that
        printer."""
        import os as _os
        dev_id = parsed.get("dev_id", "")
        dev_ip = parsed.get("dev_ip", "")
        if not (dev_id and dev_ip):
            return
        creds = self._known_creds.get(dev_id)
        if creds is None:
            return
        code, _name = creds
        for app_dir in ("BambuStudio", "BambuStudioInternal"):
            appconf = Path.home() / ".config" / app_dir / "BambuStudio.conf"
            if not appconf.exists() or appconf.stat().st_size == 0:
                continue
            try:
                data = json.loads(appconf.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            changed = False
            for key in ("access_code", "user_access_code"):
                slot = data.setdefault(key, {})
                if not isinstance(slot, dict):
                    slot = {}
                    data[key] = slot
                if slot.get(dev_id) != code:
                    slot[dev_id] = code
                    changed = True
            slot_ip = data.setdefault("ip_address", {})
            if not isinstance(slot_ip, dict):
                slot_ip = {}
                data["ip_address"] = slot_ip
            if slot_ip.get(dev_id) != dev_ip:
                slot_ip[dev_id] = dev_ip
                changed = True
            app = data.setdefault("app", {})
            if app.get("user_last_selected_machine") != dev_id:
                app["user_last_selected_machine"] = dev_id
                changed = True
            if not changed:
                continue
            tmp = appconf.with_suffix(appconf.suffix + ".tmp-x2d-ac")
            tmp.write_text(json.dumps(data, indent=4))
            _os.replace(tmp, appconf)
            print(f"[serve] access-code seed: {appconf} dev_id={dev_id} "
                  f"ip={dev_ip}", file=sys.stderr)

    def _ssdp_loop(self) -> None:
        """Listen for Bambu's multicast NOTIFY broadcasts on UDP 2021
        and convert each into the JSON shape BambuStudio's
        DeviceManager::on_machine_alive expects."""
        import socket as _socket
        import struct as _struct
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM, _socket.IPPROTO_UDP)
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            sock.bind(("", 2021))
            sock.setsockopt(_socket.IPPROTO_IP, _socket.IP_ADD_MEMBERSHIP,
                            _struct.pack("4sl",
                                         _socket.inet_aton("239.255.255.250"),
                                         _socket.INADDR_ANY))
            sock.settimeout(1.0)
        except OSError as e:
            print(f"[serve] ssdp bind failed: {e}", file=sys.stderr)
            return
        print("[serve] ssdp listening on udp/2021 (239.255.255.250)", file=sys.stderr)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except (_socket.timeout, BlockingIOError):
                continue
            except OSError:
                break
            parsed = self._parse_ssdp(data, addr[0])
            if parsed is None:
                continue
            with self._ssdp_lock:
                self._ssdp_cache[parsed["dev_id"]] = parsed
            _metric_global_inc("ssdp_notifies_total")
            with self._ssdp_lock:
                listeners = list(self._ssdp_listeners)
            # Item #40: proactive auto-connect. If this NOTIFY's USN
            # matches a credentials section's serial, open the MQTT
            # subscription before any shim asks. _PrinterSession is
            # refcounted, so a persistent acquire() here keeps the
            # connection live across shim subscribe/unsubscribe cycles
            # — and the cached state replay (#29) means the GUI's
            # StatusPanel populates within milliseconds of subscribe.
            try:
                self._maybe_auto_connect(parsed)
            except Exception as e:
                print(f"[serve] ssdp auto-connect failed: {e}", file=sys.stderr)
            # Fire-and-forget: ensure the AppConfig has a Bambu preset
            # so the GUI's Device tab works on first launch (#17).
            try:
                self._seed_appconfig_for_ssdp(parsed)
            except Exception as e:
                print(f"[serve] ssdp seed failed: {e}", file=sys.stderr)
            # Also seed access_code / user_access_code / ip_address /
            # user_last_selected_machine — runs every NOTIFY, idempotent.
            # This makes the GUI auto-bind without the user clicking
            # through the ConnectPrinterDialog (which has UX bugs on
            # the wx 3.3 / GTK build).
            try:
                self._seed_access_code(parsed)
            except Exception as e:
                print(f"[serve] access-code seed failed: {e}",
                      file=sys.stderr)
            for fn in listeners:
                try:
                    fn(parsed)
                except Exception as e:
                    print(f"[serve] ssdp listener raised: {e}", file=sys.stderr)

    @staticmethod
    def _parse_ssdp(data: bytes, src_ip: str) -> dict | None:
        """Extract the on_machine_alive fields from a Bambu NOTIFY.
        Format example:
            NOTIFY * HTTP/1.1\r\n
            Location: 192.168.x.y\r\n
            USN: <serial>\r\n
            DevModel.bambu.com: N6\r\n
            DevName.bambu.com: x2d\r\n
            DevConnect.bambu.com: cloud|lan\r\n
            DevBind.bambu.com: free|occupied\r\n
            Devseclink.bambu.com: secure\r\n
            DevVersion.bambu.com: 01.01.00.00\r\n
        """
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return None
        if not text.startswith("NOTIFY "):
            return None
        headers: dict[str, str] = {}
        for line in text.split("\r\n")[1:]:
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        usn = headers.get("usn", "")
        if not usn:
            return None
        dev_ip = headers.get("location", src_ip) or src_ip
        connect_type = headers.get("devconnect.bambu.com", "lan").lower()
        if connect_type == "cloud":
            # The bridge replaces what the cloud plug-in would do, so
            # tell the host this is reachable as a LAN device.
            connect_type = "lan"
        return {
            "dev_name":       headers.get("devname.bambu.com", ""),
            "dev_id":         usn,
            "dev_ip":         dev_ip,
            "dev_type":       headers.get("devmodel.bambu.com", ""),
            "dev_signal":     "",  # Bambu doesn't advertise signal strength in SSDP
            "connect_type":   connect_type,
            "bind_state":     headers.get("devbind.bambu.com", "free").lower(),
            "sec_link":       headers.get("devseclink.bambu.com", ""),
            "ssdp_version":   headers.get("devversion.bambu.com", ""),
            "connection_name": "",
        }

    def _maybe_auto_connect(self, parsed: dict) -> None:
        """Item #40: open MQTT proactively when an SSDP NOTIFY matches a
        known credentials section. Idempotent — only one persistent
        acquire() per serial, so repeated NOTIFYs (every ~30s) don't
        rack up the refcount. IP changes are tolerated because
        get_or_open_printer rebuilds the session on mismatch."""
        dev_id = parsed.get("dev_id", "")
        dev_ip = parsed.get("dev_ip", "")
        if not dev_id or not dev_ip:
            return
        creds = self._known_creds.get(dev_id)
        if creds is None:
            return
        code, _name = creds
        with self._printers_lock:
            existing = self._proactive_sessions.get(dev_id)
            existing_ip = existing.dev_ip if existing else None
        # If we already hold a proactive ref AND IP is unchanged → done.
        if existing is not None and existing_ip == dev_ip:
            return
        # Either fresh or IP changed; acquire (will rebuild on IP mismatch).
        try:
            sess = self.get_or_open_printer(dev_id, dev_ip, code)
        except _OpError as e:
            print(f"[serve] auto-connect {dev_id}@{dev_ip} failed: {e}",
                  file=sys.stderr)
            return
        with self._printers_lock:
            stale = self._proactive_sessions.get(dev_id)
            self._proactive_sessions[dev_id] = sess
        # Drop the previous proactive ref now that the new one is in place.
        if stale is not None and stale is not sess:
            try:
                stale.release()
            except Exception:
                pass
        print(f"[serve] auto-connect {dev_id}@{dev_ip} (proactive, "
              f"matched creds section {_name or '<default>'!r})",
              file=sys.stderr)

    def _release_proactive_sessions(self) -> None:
        """Drop the persistent SSDP-driven refs at shutdown so MQTT
        connections close cleanly."""
        with self._printers_lock:
            sessions = list(self._proactive_sessions.values())
            self._proactive_sessions.clear()
        for sess in sessions:
            try:
                sess.release()
            except Exception:
                pass

    # --- printer registry ---------------------------------------------

    def get_or_open_printer(self, dev_id: str, dev_ip: str, code: str) -> _PrinterSession:
        with self._printers_lock:
            sess = self._printers.get(dev_id)
            if sess is None:
                sess = _PrinterSession(dev_id, dev_ip, code)
                self._printers[dev_id] = sess
            elif sess.dev_ip != dev_ip or sess.code != code:
                # IP/code changed under us — close old, open new.
                try:
                    sess.client.disconnect()
                except Exception:
                    pass
                sess = _PrinterSession(dev_id, dev_ip, code)
                self._printers[dev_id] = sess
        sess.acquire()
        return sess

    def release_printer(self, dev_id: str) -> None:
        with self._printers_lock:
            sess = self._printers.get(dev_id)
        if sess is not None:
            sess.release()

    # --- main loop ----------------------------------------------------

    def serve_forever(self) -> int:
        import socket
        from threading import Thread as _Thread

        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(str(self.sock_path))
        os.chmod(str(self.sock_path), 0o600)
        srv.listen(8)
        srv.settimeout(0.5)

        import signal as _signal
        def _stop_handler(signum, frame):  # noqa: ARG001
            self._stop.set()
        _signal.signal(_signal.SIGINT, _stop_handler)
        _signal.signal(_signal.SIGTERM, _stop_handler)

        # Start SSDP discovery up-front so the AppConfig auto-pop (#17)
        # fires even when no shim has connected yet (e.g. when run_gui.sh's
        # watchdog brought us up before bambu-studio's plug-in load).
        self._ensure_ssdp_thread()

        print(f"[serve] listening on {self.sock_path}", file=sys.stderr)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            handler = _ConnHandler(self, conn)
            t = _Thread(target=handler.run, name=f"shim-{handler.id}", daemon=True)
            t.start()

        srv.close()
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        # Drop SSDP-driven proactive refs (#40) before the bulk close
        # so refcounts don't underflow when we hit the disconnect loop.
        self._release_proactive_sessions()
        # Disconnect every active printer cleanly.
        with self._printers_lock:
            for sess in self._printers.values():
                try:
                    sess.client.disconnect()
                except Exception:
                    pass
        print("[serve] stopped cleanly", file=sys.stderr)
        return 0


_conn_id = 0


class _ConnHandler:
    """One shim connection. Owns its socket; spawns no extra threads."""

    def __init__(self, server: ServeServer, sock):
        global _conn_id
        _conn_id += 1
        self.id = _conn_id
        self.server = server
        self.sock = sock
        self._write_lock = __import__("threading").Lock()
        self._subscribed: set[str] = set()
        self._state_cb: Callable[[dict], None] | None = None
        self._connect_cb: Callable[[int, str, str], None] | None = None
        self._ssdp_cb: Callable[[dict], None] | None = None

    # --- I/O primitives ----------------------------------------------

    def _send(self, obj: dict) -> None:
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._write_lock:
            try:
                self.sock.sendall(line)
            except (BrokenPipeError, OSError):
                pass

    def _read_lines(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except (ConnectionResetError, OSError):
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    yield line

    # --- callbacks injected into _PrinterSession ---------------------

    def _emit_local_message(self, dev_id: str, payload: dict) -> None:
        self._send({
            "kind": "evt",
            "name": "local_message",
            "data": {
                "dev_id": dev_id,
                "msg": json.dumps(payload, separators=(",", ":")),
            },
        })

    def _emit_local_connect(self, status: int, dev_id: str, msg: str) -> None:
        self._send({
            "kind": "evt",
            "name": "local_connect",
            "data": {"status": status, "dev_id": dev_id, "msg": msg},
        })

    # --- main loop ----------------------------------------------------

    def run(self) -> None:
        try:
            for raw in self._read_lines():
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    print(f"[serve] bad json from shim: {e}", file=sys.stderr)
                    continue
                if msg.get("kind") != "req":
                    continue
                self._handle_request(msg)
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        # Drop our subscriptions and release each printer ref.
        for dev_id in list(self._subscribed):
            sess = self.server._printers.get(dev_id)
            if sess is not None:
                if self._state_cb:
                    sess.remove_listener(self._state_cb)
                if self._connect_cb:
                    sess.remove_connect_listener(self._connect_cb)
                sess.release()
        if self._ssdp_cb is not None:
            self.server.remove_ssdp_listener(self._ssdp_cb)
            self._ssdp_cb = None
        try:
            self.sock.close()
        except OSError:
            pass

    def _handle_request(self, req: dict) -> None:
        op = req.get("op", "")
        args = req.get("args") or {}
        rid = req.get("id")
        handler = _OPS.get(op)
        if handler is None:
            self._send({
                "kind": "rsp", "id": rid, "ok": False,
                "error": {"code": -1, "message": f"unknown op: {op}"},
            })
            return
        try:
            result = handler(self, args)
            self._send({"kind": "rsp", "id": rid, "ok": True, "result": result})
        except _OpError as e:
            self._send({
                "kind": "rsp", "id": rid, "ok": False,
                "error": {"code": e.code, "message": str(e)},
            })
        except Exception as e:
            self._send({
                "kind": "rsp", "id": rid, "ok": False,
                "error": {"code": -128, "message": f"{type(e).__name__}: {e}"},
            })


# ---------------------------------------------------------------------------
# Op handlers — small, one per `op` in PROTOCOL.md
# ---------------------------------------------------------------------------

def _op_hello(h: _ConnHandler, args: dict) -> dict:
    abi = int(args.get("abi", 0))
    if abi != ABI_VERSION:
        raise _OpError(-100, f"abi mismatch: shim {abi}, bridge {ABI_VERSION}")
    return {"bridge_version": SHIM_VERSION, "abi": ABI_VERSION,
            "default_printer": None}


def _op_connect_printer(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    dev_ip = str(args.get("dev_ip", ""))
    code = str(args.get("password", args.get("code", "")))
    if not (dev_id and dev_ip and code):
        raise _OpError(-1, "missing dev_id/dev_ip/password")
    sess = h.server.get_or_open_printer(dev_id, dev_ip, code)
    h._subscribed.add(dev_id)

    def listener(p: dict) -> None:
        h._emit_local_message(dev_id, p)

    sess.add_listener(listener)
    sess.add_connect_listener(h._emit_local_connect)
    h._state_cb = listener
    h._connect_cb = h._emit_local_connect
    return {}


def _op_disconnect_printer(h: _ConnHandler, args: dict) -> dict:
    for dev_id in list(h._subscribed):
        sess = h.server._printers.get(dev_id)
        if sess is not None:
            if h._state_cb:
                sess.remove_listener(h._state_cb)
            if h._connect_cb:
                sess.remove_connect_listener(h._connect_cb)
            sess.release()
        h._subscribed.discard(dev_id)
    h._state_cb = None
    h._connect_cb = None
    return {}


def _op_send_message_to_printer(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    payload_json = args.get("json", "")
    if not (dev_id and payload_json):
        raise _OpError(-1, "missing dev_id/json")
    sess = h.server._printers.get(dev_id)
    if sess is None:
        raise _OpError(-1, "printer not connected")
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
    except json.JSONDecodeError as e:
        raise _OpError(-19, f"invalid json payload: {e}") from e
    try:
        sess.client.publish(payload, qos=int(args.get("qos", 1)))
    except Exception as e:
        raise _OpError(-4, f"publish failed: {e}") from e
    return {}


def _op_start_local_print(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    dev_ip = str(args.get("dev_ip", ""))
    code = str(args.get("password", ""))
    filename = str(args.get("filename", ""))
    if not (dev_id and dev_ip and code and filename):
        raise _OpError(-1, "missing dev_id/dev_ip/password/filename")
    creds = Creds(ip=dev_ip, code=code, serial=dev_id)
    local = Path(filename)
    if not local.is_file():
        raise _OpError(-14, f"file not found: {filename}")
    remote = local.name
    try:
        upload_file(creds, local, remote_name=remote)
    except Exception as e:
        raise _OpError(-20, f"FTPS upload failed: {e}") from e
    sess = h.server._printers.get(dev_id)
    if sess is None:
        # Auto-connect for fire-and-forget print flows.
        sess = h.server.get_or_open_printer(dev_id, dev_ip, code)
    ams_mapping_str = args.get("ams_mapping") or "[0]"
    try:
        ams_mapping = json.loads(ams_mapping_str) if isinstance(ams_mapping_str, str) else ams_mapping_str
    except json.JSONDecodeError:
        ams_mapping = [0]
    use_ams = bool(args.get("task_use_ams", True))
    # Defaults removed (per code-review #1): pre-existing
    # `task_bed_type="textured_plate"` fallback re-introduced the same
    # silent-misheat hole. start_print() will auto-derive from `local`
    # (the 3MF we just uploaded) when the GUI shim doesn't supply
    # task_bed_type / task_bed_temp.
    raw_bed = args.get("task_bed_type")
    bed_type_arg = str(raw_bed) if raw_bed else None
    raw_temp = args.get("task_bed_temp")
    # Per second-pass review NIT #6: don't crash the worker on
    # float-as-string. coerce defensively.
    try:
        bed_temp_arg = int(float(raw_temp)) if raw_temp is not None else None
    except (TypeError, ValueError):
        bed_temp_arg = None
    target_slot = ams_mapping[0] if ams_mapping else 0
    # Per code-review #1: parity with cmd_print — validate AMS state
    # before publishing. The serve-mode shim is the BS GUI's primary
    # path and was previously skipping this guard entirely.
    if use_ams:
        try:
            derived = _derive_print_params_from_3mf(local, filament_index=0)
        except PrintRefusal as e:
            raise _OpError(-4031, f"3MF derive failed: {e}") from e
        try:
            live = sess.client.request_state(timeout=15.0)
        except TimeoutError as e:
            raise _OpError(-4032, f"could not pull printer state: {e}") from e
        try:
            _validate_ams_slot(live, int(target_slot), derived, force=False)
        except PrintRefusal as e:
            raise _OpError(-4033, f"AMS slot validation failed: {e}") from e
    try:
        start_print(
            sess.client, remote,
            use_ams=use_ams,
            ams_slot=target_slot,
            bed_levelling=bool(args.get("task_bed_leveling", True)),
            flow_cali=bool(args.get("task_flow_cali", False)),
            timelapse=bool(args.get("task_record_timelapse", False)),
            vibration_cali=bool(args.get("task_vibration_cali", False)),
            bed_type=bed_type_arg,
            bed_temp=bed_temp_arg,
            local_path=local,
        )
    except PrintRefusal as e:
        raise _OpError(-4034, f"start_print refused: {e}") from e
    except Exception as e:
        raise _OpError(-4030, f"start_print MQTT failed: {e}") from e
    return {}


def _op_start_send_gcode_to_sdcard(h: _ConnHandler, args: dict) -> dict:
    dev_ip = str(args.get("dev_ip", ""))
    code = str(args.get("password", ""))
    filename = str(args.get("filename", ""))
    if not (dev_ip and code and filename):
        raise _OpError(-1, "missing dev_ip/password/filename")
    creds = Creds(ip=dev_ip, code=code, serial=str(args.get("dev_id", "")))
    local = Path(filename)
    if not local.is_file():
        raise _OpError(-14, f"file not found: {filename}")
    try:
        upload_file(creds, local, remote_name=local.name)
    except Exception as e:
        raise _OpError(-5010, f"FTPS upload failed: {e}") from e
    return {}


def _op_start_discovery(h: _ConnHandler, args: dict) -> dict:
    """Begin (or stop) SSDP listener; pipe each parsed device to this
    shim as `evt:ssdp_msg`. Idempotent — re-arming twice doesn't
    duplicate listeners."""
    enable = bool(args.get("start", True))
    if not enable:
        # Tear down this shim's listener.
        if h._ssdp_cb is not None:
            h.server.remove_ssdp_listener(h._ssdp_cb)
            h._ssdp_cb = None
        return {}

    h.server._ensure_ssdp_thread()
    if h._ssdp_cb is None:
        def emit(parsed: dict) -> None:
            h._send({
                "kind": "evt",
                "name": "ssdp_msg",
                "data": {"json": json.dumps(parsed, separators=(",", ":"))},
            })
        h._ssdp_cb = emit
        h.server.add_ssdp_listener(emit)
        # Replay every SSDP packet the bridge has seen so far so the
        # GUI's DeviceManager populates immediately instead of waiting
        # up to 30s for the next NOTIFY. This is the SSDP analogue of
        # the local_message latest-state replay (#29). Same shape as a
        # live ssdp_msg event so DeviceManager::on_machine_alive
        # processes them through the normal path.
        with h.server._ssdp_lock:
            cached_packets = list(h.server._ssdp_cache.values())
        for parsed in cached_packets:
            try:
                emit(parsed)
            except Exception as e:
                print(f"[serve] ssdp replay failed: {e}", file=sys.stderr)
    return {}


def _op_subscribe_local(h: _ConnHandler, args: dict) -> dict:
    dev_id = str(args.get("dev_id", ""))
    interval = int(args.get("interval_s", 5))
    enable = bool(args.get("enable", True))
    sess = h.server._printers.get(dev_id) if dev_id else None
    if sess is None:
        raise _OpError(-1, "printer not connected")
    if enable:
        # Item #29: replay cached state immediately so DeviceManager
        # populates MachineObject (AMS slots, temps, lights, etc.)
        # without waiting up to 30s for the next live push. The cached
        # state was set by _PrinterSession._dispatch_state from a prior
        # MQTT push (typically the initial pushall after connect).
        cached = sess.latest_state()
        if cached is not None:
            try:
                h._emit_local_message(dev_id, cached)
            except Exception as e:
                print(f"[serve] state replay raised: {e}", file=sys.stderr)
        # The X2DClient already listens for state pushes once subscribed
        # in connect; kick a fresh pushall here for good measure.
        try:
            sess.client.publish(
                {"pushing": {"sequence_id": _next_seq(),
                             "command": "pushall"}},
            )
        except Exception as e:
            raise _OpError(-4, f"pushall publish failed: {e}") from e
    return {"interval_s": interval, "enable": enable}


def _op_get_version(h: _ConnHandler, args: dict) -> dict:
    return {"version": "02.06.00.50"}  # matches BAMBU_NETWORK_AGENT_VERSION


def _op_noop_ok(h: _ConnHandler, args: dict) -> dict:
    """Cloud-only entry points return success-with-empty so the GUI's
    paint paths don't choke on missing data."""
    return {}


def _cloud_client():
    """Lazy-load the cloud_client module + session. Returns None if the
    module isn't importable (older install without the file) so the
    bridge stays alive even when cloud is broken."""
    try:
        import cloud_client  # noqa: WPS433 — intentional lazy import
        return cloud_client.CloudClient.load_or_anonymous()
    except Exception as e:
        print(f"[serve] cloud_client unavailable: {e}", file=sys.stderr)
        return None


def _op_login_status(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    return {"logged_in": bool(cli and cli.is_logged_in())}


def _op_user_id(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    if cli and cli.is_logged_in():
        try:
            return {"id": cli.get_user_id()}
        except Exception as e:
            print(f"[serve] get_user_id failed: {e}", file=sys.stderr)
    return {"id": ""}


def _op_user_presets(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    if cli and cli.is_logged_in():
        try:
            return {"presets": cli.get_user_presets()}
        except Exception as e:
            print(f"[serve] get_user_presets failed: {e}", file=sys.stderr)
    # Anonymous fallback: load the BBL filament JSONs that ship with
    # bambu-studio plus a small community-curated set, so the GUI's
    # AMS spool dropdown isn't empty for users who haven't signed in.
    return {"presets": _load_local_presets()}


def _stringify_preset_values(d: dict) -> dict:
    """PresetBundle::load_user_presets expects every value as a string
    (or list of strings, which it joins). Re-encode any non-string
    leaf values so the dict round-trips correctly."""
    out: dict[str, str] = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = v
        elif isinstance(v, list):
            out[k] = ",".join(str(x) for x in v)
        elif isinstance(v, (int, float, bool)):
            out[k] = str(v).lower() if isinstance(v, bool) else str(v)
        elif v is None:
            out[k] = ""
        else:
            out[k] = json.dumps(v)
    return out


def _x2d_search_roots() -> list[Path]:
    """Candidate roots for shipped data files. Try (in order):
    - the script's own directory (dev tree, x2d_bridge.py at repo root)
    - the parent (dist tree, x2d_bridge.py at <root>/helpers/)
    so the same code finds files in either layout."""
    here = Path(__file__).parent
    return [here, here.parent]


def _local_preset_dirs() -> list[Path]:
    """Where to look for shipped BBL filament profiles. The first
    candidate that exists wins; the rest are silently skipped so this
    works in both the dev tree (bs-bionic/...) and the unpacked
    tarball (resources/...)."""
    dirs: list[Path] = []
    for root in _x2d_search_roots():
        dirs.append(root / "resources" / "profiles" / "BBL" / "filament")
        dirs.append(root / "bs-bionic" / "resources" / "profiles" / "BBL" / "filament")
    return dirs


def _load_local_presets() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}

    # Community-curated presets — small JSON shipped under runtime/.
    # Same dev-vs-dist multi-root lookup as the BBL profile dirs.
    community = None
    for root in _x2d_search_roots():
        cand = root / "runtime" / "network_shim" / "data" / "community_filaments.json"
        if cand.exists():
            community = cand
            break
    if community is not None:
        try:
            blob = json.loads(community.read_text())
            for name, raw in blob.items():
                if name.startswith("_"):  # comment keys
                    continue
                if not isinstance(raw, dict):
                    continue
                out[name] = _stringify_preset_values(raw)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[serve] local presets: bad community json: {e}", file=sys.stderr)

    # Vendor-shipped BBL filaments — every "instantiation":"true" entry.
    for d in _local_preset_dirs():
        if not d.is_dir():
            continue
        for jf in sorted(d.glob("*.json")):
            try:
                raw = json.loads(jf.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if raw.get("instantiation") != "true":
                continue
            name = raw.get("name") or jf.stem
            # Already loaded? Community version wins.
            if name in out:
                continue
            out[name] = _stringify_preset_values(raw)
        break  # only the first directory that exists

    return out


def _op_user_tasks(h: _ConnHandler, args: dict) -> dict:
    cli = _cloud_client()
    if cli and cli.is_logged_in():
        try:
            limit = int(args.get("limit", 20))
            return {"tasks": cli.get_user_tasks(limit=limit)}
        except Exception as e:
            print(f"[serve] get_user_tasks failed: {e}", file=sys.stderr)
    return {"tasks": []}


_OPS: dict[str, Callable[[_ConnHandler, dict], dict]] = {
    "hello":                       _op_hello,
    "get_version":                 _op_get_version,
    "connect_printer":             _op_connect_printer,
    "disconnect_printer":          _op_disconnect_printer,
    "send_message_to_printer":     _op_send_message_to_printer,
    "start_local_print":           _op_start_local_print,
    "start_local_print_with_record": _op_start_local_print,
    "start_send_gcode_to_sdcard":  _op_start_send_gcode_to_sdcard,
    "subscribe_local":             _op_subscribe_local,
    "start_discovery":             _op_start_discovery,
    # cloud / catalog stubs
    "connect_server":              _op_noop_ok,
    "is_user_login":               _op_login_status,
    "get_user_id":                 _op_user_id,
    "get_user_presets":            _op_user_presets,
    "get_user_tasks":              _op_user_tasks,
    "start_print":                 _op_start_local_print,  # cloud → LAN
}


# ---------------------------------------------------------------------------
# Print-control verbs — direct signed-MQTT publishes for the most common
# operator actions. Payload schemas reverse-engineered from
# bs-bionic/src/slic3r/GUI/DeviceManager.cpp::MachineObject::command_*
# (see comments next to each).
# ---------------------------------------------------------------------------

def _publish_one(args: argparse.Namespace, payload: dict) -> int:
    creds = Creds.resolve(args)
    cli = X2DClient(creds)
    cli.connect()
    try:
        cli.publish(payload)
    finally:
        cli.disconnect()
    print(json.dumps(payload, indent=2))
    return 0


# _print_cmd / _system_cmd / _camera_cmd now live in
# beambam/cli/_helpers.py (Phase 5b). Re-exported at the top.


# _xcam_cmd moved to beambam/cli/_helpers.py (Phase 5a batch 3). Imported
# at the top of this file alongside the other helpers.
from beambam.cli._helpers import _xcam_cmd  # noqa: E402, F401


# cmd_pause / cmd_resume / cmd_stop moved to beambam/cli/control.py
# (Phase 5a). Re-exported below alongside the other LAN control verbs.


# Constant + helper used by cmd_reboot and its unit tests so the wire
# payload is reachable without instantiating an argparse.Namespace.
_REBOOT_GCODE = "M999"


def _reboot_payload() -> dict:
    """The wire payload `beambam reboot --confirm` sends.

    M999 is the Marlin "restart from emergency stop" gcode. On Bambu
    firmware it clears the printer's halt/error flags and re-arms the
    motion system; it does NOT power-cycle the SoC, the MQTT broker,
    or the network stack. There is no documented MQTT verb for a true
    soft reboot on current X-series firmware — the only paths are the
    physical power button or an OTA firmware update."""
    return _print_cmd("gcode_line", param=f"{_REBOOT_GCODE}\n")


# cmd_reboot moved to beambam/cli/control.py (Phase 5a batch 2). The
# `_reboot_payload` helper + `_REBOOT_GCODE` constant stay here so
# `from x2d_bridge import _reboot_payload` keeps working for tests.


# LAN control verbs live in beambam/cli/control.py (Phase 5a). The
# bridge re-exports each one so external callers + tests that
# `from x2d_bridge import cmd_*` keep working without modification.
from beambam.cli.control import (  # noqa: E402, F401
    cmd_pause,
    cmd_resume,
    cmd_stop,
    cmd_gcode,
    cmd_home,
    cmd_level,
    cmd_set_temp,
    cmd_chamber_light,
    cmd_reboot,
    cmd_jog,
    cmd_record,
    cmd_timelapse,
    cmd_resolution,
    cmd_fod_check,
    cmd_ams_load,
    cmd_ams_unload,
)


# cmd_fod_check / cmd_ams_unload / cmd_ams_load moved to
# beambam/cli/control.py (Phase 5a batch 3).


# cmd_jog moved to beambam/cli/control.py (Phase 5a batch 2).


# ---------------------------------------------------------------------------
# `camera` subcommand — RTSPS-to-MJPEG proxy.
#
# Bambu's GUI streams the printer's chamber camera via either:
#   * `rtsps://bblp:<code>@<ip>:322/streaming/live/1`
#     — standard RTSPS, works iff the printer's own "LAN-mode liveview"
#     toggle is enabled (Settings → Network → Liveview on the touchscreen,
#     OR the `ipcam.rtsp_url` field comes back as a real URL instead of
#     "disable" in the printer's pushed state).
#   * The closed proprietary "LVL_Local" protocol on TCP port 6000, only
#     speakable through the x86_64-only libBambuSource.so. Not usable on
#     aarch64 until that protocol is reverse-engineered.
#
# This subcommand wraps the RTSPS path with ffmpeg → MJPEG-over-HTTP so a
# phone browser at http://127.0.0.1:8766/cam.mjpeg sees the stream live.
# Multiple browser clients tee off the same single ffmpeg subprocess.
# Surfaces a clear error when the printer reports rtsp_url=disable.
# ---------------------------------------------------------------------------

# x2d/termux #88 — IPCAM control commands matching BS DeviceManager.cpp:
#   command_ipcam_record (2027), command_ipcam_timelapse (2038),
#   command_ipcam_resolution_set (2049). Plain MQTT publish to
#   device/<sn>/request, no Bambu Connect signing. Sub-commands match
#   the printer's bambu IPCAM service which controls the chamber camera
#   (recording to SD, timelapse capture, resolution).
# cmd_record / cmd_timelapse moved to beambam/cli/control.py
# (Phase 5a batch 2).


def cmd_files(args: argparse.Namespace) -> int:
    """List SD-card files via FTPS — see runtime/network_shim/file_tunnel.py.
    Empirical finding (#92): X2D firmware exposes its SD card via vsFTPd
    on port 990, NOT the BambuTunnel:6000 protocol that older Bambu
    printers + BambuStudio source assume."""
    import json as _json
    creds = Creds.resolve(args)
    try:
        from runtime.network_shim.file_tunnel import (
            FileTunnelClient, FileTunnelError,
        )
    except ImportError as e:
        sys.exit(f"file_tunnel module missing: {e}")

    try:
        with FileTunnelClient(creds.ip, creds.code) as cli:
            files = cli.list_files(args.kind)
    except FileTunnelError as e:
        sys.exit(f"file_tunnel: {e}")
    except OSError as e:
        sys.exit(f"socket error: {e}")

    if args.json:
        print(_json.dumps(
            [{"name": f.name, "path": f.path, "time": f.time,
              "size": f.size, "is_dir": f.is_dir}
             for f in files], indent=2,
        ))
    else:
        if not files:
            print(f"(no {args.kind} files)")
        for f in files:
            print(f)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Download a model from MakerWorld / Printables / Thingiverse — or
    any direct STL/3MF URL — into ~/Downloads/x2d-models/ and optionally
    open it in BambuStudio (#98 in IMPROVEMENTS.md).

    URL parsers:
      * MakerWorld: https://makerworld.com/en/models/<NUMERIC_ID>* —
        fetches the page, finds the "instant download" 3mf URL.
      * Printables: https://www.printables.com/model/<NUMERIC_ID>-<slug>* —
        fetches the JSON API endpoint, picks the first "files/file_models" download URL.
      * Thingiverse: https://www.thingiverse.com/thing:<NUMERIC_ID>* —
        downloads from /thing:<id>/zip and unpacks STLs into the dir.
      * Direct .stl / .3mf / .obj / .step URLs: just curl them.
    """
    import json as _json
    import re
    import shutil
    import subprocess
    import urllib.parse
    from urllib.request import Request, urlopen

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    UA = ("Mozilla/5.0 (Linux; Android 14; SM-S938U) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

    def http_get(u: str, accept: str = "*/*", timeout: int = 30,
                 extra_headers: dict[str, str] | None = None) -> bytes:
        hdrs = {"User-Agent": UA, "Accept": accept}
        if extra_headers:
            hdrs.update(extra_headers)
        req = Request(u, headers=hdrs)
        with urlopen(req, timeout=timeout) as r:
            return r.read()

    def save(name: str, data: bytes) -> Path:
        p = out_dir / name
        p.write_bytes(data)
        print(f"[fetch] saved {len(data)//1024} KiB → {p}", file=sys.stderr)
        return p

    url = args.url.strip()
    pu = urllib.parse.urlparse(url)
    host = pu.netloc.lower()
    path = pu.path

    saved: list[Path] = []

    if any(url.lower().endswith(ext) for ext in (".stl", ".3mf", ".obj", ".step", ".stp")):
        # Direct file URL — easy path.
        name = Path(urllib.parse.unquote(path)).name or "model.stl"
        saved.append(save(name, http_get(url, accept="application/octet-stream")))

    elif "makerworld" in host:
        # MakerWorld: extract the design ID; the 2026 backend rewrote the
        # API surface. The old /api/v1/design/design-detail endpoint is
        # gone (404). Use /api/v1/design-service/design/<id> for metadata.
        # NOTE: MakerWorld's public API does NOT expose a 3mf download URL
        # for arbitrary designs — the bbsl bucket needs MakerWorld web
        # cookies. We fetch metadata + cover and surface a clear "manual
        # download required" message for the 3mf itself. For richer
        # access use `beambam cloud-fetch --info <id>` / `--instances <id>`.
        m = re.search(r"/models/(\d+)", path)
        if not m:
            sys.exit(f"can't extract MakerWorld design ID from {url!r}")
        mid = m.group(1)
        api = f"https://makerworld.com/api/v1/design-service/design/{mid}"
        meta = _json.loads(http_get(api, accept="application/json"))
        title = meta.get("title") or f"makerworld-{mid}"
        cover = meta.get("coverUrl") or meta.get("coverPortrait")
        if cover:
            cover_name = f"{title.replace('/', '_').strip()}.cover.png"
            saved.append(save(cover_name,
                              http_get(cover, accept="image/*")))
        instance_count = len(meta.get("instances") or [])
        print(f"[fetch] MakerWorld design {mid} \"{title}\" — {instance_count} "
              f"sliced instances. The 3mf itself can't be downloaded via "
              f"public API (CDN requires MakerWorld web cookies). Open\n"
              f"  https://makerworld.com/en/models/{mid}\n"
              f"in a browser and click \"Download\" to get the .gcode.3mf.\n"
              f"Run `beambam cloud-fetch --instances {mid}` to see all\n"
              f"slicer profiles before deciding which to download.",
              file=sys.stderr)

    elif "printables" in host:
        m = re.search(r"/model/(\d+)", path)
        if not m:
            sys.exit(f"can't extract Printables model ID from {url!r}")
        pid_ = m.group(1)
        # Printables uses a public GraphQL endpoint (2026 surface). The legacy
        # REST /v2/models/<id>/files returns 404 since the 2026 backend
        # rewrite. The Query.print(id:) field returns stls + otherFiles with
        # filePreviewPath; the real file URL is the same directory minus the
        # "_preview.png" suffix, joined under files.printables.com.
        gql_q = ("query($id:ID!){print(id:$id){id name slug "
                 "stls{id name fileSize filePreviewPath} "
                 "otherFiles{id name fileSize filePreviewPath fileFormat}}}")
        try:
            body = _json.dumps({"query": gql_q, "variables": {"id": pid_}}).encode()
            req = urllib.request.Request(
                "https://api.printables.com/graphql/",
                data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": f"beambam/{PACKAGE_VERSION}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                gql = _json.loads(resp.read())
            prt = (gql.get("data") or {}).get("print") or {}
            entries = []
            entries += [(s, ".stl") for s in (prt.get("stls") or [])]
            entries += [(s, "") for s in (prt.get("otherFiles") or [])]
            for entry, fallback_ext in entries:
                preview = entry.get("filePreviewPath") or ""
                name = entry.get("name") or ""
                # name may already include extension (otherFiles); stls don't
                # always carry it on the API but always do in the path.
                if name and not any(name.lower().endswith(e)
                                    for e in (".stl", ".3mf", ".obj", ".step",
                                              ".stp")):
                    name += fallback_ext
                if not name or not preview:
                    continue
                # preview path: "media/prints/<PID>/stls/<UID>/<stem>_preview.png"
                # real path:    "media/prints/<PID>/stls/<UID>/<name>"
                p = Path(preview)
                dir_ = "/".join(p.parts[:-1])
                file_url = f"https://files.printables.com/{dir_}/{name}"
                try:
                    saved.append(save(name, http_get(
                        file_url, accept="application/octet-stream")))
                    if not args.all:
                        break
                except Exception as e:
                    print(f"[fetch] {file_url} → {e}", file=sys.stderr)
        except Exception as e:
            print(f"[fetch] Printables GraphQL failed: {e}", file=sys.stderr)
        if not saved:
            sys.exit(f"no STL/3MF download URL found for Printables {pid_} — "
                     f"model may be premium-only or the GraphQL schema has "
                     f"shifted again. Workaround: open in browser, save the "
                     f"download URL, and pass it directly to `fetch`.")

    elif "thingiverse" in host:
        m = re.search(r"thing:(\d+)", path)
        if not m:
            sys.exit(f"can't extract Thingiverse model ID from {url!r}")
        tid = m.group(1)
        # Thingiverse gated public-API + /download:NNN behind auth in 2026.
        # Three input modes, in priority order:
        #   1) THINGIVERSE_TOKEN — official Bearer token from
        #      https://www.thingiverse.com/apps/create (free).
        #   2) THINGIVERSE_COOKIE — raw Cookie header from a logged-in
        #      browser session. Use this if you've signed in on the same
        #      machine: copy the Cookie value from devtools Network tab on
        #      any thingiverse.com request.
        #   3) No auth → public-API will 401 and we surface a clear error.
        ti_token  = os.environ.get("THINGIVERSE_TOKEN", "").strip()
        ti_cookie = os.environ.get("THINGIVERSE_COOKIE", "").strip()
        api = f"https://api.thingiverse.com/things/{tid}/files"
        try:
            extra = {}
            if ti_token:  extra["Authorization"] = f"Bearer {ti_token}"
            if ti_cookie: extra["Cookie"] = ti_cookie
            files_meta = _json.loads(http_get(
                api, accept="application/json",
                extra_headers=extra or None))
            for entry in files_meta:
                dl = entry.get("download_url") or entry.get("public_url")
                name = entry.get("name", f"thingiverse-{tid}-{entry.get('id', 'x')}.stl")
                if dl and name.lower().endswith((".stl", ".3mf", ".obj", ".step", ".stp")):
                    saved.append(save(name, http_get(
                        dl, accept="application/octet-stream",
                        extra_headers=(
                            {"Cookie": ti_cookie} if ti_cookie else None))))
                    if not args.all:
                        break
        except Exception as e:
            print(f"[fetch] Thingiverse API failed: {e}", file=sys.stderr)
        if not saved:
            # Page scrape (still works for some legacy models)
            page_url = f"https://www.thingiverse.com/thing:{tid}"
            try:
                page = http_get(
                    page_url,
                    extra_headers=({"Cookie": ti_cookie} if ti_cookie else None)
                ).decode("utf-8", errors="replace")
            except Exception as e:
                print(f"[fetch] page fetch failed: {e}", file=sys.stderr)
                page = ""
            for fpat in (r'href="(https://cdn\.thingiverse\.com/[^"]+\.(?:stl|3mf|obj))"',
                         r'href="(/download:\d+)"'):
                for fm in re.finditer(fpat, page):
                    fu = fm.group(1)
                    if fu.startswith("/"):
                        fu = "https://www.thingiverse.com" + fu
                    name = Path(urllib.parse.unquote(urllib.parse.urlparse(fu).path)).name or "model.stl"
                    if not any(name.lower().endswith(e) for e in (".stl", ".3mf", ".obj")):
                        name += ".stl"
                    try:
                        saved.append(save(name, http_get(
                            fu, accept="application/octet-stream",
                            extra_headers=(
                                {"Cookie": ti_cookie} if ti_cookie else None))))
                    except Exception as e:
                        print(f"[fetch] {fu} → {e}", file=sys.stderr)
                    if not args.all:
                        break
                if saved:
                    break
        if not saved:
            sys.exit(f"no STL/3MF links found for Thingiverse {tid}.\n"
                     f"Thingiverse requires authentication for downloads as of "
                     f"2026 — set ONE of:\n"
                     f"  THINGIVERSE_TOKEN=<bearer>   "
                     f"(get one at thingiverse.com/apps/create, free)\n"
                     f"  THINGIVERSE_COOKIE='session=...'   "
                     f"(copy Cookie header from a logged-in browser)\n"
                     f"Alternatively: open in browser, save the .stl/.3mf, "
                     f"and pass the local file path to BambuStudio directly.")
    else:
        sys.exit(f"unsupported URL host {host!r} — supported: "
                 f"direct STL/3MF/OBJ links, makerworld.com, printables.com, "
                 f"thingiverse.com")

    if args.json:
        print(_json.dumps([str(p) for p in saved], indent=2))
    else:
        for p in saved:
            print(p)

    if args.open:
        bs_bin = X2D_ROOT_PATH / "bs-bionic" / "build" / "src" / "bambu-studio"
        if not bs_bin.exists():
            print(f"[fetch] bambu-studio not at {bs_bin} — skipping --open",
                  file=sys.stderr)
        else:
            # Spawn BS with the file(s) on argv. If BS is already running,
            # this will start a NEW instance — wxApp single-instance mode
            # would forward, but BS has it disabled. The user gets a 2nd window.
            subprocess.Popen(
                [str(bs_bin)] + [str(p) for p in saved],
                env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"[fetch] launched bambu-studio with {len(saved)} file(s)",
                  file=sys.stderr)
    return 0


def cmd_slice_print(args: argparse.Namespace) -> int:
    """One-shot pipeline: slice an STL with x2d_slice.py + upload + start
    print on the configured X2D. Resolves #99 in IMPROVEMENTS.md.

    Equivalent to:
        x2d_slice.py model.stl --out tmp.gcode.3mf
        x2d_bridge.py print tmp.gcode.3mf

    With --dry-run, slices but doesn't upload — useful for testing.
    """
    import subprocess
    import tempfile

    stl = Path(args.stl)
    if not stl.exists():
        sys.exit(f"input not found: {stl}")
    if stl.suffix.lower() not in (".stl", ".step", ".stp", ".obj", ".3mf"):
        sys.exit(f"unsupported input extension: {stl.suffix}")

    # Slice into a temp .gcode.3mf using x2d_slice.py
    slice_bin = X2D_ROOT_PATH / "x2d_slice.py"
    if not slice_bin.exists():
        sys.exit(f"x2d_slice.py not found at {slice_bin}")

    with tempfile.TemporaryDirectory(prefix="x2d_sp_") as td:
        out_3mf = Path(td) / f"{stl.stem}.gcode.3mf"
        if stl.suffix.lower() == ".3mf":
            # Already a 3mf — just re-slice via BS CLI directly to refresh metadata
            print(f"[slice-print] input already a 3mf, re-slicing in place",
                  file=sys.stderr)
            bs_bin = X2D_ROOT_PATH / "bs-bionic" / "build" / "src" / "bambu-studio"
            rc = subprocess.call([
                str(bs_bin), "--slice", "0",
                "--outputdir", str(out_3mf.parent),
                "--export-3mf", out_3mf.name,
                str(stl),
            ], env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")})
        else:
            # STL/OBJ/STEP — graft into template and slice
            cmd = [str(slice_bin), str(stl), "--out", str(out_3mf)]
            if args.template:
                cmd.extend(["--template", str(args.template)])
            if args.scale and args.scale != 1.0:
                cmd.extend(["--scale", str(args.scale)])
            if getattr(args, "scale_pct", None) is not None:
                cmd.extend(["--scale-pct", str(args.scale_pct)])
            if getattr(args, "mm", None) is not None:
                cmd.extend(["--mm", str(args.mm)])
            if getattr(args, "copies", 1) and int(args.copies) != 1:
                cmd.extend(["--copies", str(int(args.copies))])
            if args.color:
                cmd.extend(["--color", args.color])
            rc = subprocess.call(cmd)
        if rc != 0:
            sys.exit(f"slicing failed rc={rc}")
        if not out_3mf.exists():
            sys.exit("slicing reported success but no .gcode.3mf produced")

        # Print metrics for confirmation
        import zipfile as _zf
        try:
            with _zf.ZipFile(out_3mf) as z:
                info = z.read("Metadata/slice_info.config").decode("utf-8", errors="replace")
            for key in ("prediction", "weight", "used_m"):
                for line in info.splitlines():
                    if f'key="{key}"' in line or f'tray_info_idx' in line:
                        print(f"  {line.strip()}", file=sys.stderr)
                        break
        except Exception:
            pass

        if args.dry_run:
            # Save the sliced .gcode.3mf so user can inspect it
            keep = stl.with_suffix(".sliced.gcode.3mf")
            shutil.copy2(out_3mf, keep)
            print(f"[slice-print] DRY RUN — sliced to {keep}; not uploading",
                  file=sys.stderr)
            return 0

        # Upload + print via existing path
        creds = Creds.resolve(args)
        upload_file(creds, out_3mf, remote_name=args.remote)
        cli = X2DClient(creds)
        cli.connect()
        name = args.remote or out_3mf.name
        # "auto" sentinel = let start_print() derive from the 3MF we just
        # produced, which is the slicer's authoritative contract.
        sliced_bed = args.bed_type if args.bed_type and args.bed_type != "auto" else None
        # Per code-review #3: parity with cmd_print — refuse to send if
        # the targeted AMS slot is empty or has the wrong filament class.
        # `--force` is not exposed on slice-print; users wanting to bypass
        # should re-slice or load matching material.
        if not args.no_ams:
            derived = _derive_print_params_from_3mf(out_3mf, filament_index=0)
            try:
                live = cli.request_state(timeout=15.0)
            except TimeoutError:
                cli.disconnect()
                raise SystemExit(
                    "could not pull live printer state to validate AMS slot "
                    "before sending. Re-run when the printer is reachable.")
            _validate_ams_slot(live, args.slot, derived, force=False)
        start_print(cli, name,
                    use_ams=not args.no_ams, ams_slot=args.slot,
                    bed_levelling=not args.no_bed_level,
                    flow_cali=args.flow_cali,
                    timelapse=args.timelapse,
                    vibration_cali=args.vib_cali,
                    bed_type=sliced_bed,
                    local_path=out_3mf)
        print(f"[slice-print] queued: {name} on {creds.ip} (ams_slot={args.slot})")
        cli.disconnect()
    return 0


# cmd_resolution moved to beambam/cli/control.py (Phase 5a batch 2).


# ---------------------------------------------------------------------------
# x2d/termux #88 — `health` one-shot diagnostic. Combines what the user
# typically needs to debug a fresh install: TCP reachability, MQTT
# connect, last printer state, AMS slot summary, camera port. Output
# is concise (one line per check) so it fits in a phone terminal.

# cmd_health moved to beambam/cli/info.py (Phase 5c batch 2). Re-exported
# alongside cmd_status / cmd_printers below.


# cmd_watch moved to beambam/cli/info.py (Phase 5c batch 2). Re-exported
# alongside cmd_health / cmd_status / cmd_printers below.


# _TailDispatcher / _tail_print / cmd_tail moved to beambam/cli/info.py
# (Phase 5c batch 3). Re-exported below alongside the other info verbs
# so tests using `from x2d_bridge import _TailDispatcher` keep working.


# cmd_notify moved to beambam/cli/info.py (Phase 5c batch 3).


def cmd_camera(args: argparse.Namespace) -> int:
    import http.server
    import shutil
    import signal as _signal
    import socketserver
    import subprocess as _sp
    from threading import Lock as _Lock

    creds = Creds.resolve(args)

    # Pre-flight: poke the printer's state to confirm RTSP is enabled.
    if not args.skip_check:
        try:
            cli = X2DClient(creds)
            cli.connect(timeout=8.0)
            state = cli.request_state(timeout=8.0)
            cli.disconnect()
            ipcam = state.get("print", {}).get("ipcam", {})
            rtsp_url = ipcam.get("rtsp_url", "disable")
            if rtsp_url == "disable":
                print(
                    "[camera] printer reports ipcam.rtsp_url=\"disable\".\n"
                    "         Enable LAN-mode liveview on the printer's\n"
                    "         touchscreen (Settings → Network → Liveview)\n"
                    "         and re-run. Or pass --skip-check to try anyway.",
                    file=sys.stderr,
                )
                return 2
            elif rtsp_url and not rtsp_url.startswith(("rtsp://", "rtsps://")):
                print(f"[camera] unexpected ipcam.rtsp_url: {rtsp_url}",
                      file=sys.stderr)
                return 2
            print(f"[camera] printer rtsp_url=ok ({rtsp_url[:40]}...)",
                  file=sys.stderr)
        except Exception as e:
            print(f"[camera] state-pre-flight failed: {e} — continuing anyway",
                  file=sys.stderr)

    if shutil.which("ffmpeg") is None:
        print("[camera] ffmpeg not installed. `pkg install ffmpeg` first.",
              file=sys.stderr)
        return 2

    rtsp_url_full = (
        f"rtsps://bblp:{creds.code}@{creds.ip}:{args.port}/streaming/live/1"
    )

    # Single shared frame buffer + cv. ffmpeg writes JPEG frames here;
    # every HTTP client reads the latest. We never queue history — old
    # frames are dropped, viewers see live.
    state_lock = _Lock()
    latest_frame = {"data": b"", "ts": 0.0}
    # Two events:
    #   global_stop  — SIGINT/SIGTERM only; tears down the whole daemon
    #   local_stop   — per-pump-instance; the supervisor sets this when
    #                  the idle reaper decides ffmpeg can rest. A fresh
    #                  Event is created for each new pump spawn.
    global_stop = Event()

    # HLS output dir (item #20). Each segment is ~2s of mpegts; we keep
    # a sliding window of 6 (12s of buffer) and let ffmpeg auto-delete
    # older ones via -hls_flags delete_segments. Cleaned up at exit.
    import tempfile as _tempfile
    hls_dir = Path(_tempfile.mkdtemp(prefix="x2d-hls-"))
    hls_playlist = hls_dir / "cam.m3u8"
    hls_segment_pattern = hls_dir / "cam%04d.ts"

    def ffmpeg_pump(local_stop: Event):
        backoff = 1.0
        while not local_stop.is_set() and not global_stop.is_set():
            cmd = [
                "ffmpeg",
                "-loglevel", "error",
                "-rtsp_transport", "tcp",
                "-i", rtsp_url_full,
                # Output 1: MJPEG-on-stdout, consumed by the JPEG buffer
                # below for /cam.mjpeg + /cam.jpg.
                "-map", "0:v",
                "-an",
                "-c:v", "mjpeg",
                "-q:v", "5",
                "-f", "image2pipe",
                "-update", "1",
                "pipe:1",
                # Output 2: HLS segments + playlist for /cam.m3u8.
                # -c:v copy when the input is already H.264 (the X2D's
                # RTSPS stream); ffmpeg falls back to re-encode if not.
                "-map", "0:v",
                "-an",
                "-c:v", "copy",
                "-f", "hls",
                "-hls_time", "2",
                "-hls_list_size", "6",
                "-hls_flags", "delete_segments+append_list+omit_endlist",
                "-hls_segment_filename", str(hls_segment_pattern),
                str(hls_playlist),
            ]
            print(f"[camera] spawning ffmpeg (port {args.port})", file=sys.stderr)
            proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.PIPE,
                             close_fds=True)
            try:
                jpeg_buf = b""
                # stdout.read() blocks indefinitely; we add a tiny poll
                # loop on local_stop via select-with-timeout so the idle
                # reaper can stop ffmpeg promptly without waiting for the
                # next chunk arrival.
                import select as _select
                while not local_stop.is_set() and not global_stop.is_set():
                    rlist, _, _ = _select.select([proc.stdout], [], [], 0.5)
                    if not rlist:
                        continue
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        err = proc.stderr.read().decode(errors="replace")[-400:]
                        print(f"[camera] ffmpeg eof; stderr tail: {err}",
                              file=sys.stderr)
                        break
                    jpeg_buf += chunk
                    # MJPEG single-image output writes back-to-back JPEGs.
                    # Split on SOI marker (0xFFD8) — keep the most-recent
                    # complete frame.
                    while True:
                        idx = jpeg_buf.find(b"\xff\xd8", 1)
                        if idx == -1:
                            break
                        frame, jpeg_buf = jpeg_buf[:idx], jpeg_buf[idx:]
                        if frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9"):
                            with state_lock:
                                latest_frame["data"] = frame
                                latest_frame["ts"]   = time.time()
            finally:
                # Reap the subprocess cleanly — terminate, wait, escalate
                # to kill if necessary, then wait again. Without the final
                # wait() after kill() the process becomes a zombie
                # (<defunct> in ps) until the daemon itself exits.
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except _sp.TimeoutExpired:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
                except Exception:
                    pass
            if local_stop.is_set() or global_stop.is_set():
                break
            print(f"[camera] reconnecting in {backoff:.1f}s", file=sys.stderr)
            local_stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
        # On pump exit (idle reaper or shutdown), invalidate the cached
        # frame so the next supervisor spawn can't serve a stale image.
        with state_lock:
            latest_frame["data"] = b""
            latest_frame["ts"] = 0.0

    def lvl_local_pump(local_stop: Event):
        # Push module path so a repo-checkout install also imports it.
        # Same dev-vs-dist multi-root lookup as _x2d_search_roots().
        for root in _x2d_search_roots():
            cand = root / "runtime" / "network_shim"
            if (cand / "lvl_local.py").exists():
                sys.path.insert(0, str(cand))
                break
        try:
            import lvl_local
        except ImportError as e:
            print(f"[camera] lvl_local module unavailable: {e}", file=sys.stderr)
            return

        def _store(jpeg, ts):
            if local_stop.is_set() or global_stop.is_set():
                raise SystemExit
            with state_lock:
                latest_frame["data"] = jpeg
                latest_frame["ts"] = time.time()

        try:
            lvl_local.stream_frames(creds.ip, creds.code, on_frame=_store)
        except SystemExit:
            pass
        except lvl_local.LVLLocalError as e:
            # Fatal vs transient is hard to know — surface and let the
            # outer reconnect logic in stream_frames handle the retry
            # (which it does until it gets a non-LVLLocalError).
            print(f"[camera] LVL_Local fatal: {e}", file=sys.stderr)
        # On pump exit, invalidate the cached frame (see ffmpeg_pump for
        # rationale).
        with state_lock:
            latest_frame["data"] = b""
            latest_frame["ts"] = 0.0

    if args.proto == "local":
        print("[camera] proto=local — using TLS:6000 LVL_Local stream", file=sys.stderr)
        pump_factory = lvl_local_pump
        pump_label = "lvl_local"
    else:
        pump_factory = ffmpeg_pump
        pump_label = "ffmpeg"

    # ---------------------------------------------------------------------
    # On-demand pump supervisor (item-89 — battery drain triage).
    # Previously the pump (ffmpeg or lvl_local) was eagerly started at
    # daemon launch and ran 24/7 at ~66% CPU even when nobody was viewing
    # the stream. The supervisor lazy-spawns the pump on first request,
    # tracks long-poll viewers (refcount) plus one-shot endpoint hits
    # (last-touch deadline), and the reaper thread terminates the pump
    # after IDLE_TIMEOUT seconds of zero activity. Touch endpoints
    # (/cam.jpg, /cam.m3u8, /cam*.ts) keep the pump alive between polls.
    # ---------------------------------------------------------------------
    class CameraStreamSupervisor:
        IDLE_TIMEOUT = float(getattr(args, "idle_timeout", 30.0))
        FIRST_FRAME_TIMEOUT = 8.0  # max seconds to wait for ffmpeg's first JPEG

        def __init__(self):
            self._lock = _Lock()
            self._refs = 0
            self._last_touch = 0.0
            self._local_stop: Event | None = None
            self._thread: Thread | None = None
            self._reaper = Thread(target=self._reap_loop,
                                   name="camera-reaper", daemon=True)
            self._reaper.start()

        def _ensure_running_locked(self) -> None:
            if self._thread is not None and self._thread.is_alive():
                return
            self._local_stop = Event()
            local = self._local_stop
            self._thread = Thread(target=lambda: pump_factory(local),
                                   name=f"camera-pump-{pump_label}",
                                   daemon=True)
            print(f"[camera] starting {pump_label} pump (refs={self._refs}, "
                  f"idle_timeout={self.IDLE_TIMEOUT}s)", file=sys.stderr)
            self._thread.start()

        def acquire(self) -> None:
            with self._lock:
                self._refs += 1
                self._last_touch = time.time()
                self._ensure_running_locked()

        def release(self) -> None:
            with self._lock:
                if self._refs > 0:
                    self._refs -= 1

        def touch(self) -> None:
            with self._lock:
                self._last_touch = time.time()
                self._ensure_running_locked()

        def wait_for_frame(self, timeout: float) -> bool:
            """Block until latest_frame has data, or timeout expires.
            Used by /cam.jpg and /cam.m3u8 on the cold-start path so the
            first request after idle doesn't 503 immediately."""
            deadline = time.time() + timeout
            while time.time() < deadline:
                if global_stop.is_set():
                    return False
                with state_lock:
                    if latest_frame["data"]:
                        return True
                time.sleep(0.1)
            with state_lock:
                return bool(latest_frame["data"])

        def _reap_loop(self) -> None:
            while not global_stop.is_set():
                global_stop.wait(2.0)
                if global_stop.is_set():
                    break
                with self._lock:
                    if self._thread is None or not self._thread.is_alive():
                        continue
                    if self._refs > 0:
                        continue
                    idle = time.time() - self._last_touch
                    if idle <= self.IDLE_TIMEOUT:
                        continue
                    print(f"[camera] idle {idle:.0f}s ≥ {self.IDLE_TIMEOUT}s "
                          f"with no viewers; stopping {pump_label} pump",
                          file=sys.stderr)
                    if self._local_stop is not None:
                        self._local_stop.set()
                    # Don't join here — the pump thread cleans up its own
                    # subprocess in its finally block. is_alive() check on
                    # the next reap pass tells us when it's done.

        def shutdown(self) -> None:
            with self._lock:
                if self._local_stop is not None:
                    self._local_stop.set()

    supervisor = CameraStreamSupervisor()

    # Tiny HTTP server. Two endpoints:
    #   /cam.mjpeg  → multipart/x-mixed-replace (browser-renderable)
    #   /cam.jpg    → single latest JPEG
    class CameraHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): return
        def do_GET(self):  # noqa: N802
            if not _check_bearer(self, args.auth_token or None, host):
                return
            if self.path in ("/cam.mjpeg", "/"):
                # Long-poll viewer — refcount the supervisor so the pump
                # stays alive while this client is connected, even if no
                # touch endpoints are being hit.
                supervisor.acquire()
                try:
                    # Wait briefly for the pump's first frame so we don't
                    # send headers and then immediately stall.
                    supervisor.wait_for_frame(supervisor.FIRST_FRAME_TIMEOUT)
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    last_ts = 0.0
                    try:
                        while not global_stop.is_set():
                            with state_lock:
                                frame = latest_frame["data"]
                                ts    = latest_frame["ts"]
                            if frame and ts > last_ts:
                                last_ts = ts
                                self.wfile.write(b"--frame\r\n")
                                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                                self.wfile.write(
                                    f"Content-Length: {len(frame)}\r\n\r\n".encode())
                                self.wfile.write(frame)
                                self.wfile.write(b"\r\n")
                                self.wfile.flush()
                            else:
                                time.sleep(0.05)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                finally:
                    supervisor.release()
            elif self.path == "/cam.jpg":
                supervisor.touch()
                with state_lock:
                    frame = latest_frame["data"]
                if not frame:
                    # Cold-start path: pump might have just spawned and is
                    # waiting for its first JPEG. Block briefly so the
                    # caller gets a frame instead of 503.
                    supervisor.wait_for_frame(supervisor.FIRST_FRAME_TIMEOUT)
                    with state_lock:
                        frame = latest_frame["data"]
                if not frame:
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
            elif self.path == "/cam.m3u8":
                # HLS playlist (item #20). 503 until ffmpeg has emitted
                # at least one segment and the playlist file exists.
                supervisor.touch()
                if not hls_playlist.exists():
                    # Cold-start path: wait briefly for ffmpeg to emit
                    # the first segment after a lazy spawn.
                    deadline = time.time() + supervisor.FIRST_FRAME_TIMEOUT
                    while time.time() < deadline and not hls_playlist.exists():
                        if global_stop.is_set():
                            break
                        time.sleep(0.2)
                if not hls_playlist.exists():
                    self.send_response(503)
                    self.end_headers()
                    return
                try:
                    body = hls_playlist.read_bytes()
                except OSError:
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/cam") and self.path.endswith(".ts"):
                # HLS segment. Validate the filename to prevent path
                # traversal (only `cam<digits>.ts` shape allowed).
                supervisor.touch()
                seg_name = self.path[1:]  # strip leading slash
                import re as _re
                if not _re.fullmatch(r"cam\d+\.ts", seg_name):
                    self.send_response(404)
                    self.end_headers()
                    return
                seg = hls_dir / seg_name
                if not seg.exists():
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    body = seg.read_bytes()
                except OSError:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "max-age=10")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    host, _, port = args.bind.rpartition(":")
    host = host or "127.0.0.1"
    port = int(port)
    server = ThreadingServer((host, port), CameraHandler)

    def _stop(signum, frame):  # noqa: ARG001
        global_stop.set()
        supervisor.shutdown()
        server.shutdown()
    _signal.signal(_signal.SIGINT,  _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    print(f"[camera] HTTP at http://{host}:{port}/cam.mjpeg "
          f"(JPEG snapshot /cam.jpg, HLS /cam.m3u8). On-demand pump "
          f"(idles after {CameraStreamSupervisor.IDLE_TIMEOUT}s of no "
          f"viewers).", file=sys.stderr)
    print(f"[camera] HLS segments → {hls_dir}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        global_stop.set()
        supervisor.shutdown()
        server.server_close()
        # HLS cleanup — best-effort, don't propagate errors.
        import shutil as _shutil
        try:
            _shutil.rmtree(hls_dir, ignore_errors=True)
        except Exception:
            pass
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    sock_path = Path(args.sock).expanduser()
    server = ServeServer(sock_path)
    return server.serve_forever()


def cmd_daemon(args: argparse.Namespace) -> int:
    """Multi-printer daemon (item #36).

    Spawns one X2DClient per printer section in ~/.x2d/credentials. If
    --printer is passed, only that one is started. State, last_message_ts
    and pushall polling are tracked per printer. The HTTP server routes
    `?printer=NAME` to the matching client. Connection failures are
    isolated: a single unreachable printer doesn't take down the others.
    """
    # Determine the set of printers to drive.
    if args.printer:
        names_to_run: list[str] = [args.printer]
    else:
        names = Creds.list_names()
        names_to_run = names if names else [""]
    # Per-printer state cache + clients.
    states: dict[str, dict | None] = {n: None for n in names_to_run}
    # Per-printer pub/sub hub. Drop-oldest-on-full so a slow SSE/HA
    # consumer can't backpressure the MQTT thread. Wired into the SSE
    # handler so /state.events pushes are immediate (no 1 Hz polling)
    # and a fresh client replays last_state on connect.
    from beambam.state_hub import StateHub
    hubs: dict[str, StateHub] = {n: StateHub(maxqueue=8) for n in names_to_run}

    # Item #55: optional queue manager. Hooks into per-printer state
    # callbacks so it can dispatch the next pending job when a printer
    # goes idle.
    # Item #56: optional timelapse recorder. Hooks per-printer state;
    # captures /snapshot.jpg every --timelapse-interval seconds during
    # active prints; saves under ~/.x2d/timelapses/<printer>/<job>/.
    timelapse_rec = None
    if getattr(args, "timelapse", False):
        from runtime.timelapse.recorder import TimelapseRecorder
        # Build a self-referential URL so the recorder pulls from
        # OUR /snapshot.jpg (which itself proxies the camera daemon).
        host_part, _, port_part = (args.http or "127.0.0.1:8765").rpartition(":")
        snap_host = host_part if host_part not in ("", "0.0.0.0") else "127.0.0.1"
        timelapse_rec = TimelapseRecorder(
            snapshot_url=f"http://{snap_host}:{port_part}/snapshot.jpg",
            interval_s=float(args.timelapse_interval))
        print(f"[x2d-bridge] timelapse recorder enabled "
              f"(every {args.timelapse_interval}s during prints)",
              file=sys.stderr)

    queue_mgr = None
    if getattr(args, "queue", False):
        from runtime.queue.manager import QueueManager
        from threading import Lock as _DispatchLock
        _dispatch_lock = _DispatchLock()

        def _dispatch_job(job) -> bool:
            """Upload the job's .gcode.3mf to the printer + start_print.
            Runs synchronously while the queue manager waits."""
            cli = clients.get(job.printer)
            if cli is None:
                LOG_QUEUE.warning("queue dispatch: no client for printer %r",
                                   job.printer)
                return False
            try:
                with _dispatch_lock:
                    creds = cli.creds
                    upload_file(creds, Path(job.gcode),
                                  remote_name=Path(job.gcode).name)
                    # Per code-review #2: queue dispatch must validate AMS
                    # state at dispatch time — a job enqueued an hour ago
                    # may target a slot whose spool was swapped out since.
                    # local_path also lets start_print() auto-derive
                    # bed_type/bed_temp so heat profile matches the slice.
                    queued_3mf = Path(job.gcode)
                    derived = _derive_print_params_from_3mf(queued_3mf,
                                                            filament_index=0)
                    live = cli.request_state(timeout=15.0)
                    _validate_ams_slot(live, int(job.slot), derived,
                                        force=False)
                    start_print(cli, queued_3mf.name,
                                use_ams=True, ams_slot=int(job.slot),
                                local_path=queued_3mf)
                LOG_QUEUE.info("queue dispatched %s → %s slot %d",
                                job.label or job.gcode, job.printer, job.slot)
                return True
            except Exception as e:
                LOG_QUEUE.exception("queue dispatch failed for %s: %s",
                                     job.label or job.gcode, e)
                return False

        queue_mgr = QueueManager(dispatch_cb=_dispatch_job)
        print(f"[x2d-bridge] queue enabled; persisted at "
              f"{queue_mgr._path}", file=sys.stderr)

    def make_on_state(name: str):
        def on_state(state: dict) -> None:
            states[name] = state
            # Fan out to every subscriber of the per-printer hub. Slow
            # consumers drop oldest entries; the MQTT thread never blocks.
            hub = hubs.get(name)
            if hub is not None:
                try:
                    hub.publish(state)
                except Exception as e:
                    print(f"[x2d-bridge] hub.publish({name}) failed: {e}",
                          file=sys.stderr)
            if queue_mgr is not None:
                try:
                    queue_mgr.on_state(name, state)
                except Exception as e:
                    print(f"[x2d-bridge] queue.on_state({name}) failed: {e}",
                          file=sys.stderr)
            if timelapse_rec is not None:
                try:
                    timelapse_rec.on_state(name, state)
                except Exception as e:
                    print(f"[x2d-bridge] timelapse.on_state({name}) failed: {e}",
                          file=sys.stderr)
            if not args.quiet:
                print(json.dumps({"ts": time.time(),
                                  "printer": name,
                                  "state": state}), flush=True)
        return on_state

    clients: dict[str, X2DClient] = {}
    failed: list[tuple[str, str]] = []
    for name in names_to_run:
        try:
            ns = argparse.Namespace(ip=None, code=None, serial=None,
                                    printer=(name or None))
            creds = Creds.resolve(ns)
            cli = X2DClient(creds, on_state=make_on_state(name))
            cli.connect()
            clients[name] = cli
            print(f"[x2d-bridge] {name or '<default>'}: connected to {creds.ip}",
                  file=sys.stderr)
        except SystemExit as e:
            failed.append((name, f"creds resolve failed (exit {e.code})"))
        except Exception as e:
            failed.append((name, str(e)))
            print(f"[x2d-bridge] {name or '<default>'}: connect failed: {e} "
                  f"— other printers continue", file=sys.stderr)
    if not clients:
        print(f"[x2d-bridge] no printers reachable: {failed}", file=sys.stderr)
        return 2

    def _safe_pushall(name: str, cli: X2DClient) -> None:
        try:
            cli.publish({"pushing": {"sequence_id": _next_seq(),
                                     "command": "pushall"}})
        except Exception as e:
            print(f"[x2d-bridge] {name or '<default>'}: pushall failed: {e}",
                  file=sys.stderr)

    for name, cli in clients.items():
        _safe_pushall(name, cli)

    if args.http:
        def get_state(printer: str):
            return states.get(printer)

        def get_last_ts(printer: str):
            cli = clients.get(printer)
            return cli.last_message_ts if cli else 0.0

        def get_hub(printer: str):
            return hubs.get(printer)

        Thread(target=_serve_http,
               kwargs={"bind": args.http, "get_state": get_state,
                       "get_last_ts": get_last_ts,
                       "max_staleness": float(args.max_staleness),
                       "auth_token": args.auth_token or None,
                       "printer_names": list(clients.keys()),
                       "clients": clients,
                       "web_dir": _WEB_DIR_DEFAULT,
                       "queue_mgr": queue_mgr,
                       "timelapse_rec": timelapse_rec,
                       "get_hub": get_hub},
               daemon=True).start()

    period = max(1, int(args.interval))
    print(f"[x2d-bridge] daemon up; {len(clients)} printer(s); polling every "
          f"{period}s. Ctrl-C / SIGTERM to quit.", file=sys.stderr)

    import signal as _signal
    stop = Event()

    def _handle_sig(signum, frame):  # noqa: ARG001
        stop.set()

    _signal.signal(_signal.SIGINT, _handle_sig)
    _signal.signal(_signal.SIGTERM, _handle_sig)

    while not stop.is_set():
        if stop.wait(period):
            break
        for name, cli in clients.items():
            _safe_pushall(name, cli)
    for cli in clients.values():
        try:
            cli.disconnect()
        except Exception:
            pass
    return 0


def cmd_webrtc(args: argparse.Namespace) -> int:
    """Run the WebRTC video gateway (item #45). Pulls JPEG frames from
    a running camera daemon and re-publishes them as a live VP8/H.264
    track over WebRTC. Sub-second latency vs HLS's ~6-8 s.

    The signaling endpoint is POST /cam.webrtc/offer; the static viewer
    page is GET /cam.webrtc.html.
    """
    try:
        from runtime.webrtc.server import run as _run_webrtc
    except ImportError as e:
        print(f"[x2d-bridge] webrtc deps missing: {e}\n"
              f"  Install: python3.12 -m pip install --no-build-isolation "
              f"aiortc 'av==13.1.0' aiohttp\n"
              f"  See docs/MCP.md §2 for Termux-specific libsrtp build steps.",
              file=sys.stderr)
        return 2
    host_part, _, port_part = args.bind.rpartition(":")
    host = host_part or "127.0.0.1"
    port = int(port_part)
    stun = [s.strip() for s in args.stun.split(",") if s.strip()] \
        if args.stun else None
    return _run_webrtc(host=host, port=port,
                       camera_url=args.camera_url,
                       frame_hz=float(args.frame_hz),
                       stun_servers=stun)


def cmd_ha_publish(args: argparse.Namespace) -> int:
    """Bridge a running x2d_bridge.py daemon's state to a Home Assistant
    MQTT broker via the HA discovery protocol (item #50). Without
    `--printer`, spawns one HAPublisher per `[printer:NAME]` section
    in ~/.x2d/credentials so HA gets a separate Device per printer
    (item #54). Connection failures are isolated — if one printer's
    publisher errors out, the others stay up."""
    try:
        from runtime.ha.publisher import HAPublisher
    except ImportError as e:
        print(f"[x2d-bridge] HA publisher import failed: {e}\n"
              "  Required: paho-mqtt (already a bridge dep).",
              file=sys.stderr)
        return 2

    # Build the work list: one entry per printer.
    if args.printer:
        targets = [(args.printer, args.device_serial)]
    else:
        names = Creds.list_names() or [""]
        targets = []
        for name in names:
            serial = ""
            try:
                ns = argparse.Namespace(ip=None, code=None, serial=None,
                                         printer=(name or None))
                creds = Creds.resolve(ns)
                serial = creds.serial
            except SystemExit:
                pass
            targets.append((name, serial))
    if args.device_serial and len(targets) == 1:
        targets = [(targets[0][0], args.device_serial)]

    host_part, _, port_part = args.broker.rpartition(":")
    host = host_part or args.broker
    port = int(port_part) if port_part.isdigit() else 1883

    logging.basicConfig(
        level=os.environ.get("X2D_HA_LOG", "INFO"),
        format="[%(asctime)s] %(name)s %(levelname)s %(message)s")

    publishers: list = []
    failed: list[tuple[str, str]] = []
    for name, serial in targets:
        try:
            pub = HAPublisher(
                broker_host=host, broker_port=port,
                broker_username=args.broker_username or None,
                broker_password=args.broker_password or None,
                daemon_url=args.daemon_url,
                daemon_token=args.daemon_token or None,
                discovery_prefix=args.discovery_prefix,
                printer_name=name or "",
                device_serial=serial or name or "default",
                device_model=args.device_model)
            pub.start()
            publishers.append(pub)
            print(f"[x2d-ha] {name or '<default>'}: started "
                  f"device_id={pub.device_id} base_topic={pub.base_topic}",
                  file=sys.stderr, flush=True)
        except Exception as e:
            failed.append((name, str(e)))
            print(f"[x2d-ha] {name or '<default>'}: start failed: {e} "
                  "— other printers continue", file=sys.stderr)

    if not publishers:
        print(f"[x2d-ha] no publishers started: {failed}", file=sys.stderr)
        return 2

    # Run until interrupted.
    import signal as _signal
    stop = Event()
    def _handle(_n, _f): stop.set()
    _signal.signal(_signal.SIGINT, _handle)
    _signal.signal(_signal.SIGTERM, _handle)
    try:
        while not stop.is_set():
            stop.wait(1)
    finally:
        for p in publishers:
            try: p.stop()
            except Exception: pass
    return 0


import logging  # used by cmd_ha_publish above
LOG_QUEUE = logging.getLogger("x2d.queue")


# cmd_printers moved to beambam/cli/info.py (Phase 5c). Re-exported below.
from beambam.cli.info import cmd_printers  # noqa: E402, F401


def cmd_cloud_login(args: argparse.Namespace) -> int:
    import cloud_client
    if args.dry_run:
        # Probe-only mode: confirm the cloud endpoint is reachable
        # without sending credentials. Useful for CI and install-time
        # smoke tests against networks that may block Bambu's API.
        region = args.region or "us"
        result = cloud_client.CloudClient.dry_run_check(region=region)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    # Allow email/password from CLI, env, or interactive stdin (handy
    # when the user doesn't want creds in shell history).
    email = args.email or os.environ.get("BAMBU_EMAIL", "")
    password = args.password or os.environ.get("BAMBU_PASSWORD", "")
    if not email:
        try:
            email = input("Bambu account email: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return 1

    # --code-only ("device code" style) flow: skip the password entirely.
    # Bambu emails a 6-digit code that proves possession of the inbox.
    # Right path for fresh-OS uvx users who don't want to paste a Bambu
    # password into a terminal they may not fully trust.
    if getattr(args, "code_only", False):
        def _prompt_for_code(addr: str) -> str:
            if getattr(args, "email_code", None):
                return args.email_code.strip()
            print(f"\nA verification code was emailed to {addr}. Enter it:")
            return input("Email code: ").strip()
        cli = cloud_client.CloudClient.load_or_anonymous()
        try:
            cli.login_code_only(email, region=args.region,
                                 code_resolver=_prompt_for_code)
        except cloud_client.CloudError as e:
            print(f"cloud-login (code-only) failed: {e}", file=sys.stderr)
            return 1
        print(f"logged in (code-only) as user {cli.session.user_id} "
              f"(region={cli.session.region}); session saved.")
        return 0

    if not password:
        import getpass
        try:
            password = getpass.getpass("Password: ")
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return 1
    if not email or not password:
        print("email and password are required (or use --code-only)", file=sys.stderr)
        return 2

    def prompt_tfa(_email: str) -> str:
        if getattr(args, "tfa_code", None):
            return args.tfa_code.strip()
        print(f"\nThis account requires 2FA. Open your authenticator "
              f"app, then enter the 6-digit code:")
        return input("2FA code: ").strip()

    def prompt_email_code(_email: str) -> str:
        if getattr(args, "email_code", None):
            return args.email_code.strip()
        print(f"\nA verification code was emailed to {_email}. Enter it:")
        return input("Email code: ").strip()

    cli = cloud_client.CloudClient.load_or_anonymous()
    try:
        cli.login(email, password, region=args.region,
                  two_factor_resolver=prompt_tfa,
                  email_code_resolver=prompt_email_code)
    except cloud_client.CloudError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 1
    expires_in = int(max(0, cli.session.expires_at - time.time()))
    expires_str = time.strftime('%Y-%m-%d %H:%M:%S',
                                time.localtime(cli.session.expires_at))
    print(f"logged in as user_id={cli.session.user_id or '?'} "
          f"(region={cli.session.region}, "
          f"expires_at={expires_str}, "
          f"valid for {expires_in // 86400}d {(expires_in % 86400) // 3600}h)")
    print(f"session saved to {cloud_client.SESSION_PATH}")

    # Auto-bootstrap ~/.x2d/credentials with every bound printer's
    # LAN access code unless explicitly disabled. Mirrors what
    # BambuStudio does after first cloud-bind: pulls dev_id+ip from
    # the bound-devices REST endpoint and the LAN code via the
    # `system.get_access_code` cloud-MQTT roundtrip. End state:
    # subsequent `lan_print.py` / `x2d_bridge.py print` commands
    # work with no extra flags.
    if getattr(args, "no_bootstrap", False):
        return 0
    try:
        devices = cli.get_bound_devices() or []
    except Exception as e:
        print(f"[bootstrap] couldn't list bound printers: {e} "
              "— skipping credential auto-write", file=sys.stderr)
        return 0
    if not devices:
        print("[bootstrap] no printers bound to this account — nothing to write")
        return 0
    print(f"[bootstrap] found {len(devices)} printer(s) — pulling LAN access codes")
    bootstrap_count = 0
    for dev in devices:
        serial = dev.get("dev_id") or dev.get("device_id") or ""
        ip = dev.get("dev_ip") or dev.get("ip") or ""
        if not serial:
            continue
        # Reuse cmd_cloud_get_access_code's logic by faking an args namespace.
        class _GACArgs:
            pass
        gac_args = _GACArgs()
        gac_args.serial = serial
        gac_args.timeout = 10.0
        gac_args.persist = True
        gac_args.ip = ip
        gac_args.section = ""  # default to printer:<serial>
        try:
            rc = cmd_cloud_get_access_code(gac_args)
            if rc == 0:
                bootstrap_count += 1
        except Exception as e:
            print(f"[bootstrap] {serial}: {e}", file=sys.stderr)
    print(f"[bootstrap] wrote {bootstrap_count}/{len(devices)} printer "
          f"section(s) to {Path.home() / '.x2d' / 'credentials'}")
    return 0


def cmd_cloud_printers(args: argparse.Namespace) -> int:
    """List the printers bound to the logged-in Bambu account."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    try:
        devices = cli.get_bound_devices()
    except cloud_client.CloudError as e:
        print(f"cloud API call failed: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(devices, indent=2))
    else:
        if not devices:
            print("(no printers bound to this account)")
            return 0
        print(f"{len(devices)} printer(s) bound to user "
              f"{cli.session.user_id}:")
        for d in devices:
            online = "online " if d.get("online") else "offline"
            name = d.get("name") or d.get("dev_name") or "?"
            dev_id = d.get("dev_id") or d.get("device_id") or "?"
            model = d.get("dev_product_name") or d.get("dev_model_name") or "?"
            access_code = (d.get("dev_access_code") or "").strip()
            print(f"  [{online}] {name}  serial={dev_id}  "
                  f"model={model}  access_code={access_code or '(hidden)'}")
    return 0


def cmd_cloud_status(args: argparse.Namespace) -> int:
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in (no ~/.x2d/cloud_session.json)")
        return 0
    age_s = max(0, cli.session.expires_at - time.time())
    print(json.dumps({
        "logged_in":  True,
        "user_id":    cli.session.user_id,
        "region":     cli.session.region,
        "expired":    cli.session.expired,
        "expires_at": cli.session.expires_at,
        "expires_in_s": int(age_s),
    }, indent=2))
    return 0


# cmd_cloud_logout moved to beambam/cli/cloud.py (Phase 5b).
from beambam.cli.cloud import cmd_cloud_logout  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Cloud-mediated MQTT (item #67) — uses the logged-in JWT to talk to
# Bambu's cloud broker (us.mqtt.bambulab.com:8883). Sidesteps the
# LAN-direct `print.*` verify-failure (#65/#66/#68) entirely because
# the cloud broker accepts plain JWT-authed sessions; per-installation
# cert is never invoked.
# ---------------------------------------------------------------------------

def _cloud_mqtt_connect(serial: str, cli) -> "mqtt.Client":
    """Connect to Bambu's cloud broker using the logged-in JWT.
    Returns a paho.mqtt.client.Client connected + ready to subscribe/publish.
    Caller is responsible for client.loop_stop() + disconnect() on exit."""
    import cloud_client  # noqa: WPS433 — keep cloud_client a soft import
    user, pwd = cli.mqtt_credentials()
    host = cli.mqtt_broker()
    client_id = f"x2d-bridge-{os.getpid()}-{int(time.time())}"
    c = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
    )
    c.username_pw_set(user, pwd)
    # Standard TLS — Bambu's brokers serve Let's-Encrypt-rooted certs,
    # so the system trust store is sufficient. No per-installation cert.
    c.tls_set_context(ssl.create_default_context())
    c.connect(host, cloud_client.MQTT_PORT, keepalive=60)
    return c


def cmd_cloud_state(args: argparse.Namespace) -> int:
    """Subscribe to the printer's cloud report topic and dump the first
    (or all, with --follow) state messages received. Useful for remote
    monitoring even when the printer isn't on the same LAN."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    serial = args.serial or os.environ.get("X2D_SERIAL")
    if not serial:
        try:
            devices = cli.get_bound_devices()
        except Exception as e:
            print(f"can't list bound devices: {e}", file=sys.stderr)
            return 1
        if len(devices) == 1:
            serial = devices[0].get("dev_id") or devices[0].get("device_id")
        else:
            print("multiple printers bound — pick one with --serial. "
                  "list via `x2d_bridge.py cloud-printers`.",
                  file=sys.stderr)
            return 1
    if not serial:
        print("no printer serial available", file=sys.stderr)
        return 1

    topic_report  = f"device/{serial}/report"
    topic_request = f"device/{serial}/request"

    state_seen: dict = {}
    pushall_done = _threading.Event()

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc != 0:
            print(f"[cloud-state] MQTT connect failed rc={rc}", file=sys.stderr)
            return
        c.subscribe(topic_report, qos=0)
        # Trigger a pushall so the printer publishes its full state.
        c.publish(topic_request, json.dumps({
            "pushing": {"command": "pushall", "sequence_id": _next_seq(),
                        "version": 1, "push_target": 1}
        }))

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = {"_raw": msg.payload.decode("utf-8", errors="replace")}
        if args.follow:
            print(json.dumps({"topic": msg.topic, "payload": payload}, indent=2))
        else:
            state_seen.update(payload)
            if any(k in payload for k in ("print", "system", "info")):
                pushall_done.set()

    c = _cloud_mqtt_connect(serial, cli)
    c.on_connect = on_connect
    c.on_message = on_message
    c.loop_start()
    try:
        if args.follow:
            print(f"[cloud-state] following {topic_report} — Ctrl-C to stop",
                  file=sys.stderr)
            while True:
                time.sleep(1)
        else:
            if not pushall_done.wait(timeout=args.timeout):
                print(f"[cloud-state] timeout — no state in {args.timeout}s",
                      file=sys.stderr)
                return 1
            print(json.dumps(state_seen, indent=2))
    except KeyboardInterrupt:
        pass
    finally:
        c.loop_stop()
        c.disconnect()
    return 0


# ---------------------------------------------------------------------------
# Cloud HTTP helpers — same logic as the cloud-* CLI commands but returning
# (status_code, JSON-able dict) so the serve HTTP handler can wire them in.
# Each helper is independently importable / testable.
# ---------------------------------------------------------------------------

def _http_cloud_login(*, email: str, password: str,
                      region: str | None = None,
                      email_code: str | None = None,
                      tfa_code: str | None = None) -> tuple[int, dict]:
    """HTTP-driven cloud-login. Returns the same status fields as
    /cloud/status on success, or a structured error.

    Two-step flows (verifyCode / tfa) are NOT interactive over HTTP —
    the caller passes `email_code` / `tfa_code` in a follow-up POST
    after seeing the corresponding `requires_*` flag in the first
    response. The cloud_client.login() callback uses the supplied
    fixed value rather than prompting via stdin."""
    try:
        import cloud_client
    except ImportError as e:
        return 500, {"error": f"cloud_client unavailable: {e}"}
    if not email or not password:
        return 400, {"error": "expected {email: str, password: str, region?: str, "
                              "email_code?: str, tfa_code?: str}"}
    cli = cloud_client.CloudClient.load_or_anonymous()
    requires_email_code = False
    requires_tfa = False
    def _email_resolver(_email: str) -> str:
        nonlocal requires_email_code
        if email_code is None:
            requires_email_code = True
            raise cloud_client.CloudError("email-code required (re-POST with email_code)")
        return email_code
    def _tfa_resolver(_key: str) -> str:
        nonlocal requires_tfa
        if tfa_code is None:
            requires_tfa = True
            raise cloud_client.CloudError("tfa code required (re-POST with tfa_code)")
        return tfa_code
    try:
        cli.login(email, password, region=region,
                  email_code_resolver=_email_resolver,
                  two_factor_resolver=_tfa_resolver)
    except cloud_client.CloudError as e:
        if requires_email_code:
            return 200, {"requires_email_code": True,
                         "hint": "Bambu sent a verification code to "
                                 "your email; re-POST with email_code"}
        if requires_tfa:
            return 200, {"requires_tfa": True,
                         "hint": "TOTP required; re-POST with tfa_code"}
        return 401, {"error": f"login failed: {e}",
                     "status": e.status, "body": e.body}
    except Exception as e:
        return 500, {"error": f"login crashed: {e}"}
    return 200, {
        "logged_in":    True,
        "user_id":      cli.session.user_id,
        "region":       cli.session.region,
        "expires_at":   cli.session.expires_at,
        "expires_in_s": int(max(0, cli.session.expires_at - time.time())),
    }


def _http_cloud_logout() -> tuple[int, dict]:
    try:
        import cloud_client
    except ImportError as e:
        return 500, {"error": f"cloud_client unavailable: {e}"}
    cli = cloud_client.CloudClient.load_or_anonymous()
    cli.logout()
    return 200, {"logged_out": True}


def _http_cloud_status() -> dict:
    try:
        import cloud_client
    except ImportError as e:
        return {"error": f"cloud_client unavailable: {e}", "logged_in": False}
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        return {"logged_in": False}
    return {
        "logged_in":    True,
        "user_id":      cli.session.user_id,
        "region":       cli.session.region,
        "expired":      cli.session.expired,
        "expires_at":   cli.session.expires_at,
        "expires_in_s": int(max(0, cli.session.expires_at - time.time())),
    }


def _http_cloud_printers() -> tuple[int, dict]:
    try:
        import cloud_client
    except ImportError as e:
        return 500, {"error": f"cloud_client unavailable: {e}"}
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        return 401, {"error": "not logged in",
                     "hint": "POST /cloud/login or run cloud-login first"}
    try:
        return 200, {"printers": cli.get_bound_devices()}
    except cloud_client.CloudError as e:
        return 502, {"error": f"cloud API failed: {e}",
                     "status": e.status, "body": e.body}


def _http_cloud_state(serial: str | None, timeout: float = 15.0) -> tuple[int, dict]:
    try:
        import cloud_client
    except ImportError as e:
        return 500, {"error": f"cloud_client unavailable: {e}"}
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        return 401, {"error": "not logged in"}
    if not serial:
        try:
            devs = cli.get_bound_devices()
        except Exception as e:
            return 502, {"error": f"can't list bound devices: {e}"}
        if len(devs) == 1:
            serial = devs[0].get("dev_id") or devs[0].get("device_id")
    if not serial:
        return 400, {"error": "serial required (?serial=XXX) — multiple printers bound"}
    topic_report  = f"device/{serial}/report"
    topic_request = f"device/{serial}/request"
    state_seen: dict = {}
    pushall_done = _threading.Event()

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc != 0:
            return
        c.subscribe(topic_report, qos=0)
        c.publish(topic_request, json.dumps({
            "pushing": {"command": "pushall", "sequence_id": _next_seq(),
                        "version": 1, "push_target": 1}
        }))

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = {"_raw": msg.payload.decode("utf-8", errors="replace")}
        state_seen.update(payload)
        if any(k in payload for k in ("print", "system", "info")):
            pushall_done.set()

    c = _cloud_mqtt_connect(serial, cli)
    c.on_connect = on_connect
    c.on_message = on_message
    c.loop_start()
    try:
        if not pushall_done.wait(timeout=timeout):
            return 504, {"error": f"timeout after {timeout}s",
                         "partial": state_seen, "serial": serial}
        return 200, {"serial": serial, "state": state_seen}
    finally:
        c.loop_stop()
        c.disconnect()


def _http_cloud_publish(serial: str, payload: dict,
                        timeout: float = 10.0) -> tuple[int, dict]:
    try:
        import cloud_client
    except ImportError as e:
        return 500, {"error": f"cloud_client unavailable: {e}"}
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        return 401, {"error": "not logged in"}
    topic_request = f"device/{serial}/request"
    c = _cloud_mqtt_connect(serial, cli)
    published = _threading.Event()
    def on_publish(client, userdata, mid, reason_code=None, properties=None):
        published.set()
    c.on_publish = on_publish
    c.loop_start()
    try:
        info = c.publish(topic_request, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=timeout)
        if not published.wait(timeout=timeout):
            return 504, {"error": f"no broker ack in {timeout}s"}
        return 200, {"published": True, "topic": topic_request, "payload": payload}
    finally:
        c.loop_stop()
        c.disconnect()


def _cloud_publish_payload(serial: str, payload: dict, timeout: float = 10.0) -> int:
    """Internal helper used by every cloud-side print-control CLI.
    Connects to Bambu's cloud broker, publishes one message, exits.
    Returns 0 on broker ack, 1 on error."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    topic_request = f"device/{serial}/request"
    c = _cloud_mqtt_connect(serial, cli)
    published = _threading.Event()
    def on_publish(client, userdata, mid, reason_code=None, properties=None):
        published.set()
    c.on_publish = on_publish
    c.loop_start()
    try:
        info = c.publish(topic_request, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=timeout)
        if not published.wait(timeout=timeout):
            print(f"[cloud] no broker ack in {timeout}s", file=sys.stderr)
            return 1
        print(json.dumps({"published": True, "topic": topic_request,
                          "payload": payload}, indent=2))
    finally:
        c.loop_stop()
        c.disconnect()
    return 0


def _resolve_cloud_serial(args: argparse.Namespace) -> str | None:
    """Mirror of cmd_cloud_state's auto-discovery: --serial wins, else
    X2D_SERIAL env, else if exactly one printer is bound to the
    account use that; else None."""
    serial = getattr(args, "serial", None) or os.environ.get("X2D_SERIAL")
    if serial:
        return serial
    try:
        import cloud_client
        cli = cloud_client.CloudClient.load_or_anonymous()
        if cli.session.empty:
            return None
        devs = cli.get_bound_devices()
        if len(devs) == 1:
            return devs[0].get("dev_id") or devs[0].get("device_id")
    except Exception:
        pass
    return None


# cmd_cloud_pause / resume / stop / gcode / chamber_light moved to
# beambam/cli/cloud.py (Phase 5b). They thunk through _cloud_publish
# which lazily calls back into _cloud_publish_payload here.
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_pause,
    cmd_cloud_resume,
    cmd_cloud_stop,
    cmd_cloud_gcode,
    cmd_cloud_chamber_light,
)


# cmd_cloud_history / cmd_cloud_task / cmd_cloud_messages /
# cmd_cloud_tickets moved to beambam/cli/cloud.py (Phase 5b).
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_history,
    cmd_cloud_task,
    cmd_cloud_messages,
    cmd_cloud_tickets,
)


# cmd_cloud_feed moved to beambam/cli/cloud.py (Phase 5b).
from beambam.cli.cloud import cmd_cloud_feed  # noqa: E402, F401


# cmd_cloud_firmware / cmd_cloud_filaments moved to
# beambam/cli/cloud.py (Phase 5b).
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_firmware,
    cmd_cloud_filaments,
)


def _spool_body_from_args(args: argparse.Namespace) -> dict:
    """Distil --vendor/--type/--name/--id/--color/--weight into the
    spool-record dict Bambu's API expects. Skips None fields so the
    server only sees what the user explicitly passed (matters for
    UPDATE where you might want to change just one attribute)."""
    raw = {
        "filamentVendor": args.vendor,
        "filamentType":   args.type,
        "filamentName":   args.name,
        "filamentId":     args.filament_id,
        "color":          args.color,
        "weight":         args.weight,
        "createType":     "manual",  # WRITE-side defaults to manual entries
    }
    return {k: v for k, v in raw.items() if v is not None}


def _require_allow_write(args: argparse.Namespace, what: str) -> int | None:
    """Guard that flips write-side cloud ops behind `--allow-write`. Returns
    an exit code to bubble up, or None if the caller should proceed."""
    if not getattr(args, "allow_write", False):
        print(
            f"refusing to {what} without --allow-write\n"
            f"This mutates account-side state on Bambu Cloud — re-run "
            f"with --allow-write to confirm.",
            file=sys.stderr)
        return 1
    return None


def cmd_cloud_spool_add(args: argparse.Namespace) -> int:
    """`cloud-spool add` — POST a new spool entry."""
    import cloud_client
    rc = _require_allow_write(args, "add a spool")
    if rc is not None:
        return rc
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    body = _spool_body_from_args(args)
    try:
        r = cli.add_spool(body)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    print(f"added spool: {body}")
    return 0


def cmd_cloud_spool_update(args: argparse.Namespace) -> int:
    """`cloud-spool update <filamentId>` — PUT a partial update."""
    import cloud_client
    rc = _require_allow_write(args, f"update spool {args.filament_id}")
    if rc is not None:
        return rc
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    body = _spool_body_from_args(args)
    body.pop("filamentId", None)  # path-segment, not body field for PUT
    body.pop("createType", None)  # leave the existing createType alone
    if not body:
        print("nothing to update — pass at least one of "
              "--vendor / --type / --name / --color / --weight",
              file=sys.stderr); return 2
    try:
        r = cli.update_spool(args.filament_id, body)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str)); return 0
    print(f"updated spool {args.filament_id}: {body}")
    return 0


def cmd_cloud_spool_delete(args: argparse.Namespace) -> int:
    """`cloud-spool delete <filamentId>` — DELETE one entry."""
    import cloud_client
    rc = _require_allow_write(args, f"delete spool {args.filament_id}")
    if rc is not None:
        return rc
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    try:
        cli.delete_spool(args.filament_id)
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    print(f"deleted spool {args.filament_id}")
    return 0


# cmd_cloud_ttcode moved to beambam/cli/cloud.py (start of Phase 5b);
# re-exported so `x2d_bridge.cmd_cloud_ttcode` callers (tests etc.)
# keep working without changes.
from beambam.cli.cloud import cmd_cloud_ttcode  # noqa: E402, F401


# cmd_cloud_search_suggest moved to beambam/cli/cloud.py (Phase 5b).
from beambam.cli.cloud import cmd_cloud_search_suggest  # noqa: E402, F401


# cmd_cloud_search / browse / design / design_remixes / favorites /
# liked / presets moved to beambam/cli/cloud.py (Phase 5b). _format_
# design_hits is now there too; the bridge no longer needs it.
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_search,
    cmd_cloud_browse,
    cmd_cloud_design,
    cmd_cloud_design_remixes,
    cmd_cloud_favorites,
    cmd_cloud_liked,
    cmd_cloud_presets,
)


def cmd_printables_search(args: argparse.Namespace) -> int:
    """Printables (printables.com) full-text search via the public GraphQL.

    Anonymous — no API key needed. Uses `searchPrints2(query, limit)` →
    items with id / name / slug / likesCount / downloadCount / user.
    Each hit's design page is `https://www.printables.com/model/<id>-<slug>`,
    which `beambam fetch <url>` already handles for download."""
    import urllib.request as _ur
    import urllib.error as _ue
    body = json.dumps({
        "query": "query($q:String!,$l:Int,$o:Int){searchPrints2(query:$q,limit:$l,offset:$o)"
                 "{items{id name slug likesCount downloadCount user{publicUsername}}}}",
        "variables": {"q": args.query, "l": int(args.limit), "o": int(args.offset)},
    }).encode("utf-8")
    req = _ur.Request("https://api.printables.com/graphql/", data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "beambam-cli/1.x",
    }, method="POST")
    try:
        with _ur.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
    except _ue.HTTPError as e:
        print(f"Printables API failed HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}",
              file=sys.stderr); return 1
    except Exception as e:                                  # noqa: BLE001
        print(f"Printables API failed: {e}", file=sys.stderr); return 1
    items = (resp.get("data") or {}).get("searchPrints2", {}).get("items") or []
    if args.json:
        print(json.dumps({"hits": items}, indent=2, default=str)); return 0
    print(f"{len(items)} match(es) for {args.query!r} on Printables:\n")
    for it in items:
        u = (it.get("user") or {}).get("publicUsername", "?")
        slug = it.get("slug", "")
        print(f"  https://www.printables.com/model/{it['id']}-{slug}")
        print(f"     likes={it.get('likesCount',0):<5} dls={it.get('downloadCount',0):<6}  "
              f"by {u[:20]:<20}  {it.get('name','')[:50]}")
    return 0


def _print_search_printables(args: argparse.Namespace) -> int:
    """Printables backend for `print-search --source printables`.

    Queries Printables' anonymous GraphQL, shows the same numbered
    picker as the MakerWorld flow, then prints the canonical model URL
    for the picked entry. Pipeline does NOT chain into a print —
    Printables files are .stl-in-.zip; the user follows up with
    `beambam fetch <url>` then `beambam slice/print` manually.
    """
    import urllib.request as _ur
    import urllib.error as _ue
    body = json.dumps({
        "query": "query($q:String!,$l:Int,$o:Int){searchPrints2(query:$q,limit:$l,offset:$o)"
                 "{items{id name slug likesCount downloadCount user{publicUsername}}}}",
        "variables": {"q": args.query, "l": int(args.limit),
                       "o": int(getattr(args, "offset", 0) or 0)},
    }).encode("utf-8")
    req = _ur.Request("https://api.printables.com/graphql/", data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "beambam-cli/1.x",
    }, method="POST")
    try:
        with _ur.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
    except _ue.HTTPError as e:
        print(f"Printables API failed HTTP {e.code}: "
              f"{e.read().decode('utf-8', 'replace')[:200]}",
              file=sys.stderr); return 1
    except Exception as e:                                  # noqa: BLE001
        print(f"Printables API failed: {e}", file=sys.stderr); return 1
    items = (resp.get("data") or {}).get("searchPrints2", {}).get("items") or []
    if not items:
        print(f"no Printables results for {args.query!r}"); return 1
    print(f"\n{len(items)} match(es) for {args.query!r} on Printables:\n")
    for i, it in enumerate(items, 1):
        slug = it.get("slug", "")
        likes = it.get("likesCount", 0)
        dls = it.get("downloadCount", 0)
        u = (it.get("user") or {}).get("publicUsername", "?")
        title = it.get("name", "")
        print(f"  {i:>2}. id={it['id']:<8}  likes={likes:<5} dls={dls:<6}  "
              f"by {u[:18]:<18}  {title[:40]}")
    if args.pick:
        idx = int(args.pick) - 1
    else:
        try:
            line = input(f"\nPick a number 1..{len(items)} (blank to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted"); return 1
        if not line:
            print("cancelled"); return 0
        try:
            idx = int(line) - 1
        except ValueError:
            print(f"invalid pick: {line!r}"); return 2
    if idx < 0 or idx >= len(items):
        print(f"out of range: {idx+1}"); return 2
    chosen = items[idx]
    url = f"https://www.printables.com/model/{chosen['id']}-{chosen.get('slug', '')}"
    print(f"\n→ Picked: {chosen.get('name', '')}\n  {url}")
    if args.dry_run_pick:
        return 0
    print("\nNext steps (Printables files are .stl-in-.zip):")
    print(f"  beambam fetch {url}")
    print(f"  beambam slice <stl> --out model.gcode.3mf")
    print(f"  beambam print model.gcode.3mf")
    return 0


def cmd_print_search(args: argparse.Namespace) -> int:
    """Interactive: MakerWorld OR Printables search → user picks → action.

    `beambam print-search "pokeball" --copies 4 --scale-pct 75 --color Gold`
    `beambam print-search "pokeball" --source printables`

    Steps:
      1. search the chosen catalogue
      2. show numbered table of top N hits
      3. prompt user for selection (1..N, blank = cancel)
      4. MakerWorld: chain into cloud-print-design (slice+upload+print).
         Printables: print the canonical model URL — user follows up with
         `beambam fetch <url>` then `beambam slice/print` manually
         (Printables files are .stl-in-.zip; the file pipeline differs
         from MW's pre-sliced .3mf).
    """
    if getattr(args, "source", "makerworld") == "printables":
        return _print_search_printables(args)
    import cloud_client, subprocess
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `cloud-login` first", file=sys.stderr); return 1
    try:
        r = cli.search_designs(args.query, limit=int(args.limit))
    except cloud_client.CloudError as e:
        print(f"search failed: {e}", file=sys.stderr); return 1
    hits = r.get("hits") or []
    if not hits:
        print(f"no results for {args.query!r}"); return 1
    print(f"\n{r.get('total', 0)} match(es) for {args.query!r}, top {len(hits)}:\n")
    for i, h in enumerate(hits, 1):
        d = h.get("design") or h
        did = d.get("id") or d.get("designId")
        title = str(d.get("title") or "")
        creator = (d.get("designCreator") or {}).get("name", "")
        likes = d.get("likeCount") or 0
        downloads = d.get("downloadCount") or 0
        print(f"  {i:>2}. id={did:<8}  likes={likes:<6} dls={downloads:<6}  by {creator[:18]:<18}  {title[:50]}")

    if args.pick:
        idx = int(args.pick) - 1
    else:
        try:
            line = input(f"\nPick a number 1..{len(hits)} (blank to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted"); return 1
        if not line:
            print("cancelled"); return 0
        try:
            idx = int(line) - 1
        except ValueError:
            print(f"invalid pick: {line!r}"); return 2
    if idx < 0 or idx >= len(hits):
        print(f"out of range: {idx+1}"); return 2
    chosen = hits[idx].get("design") or hits[idx]
    design_id = chosen.get("id") or chosen.get("designId")
    title = chosen.get("title", "")
    print(f"\n→ Picked design {design_id}: {title}")
    if args.dry_run_pick:
        print("(--dry-run-pick: stopped before download/slice/print)")
        return 0

    # Chain into cloud-print-design
    bridge = X2D_ROOT_PATH / "x2d_bridge.py"
    cmd = [sys.executable, str(bridge), "cloud-print-design", str(design_id)]
    if args.printer: cmd.extend(["--printer", args.printer])
    if args.ip:      cmd.extend(["--ip", args.ip])
    if args.code:    cmd.extend(["--code", args.code])
    if args.serial:  cmd.extend(["--serial", args.serial])
    if args.scale != 1.0: cmd.extend(["--scale", str(args.scale)])
    if args.scale_pct is not None: cmd.extend(["--scale-pct", str(args.scale_pct)])
    if args.mm is not None: cmd.extend(["--mm", str(args.mm)])
    if int(args.copies) != 1: cmd.extend(["--copies", str(args.copies)])
    if args.color:   cmd.extend(["--color", args.color])
    if args.slot:    cmd.extend(["--slot", str(args.slot)])
    if args.no_ams:  cmd.append("--no-ams")
    if args.dry_run: cmd.append("--dry-run")
    return subprocess.call(cmd)


# cmd_cloud_like / cmd_cloud_comments / cmd_cloud_comment_reply moved to
# beambam/cli/cloud.py (Phase 5b).
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_like,
    cmd_cloud_comments,
    cmd_cloud_comment_reply,
)


def cmd_cloud_pull_design(args: argparse.Namespace) -> int:
    """Download a MakerWorld design's .3mf bundle to a local directory.

    Resolves the signed bblmw.com URL via the design-service/instance
    f3mf endpoint, then `urlretrieve`s into `--out-dir`. The default
    instance is picked (whichever has `isDefault: true`) unless
    `--instance-id` is given explicitly.

    Companion `cloud-print-design` chains this into slice-print."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1
    out_dir = Path(args.out_dir).expanduser()
    try:
        out = cli.pull_design_3mf(args.design_id, out_dir,
                                  instance_index=int(args.instance_index or 0))
    except cloud_client.CloudError as e:
        print(f"cloud API failed: {e}", file=sys.stderr); return 1
    print(f"[cloud-pull] saved {out}  ({out.stat().st_size:,} B)")
    if args.json:
        print(json.dumps({"path": str(out), "size": out.stat().st_size}, default=str))
    return 0


def cmd_cloud_print_design(args: argparse.Namespace) -> int:
    """End-to-end: download a MakerWorld design + slice + upload + print.

    `beambam cloud-print-design 1623016 [--copies 4 --scale-pct 75 --color Gold]`

    Steps:
      1. Resolve the design's default instance + download the .3mf.
      2. (Re-)slice via x2d_slice with the user's --copies / --scale / --color.
      3. Upload + start print on the configured printer.

    This is the FRE win — search the catalogue, pick a design, hit print."""
    import cloud_client, tempfile, subprocess
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in", file=sys.stderr); return 1

    with tempfile.TemporaryDirectory(prefix="cloud_print_design_") as td:
        td_p = Path(td)
        try:
            three_mf = cli.pull_design_3mf(args.design_id, td_p,
                                            instance_index=int(args.instance_index or 0))
        except cloud_client.CloudError as e:
            print(f"cloud API failed: {e}", file=sys.stderr); return 1
        print(f"[cloud-print-design] downloaded {three_mf.name}  "
              f"({three_mf.stat().st_size:,} B)", file=sys.stderr)

        # Build the slice-print invocation. We use the same x2d_bridge but
        # via subprocess so all the existing arg validation + helpers fire.
        bridge = X2D_ROOT_PATH / "x2d_bridge.py"
        cmd = [sys.executable, str(bridge), "slice-print", str(three_mf)]
        if args.printer: cmd.extend(["--printer", args.printer])
        if args.ip:      cmd.extend(["--ip", args.ip])
        if args.code:    cmd.extend(["--code", args.code])
        if args.serial:  cmd.extend(["--serial", args.serial])
        if args.scale != 1.0: cmd.extend(["--scale", str(args.scale)])
        if args.scale_pct is not None: cmd.extend(["--scale-pct", str(args.scale_pct)])
        if args.mm is not None: cmd.extend(["--mm", str(args.mm)])
        if int(args.copies) != 1: cmd.extend(["--copies", str(args.copies)])
        if args.color:   cmd.extend(["--color", args.color])
        if args.slot:    cmd.extend(["--slot", str(args.slot)])
        if args.no_ams:  cmd.append("--no-ams")
        if args.dry_run: cmd.append("--dry-run")
        return subprocess.call(cmd)


def cmd_fcm_harvest(args: argparse.Namespace) -> int:
    """Pull finish-snapshot JPGs from a rooted Bambu Handy install.

    Bambu pushes a Firebase Cloud Messaging notification on every print
    completion, and Handy stashes them in
    `/data/data/bbl.intl.bambulab.com/shared_prefs/io.flutter.plugins.firebase.messaging.xml`.
    Each notification body contains a pre-signed AWS S3 URL to the
    finish-snapshot JPG — 1-hour signed-URL TTL. This subcommand pulls
    the XML over ADB, finds new entries, fetches their JPGs (while still
    valid), and stores them in ~/.x2d/snapshots/<print_id>.{jpg,json}.

    Modes:
      --once          (default) single sweep
      --daemon        loop forever
      --interval N    daemon poll period seconds (default 60)
      --backfill      attempt every URL even if it appears expired

    Requires a rooted device with `su` available. See
    runtime/handy_extract/HANDY_DATA_AUDIT.md for the full data-flow."""
    import subprocess
    # Just shell out to the standalone harvester so we keep the code in one
    # place and don't duplicate the harvester logic into x2d_bridge.
    script = X2D_ROOT_PATH / "runtime" / "handy_extract" / "fcm_snapshot_harvest.py"
    if not script.exists():
        sys.exit(f"harvester missing at {script}")
    cmd = ["python3", str(script), "--device", args.device]
    if args.daemon:
        cmd.append("--daemon")
        cmd.extend(["--interval", str(args.interval)])
    else:
        cmd.append("--once")
    if args.backfill:
        cmd.append("--backfill")
    if args.verbose:
        cmd.append("--verbose")
    return subprocess.call(cmd)


# cmd_cloud_app_config moved to beambam/cli/cloud.py (Phase 5b).
from beambam.cli.cloud import cmd_cloud_app_config  # noqa: E402, F401


def cmd_cloud_get_access_code(args: argparse.Namespace) -> int:
    """Fetch a printer's LAN access code over cloud MQTT (no LAN needed).

    Mirrors what BambuStudio does on first cloud-bind (see
    `MachineObject::command_get_access_code` in DeviceManager.cpp:1219):
    publish `system.get_access_code` to the printer's cloud request topic
    and wait for the report that comes back with `system.access_code`
    set. Lets a fresh `cloud-login` finish setting up `~/.x2d/credentials`
    automatically — no need to copy the code off the printer's screen.

    Use --persist to also write the discovered code (and IP if --ip given,
    or whatever was already in the section) into ~/.x2d/credentials so
    subsequent LAN-direct commands work without flags. The serial is the
    section key; missing sections get created as `[printer:<serial>]`.
    """
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    serial = _resolve_cloud_serial(args)
    if not serial:
        print("--serial required (or bind exactly one printer to the account)",
              file=sys.stderr)
        return 1

    topic_report  = f"device/{serial}/report"
    topic_request = f"device/{serial}/request"
    seq = _next_seq()
    payload = {"system": {"sequence_id": seq, "command": "get_access_code"}}

    got_code: dict[str, str | None] = {"value": None}
    done = _threading.Event()

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc != 0:
            print(f"[cloud-get-access-code] MQTT connect failed rc={rc}",
                  file=sys.stderr)
            return
        c.subscribe(topic_report, qos=0)
        c.publish(topic_request, json.dumps(payload, separators=(",", ":")))

    def on_message(c, userdata, msg):
        try:
            j = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        sysblock = (j or {}).get("system") or {}
        # Reply payload from the firmware: system.command == "get_access_code"
        # and system.access_code populated. Some firmwares also re-publish
        # the field under top-level `info` — accept both.
        code = (sysblock.get("access_code")
                or (j.get("info") or {}).get("access_code"))
        if code:
            got_code["value"] = code
            done.set()

    c = _cloud_mqtt_connect(serial, cli)
    c.on_connect = on_connect
    c.on_message = on_message
    c.loop_start()
    try:
        if not done.wait(timeout=args.timeout):
            print(f"[cloud-get-access-code] timeout waiting {args.timeout}s "
                  f"for response (printer offline?)", file=sys.stderr)
            return 1
    finally:
        c.loop_stop()
        c.disconnect()

    code = got_code["value"]
    print(code)

    if args.persist:
        ini_path = Path.home() / ".x2d" / "credentials"
        ini_path.parent.mkdir(parents=True, exist_ok=True)
        cp = configparser.ConfigParser()
        if ini_path.exists():
            cp.read(ini_path)
        # Section name precedence: --section > printer:<serial> > printer.
        target = args.section or f"printer:{serial}"
        if not cp.has_section(target):
            cp.add_section(target)
        cp.set(target, "code", code)
        cp.set(target, "serial", serial)
        if args.ip:
            cp.set(target, "ip", args.ip)
        elif not cp.has_option(target, "ip"):
            print(f"[cloud-get-access-code] no --ip given and {target} has no ip "
                  f"set — re-run with --ip <printer-ip> to make this section "
                  f"usable for LAN commands.", file=sys.stderr)
        with ini_path.open("w") as f:
            cp.write(f)
        print(f"[cloud-get-access-code] wrote {target} -> {ini_path}",
              file=sys.stderr)
    return 0


def cmd_cloud_print(args: argparse.Namespace) -> int:
    """Submit a complete cloud-mediated print job:
       1. Upload the .gcode.3mf to Bambu's OSS via the upload-token API.
       2. Publish print.project_file with print_type=cloud + the OSS URL
          to the printer's cloud request topic.
       3. Bambu cloud relays to the bound printer; printer pulls from OSS.
    Sidesteps the LAN-direct verify-failure (#65/#66) entirely."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1

    serial = args.serial or os.environ.get("X2D_SERIAL")
    if not serial:
        try:
            devs = cli.get_bound_devices()
            if len(devs) == 1:
                serial = devs[0].get("dev_id") or devs[0].get("device_id")
        except Exception:
            pass
    if not serial:
        print("--serial required (or set X2D_SERIAL, or have a single bound printer)",
              file=sys.stderr)
        return 1

    src = Path(args.file)
    if not src.is_file():
        print(f"file not found: {src}", file=sys.stderr)
        return 1

    # 1. Upload to OSS
    try:
        print(f"[cloud-print] uploading {src.name} ({src.stat().st_size} B) "
              f"to Bambu OSS…", file=sys.stderr)
        upload = cli.cloud_upload_file(src)
        print(f"[cloud-print] uploaded → {upload['url']} (md5={upload['md5']})",
              file=sys.stderr)
    except cloud_client.CloudError as e:
        print(f"[cloud-print] upload failed: {e}", file=sys.stderr)
        return 1

    # 2. Compose the print.project_file payload (cloud variant)
    job_id_int = int(time.time()) * 10
    job_id_str = str(job_id_int)
    name = upload["remote_name"]
    name_no_3mf = name
    if name_no_3mf.endswith(".gcode.3mf"):
        name_no_3mf = name_no_3mf[: -len(".3mf")]
    elif name_no_3mf.endswith(".3mf"):
        name_no_3mf = name_no_3mf[: -len(".3mf")] + ".gcode"
    use_ams = not args.no_ams
    ams_slot = int(args.slot)
    payload = {
        "print": {
            "sequence_id":              str(int(time.time())),
            "command":                  "project_file",
            "param":                    "Metadata/plate_1.gcode",
            "file":                     name,
            "url":                      upload["url"],
            "md5":                      upload["md5"],
            "task_id":                  job_id_str,
            "subtask_id":               job_id_str,
            "subtask_name":             name_no_3mf,
            "job_id":                   job_id_int,
            "project_id":               job_id_str,
            "profile_id":               "0",
            "design_id":                "0",
            "model_id":                 "0",
            "plate_idx":                int(args.plate),
            "dev_id":                   serial,
            "job_type":                 1,                 # 1 = CLOUD (vs 0 LAN)
            "timestamp":                int(time.time()),
            "bed_type":                 args.bed_type,
            "bed_temp":                 int(args.bed_temp),
            "auto_bed_leveling":        1 if not args.no_level else 0,
            "extrude_cali_flag":        1 if args.flow_cali else 0,
            "nozzle_offset_cali":       0,
            "extrude_cali_manual_mode": 0,
            "flow_cali":                bool(args.flow_cali),
            "bed_leveling":             not args.no_level,
            "vibration_cali":           bool(args.vibration_cali),
            "timelapse":                bool(args.timelapse),
            "layer_inspect":            False,
            "use_ams":                  use_ams,
            "ams_mapping":              [ams_slot] if use_ams else [],
            "ams_mapping2":             [{"ams_id": ams_slot // 4,
                                          "slot_id": ams_slot %  4}] if use_ams else [],
            "skip_objects":             None,
            "cfg":                      "0",
        }
    }
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    # 3. Publish via cloud broker
    topic_request = f"device/{serial}/request"
    c = _cloud_mqtt_connect(serial, cli)
    published = _threading.Event()
    def on_publish(client, userdata, mid, reason_code=None, properties=None):
        published.set()
    c.on_publish = on_publish
    c.loop_start()
    try:
        info = c.publish(topic_request, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=args.timeout)
        if not published.wait(timeout=args.timeout):
            print(f"[cloud-print] no broker ack in {args.timeout}s",
                  file=sys.stderr)
            return 1
        print(json.dumps({"published": True, "topic": topic_request,
                          "url": upload["url"], "md5": upload["md5"],
                          "subtask_name": name_no_3mf}, indent=2))
    finally:
        c.loop_stop()
        c.disconnect()
    return 0


def cmd_cloud_publish(args: argparse.Namespace) -> int:
    """Publish a raw JSON payload to a printer via Bambu's cloud broker.
    Useful for one-shot commands when not on the printer's LAN. Schema
    matches the LAN-direct topic — `pause`, `resume`, `stop`, `gcode_line`,
    `ledctrl` all work the same way the LAN versions do."""
    import cloud_client
    cli = cloud_client.CloudClient.load_or_anonymous()
    if cli.session.empty:
        print("not logged in — run `x2d_bridge.py cloud-login` first",
              file=sys.stderr)
        return 1
    serial = args.serial or os.environ.get("X2D_SERIAL")
    if not serial:
        print("--serial required (or set X2D_SERIAL)", file=sys.stderr)
        return 1
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"--payload is not valid JSON: {e}", file=sys.stderr)
        return 1

    topic_request = f"device/{serial}/request"
    c = _cloud_mqtt_connect(serial, cli)
    published = _threading.Event()

    def on_publish(client, userdata, mid, reason_code=None, properties=None):
        published.set()

    c.on_publish = on_publish
    c.loop_start()
    try:
        info = c.publish(topic_request, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=args.timeout)
        if not published.wait(timeout=args.timeout):
            print(f"[cloud-publish] no broker ack in {args.timeout}s",
                  file=sys.stderr)
            return 1
        print(json.dumps({"published": True, "topic": topic_request,
                          "payload": payload}, indent=2))
    finally:
        c.loop_stop()
        c.disconnect()
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """`beambam analyze <file.3mf>` — wraps beambam.analyze.cli_main()."""
    from beambam.analyze import cli_main
    return cli_main(args.file, json_out=getattr(args, "json_out", False))


def _package_version() -> str:
    """Return the installed `beambam` version, or a source-checkout
    fallback if the package isn't installed (e.g. running from a clone).
    Centralised so --version, User-Agent strings, and bridge_version all
    agree."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        return version("beambam")
    except Exception:
        return "1.3.0+source"


PACKAGE_VERSION = _package_version()


_COMMAND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("LAN control", [
        ("status",         "One-shot pushall — current bed/nozzle/print state"),
        ("print",          "Upload .gcode.3mf + start print over LAN"),
        ("pause",          "Pause the active print"),
        ("resume",         "Resume a paused print"),
        ("stop",           "Stop the active print"),
        ("reboot",         "M999 clear-error / restart-from-halt (NOT a real power-cycle)"),
        ("gcode",          "Send a raw gcode line to the printer"),
        ("home",           "Home the toolhead (G28)"),
        ("level",          "Run auto bed leveling"),
        ("set-temp",       "Set bed / nozzle / chamber temp"),
        ("chamber-light",  "Set chamber LED (on/off/flashing)"),
        ("fod-check",      "Toggle firmware foreign-object detection"),
        ("ams-load",       "Load a filament from an AMS slot"),
        ("ams-unload",     "Unload the current filament"),
        ("jog",            "Manual X/Y/Z jog moves"),
    ]),
    ("Slicing", [
        ("slice-print",    "STL → slice → upload + print (one shot)"),
        ("upload",         "FTPS-implicit-TLS upload .gcode.3mf"),
        ("frame",          "Generate a frame STL (parametric)"),
        ("simulate",       "Estimate print time + filament from a sliced 3mf"),
    ]),
    ("Cloud read", [
        ("cloud-login",    "Authenticate to Bambu cloud (+ --code-only)"),
        ("cloud-status",   "Show current session state"),
        ("cloud-logout",   "Drop the saved session"),
        ("cloud-printers", "List bound printers on this account"),
        ("cloud-state",    "Cloud-MQTT pushall on a printer"),
        ("cloud-history",  "Full server-side print task history"),
        ("cloud-task",     "Full task record incl. signed S3 URLs"),
        ("cloud-messages", "Notification inbox counts + list"),
        ("cloud-tickets",  "Customer-support ticket history"),
        ("cloud-firmware", "FW versions + available updates"),
        ("cloud-filaments","Spool inventory (AMS RFID + manual)"),
        ("cloud-presets",  "Cloud-synced slicer presets"),
        ("cloud-app-config","Global app feature-flag manifest"),
    ]),
    ("Cloud control", [
        ("cloud-print",    "Cloud-route start-print"),
        ("cloud-pause",    "Cloud-route pause"),
        ("cloud-resume",   "Cloud-route resume"),
        ("cloud-stop",     "Cloud-route stop"),
        ("cloud-gcode",    "Cloud-route raw gcode"),
        ("cloud-chamber-light", "Cloud-route LED control"),
        ("cloud-publish",  "Cloud-route raw JSON publish"),
        ("cloud-get-access-code", "Resolve LAN access code over cloud MQTT"),
    ]),
    ("MakerWorld", [
        ("cloud-search",   "Full-text search MakerWorld designs"),
        ("cloud-browse",   "Browse by nav (Trending / Foryou / ...)"),
        ("cloud-design",   "Show design details by id"),
        ("cloud-design-remixes", "Remix tree of a design"),
        ("cloud-favorites","List user's favorites lists"),
        ("cloud-liked",    "Designs the user has liked"),
        ("cloud-comments", "Comments + ratings on a design"),
        ("cloud-like",     "Toggle like on a design"),
        ("cloud-feed",     "For-You recommendation feed"),
        ("cloud-search-suggest", "Personalised search-bar terms"),
        ("cloud-pull-design",  "Download a design's .3mf bundle"),
        ("cloud-print-design", "Design id → download → slice → print"),
        ("print-search",   "Interactive: search → pick → slice → print"),
        ("printables-search", "Search Printables.com (anonymous GraphQL)"),
        ("fetch",          "Download from MW/Printables/Thingiverse URL"),
    ]),
    ("Daemon", [
        ("serve",          "HTTP daemon — per-printer multi-tenant"),
        ("daemon",         "Single-printer state daemon"),
        ("ha-publish",     "Home Assistant MQTT publisher"),
        ("webrtc",         "WebRTC signaling helper"),
    ]),
    ("Diagnostics", [
        ("doctor",         "Check prerequisites + suggest fixes"),
        ("init",           "First-run wizard (LAN discovery)"),
        ("config",         "Edit ~/.x2d/credentials"),
        ("health",         "Latency + reachability probes"),
        ("watch",          "Live-stream state changes"),
        ("tail",           "Stream events (state + HMS + progress) push-driven"),
        ("notify",         "Test push-notification path"),
        ("find",           "Discover printers via SSDP"),
        ("whoami",         "Show current user / region"),
        ("mqtt",           "MQTT broker probe"),
        ("history",        "Local print-history dir"),
        ("queue",          "Print queue management"),
        ("analyze",        "Pre-print 3mf safety check"),
        ("fcm-harvest",    "Pull finish snapshots from rooted Handy"),
    ]),
    ("Media", [
        ("camera",         "Camera snapshot / stream"),
        ("record",         "Record/snapshot from printer cam"),
        ("timelapse",      "Toggle timelapse capture"),
        ("resolution",     "Get/set camera resolution"),
        ("files",          "List SD-card files"),
        ("download",       "Pull a file off the printer SD"),
        ("ams",            "AMS-specific commands"),
        ("cam",            "Camera-specific commands"),
        ("slice",          "Slicer-specific commands"),
        ("cloud-fetch",    "Cloud-side file fetch"),
    ]),
]


def _build_epilog() -> str:
    """Format _COMMAND_GROUPS into a `beambam --help` epilog.

    argparse only supports ONE add_subparsers block, so all subcommands
    appear together in the default help. We append this grouped TOC as
    the epilog so users can scan by topic. Group entries are advisory:
    the actual command set comes from add_parser() calls below, so a
    new command shows up in the auto help even if we forget to list
    it here."""
    lines = ["", "Commands by topic:"]
    for group, cmds in _COMMAND_GROUPS:
        lines.append(f"\n  {group}")
        for name, blurb in cmds:
            lines.append(f"    {name:<24} {blurb}")
    lines.append("")
    lines.append("Run `beambam <command> --help` for full per-command flags.")
    return "\n".join(lines)


def cmd_help(args: argparse.Namespace) -> int:
    """`beambam help <topic>` — alias for `beambam <topic> --help`.

    Equivalent to invoking the subcommand with `--help`. Implemented by
    delegating to argparse on the topic's subparser so the resulting
    text matches `beambam <topic> --help` exactly (no risk of drift).
    """
    parser = args._root_parser  # injected by add_subparser below
    if not parser:
        print("internal error: root parser not threaded", file=sys.stderr)
        return 2
    # Find the topic subparser via the _SubParsersAction.
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub = action.choices.get(args.topic)
            if sub is None:
                avail = sorted(action.choices.keys())
                print(f"unknown topic {args.topic!r}\n", file=sys.stderr)
                print(f"available topics: {', '.join(avail)}",
                      file=sys.stderr)
                return 1
            sub.print_help()
            return 0
    print("internal error: no subparsers action on root parser",
          file=sys.stderr)
    return 2


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=_build_epilog())
    p.add_argument("--version", action="version",
                   version=f"beambam {PACKAGE_VERSION}")
    p.add_argument("--ip", help="Printer LAN IP (overrides env / file)")
    p.add_argument("--code", help="Printer 8-char access code (overrides env / file)")
    p.add_argument("--serial", help="Printer serial (overrides env / file)")
    p.add_argument("--printer",
                   help="Pick a [printer:NAME] section from ~/.x2d/credentials. "
                        "Required when more than one named section exists and "
                        "no plain [printer] is present. Overrides $X2D_PRINTER.")

    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="One-shot signed pushall + dump state")
    s.add_argument("--timeout", type=float, default=8.0)
    s.set_defaults(fn=cmd_status)

    u = sub.add_parser("upload", help="FTPS-implicit-TLS upload .gcode.3mf")
    u.add_argument("file", help="Local file to upload")
    u.add_argument("--remote", help="Remote filename (default: basename(local))")
    u.set_defaults(fn=cmd_upload)

    pr = sub.add_parser("print", help="Upload + start_print")
    pr.add_argument("file")
    pr.add_argument("--remote")
    pr.add_argument("--slot", type=int, default=0,
                    help="AMS global slot (AMS_index*4 + tray_in_ams), 0..15")
    pr.add_argument("--no-upload", action="store_true",
                    help="Skip upload — file is already on the printer")
    pr.add_argument("--no-ams", action="store_true")
    pr.add_argument("--no-bed-level", action="store_true")
    pr.add_argument("--bed-type", default=None,
                    help="MQTT bed_type enum string. By default this is "
                         "DERIVED from the 3MF's curr_bed_type so it can "
                         "never disagree with the slice. Pass to override "
                         "(must also pass --force). Valid: cool_plate / "
                         "eng_plate / hot_plate / textured_plate / "
                         "supertack_plate.")
    pr.add_argument("--bed-temp", type=int, default=None,
                    help="Bed initial-layer temperature in °C. By default "
                         "this is DERIVED from the 3MF's "
                         "<plate>_plate_temp_initial_layer for the active "
                         "filament so the heat profile matches the slice. "
                         "Pass to override (must also pass --force).")
    pr.add_argument("--force", action="store_true",
                    help="Bypass the 3MF/AMS soft-mismatch safety guards "
                         "(brand, colour). Hard guards (empty AMS slot, "
                         "wrong filament_type) are NEVER bypassable.")
    pr.add_argument("--flow-cali", action="store_true")
    pr.add_argument("--timelapse", action="store_true")
    pr.add_argument("--vib-cali", action="store_true")
    pr.add_argument("--dry-run", action="store_true",
                    help="Run `beambam analyze` on the file FIRST and refuse "
                         "to upload / print if the predicted purge waste "
                         "exceeds --max-flush-g. Touches neither the "
                         "printer's network surface nor FTPS. Exit code 0 "
                         "if safe, 2 if over threshold.")
    pr.add_argument("--max-flush-g", type=float, default=10.0,
                    help="Maximum total purge waste in grams that --dry-run "
                         "will accept. Default 10g — typical for a 4-color "
                         "swap-heavy print. Bigger plates with many color "
                         "transitions can blow past this fast; raise it if "
                         "you knowingly want a high-flush print.")
    pr.set_defaults(fn=cmd_print)

    d = sub.add_parser("daemon", help="Long-running monitor; emits state to stdout")
    d.add_argument("--interval", default=5,
                   help="Seconds between forced state polls (default 5)")
    d.add_argument("--http", default="",
                   help="Bind addr for status HTTP server, e.g. ':8765' or '127.0.0.1:8765'")
    d.add_argument("--quiet", action="store_true",
                   help="Only emit on the HTTP endpoint, not stdout")
    d.add_argument("--max-staleness", type=float, default=30.0,
                   help="Seconds since last printer state push beyond which "
                        "/healthz returns 503 (default 30)")
    d.add_argument("--auth-token",
                   default=os.environ.get("X2D_AUTH_TOKEN", ""),
                   help="Bearer token required for HTTP requests when "
                        "--http binds non-loopback. Default $X2D_AUTH_TOKEN. "
                        "Loopback binds (127.0.0.1) stay open even without "
                        "a token (single-user local case).")
    d.add_argument("--queue", action="store_true",
                   help="Enable the multi-printer print queue (item #55). "
                        "Auto-dispatches the next pending job to a printer "
                        "as soon as it goes idle. State persists at "
                        "~/.x2d/queue.json. Manage via /queue + "
                        "POST /queue/{add,cancel,remove,move}.")
    d.add_argument("--timelapse", action="store_true",
                   help="Enable the auto-timelapse recorder (item #56). "
                        "Captures /snapshot.jpg every "
                        "--timelapse-interval seconds during active "
                        "prints; saves under ~/.x2d/timelapses/. Browse "
                        "via /timelapses + POST /timelapses/<p>/<j>/stitch "
                        "to ffmpeg into MP4.")
    d.add_argument("--timelapse-interval", type=float, default=30.0,
                   help="Seconds between timelapse frames (default 30).")
    d.set_defaults(fn=cmd_daemon)

    # ----- print-control verbs -----------------------------------------
    pa = sub.add_parser("pause", help="Signed MQTT publish: pause current print")
    pa.set_defaults(fn=cmd_pause)

    re_ = sub.add_parser("resume", help="Signed MQTT publish: resume current print")
    re_.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("stop", help="Signed MQTT publish: abort current print")
    sp.set_defaults(fn=cmd_stop)

    rb = sub.add_parser(
        "reboot",
        help="Send M999 (clear error / restart from emergency stop) via "
             "signed MQTT. Defaults to dry-run — pass --confirm to "
             "actually publish. Note: this is not a real power-cycle; "
             "Bambu firmware doesn't expose one over MQTT.",
    )
    rb.add_argument("--confirm", action="store_true",
                    help="Actually publish the M999 payload. Without "
                         "this flag, the command prints what it would "
                         "do and exits 0 without touching the printer.")
    rb.set_defaults(fn=cmd_reboot)

    gc = sub.add_parser("gcode", help="Send a literal G-code line as a signed MQTT publish")
    gc.add_argument("gcode", help="The G-code line (a trailing newline is added if missing)")
    gc.set_defaults(fn=cmd_gcode)

    hm = sub.add_parser("home", help="Home all axes (G28)")
    hm.set_defaults(fn=cmd_home)

    lv = sub.add_parser("level", help="Auto-level the bed (G29)")
    lv.set_defaults(fn=cmd_level)

    st = sub.add_parser("set-temp", help="Set target temperature (bed/nozzle/chamber)")
    st.add_argument("target", choices=["bed", "nozzle", "chamber"])
    st.add_argument("value", type=int, help="Target temperature in °C")
    st.add_argument("--idx", type=int, default=0,
                    help="Nozzle index (0=left/main, 1=right) — only used for target=nozzle")
    st.set_defaults(fn=cmd_set_temp)

    cl = sub.add_parser("chamber-light", help="Set chamber LED state")
    cl.add_argument("state", choices=["on", "off", "flashing"])
    cl.add_argument("--on-time",   type=int, default=500)
    cl.add_argument("--off-time",  type=int, default=500)
    cl.add_argument("--loops",     type=int, default=0)
    cl.add_argument("--interval",  type=int, default=0)
    cl.set_defaults(fn=cmd_chamber_light)

    fod = sub.add_parser(
        "fod-check",
        help="Toggle X2D / N7 / H2D firmware Foreign-Object-Detection "
             "(verifies build plate is clear before each print start)",
        description="Turns on the firmware's pre-print build-plate check. "
                    "With this on, the printer scans the heatbed via its "
                    "camera (Stage 74) before starting a job; if a part or "
                    "debris from the previous run is still on the plate the "
                    "firmware refuses to start. No physical part pushoff — "
                    "this is a check + halt, not a sweep.")
    fod.add_argument("state", choices=["on", "off"])
    fod.set_defaults(fn=cmd_fod_check)

    au = sub.add_parser("ams-unload", help="Unload filament from an AMS bay")
    au.add_argument("ams", type=int, help="AMS index (0..N)")
    au.add_argument("--curr-temp", type=int, default=215,
                    help="Current nozzle temperature for the unload heat soak")
    au.add_argument("--tar-temp",  type=int, default=215,
                    help="Target temperature to hit before retract")
    au.set_defaults(fn=cmd_ams_unload)

    al = sub.add_parser("ams-load", help="Load filament from an AMS slot")
    al.add_argument("ams", type=int, help="AMS index (0..N)")
    al.add_argument("slot", type=int, help="Slot within the AMS (0..3)")
    al.add_argument("--curr-temp", type=int, default=215)
    al.add_argument("--tar-temp",  type=int, default=215)
    al.set_defaults(fn=cmd_ams_load)

    jg = sub.add_parser("jog", help="Relative axis jog via G91/G1/G90")
    jg.add_argument("axis", help="X / Y / Z / E")
    jg.add_argument("distance", type=float, help="mm to move (negative for reverse)")
    jg.add_argument("--feed", type=int, default=1500, help="Feedrate in mm/min")
    jg.set_defaults(fn=cmd_jog)

    cm = sub.add_parser(
        "camera",
        help="Spawn ffmpeg → MJPEG-over-HTTP proxy for the printer's chamber camera",
    )
    cm.add_argument("--bind", default="127.0.0.1:8766",
                    help="HTTP bind addr (default 127.0.0.1:8766)")
    cm.add_argument("--port", type=int, default=322,
                    help="Printer's RTSPS port (default 322)")
    cm.add_argument("--skip-check", action="store_true",
                    help="Skip the ipcam.rtsp_url pre-flight (useful when "
                         "MQTT can't reach the printer but RTSP is open)")
    cm.add_argument("--proto", choices=["rtsp", "local"], default="rtsp",
                    help="rtsp = RTSPS:322 via ffmpeg (default; needs "
                         "ipcam.rtsp_url != disable). "
                         "local = LVL_Local TLS:6000 (Bambu's proprietary "
                         "stream; same touchscreen LAN-mode liveview gate "
                         "applies — see runtime/network_shim/lvl_local.py)")
    cm.add_argument("--auth-token",
                    default=os.environ.get("X2D_AUTH_TOKEN", ""),
                    help="Bearer token required for HTTP requests when "
                         "--bind is non-loopback. Default $X2D_AUTH_TOKEN.")
    cm.add_argument("--idle-timeout", type=float, default=30.0,
                    help="Seconds of no-viewer activity before the upstream "
                         "pump (ffmpeg / lvl_local) is stopped to save "
                         "battery + CPU. Endpoint hits and live MJPEG "
                         "viewers reset the idle timer. Default 30s. Set "
                         "very high to keep the pump alive permanently "
                         "(matches pre-#89 behaviour).")
    cm.set_defaults(fn=cmd_camera)

    # x2d/termux #88 — IPCAM control commands (camera-side, not the
    # ffmpeg/MJPEG proxy above). These send plain MQTT to device/<sn>/request.
    rec = sub.add_parser(
        "record",
        help="Start/stop chamber-camera video recording to printer's SD card",
    )
    rec.add_argument("state", choices=["on", "off"],
                     help="on = enable recording; off = disable")
    rec.set_defaults(fn=cmd_record)

    tl = sub.add_parser(
        "timelapse",
        help="Enable/disable timelapse capture during prints (writes to SD)",
    )
    tl.add_argument("state", choices=["on", "off"],
                    help="on = enable timelapse; off = disable")
    tl.set_defaults(fn=cmd_timelapse)

    f = sub.add_parser(
        "files",
        help="List SD-card files via FTPS (see #92). Categories match "
             "the on-printer directory layout: timelapse, cache, ipcam, /.",
    )
    f.add_argument("kind", nargs="?", default="/",
                   choices=["timelapse", "video", "model", "cache", "/"],
                   help="Subdirectory to list (default: SD root)")
    f.add_argument("--json", action="store_true",
                   help="Emit JSON instead of human-readable")
    f.set_defaults(fn=cmd_files)

    fch = sub.add_parser(
        "fetch",
        help="Download a model from MakerWorld / Printables / Thingiverse "
             "or any direct STL/3MF URL (#98). With --open, also launches "
             "BambuStudio with the file(s) preloaded.",
    )
    fch.add_argument("url",
                     help="Model URL (makerworld.com, printables.com, "
                          "thingiverse.com) or direct STL/3MF link")
    fch.add_argument("--out-dir", default=os.path.expanduser("~/Downloads/x2d-models"),
                     help="Where to save downloaded files (default: %(default)s)")
    fch.add_argument("--open", action="store_true",
                     help="Spawn bambu-studio with the downloaded file(s) on argv")
    fch.add_argument("--all", action="store_true",
                     help="On Printables, download all matching files instead "
                          "of just the first one")
    fch.add_argument("--json", action="store_true",
                     help="Emit JSON list of saved paths")
    fch.set_defaults(fn=cmd_fetch)

    sp = sub.add_parser(
        "slice-print",
        help="One-shot pipeline: slice an STL with the X2D profile + "
             "upload + start print (#99). With --dry-run, slices but "
             "doesn't upload.",
    )
    sp.add_argument("stl", help="STL/STEP/OBJ/3MF input file path")
    sp.add_argument("--template", help="Reference .gcode.3mf to graft into "
                                       "(default: x2d_slice.py's default)")
    sp.add_argument("--scale", type=float, default=1.0,
                    help="Uniform scale factor applied to STL (forwards to "
                         "x2d_slice.py --scale; ignored for 3MF input)")
    sp.add_argument("--scale-pct", type=float, default=None,
                    help="Scale as a percentage (75 = 0.75×). More readable "
                         "than --scale for typed input.")
    sp.add_argument("--mm", type=float, default=None,
                    help="Auto-scale STL so its Z-extent equals this many mm.")
    sp.add_argument("--copies", "--quantity", "-n", type=int, default=1,
                    dest="copies",
                    help="How many copies of the model to tile on the plate. "
                         "Forwards to x2d_slice.py --copies; ignored for 3MF input.")
    sp.add_argument("--color",
                    help="Primary filament color #RRGGBB (forwards to "
                         "x2d_slice.py --color; ignored for 3MF input)")
    sp.add_argument("--remote", help="Remote filename on printer "
                                     "(default: input basename)")
    sp.add_argument("--slot", type=int, default=0,
                    help="AMS tray slot 0-3 (default: 0)")
    sp.add_argument("--no-ams", action="store_true",
                    help="Print without AMS (use external spool)")
    sp.add_argument("--no-bed-level", action="store_true",
                    help="Skip auto bed leveling before print")
    sp.add_argument("--flow-cali", action="store_true",
                    help="Run flow calibration before print")
    sp.add_argument("--timelapse", action="store_true",
                    help="Enable timelapse during print")
    sp.add_argument("--vib-cali", action="store_true",
                    help="Run vibration calibration before print")
    sp.add_argument("--bed-type", default="auto",
                    help="Bed surface type (auto/PEI/PETG/etc., default: auto)")
    sp.add_argument("--dry-run", action="store_true",
                    help="Slice + save .gcode.3mf locally; do NOT upload/print")
    sp.add_argument("--printer", help="Printer name in ~/.x2d/credentials")
    sp.add_argument("--ip", help="Override printer IP")
    sp.add_argument("--code", help="Override access code")
    sp.add_argument("--serial", help="Override printer serial")
    sp.set_defaults(fn=cmd_slice_print)

    h = sub.add_parser(
        "health",
        help="One-shot diagnostic: TCP reachability + MQTT state + "
             "AMS + camera + SD card + bridge daemon. Useful to debug "
             "a fresh install or a flaky printer.",
    )
    h.set_defaults(fn=cmd_health)

    w = sub.add_parser(
        "watch",
        help="Live one-line printer status updated every N seconds. "
             "Format: [HH:MM:SS] STATE Lx/y P%% eta=HHhMMm  N:cur/tgt°C  B:cur/tgt°C. "
             "Ctrl+C to exit.",
    )
    w.add_argument("--interval", type=int, default=5,
                   help="Polling interval in seconds (default: 5)")
    w.add_argument("--once", action="store_true",
                   help="Print one status line and exit (good for scripts)")
    w.set_defaults(fn=cmd_watch)

    t = sub.add_parser(
        "tail",
        help="Stream printer events (state transitions, progress "
             "milestones every 10%%, HMS code add/clear) as a live "
             "log — push-based; events surface within ms of the "
             "printer's MQTT push. Distinct from `watch` (polling). "
             "Ctrl-C to exit.",
    )
    t.add_argument("--every-state", action="store_true",
                   help="Also emit a line on every layer change "
                        "(chatty — off by default)")
    t.add_argument("--no-progress", action="store_true",
                   help="Suppress the progress-milestone lines "
                        "(useful with --json piped to a script)")
    t.add_argument("--no-hms", action="store_true",
                   help="Suppress HMS error-code add/clear lines")
    t.add_argument("--json", action="store_true",
                   help="Emit one ndjson object per event instead of "
                        "the human-readable format. Schema: "
                        "{ts, category, level, message}.")
    t.set_defaults(fn=cmd_tail)

    n = sub.add_parser(
        "notify",
        help="Background poller that fires termux-notification on print "
             "state transitions (RUNNING → FINISH / FAILED / PAUSE) and "
             "optional layer milestones. Requires termux-api package.",
    )
    n.add_argument("--interval", type=int, default=10,
                   help="Polling interval in seconds (min 5, default: 10)")
    n.add_argument("--layer-milestone", type=int, default=0,
                   help="Also notify every N layers during RUNNING. "
                        "0 disables (default).")
    n.add_argument("--exit-on-finish", action="store_true",
                   help="Exit cleanly after FINISH notification")
    n.set_defaults(fn=cmd_notify)

    res = sub.add_parser(
        "resolution",
        help="Set chamber camera resolution",
    )
    res.add_argument("resolution", choices=["low", "medium", "high", "full"],
                     help="low/medium/high/full")
    res.set_defaults(fn=cmd_resolution)

    cli_login = sub.add_parser(
        "cloud-login",
        help="Exchange Bambu cloud email + password for a session token. "
             "Stored at ~/.x2d/cloud_session.json (chmod 600). "
             "Subsequent shim calls (is_user_login / get_user_id / "
             "get_user_presets / get_user_tasks) start returning real data.",
    )
    cli_login.add_argument("--email",
                           help="Required unless --dry-run.")
    cli_login.add_argument("--password",
                           help="Required unless --dry-run. "
                                "Use a shell secret store; this lands in `ps`.")
    cli_login.add_argument("--region", choices=["us", "cn"],
                           help="Override region (default: 'cn' if email "
                                "ends with .cn, else 'us')")
    cli_login.add_argument("--dry-run", action="store_true",
                           help="Don't send credentials. Just verify the cloud "
                                "endpoint is reachable (DNS + TLS + HTTP). "
                                "Returns ok/status/region/endpoint JSON.")
    cli_login.add_argument("--email-code",
                           help="Pre-supply the email-verification code. "
                                "Skips the interactive prompt — useful for "
                                "non-interactive shells / piped input.")
    cli_login.add_argument("--code-only", action="store_true",
                           help="'Device code' mode: skip the password entirely. "
                                "Bambu emails you a 6-digit code; you paste it in. "
                                "Recommended for `uvx beambam` fresh-OS flows where "
                                "you don't want a Bambu password in your terminal "
                                "history. Pair with --email-code <code> for "
                                "non-interactive.")
    cli_login.add_argument("--tfa-code",
                           help="Pre-supply the 6-digit TOTP. "
                                "Skips the interactive prompt.")
    cli_login.add_argument("--no-bootstrap", action="store_true",
                           help="Skip the auto-write of every bound printer's "
                                "LAN access code into ~/.x2d/credentials. "
                                "Default: after login, fetch each printer's "
                                "access code via cloud MQTT (system."
                                "get_access_code) and persist as "
                                "[printer:<serial>].")
    cli_login.set_defaults(fn=cmd_cloud_login)

    cli_status = sub.add_parser(
        "cloud-status",
        help="Show the cached cloud session: logged-in / user-id / token age.",
    )
    cli_status.set_defaults(fn=cmd_cloud_status)

    # cloud-logout subparser now registered by beambam/cli/cloud.py.

    cli_printers = sub.add_parser(
        "cloud-printers",
        help="List Bambu cloud-bound printers for the logged-in account "
             "(requires `cloud-login` first). Shows dev_id, online status, "
             "model, and access_code so you can populate ~/.x2d/credentials.",
    )
    cli_printers.add_argument("--json", action="store_true",
                              help="Raw JSON output instead of the human table")
    cli_printers.set_defaults(fn=cmd_cloud_printers)

    cli_state = sub.add_parser(
        "cloud-state",
        help="Subscribe to a printer's cloud report topic via Bambu's MQTT "
             "broker (us.mqtt.bambulab.com:8883) and dump its first state "
             "message. Use --follow to stream all messages instead of "
             "exiting on first state. Requires `cloud-login` first. "
             "Sidesteps the LAN-direct verify-failure (#65) entirely "
             "because the cloud broker uses standard TLS — no per-"
             "installation cert needed.",
    )
    cli_state.add_argument("--serial",
                           help="Printer serial. Auto-picks the only one if "
                                "exactly one printer is bound to the account.")
    cli_state.add_argument("--follow", action="store_true",
                           help="Stream every message instead of exiting "
                                "after the first state push.")
    cli_state.add_argument("--timeout", type=float, default=15.0,
                           help="Seconds to wait for the first state message "
                                "(default 15). Ignored with --follow.")
    cli_state.set_defaults(fn=cmd_cloud_state)

    cli_print = sub.add_parser(
        "cloud-print",
        help="Submit a cloud-mediated print: upload .gcode.3mf to Bambu's "
             "OSS, then publish print.project_file (print_type=cloud) via "
             "the cloud MQTT broker. Printer downloads from OSS via the "
             "cloud channel — sidesteps LAN-direct verify-failure (#65). "
             "Requires `cloud-login` first.",
    )
    cli_print.add_argument("file", help="Local .gcode.3mf to upload + print")
    cli_print.add_argument("--serial", help="Printer serial (auto-picks if "
                                            "exactly one printer is bound)")
    cli_print.add_argument("--slot", type=int, default=0,
                           help="AMS global slot (AMS_idx*4 + tray, 0..15)")
    cli_print.add_argument("--no-ams", action="store_true",
                           help="External spool / direct feed; AMS off")
    cli_print.add_argument("--plate", type=int, default=1, help="Plate index")
    cli_print.add_argument("--bed-type", default="textured_plate",
                           help="textured_plate / cool_plate / engineering / hot")
    cli_print.add_argument("--bed-temp", type=int, default=65)
    cli_print.add_argument("--no-level", action="store_true",
                           help="Skip auto bed leveling")
    cli_print.add_argument("--flow-cali", action="store_true")
    cli_print.add_argument("--vibration-cali", action="store_true")
    cli_print.add_argument("--timelapse", action="store_true")
    cli_print.add_argument("--dry-run", action="store_true",
                           help="Print the MQTT payload but don't upload "
                                "or publish anything")
    cli_print.add_argument("--timeout", type=float, default=30.0,
                           help="Seconds to wait for broker ack (default 30).")
    cli_print.set_defaults(fn=cmd_cloud_print)

    # Cloud convenience commands — same flag style as the LAN versions
    # (pause/resume/stop/gcode/chamber-light) but route through the
    # cloud MQTT broker so they work off-LAN.
    def _add_cloud_cmd(name: str, helptext: str, fn):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--serial",
                        help="Printer serial (auto-picks if exactly one is bound).")
        sp.add_argument("--timeout", type=float, default=10.0,
                        help="Seconds to wait for broker ack (default 10).")
        sp.set_defaults(fn=fn)
        return sp
    _add_cloud_cmd("cloud-pause",
                   "Pause the active print remotely via cloud MQTT.",
                   cmd_cloud_pause)
    _add_cloud_cmd("cloud-resume",
                   "Resume a paused print remotely via cloud MQTT.",
                   cmd_cloud_resume)
    _add_cloud_cmd("cloud-stop",
                   "Stop / abort the active print remotely via cloud MQTT.",
                   cmd_cloud_stop)
    cli_cgcode = _add_cloud_cmd(
        "cloud-gcode",
        "Run a single gcode line on the printer via cloud MQTT (e.g. G28 / M141).",
        cmd_cloud_gcode)
    cli_cgcode.add_argument("gcode", help='gcode line, e.g. "G28" or "M141 S30"')
    cli_clamp = _add_cloud_cmd(
        "cloud-chamber-light",
        "Chamber-light remote control via cloud MQTT (on/off/flashing).",
        cmd_cloud_chamber_light)
    cli_clamp.add_argument("state", help="on / off / flashing")
    cli_clamp.add_argument("--on-time",  type=int, default=500)
    cli_clamp.add_argument("--off-time", type=int, default=500)
    cli_clamp.add_argument("--loops",    type=int, default=0,
                           help="0 = forever for `flashing`")
    cli_clamp.add_argument("--interval", type=int, default=0)

    # Read-only Bambu cloud REST queries -------------------------------
    # cloud-history / cloud-task / cloud-messages / cloud-tickets
    # subparsers now registered by beambam/cli/cloud.py.

    # cloud-feed subparser now registered by beambam/cli/cloud.py.

    # cloud-firmware / cloud-filaments subparsers now registered by
    # beambam/cli/cloud.py.

    # cloud-spool write-side CRUD. Gated by --allow-write because each
    # subcommand mutates account-side state on Bambu Cloud.
    cli_spool = sub.add_parser(
        "cloud-spool",
        help="Add / update / delete entries in your cloud spool "
             "inventory. WRITES — needs --allow-write.")
    cli_spool_sub = cli_spool.add_subparsers(dest="spool_action", required=True)

    # `cloud-spool add`
    cli_spool_add = cli_spool_sub.add_parser(
        "add", help="POST a new spool entry to /v1/.../my/filament/v2.")
    for arg, hlp in (
        ("--vendor",       "filamentVendor (e.g. Bambu / Polymaker)"),
        ("--type",         "filamentType (e.g. 'PLA Basic')"),
        ("--name",         "filamentName (e.g. 'Galaxy Black')"),
        ("--filament-id",  "filamentId (e.g. 'GFB02'; required)"),
        ("--color",        "color hex (e.g. '#0F0F0F')"),
    ):
        cli_spool_add.add_argument(arg, help=hlp)
    cli_spool_add.add_argument("--weight", type=int,
                                help="weight in grams (e.g. 1000)")
    cli_spool_add.add_argument("--allow-write", action="store_true",
                                help="Required: confirm this mutates account state.")
    cli_spool_add.add_argument("--json", action="store_true")
    cli_spool_add.set_defaults(fn=cmd_cloud_spool_add)

    # `cloud-spool update`
    cli_spool_upd = cli_spool_sub.add_parser(
        "update",
        help="PUT a partial update to an existing spool entry.")
    cli_spool_upd.add_argument("filament_id",
                                help="filamentId of the spool to update")
    for arg, hlp in (
        ("--vendor", "filamentVendor (optional override)"),
        ("--type",   "filamentType (optional override)"),
        ("--name",   "filamentName (optional override)"),
        ("--color",  "color hex (optional override)"),
    ):
        cli_spool_upd.add_argument(arg, help=hlp)
    cli_spool_upd.add_argument("--weight", type=int,
                                help="weight in grams (optional override)")
    cli_spool_upd.add_argument("--allow-write", action="store_true",
                                help="Required: confirm this mutates account state.")
    cli_spool_upd.add_argument("--json", action="store_true")
    cli_spool_upd.set_defaults(fn=cmd_cloud_spool_update)

    # `cloud-spool delete`
    cli_spool_del = cli_spool_sub.add_parser(
        "delete",
        help="DELETE a spool entry by filamentId.")
    cli_spool_del.add_argument("filament_id",
                                help="filamentId of the spool to delete")
    cli_spool_del.add_argument("--allow-write", action="store_true",
                                help="Required: confirm this mutates account state.")
    cli_spool_del.set_defaults(fn=cmd_cloud_spool_delete)

    # cloud-search-suggest subparser now registered by beambam/cli/cloud.py.

    # Phase 5b: cloud-* handlers progressively migrate to beambam.cli.cloud.
    # Currently owns: cloud-logout / cloud-search-suggest / cloud-app-config
    # / cloud-ttcode.
    from beambam.cli.cloud import add_subparser as _cloud_subparser
    _cloud_subparser(sub)

    # cloud-search / cloud-browse / cloud-design / cloud-design-remixes /
    # cloud-favorites / cloud-liked / cloud-presets subparsers now
    # registered by beambam/cli/cloud.py.

    # cloud-app-config subparser now registered by beambam/cli/cloud.py.

    cli_psr = sub.add_parser(
        "printables-search",
        help="Search Printables.com via their public GraphQL. "
             "No auth needed; results include the model page URL "
             "that `beambam fetch` can download.")
    cli_psr.add_argument("query")
    cli_psr.add_argument("--limit",  type=int, default=10)
    cli_psr.add_argument("--offset", type=int, default=0)
    cli_psr.add_argument("--json", action="store_true")
    cli_psr.set_defaults(fn=cmd_printables_search)

    cli_ps = sub.add_parser(
        "print-search",
        help="Interactive: MakerWorld search → user picks → slice → upload → print. "
             "The whole-pipeline FRE win. Pass --copies/--scale-pct/--color/--slot "
             "the same way as slice-print.")
    cli_ps.add_argument("query", help="Search query string")
    cli_ps.add_argument("--source", default="makerworld",
                        choices=("makerworld", "printables"),
                        help="Which catalogue to search. `makerworld` "
                             "(default) chains into cloud-print-design "
                             "with full slice+upload+print. `printables` "
                             "shows the picker and emits the URL — chain "
                             "via `beambam fetch` + `beambam slice/print` "
                             "manually (file shape differs from MW).")
    cli_ps.add_argument("--limit", type=int, default=10, help="How many hits to show")
    cli_ps.add_argument("--offset", type=int, default=0,
                        help="Pagination offset (Printables only — MW uses --limit)")
    cli_ps.add_argument("--pick", type=int, default=None,
                        help="Skip the interactive prompt; pick this index (1..N)")
    cli_ps.add_argument("--dry-run-pick", action="store_true",
                        help="Show results + picked design but stop before download")
    # Mirror slice-print's slice + print options
    cli_ps.add_argument("--scale", type=float, default=1.0)
    cli_ps.add_argument("--scale-pct", type=float, default=None)
    cli_ps.add_argument("--mm", type=float, default=None)
    cli_ps.add_argument("--copies", "--quantity", "-n", type=int, default=1)
    cli_ps.add_argument("--color")
    cli_ps.add_argument("--slot", type=int, default=0)
    cli_ps.add_argument("--no-ams", action="store_true")
    cli_ps.add_argument("--dry-run", action="store_true",
                        help="Download + slice but don't upload/print")
    cli_ps.add_argument("--printer")
    cli_ps.add_argument("--ip")
    cli_ps.add_argument("--code")
    cli_ps.add_argument("--serial")
    cli_ps.set_defaults(fn=cmd_print_search)

    # cloud-like / cloud-comments / cloud-comment-reply subparsers now
    # registered by beambam/cli/cloud.py.

    cli_pull = sub.add_parser(
        "cloud-pull-design",
        help="Download a MakerWorld design's .3mf bundle to a local dir.")
    cli_pull.add_argument("design_id", type=int)
    cli_pull.add_argument("--instance-id", "--instance", "--inst",
                          dest="instance_index", type=int, default=0,
                          help="Which instance to pull (0=default)")
    cli_pull.add_argument("--out-dir", default="~/Downloads/x2d-models",
                          help="Where to save the .3mf (default %(default)s)")
    cli_pull.add_argument("--json", action="store_true")
    cli_pull.set_defaults(fn=cmd_cloud_pull_design)

    cli_cpd = sub.add_parser(
        "cloud-print-design",
        help="End-to-end: MakerWorld design → download → slice → upload → print. "
             "Pass any of --copies / --scale-pct / --mm / --color / --slot to "
             "control the slice + print parameters.")
    cli_cpd.add_argument("design_id", type=int,
                         help="MakerWorld design ID (from `cloud-search` etc.)")
    cli_cpd.add_argument("--instance-id", "--instance",
                         dest="instance_index", type=int, default=0)
    cli_cpd.add_argument("--scale", type=float, default=1.0)
    cli_cpd.add_argument("--scale-pct", type=float, default=None)
    cli_cpd.add_argument("--mm", type=float, default=None)
    cli_cpd.add_argument("--copies", "--quantity", "-n", type=int, default=1)
    cli_cpd.add_argument("--color", help="Primary filament colour (hex or name)")
    cli_cpd.add_argument("--slot", type=int, default=0)
    cli_cpd.add_argument("--no-ams", action="store_true")
    cli_cpd.add_argument("--dry-run", action="store_true",
                         help="Download + slice, but don't upload/print")
    cli_cpd.add_argument("--printer")
    cli_cpd.add_argument("--ip")
    cli_cpd.add_argument("--code")
    cli_cpd.add_argument("--serial")
    cli_cpd.set_defaults(fn=cmd_cloud_print_design)

    cli_fcm = sub.add_parser(
        "fcm-harvest",
        help="Pull finish-snapshot JPGs from a rooted Bambu Handy install "
             "before their 1-hour AWS-signed URLs expire. Result: a permanent "
             "local archive in ~/.x2d/snapshots/ that Handy itself doesn't expose.")
    cli_fcm.add_argument("--device", required=True,
                         help="adb serial / ip:port of the rooted Handy host")
    cli_fcm.add_argument("--daemon", action="store_true",
                         help="Loop forever (default: single sweep)")
    cli_fcm.add_argument("--interval", type=int, default=60,
                         help="Seconds between sweeps in --daemon mode (default 60)")
    cli_fcm.add_argument("--backfill", action="store_true",
                         help="Try every URL even if its X-Amz-Date is >1h old")
    cli_fcm.add_argument("--verbose", action="store_true")
    cli_fcm.set_defaults(fn=cmd_fcm_harvest)

    cli_gac = sub.add_parser(
        "cloud-get-access-code",
        help="Fetch a printer's LAN access code over cloud MQTT — same "
             "system.get_access_code path BambuStudio uses on first "
             "cloud-bind. With --persist, also writes the discovered "
             "code (and --ip if given) into ~/.x2d/credentials so "
             "subsequent LAN commands work without flags.",
    )
    cli_gac.add_argument("--serial",
                         help="Printer serial (auto-picks if exactly one is bound).")
    cli_gac.add_argument("--timeout", type=float, default=10.0,
                         help="Seconds to wait for printer reply (default 10).")
    cli_gac.add_argument("--persist", action="store_true",
                         help="Save the discovered code into ~/.x2d/credentials.")
    cli_gac.add_argument("--ip", default="",
                         help="Printer IP — written into the section when "
                              "--persist is set. If omitted and the section "
                              "already exists with an IP, the existing one "
                              "is kept.")
    cli_gac.add_argument("--section", default="",
                         help="Section name in ~/.x2d/credentials to write to "
                              "(default 'printer:<serial>'). Use 'printer' to "
                              "make this the default printer.")
    cli_gac.set_defaults(fn=cmd_cloud_get_access_code)

    cli_pub = sub.add_parser(
        "cloud-publish",
        help="Publish a raw JSON payload to a printer's request topic via "
             "Bambu's cloud MQTT broker. Schema matches the LAN topic — "
             "{\"print\":{\"command\":\"pause\",...}} etc. Useful for "
             "remote pause/resume/stop/light when you're not on the "
             "printer's LAN.",
    )
    cli_pub.add_argument("--serial",
                         help="Printer serial (or set X2D_SERIAL env).")
    cli_pub.add_argument("--payload", required=True,
                         help='JSON payload, e.g. \'{"print":{"command":"pause"}}\'')
    cli_pub.add_argument("--timeout", type=float, default=10.0,
                         help="Seconds to wait for broker ack (default 10).")
    cli_pub.set_defaults(fn=cmd_cloud_publish)

    pl = sub.add_parser(
        "printers",
        help="List every [printer] / [printer:NAME] section in "
             "~/.x2d/credentials. The default section is reported as "
             "the empty string.",
    )
    pl.set_defaults(fn=cmd_printers)

    ha = sub.add_parser(
        "ha-publish",
        help="Bridge state from a running daemon to a Home Assistant "
             "MQTT broker via HA discovery (item #50). Forwards "
             "command topics back to /control/<verb> on the daemon.",
    )
    ha.add_argument("--broker", default="127.0.0.1:1883",
                    help="MQTT broker host:port (default 127.0.0.1:1883)")
    ha.add_argument("--broker-username", default=os.environ.get("X2D_HA_USER", ""),
                    help="MQTT broker username (or $X2D_HA_USER)")
    ha.add_argument("--broker-password", default=os.environ.get("X2D_HA_PASS", ""),
                    help="MQTT broker password (or $X2D_HA_PASS)")
    ha.add_argument("--daemon-url", default="http://127.0.0.1:8765",
                    help="x2d_bridge daemon HTTP base URL")
    ha.add_argument("--daemon-token", default=os.environ.get("X2D_AUTH_TOKEN", ""),
                    help="Bearer token for the daemon (--auth-token side)")
    ha.add_argument("--printer", default="",
                    help="Printer name (matches --printer on daemon)")
    ha.add_argument("--device-serial", default="",
                    help="HA device identifier (defaults to printer's serial)")
    ha.add_argument("--device-model", default="X2D",
                    help="Model string for HA device card (default X2D)")
    ha.add_argument("--discovery-prefix", default="homeassistant",
                    help="HA discovery topic prefix (default homeassistant)")
    ha.set_defaults(fn=cmd_ha_publish)

    from beambam.frame import add_subparser as _frame_subparser
    _frame_subparser(sub)

    from beambam.simulate import add_subparser as _simulate_subparser
    _simulate_subparser(sub)

    from beambam.cloud_fetch import add_subparser as _cloud_fetch_subparser
    _cloud_fetch_subparser(sub)

    from beambam.download import add_subparser as _download_subparser
    _download_subparser(sub)

    from beambam.ams import add_subparser as _ams_subparser
    _ams_subparser(sub)

    from beambam.cam import add_subparser as _cam_subparser
    _cam_subparser(sub)

    from beambam.slice import add_subparser as _slice_subparser
    _slice_subparser(sub)

    from beambam.find import add_subparser as _find_subparser
    _find_subparser(sub)

    from beambam.cloud_data import add_subparser as _cloud_data_subparser
    _cloud_data_subparser(sub)

    from beambam.configcli import add_subparser as _config_subparser
    _config_subparser(sub)

    from beambam.mqttcli import add_subparser as _mqttcli_subparser
    _mqttcli_subparser(sub)

    from beambam.queuecli import add_subparser as _queuecli_subparser
    _queuecli_subparser(sub)

    from beambam.doctor import add_subparser as _doctor_subparser
    _doctor_subparser(sub)

    from beambam.init_wizard import add_subparser as _init_subparser
    _init_subparser(sub)

    from beambam.install_completion import add_subparser as _install_compl_subparser
    _install_compl_subparser(sub, root_parser=p)

    from beambam.upgrade import add_subparser as _upgrade_subparser
    _upgrade_subparser(sub)

    from beambam.plate import add_subparser as _plate_subparser
    _plate_subparser(sub)

    an = sub.add_parser(
        "analyze",
        help="Dissect a .gcode.3mf — filament/nozzle assignment, per-phase "
             "toolchanges, real flush volume, AMS-tray requirements, hints.",
    )
    an.add_argument("file", help="Path to a .gcode.3mf file")
    an.add_argument("--json", dest="json_out", action="store_true",
                    help="Machine-readable JSON instead of human summary")
    an.set_defaults(fn=cmd_analyze)

    wr = sub.add_parser(
        "webrtc",
        help="WebRTC gateway: pulls /cam.jpg from the camera daemon "
             "and re-publishes as a live VP8 track over WebRTC. "
             "Browser viewer at /cam.webrtc.html.",
    )
    wr.add_argument("--bind", default="127.0.0.1:8765",
                    help="HTTP signaling bind addr (default 127.0.0.1:8765)")
    wr.add_argument("--camera-url", default="http://127.0.0.1:8766",
                    help="Upstream camera daemon URL "
                         "(default http://127.0.0.1:8766)")
    wr.add_argument("--frame-hz", default=os.environ.get(
        "X2D_WEBRTC_FRAME_HZ", "30"),
                    help="JPEG poll rate from the camera daemon")
    wr.add_argument("--stun", default=os.environ.get(
        "X2D_WEBRTC_ICE_STUN", "stun:stun.l.google.com:19302"),
                    help="Comma-separated STUN URLs (empty disables)")
    wr.set_defaults(fn=cmd_webrtc)

    sv = sub.add_parser(
        "serve",
        help="Run a Unix-socket RPC server for libbambu_networking.so "
             "(see runtime/network_shim/PROTOCOL.md)",
    )
    sv.add_argument(
        "--sock",
        default=os.environ.get("X2D_BRIDGE_SOCK",
                               str(Path.home() / ".x2d" / "bridge.sock")),
        help="Unix socket path (default $X2D_BRIDGE_SOCK or "
             "~/.x2d/bridge.sock)",
    )
    sv.set_defaults(fn=cmd_serve)

    # `beambam help <topic>` alias for `beambam <topic> --help`.
    help_sub = sub.add_parser(
        "help",
        help="Show help for a topic (alias for `<topic> --help`).",
    )
    help_sub.add_argument(
        "topic",
        help="Subcommand name to print help for. e.g. `beambam help print`.",
    )
    help_sub.set_defaults(fn=cmd_help, _root_parser=p)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
