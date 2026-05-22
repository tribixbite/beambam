"""beambam.serve_http — the multi-printer HTTP daemon body.

The bridge's daemon mode (`beambam.cli.daemon.cmd_daemon`) instantiates
this function with its own get_state / get_hub / clients closures.
Routes are documented in the function's own docstring; the full route
catalogue lives in `docs/HTTP_ROUTES.md` (kept in sync by the route
inventory test).

Helpers it depends on live in `beambam.serve_http_helpers`
(`_AUTH_COOKIE_NAME`, `_parse_cookie`, `_check_bearer`,
`_format_prometheus_metrics`, `_write_access_log`). The `_WEB_DIR_DEFAULT`
constant is owned by x2d_bridge today; imported lazily here so a fresh
interpreter doesn't have to load the whole bridge just to start a
test HTTP server.

Moved out of x2d_bridge.py in Phase 5e batch 3. The wire surface is
byte-stable — the same Handler class with the same do_GET / do_POST
dispatch table, same closure semantics over the constructor args."""
from __future__ import annotations

import argparse
import base64
import configparser
import io
import json
import logging
import os
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable

# Module-level helpers all sourced from the dedicated helpers module
# (extracted in Phase 5e batches 2-4 of the original split work).
from beambam.serve_http_helpers import (
    _AUTH_COOKIE_NAME,
    _is_loopback,
    _parse_cookie,
    _check_bearer,
    _format_prometheus_metrics,
    _write_access_log,
)
from beambam.cli._helpers import _print_cmd, _system_cmd, _camera_cmd
from beambam.http_cloud import (
    _http_cloud_login,
    _http_cloud_logout,
    _http_cloud_printers,
    _http_cloud_publish,
    _http_cloud_state,
    _http_cloud_status,
)

# _WEB_DIR_DEFAULT lives in x2d_bridge.py until Phase 5e.5 (main() move);
# lazy-imported inside _serve_http so the test fixtures that build a
# server with an explicit web_dir don't need the bridge import.

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
      POST /control/ams_set   → {"slot":0..15, "tray_type":str,
                                  "tray_info_idx":str?,
                                  "nozzle_temp_min":int,
                                  "nozzle_temp_max":int,
                                  "tray_color":str?, "setting_id":str?,
                                  "dry_run":bool?}
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
    if web_dir is None:
        # Lazy-import to dodge the
        # serve_http → x2d_bridge → serve_http import cycle. Phase 5e.5
        # will move _WEB_DIR_DEFAULT out of the bridge entirely.
        from x2d_bridge import _WEB_DIR_DEFAULT
        web_dir = _WEB_DIR_DEFAULT
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
            elif verb == "ams_set":
                # Push tray_info_idx + nozzle range + color to a single
                # AMS slot. UI sends raw fields (no profile loading on
                # the daemon side); CLI keeps the profile-JSON loader.
                # Slot here is 0..15 GLOBAL (matches `beambam ams set`),
                # NOT the 1..16 UI labeling used by ams_load.
                from beambam.ams import build_tray_metadata_payload
                slot = body.get("slot")
                if not isinstance(slot, int) or not 0 <= slot <= 15:
                    self._send_json({"error":
                        "slot must be int 0..15"}, status=400)
                    return
                try:
                    payload = build_tray_metadata_payload(
                        slot,
                        tray_type=str(body.get("tray_type", "PLA")),
                        tray_info_idx=str(body.get("tray_info_idx",
                                                    "GFL99")),
                        nozzle_temp_min=int(body.get("nozzle_temp_min",
                                                       190)),
                        nozzle_temp_max=int(body.get("nozzle_temp_max",
                                                       240)),
                        tray_color=(body.get("tray_color") or None),
                        setting_id=(body.get("setting_id") or None),
                    )
                except (ValueError, TypeError) as e:
                    self._send_json({"error": f"bad input: {e}"},
                                      status=400)
                    return
                if body.get("dry_run"):
                    self._send_json({"ok": True, "dry_run": True,
                                      "payload": payload})
                    return
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
                                                "ams_set", "gcode", "sound"]},
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
