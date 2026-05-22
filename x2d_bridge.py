#!/usr/bin/env python3
# BRIDGE-SPLIT MILESTONE (commit 404352f, 2026-05-21):
# Zero `def cmd_*` handlers remain in this file — every CLI command
# now lives under beambam/cli/{cloud,control,daemon,info,lan}.py and
# is re-exported below for back-compat. What's still here:
#   * argparse construction in `main()` (~700 LoC; Phase 5e batch 2)
#   * ServeServer class + supporting _OpError / _PrinterSession /
#     _ConnHandler / 25 _op_* functions (~1500 LoC; Phase 5e batch 3)
#   * _serve_http function + HTTP helpers (~580 LoC; Phase 5e batch 4)
#   * module-level constants (X2D_ROOT_PATH, PACKAGE_VERSION,
#     _WEB_DIR_DEFAULT, LOG_QUEUE, _ACCESS_LOG_PATH, etc.)
# When all three are extracted, x2d_bridge.py becomes a thin shim that
# only does `from beambam.cli import main; sys.exit(main())`.
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


# _md5_of / start_print / PrintRefusal / _filament_class /
# _derive_print_params_from_3mf / _validate_ams_slot moved to
# beambam/print_job.py (Phase 5d batch 3). Re-exported below so
# `from x2d_bridge import start_print` (lan_print.py, beambam.simulate,
# beambam.printer, tests/test_ams_mapping.py) keeps working.
from beambam.print_job import (  # noqa: E402, F401
    PrintRefusal,
    _md5_of,
    _filament_class,
    _BED_NAME_TO_MQTT,
    _BED_TEMP_KEY_BY_MQTT,
    _derive_print_params_from_3mf,
    _validate_ams_slot,
    start_print,
)


# ---------------------------------------------------------------------------
# Optional HTTP status endpoint (so other tools can poll a JSON URL)
# ---------------------------------------------------------------------------

# HTTP helpers moved to beambam/serve_http_helpers.py (Phase 5e batch 2).
# _serve_http (below) imports them; daemon.py:cmd_camera also uses
# _check_bearer via lazy thunk that resolves through the re-export.
from beambam.serve_http_helpers import (  # noqa: E402, F401
    _is_loopback,
    _AUTH_COOKIE_NAME,
    _parse_cookie,
    _check_bearer,
    _format_prometheus_metrics,
    _ACCESS_LOG_PATH,
    _ACCESS_LOG_MAX_BYTES,
    _access_log_lock,
    _write_access_log,
)


_WEB_DIR_DEFAULT = Path(__file__).resolve().parent / "web"


# Logger used by the queue dispatcher inside `cmd_daemon` (which lives
# in beambam.cli.daemon and lazy-imports this back). Each LOG_QUEUE.*
# call lands in the standard `logging` tree under the "x2d.queue"
# namespace so operators can filter daemon logs by stream.
import logging  # noqa: E402
LOG_QUEUE = logging.getLogger("x2d.queue")


# Phase 5e batch 3 — `_serve_http()` body moved to
# `beambam/serve_http.py`. The bridge re-exports it so
# `from x2d_bridge import _serve_http` (mobile daemon, all the
# HTTP route tests, anything inspecting the old surface) keeps
# working unchanged.
from beambam.serve_http import _serve_http  # noqa: E402, F401
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
    cmd_fetch,
    cmd_analyze,
    cmd_fcm_harvest,
    cmd_help,
    _TailDispatcher,
    _tail_print,
)


# cmd_upload moved to beambam/cli/lan.py (Phase 5d batch 1).
from beambam.cli.lan import cmd_upload  # noqa: E402, F401


# cmd_print moved to beambam/cli/lan.py (Phase 5d batch 5).
from beambam.cli.lan import cmd_print  # noqa: E402, F401


# ---------------------------------------------------------------------------
# `serve` mode — Unix-domain socket server that the libbambu_networking.so
# shim talks to. See runtime/network_shim/PROTOCOL.md for the wire format.
#
# One ServeServer process accepts many shim connections (one per
# bambu-studio instance). Each connection runs in its own reader thread.
# Printer-side MQTT clients are shared globally keyed by dev_id, so two
# shims pointing at the same printer don't double-subscribe.
# ---------------------------------------------------------------------------

# Phase 5e batch 2 — the ServeServer + _PrinterSession + _ConnHandler
# + every _op_* handler + the _OPS dispatch table moved to
# beambam/serve_socket.py. Re-exported here so:
#   * `python3 x2d_bridge.py serve` (the libbambu_networking.so
#     spawn-by-pathname entry point) keeps working through
#     beambam.cli.daemon.cmd_serve.
#   * `from x2d_bridge import ServeServer` (anything inspecting
#     the old surface) keeps resolving.
from beambam.serve_socket import (  # noqa: E402, F401
    ABI_VERSION,
    SHIM_VERSION,
    _OpError,
    _PrinterSession,
    ServeServer,
    _ConnHandler,
    _cloud_client,
    _op_hello,
    _op_connect_printer,
    _op_disconnect_printer,
    _op_send_message_to_printer,
    _op_start_local_print,
    _op_start_send_gcode_to_sdcard,
    _op_start_discovery,
    _op_subscribe_local,
    _op_get_version,
    _op_noop_ok,
    _op_login_status,
    _op_user_id,
    _op_user_presets,
    _op_user_tasks,
    _OPS,
)


# ---------------------------------------------------------------------------
# Print-control verbs — direct signed-MQTT publishes for the most common
# operator actions. Payload schemas reverse-engineered from
# bs-bionic/src/slic3r/GUI/DeviceManager.cpp::MachineObject::command_*
# (see comments next to each).
# ---------------------------------------------------------------------------

def _publish_one(args: argparse.Namespace, payload: dict) -> int:
    """Connect, publish one signed-MQTT payload, disconnect, echo JSON.

    Stays inline here (rather than moving to beambam.cli._helpers in
    Phase 5e.4) because ~8 test files monkeypatch
    `x2d_bridge.X2DClient` and rely on this function's `X2DClient(...)`
    lookup resolving from THIS module's namespace. Moving it broke 23
    tests; the cost outweighed the 9 LoC saved."""
    creds = Creds.resolve(args)
    cli = X2DClient(creds)
    cli.connect()
    try:
        cli.publish(payload)
    finally:
        cli.disconnect()
    print(json.dumps(payload, indent=2))
    return 0


# `_reboot_payload` + `_REBOOT_GCODE` moved to beambam/cli/control.py
# next to `cmd_reboot` (Phase 5e batch 4). Re-exported here for
# back-compat — tests/test_reboot.py reaches for these on the
# x2d_bridge surface.
from beambam.cli._helpers import _xcam_cmd  # noqa: E402, F401
from beambam.cli.control import (  # noqa: E402, F401
    _REBOOT_GCODE,
    _reboot_payload,
)


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


# cmd_files moved to beambam/cli/lan.py (Phase 5d batch 1).
from beambam.cli.lan import cmd_files  # noqa: E402, F401


# cmd_fetch moved to beambam/cli/info.py (Phase 5c batch 4 — closes Phase 5c).
# Re-exported alongside the other info verbs below.


# cmd_slice_print moved to beambam/cli/lan.py (Phase 5d batch 4).
from beambam.cli.lan import cmd_slice_print  # noqa: E402, F401


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


# cmd_camera moved to beambam/cli/daemon.py (Phase 5d batch 6).
from beambam.cli.daemon import cmd_camera  # noqa: E402, F401


# cmd_serve moved to beambam/cli/daemon.py (Phase 5d batch 7).
# Re-exported below alongside the other daemon handlers.
from beambam.cli.daemon import cmd_serve  # noqa: E402, F401


# cmd_daemon moved to beambam/cli/daemon.py (Phase 5d batch 8 — closes Phase 5d).
from beambam.cli.daemon import cmd_daemon  # noqa: E402, F401


# Re-exports collateral-restored after the Phase 5d batch 8 bulk-delete
# of cmd_daemon also took out the adjacent re-export lines for these
# names. Each `from beambam.cli.* import cmd_*` here mirrors a moved
# handler — main() below references each name via `set_defaults(fn=...)`.
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_login,
    cmd_cloud_logout,
    cmd_cloud_printers,
    cmd_cloud_status,
)
from beambam.cli.daemon import (  # noqa: E402, F401
    cmd_ha_publish,
    cmd_webrtc,
)
from beambam.cli.info import cmd_printers  # noqa: E402, F401


# _http_cloud_* HTTP-route helpers moved to beambam/http_cloud.py
# (Phase 5e batch 4). Re-exported so the inline Handler class in
# _serve_http (which references the names via global lookup) keeps
# resolving them.
from beambam.http_cloud import (  # noqa: E402, F401
    _http_cloud_login,
    _http_cloud_logout,
    _http_cloud_status,
    _http_cloud_printers,
    _http_cloud_state,
    _http_cloud_publish,
)


# _cloud_publish_payload + _resolve_cloud_serial moved to
# beambam/cli/cloud.py (Phase 5b batch 7). Re-exported below.
from beambam.cli.cloud import (  # noqa: E402, F401
    _cloud_mqtt_connect,
    _cloud_publish_payload,
    _resolve_cloud_serial,
    cmd_cloud_state,
)


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


# _spool_body_from_args / _require_allow_write / cmd_cloud_spool_*
# moved to beambam/cli/cloud.py (Phase 5b batch 6). Re-exported.
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_spool_add,
    cmd_cloud_spool_update,
    cmd_cloud_spool_delete,
    _spool_body_from_args,
    _require_allow_write,
)


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


# cmd_printables_search + _print_search_printables + cmd_print_search
# moved to beambam/cli/cloud.py (Phase 5b batch 12). Re-exported.
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_printables_search,
    cmd_print_search,
    _print_search_printables,
)


# cmd_cloud_like / cmd_cloud_comments / cmd_cloud_comment_reply moved to
# beambam/cli/cloud.py (Phase 5b).
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_like,
    cmd_cloud_comments,
    cmd_cloud_comment_reply,
)


# cmd_cloud_pull_design + cmd_cloud_print_design moved to
# beambam/cli/cloud.py (Phase 5b batch 11). Re-exported.
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_pull_design,
    cmd_cloud_print_design,
)


# cmd_fcm_harvest moved to beambam/cli/info.py (Phase 5c batch 5).


# cmd_cloud_app_config moved to beambam/cli/cloud.py (Phase 5b).
from beambam.cli.cloud import cmd_cloud_app_config  # noqa: E402, F401

# cmd_cloud_profile / points / unread added 2026-05-21 to close the
# remaining 5 cloud-only endpoint-audit gaps.
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_points,
    cmd_cloud_profile,
    cmd_cloud_unread,
)


# cmd_cloud_get_access_code moved to beambam/cli/cloud.py (Phase 5b
# batch 9). Re-exported below.
from beambam.cli.cloud import cmd_cloud_get_access_code  # noqa: E402, F401


# cmd_cloud_print + cmd_cloud_publish moved to beambam/cli/cloud.py
# (Phase 5b batch 10). Re-exported.
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_print,
    cmd_cloud_publish,
)


# cmd_analyze moved to beambam/cli/info.py (Phase 5c batch 5).


# `_package_version` + `PACKAGE_VERSION` moved to beambam/_version.py
# (Phase 5e batch 1). Re-export here for back-compat with existing
# call sites (`from x2d_bridge import PACKAGE_VERSION`) in
# beambam/cli/info.py + scratch/installed mirror.
from beambam._version import PACKAGE_VERSION, _package_version  # noqa: F401, E402


# _COMMAND_GROUPS catalog + _build_epilog() helper moved to
# beambam/cli/__init__.py (Phase 5e batch 1). Re-imported here so
# main()'s argparse builder keeps working unchanged.
from beambam.cli import (  # noqa: E402
    _COMMAND_GROUPS,  # noqa: F401
    _build_epilog,
)


# cmd_help moved to beambam/cli/info.py (Phase 5c batch 6).


# Phase 5e batch 5 — `main()` moved to `beambam/cli/__init__.py`.
# x2d_bridge re-exports `main` so:
#   * `python3 x2d_bridge.py` (the libbambu_networking.so
#     spawn-by-pathname entry point) keeps working via the
#     `if __name__` block below.
#   * `from x2d_bridge import main` (legacy test imports) still
#     resolves.
from beambam.cli import main  # noqa: E402, F401


if __name__ == "__main__":
    sys.exit(main())
