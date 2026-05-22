#!/usr/bin/env python3
"""x2d_bridge — backwards-compatibility shim.

The original 7,800-line monolith was drained over Phase 4 + 5 of the
bridge split (see `docs/BRIDGE_SPLIT_PLAN.md`). Every implementation now
lives under the `beambam/` package; this file remains as:

  * the literal-pathname spawn target for `libbambu_networking.so`
    (the GUI shim does `python3 x2d_bridge.py serve`)
  * the historical import surface — every `from x2d_bridge import X`
    or `x2d_bridge.X` call site in the test suite + `runtime/*` keeps
    working through the re-exports below.

The `beambam` console-script entry point + the `bb` alias point at
`beambam.cli:main` directly (per `pyproject.toml`); only the
pathname-spawn path goes through this file.

If you're writing new code, import from the canonical home:
  * `beambam.config.Creds`
  * `beambam.mqtt.{X2DClient, sign_payload, BAMBU_CERT_ID}`
  * `beambam.ftps.{upload_file, download_file, list_files}`
  * `beambam.serve_socket.{ServeServer, ...}`
  * `beambam.serve_http._serve_http`
  * `beambam.cli.{control, cloud, info, daemon, lan}.cmd_*`
  * `beambam.cli.main`

Phase 5e ratchet history (lines of code that lived here, by phase):
  pre-split → 7,800  (v1.2.0, July 2025)
  Phase 4   → 3,470  (X2DClient + metrics → beambam.mqtt)
  5e.1      → 3,462  (PACKAGE_VERSION → beambam._version)
  5e.2      → 2,501  (ServeServer + 14 _op_* → beambam.serve_socket)
  5e.3      → 1,543  (_serve_http body → beambam.serve_http)
  5e.4      → 1,531  (_reboot_payload → beambam.cli.control)
  5e.5      → 646    (main() → beambam.cli.__init__)
  5e.6      → 100ish (this final shim collapse — pure re-exports)
"""
from __future__ import annotations

import json  # used by inline _publish_one below
import logging
import os
import sys
from pathlib import Path

# Force UTF-8 stdout/stderr on platforms whose default codec (cp1252 on
# Windows) can't encode the non-ASCII characters in `beambam` help text
# + status glyphs (✓ ✗ → · ⚠).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
del _stream


# Repo root — bin/, bs-bionic/, runtime/ all live under here. Read by
# `fetch --open` and `serve`'s default sock-path resolver.
X2D_ROOT_PATH = Path(os.environ.get("X2D_ROOT", str(Path(__file__).resolve().parent)))

# Bambu's signing cert + private key. Soft-imported (None when the cert
# isn't installed) so the bridge stays importable even on a fresh clone
# without `bambu_cert.py`.
try:
    from bambu_cert import BAMBU_PRIVATE_KEY_PEM
except ModuleNotFoundError:
    BAMBU_PRIVATE_KEY_PEM = None


# ---------------------------------------------------------------------------
# Module-level constants the daemon + tests still reach via
# `x2d_bridge.<NAME>`. These two live here because they're tied to the
# bridge file's location on disk — moving them would change the
# semantics for anything that follows web_dir or queue-logger config.
# ---------------------------------------------------------------------------

# Path to the served web UI assets. runtime/{webui,ha,queue,timelapse,
# assistant}/test_*.py all do `web_dir=x2d_bridge._WEB_DIR_DEFAULT`.
_WEB_DIR_DEFAULT = Path(__file__).resolve().parent / "web"

# Logger handle for the queue-dispatch path. cmd_daemon lazy-imports
# this back so dispatch errors land under the `x2d.queue` log namespace.
LOG_QUEUE = logging.getLogger("x2d.queue")


# ---------------------------------------------------------------------------
# Re-exports — every public + leading-underscore symbol any consumer
# reaches via `x2d_bridge.X` or `from x2d_bridge import X`. Canonical
# implementation homes documented next to each block.
# ---------------------------------------------------------------------------

# Credentials (beambam.config) + signed-MQTT (beambam.mqtt) + FTPS file
# transfer (beambam.ftps) + start_print helpers (beambam.print_job).
from beambam.config import Creds  # noqa: E402, F401
from beambam.mqtt import (  # noqa: E402, F401
    BAMBU_CERT_ID,
    X2DClient,
    sign_payload,
    metric_inc as _metric_inc,
    metric_global_inc as _metric_global_inc,
    metrics_snapshot as _metrics_snapshot,
)
from beambam.ftps import (  # noqa: E402, F401
    _ImplicitFTPTLS,
    upload_file,
    download_file,
    list_files,
)
from beambam.print_job import (  # noqa: E402, F401
    PrintRefusal,
    start_print,
    _md5_of,
    _filament_class,
    _BED_NAME_TO_MQTT,
    _BED_TEMP_KEY_BY_MQTT,
    _derive_print_params_from_3mf,
    _validate_ams_slot,
)
from beambam._version import (  # noqa: E402, F401
    PACKAGE_VERSION,
    _package_version,
)

# Shared CLI / payload helpers (beambam.cli._helpers).
from beambam.cli._helpers import (  # noqa: E402, F401
    _next_seq,
    _print_cmd,
    _system_cmd,
    _camera_cmd,
    _xcam_cmd,
)

# JSON-RPC socket server (beambam.serve_socket) — the libbambu_networking.so
# shim consumer. _OPS table + all 14 _op_* handlers re-exported.
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

# HTTP daemon — server body + helpers + cloud-route helpers.
from beambam.serve_http import _serve_http  # noqa: E402, F401
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
from beambam.http_cloud import (  # noqa: E402, F401
    _http_cloud_login,
    _http_cloud_logout,
    _http_cloud_status,
    _http_cloud_printers,
    _http_cloud_state,
    _http_cloud_publish,
)

# CLI handlers — all 87 cmd_* moved out, re-exported here.
from beambam.cli.control import (  # noqa: E402, F401
    cmd_pause, cmd_resume, cmd_stop, cmd_gcode,
    cmd_home, cmd_level, cmd_set_temp, cmd_chamber_light,
    cmd_reboot, cmd_jog, cmd_record, cmd_timelapse, cmd_resolution,
    cmd_fod_check, cmd_ams_load, cmd_ams_unload,
    _REBOOT_GCODE, _reboot_payload,
)
from beambam.cli.cloud import (  # noqa: E402, F401
    cmd_cloud_login, cmd_cloud_logout, cmd_cloud_status,
    cmd_cloud_printers, cmd_cloud_state, cmd_cloud_print,
    cmd_cloud_pause, cmd_cloud_resume, cmd_cloud_stop, cmd_cloud_gcode,
    cmd_cloud_chamber_light, cmd_cloud_spool_add, cmd_cloud_spool_update,
    cmd_cloud_spool_delete, cmd_cloud_get_access_code, cmd_cloud_publish,
    cmd_cloud_pull_design, cmd_cloud_print_design,
    cmd_cloud_history, cmd_cloud_task, cmd_cloud_messages, cmd_cloud_tickets,
    cmd_cloud_feed, cmd_cloud_firmware, cmd_cloud_filaments,
    cmd_cloud_ttcode, cmd_cloud_search_suggest, cmd_cloud_search,
    cmd_cloud_browse, cmd_cloud_design, cmd_cloud_design_remixes,
    cmd_cloud_favorites, cmd_cloud_liked, cmd_cloud_presets,
    cmd_cloud_like, cmd_cloud_comments, cmd_cloud_comment_reply,
    cmd_cloud_app_config, cmd_cloud_points, cmd_cloud_profile, cmd_cloud_unread,
    cmd_printables_search, cmd_print_search,
    _cloud_mqtt_connect, _cloud_publish_payload, _resolve_cloud_serial,
    _spool_body_from_args, _require_allow_write,
    _print_search_printables,
)
from beambam.cli.info import (  # noqa: E402, F401
    cmd_status, cmd_printers, cmd_health, cmd_watch, cmd_tail,
    cmd_notify, cmd_fetch, cmd_analyze, cmd_fcm_harvest, cmd_help,
    _TailDispatcher, _tail_print,
)
from beambam.cli.daemon import (  # noqa: E402, F401
    cmd_daemon, cmd_serve, cmd_camera, cmd_webrtc, cmd_ha_publish,
)
from beambam.cli.lan import (  # noqa: E402, F401
    cmd_upload, cmd_print, cmd_slice_print, cmd_files,
)

# `main()` lives in beambam.cli.__init__ as of Phase 5e.5.
from beambam.cli import main  # noqa: E402, F401


# ---------------------------------------------------------------------------
# `_signing_key()` — deprecated alias kept so downstream importers
# (network_shim, anything that grepped the old surface) still resolve.
# Prefer `from beambam.mqtt import sign_payload` directly.
# ---------------------------------------------------------------------------
def _signing_key():
    """Deprecated alias. Use `beambam.mqtt.sign_payload` directly."""
    from beambam.mqtt import _load_private_key
    return _load_private_key()


# ---------------------------------------------------------------------------
# `_publish_one` — stays inline (intentionally not in
# beambam.cli._helpers). ~8 test files monkeypatch
# `x2d_bridge.X2DClient` and rely on this function's `X2DClient(...)`
# lookup resolving from THIS module's namespace. Moving it broke 23
# tests in Phase 5e.4; the 9-LoC saving wasn't worth the test rework.
# ---------------------------------------------------------------------------
def _publish_one(args, payload: dict) -> int:
    creds = Creds.resolve(args)
    cli = X2DClient(creds)
    cli.connect()
    try:
        cli.publish(payload)
    finally:
        cli.disconnect()
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
