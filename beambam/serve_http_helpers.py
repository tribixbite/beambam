"""beambam.serve_http_helpers — request-handler utilities used by
x2d_bridge's HTTP daemon (`_serve_http`) and the standalone camera
daemon (`beambam.cli.daemon.cmd_camera`).

Pure helpers — no global state beyond a module-level rotation lock
for the access log. Importable on every supported platform (Linux /
macOS / Windows) because nothing here calls into MQTT, paho, FTPS,
or any printer-specific surface.

Currently:
  _is_loopback(host)                — auth-gate decision for non-localhost
  _AUTH_COOKIE_NAME                 — cookie name the in-browser UI uses
  _parse_cookie(header, name)       — pull one cookie value out of a header
  _check_bearer(handler, ...)       — Bearer + cookie auth decision +
                                      sends 401 on rejection
  _format_prometheus_metrics(...)   — Prometheus exposition format
                                      (counters, gauges, AMS humidity)
  _ACCESS_LOG_PATH                  — ~/.x2d/access.log
  _ACCESS_LOG_MAX_BYTES             — 1 MiB rotation threshold
  _write_access_log(record)         — append one JSON line + rotate

x2d_bridge.py re-exports each public name so existing callers
(`from x2d_bridge import _check_bearer` in beambam.cli.daemon's
cmd_camera, plus the `_serve_http` body inside x2d_bridge itself)
keep working without modification.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


def _is_loopback(host: str) -> bool:
    """True if the host is a loopback address (auth not required).
    Anything else (LAN IP, 0.0.0.0) is treated as exposed and gates
    on bearer-token auth when one is configured."""
    return host in {"127.0.0.1", "::1", "localhost", ""}


_AUTH_COOKIE_NAME = "x2d_token"


def _parse_cookie(header: str, name: str) -> str:
    """Extract a single cookie value by name from a Cookie: header.
    Returns "" if not present. Tolerant of quotes and surrounding
    spaces."""
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
    a `x2d_token=<token>` cookie. The cookie path is what the
    in-browser web UI (#48) uses so SSE/EventSource works
    (EventSource doesn't allow custom headers from JS). Static asset
    routes that don't need auth (login page bootstrap) bypass this
    check via the `bypass_auth` handler attr — see `do_GET`.
    """
    import hmac
    if not expected:
        if not _is_loopback(host):
            handler.send_response(401)
            handler.send_header(
                "WWW-Authenticate",
                'Bearer realm="x2d", '
                'error="invalid_request", '
                'error_description="--auth-token required for '
                'non-loopback binds"')
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
        handler.send_header("WWW-Authenticate",
                            'Bearer realm="x2d"')
        handler.end_headers()
        return False
    # Constant-time compare so we don't leak token length via timing.
    if not hmac.compare_digest(presented, expected):
        handler.send_response(401)
        handler.send_header("WWW-Authenticate",
                            'Bearer realm="x2d", '
                            'error="invalid_token"')
        handler.end_headers()
        return False
    return True


def _format_prometheus_metrics(
    states: dict[str, dict | None],
    last_ts_by_name: dict[str, float],
) -> bytes:
    """Render counters + per-printer gauges in Prometheus text
    exposition format (item #38). Stateless render — pulls counters
    from beambam.mqtt._metrics_snapshot and gauges from the live
    state cache."""
    from beambam.mqtt import _metrics_snapshot
    counters, glob = _metrics_snapshot()
    lines: list[str] = []

    # Global counters (no printer label)
    lines.append("# HELP x2d_ssdp_notifies_total Total SSDP NOTIFY "
                 "broadcasts received")
    lines.append("# TYPE x2d_ssdp_notifies_total counter")
    lines.append(
        f"x2d_ssdp_notifies_total "
        f"{glob.get('ssdp_notifies_total', 0)}")

    # Per-printer counters
    counter_help = {
        "messages_total":         ("counter",
                                    "MQTT state push messages received"),
        "mqtt_connects_total":    ("counter",
                                    "MQTT connect successes"),
        "mqtt_disconnects_total": ("counter",
                                    "MQTT connect failures (rc!=0)"),
    }
    for cname, (ctype, chelp) in counter_help.items():
        lines.append(f"# HELP x2d_{cname} {chelp}")
        lines.append(f"# TYPE x2d_{cname} {ctype}")
        for serial, kvs in counters.items():
            v = kvs.get(cname, 0)
            lines.append(f'x2d_{cname}{{serial="{serial}"}} {v}')

    # Per-printer last_message_ts as a gauge
    lines.append("# HELP x2d_last_message_ts Unix-epoch seconds of "
                 "last printer push")
    lines.append("# TYPE x2d_last_message_ts gauge")
    for name, ts in last_ts_by_name.items():
        lines.append(
            f'x2d_last_message_ts{{printer="{name}"}} {ts}')

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
            # `node` walks through nested dict levels and ends as
            # either a leaf number or None. The walker variable is
            # renamed from `v` so it doesn't collide with the earlier
            # counter-loop's `v` (mypy treats them as same-scope).
            node: Any = state
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    node = None
                    break
                node = node[key]
            if node is None or not isinstance(node, (int, float)):
                continue
            lines.append(
                f'x2d_{gname}{{printer="{printer}"}} {node}')

    # AMS slot humidity (per slot) — common scrape target
    lines.append("# HELP x2d_ams_humidity AMS slot humidity rating "
                 "(0=dry, 5=wet)")
    lines.append("# TYPE x2d_ams_humidity gauge")
    for printer, state in states.items():
        if not state:
            continue
        ams_list = ((state.get("print", {}).get("ams", {})
                     .get("ams")) or [])
        for ams in ams_list:
            try:
                ams_id = ams.get("id", "?")
                hum = float(ams.get("humidity", 0))
                lines.append(
                    f'x2d_ams_humidity{{printer="{printer}",'
                    f'ams_id="{ams_id}"}} {hum}')
            except (ValueError, TypeError, AttributeError):
                continue

    body = "\n".join(lines) + "\n"
    return body.encode("utf-8")


_ACCESS_LOG_PATH = Path.home() / ".x2d" / "access.log"
_ACCESS_LOG_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB
_access_log_lock = threading.Lock()


def _write_access_log(record: dict) -> None:
    """Append one JSON line to ~/.x2d/access.log; rotate to
    access.log.1 when the active file exceeds 1 MiB. Single rotation
    slot — older rotated logs are overwritten. Match the bridge.log
    rotation scheme used by run_gui_clean.sh so operators see the
    same shape everywhere.
    """
    path = _ACCESS_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with _access_log_lock:
        try:
            if (path.exists()
                    and path.stat().st_size + len(line)
                    > _ACCESS_LOG_MAX_BYTES):
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


__all__ = [
    "_is_loopback",
    "_AUTH_COOKIE_NAME",
    "_parse_cookie",
    "_check_bearer",
    "_format_prometheus_metrics",
    "_ACCESS_LOG_PATH",
    "_ACCESS_LOG_MAX_BYTES",
    "_access_log_lock",
    "_write_access_log",
]
# Make `time` reachable via this module — unused by helpers but keeps
# the import discoverable from x2d_bridge's `from .* import *` style.
_ = time
