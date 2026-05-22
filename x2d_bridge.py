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

    u = sub.add_parser("push", aliases=["upload"],
                        help="FTPS-implicit-TLS push (was: upload)")
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

    d = sub.add_parser("boo", aliases=["daemon"],
                        help="Long-running monitor; emits state to stdout "
                             "(was: daemon)")
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

    cl = sub.add_parser("led", aliases=["chamber-light"],
                         help="Set chamber LED state (was: chamber-light)")
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
                             "(default) chains into cloud-print-design. "
                             "`printables` fetches the STL through the "
                             "Printables GraphQL then chains into "
                             "slice-print. Both end at an actual print.")
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
        "ha",
        aliases=["ha-publish"],
        help="Bridge state from a running daemon to a Home Assistant "
             "MQTT broker via HA discovery (item #50). Forwards "
             "command topics back to /control/<verb> on the daemon. "
             "(was: ha-publish)",
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
