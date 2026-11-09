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
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Thread
from typing import TYPE_CHECKING, Any, Callable
from beambam.paths import config_path

if TYPE_CHECKING:                       # forward-ref types only, no runtime import
    from beambam.state_hub import StateHub

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
from beambam.http_validate import (
    JSON_MAX_BYTES,
    BadRequest,
    allowed_host_names,
    content_length,
    get_bool,
    get_float,
    get_int,
    get_optional_int,
    get_str,
    is_safe_slug,
    parse_json_object,
    request_guard_error,
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


# How long POST /control/* waits for the printer's reply. Kept under the
# Home Assistant publisher's 5 s HTTP timeout; the printer normally
# answers within a second.
CONTROL_REPLY_TIMEOUT_S = 4.0

# /control verbs whose command the firmware always answers (print job
# control + AMS load); silence there is an error (504). Other verbs may
# legitimately go unanswered (202).
_CONTROL_VERBS_THAT_REPLY = frozenset({"pause", "resume", "stop", "ams_load"})


def _slice_print_argv(model: Path, *, printer: str, slot: int | None,
                      bed_type: str, no_ams: bool) -> list[str]:
    """argv for the `beambam slice-print` child of POST /slice-print.

    Runs the real CLI module with this interpreter. (The route used to
    spawn ``python serve_http.py slice-print``, which only defines
    functions and exits 0 without doing anything, behind a 202.)
    `slice-print` defines its own --printer, so it follows the
    subcommand; flags left unset keep the CLI's defaults.
    """
    argv = [sys.executable, "-m", "beambam.cli", "slice-print", str(model),
            "--bed-type", bed_type]
    if printer:
        argv += ["--printer", printer]
    if slot is not None:
        argv += ["--slot", str(slot)]
    if no_ams:
        argv.append("--no-ams")
    return argv


@dataclass
class SliceJob:
    """One `slice-print` child spawned by this daemon."""
    pid: int
    proc: "subprocess.Popen[bytes]"
    log_path: Path
    model: Path
    started: float

    def status(self) -> dict[str, Any]:
        rc = self.proc.poll()
        return {
            "pid":       self.pid,
            "state":     "running" if rc is None else "exited",
            "exit_code": rc,
            "ok_exit":   None if rc is None else rc == 0,
            "started":   self.started,
            "model":     str(self.model),
            "log":       str(self.log_path),
            "log_tail":  _tail_text(self.log_path),
        }


def _tail_text(path: Path, max_bytes: int = 4096) -> str:
    """Last `max_bytes` of a log file, decoded leniently ("" if absent)."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


class SliceJobRegistry:
    """The slice-print children this server spawned, keyed by PID.

    Only these PIDs are reported by GET /slice-print/jobs/<pid> (the
    route used to report on ANY pid via /proc, which also does not exist
    on Windows). Finished jobs beyond `keep_finished` are dropped,
    oldest first, so the registry cannot grow without bound.
    """

    def __init__(self, keep_finished: int = 50) -> None:
        self._jobs: dict[int, SliceJob] = {}
        self._lock = threading.Lock()
        self._keep = keep_finished

    def spawn(self, argv: list[str], *, log_path: Path,
              model: Path) -> SliceJob:
        # Run from the directory that contains this `beambam` package so
        # `-m beambam.cli` resolves to the same code as the daemon (a
        # source checkout is not necessarily on the child's sys.path).
        pkg_parent = Path(__file__).resolve().parents[1]
        # The child gets its own duplicate of the log handle; the
        # parent's copy is closed as soon as Popen returns so long-lived
        # daemons do not leak one handle per upload.
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(argv, stdout=log,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    cwd=str(pkg_parent), close_fds=True)
        job = SliceJob(pid=proc.pid, proc=proc, log_path=log_path,
                       model=model, started=time.time())
        with self._lock:
            self._jobs[job.pid] = job
            self._prune()
        return job

    def get(self, pid: int) -> SliceJob | None:
        with self._lock:
            return self._jobs.get(pid)

    def _prune(self) -> None:
        finished = [j for j in self._jobs.values() if j.proc.poll() is not None]
        finished.sort(key=lambda j: j.started)
        for j in finished[:max(0, len(finished) - self._keep)]:
            self._jobs.pop(j.pid, None)

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
                get_hub: Callable[[str], "StateHub | None"] | None = None,
                allowed_hosts: list[str] | None = None) -> None:
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

    Every request passes `beambam.http_validate.request_guard_error`
    first (Host allow-list against DNS rebinding; for POST: same-origin
    Origin and a JSON / octet-stream Content-Type against cross-site
    forgery). `allowed_hosts` adds host names beyond IP literals,
    localhost and this machine's names. Malformed bodies and fields
    answer 400 via `BadRequest`.
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
        from beambam import _WEB_DIR_DEFAULT
        web_dir = _WEB_DIR_DEFAULT
    clients = clients or {}
    host_allow = allowed_host_names(host, allowed_hosts)
    slice_jobs = SliceJobRegistry()

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
            if web_dir is None:                 # no web UI configured
                self.send_response(404); self.end_headers(); return
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

        # ---- request guard + error funnel ---------------------------
        def _guard(self) -> bool:
            """Refuse DNS-rebinding Hosts and cross-site POSTs before any
            route (including the auth-free ones) runs."""
            path = urllib.parse.urlparse(self.path).path
            err = request_guard_error(self.command, path, self.headers,
                                      host_allow)
            if err is None:
                return True
            status, reason = err
            self._send_json({"error": reason}, status=status)
            return False

        def _dispatch(self, handler: Callable[[], None]) -> None:
            self._x2d_start = time.time()
            self._x2d_printer = None
            self._x2d_authed = None
            if not self._guard():
                return
            try:
                handler()
            except BadRequest as e:
                self._send_json({"error": e.message}, status=e.status)
            except TimeoutError as e:
                # e.g. the queue's cross-process lock is held: answer a
                # retryable 503 instead of dropping the connection unanswered.
                self._send_json({"error": f"temporarily unavailable: {e}"}, status=503)

        def do_GET(self):
            self._dispatch(self._handle_get)

        def do_POST(self):
            self._dispatch(self._handle_post)

        def _handle_get(self):
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
                printer, job_id = self._timelapse_ids(tl_match)
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
                try:
                    timeout = float((qs.get("timeout") or ["15"])[0])
                except ValueError:
                    raise BadRequest("timeout must be a number") from None
                if not 0.1 <= timeout <= 120.0:
                    raise BadRequest("timeout must be in 0.1..120 s")
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
                snap_dir = config_path("snapshots")
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
                snap_dir = config_path("snapshots")
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
                # GET status of a slice-print job THIS daemon spawned
                # (Popen.poll — portable, no /proc). Any other PID is
                # 404: the route never reports on processes it does not
                # own.
                pid_str = path[len("/slice-print/jobs/"):]
                if not pid_str.isdigit():
                    raise BadRequest("job id must be the numeric pid "
                                     "returned by POST /slice-print")
                job = slice_jobs.get(int(pid_str))
                if job is None:
                    self._send_json({"error": f"no slice-print job "
                                     f"{pid_str} spawned by this daemon"},
                                    status=404)
                    return
                self._send_json({"ok": True, **job.status()}, status=200)
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

        def _json_body(self) -> dict[str, Any]:
            """The request body as a JSON object ({} when empty). Raises
            BadRequest for a bad Content-Length, oversize, non-JSON or
            non-object body."""
            n = content_length(self.headers, JSON_MAX_BYTES)
            raw = self.rfile.read(n) if n else b""
            if len(raw) != n:
                raise BadRequest(f"short body: expected {n} B, got {len(raw)}")
            return parse_json_object(raw)

        def _raw_body(self, max_bytes: int, what: str) -> bytes:
            """A raw (octet) body of 1..max_bytes bytes, fully read."""
            n = content_length(self.headers, max_bytes)
            if n <= 0:
                raise BadRequest(f"expected raw {what} body "
                                 "(Content-Length > 0)")
            raw = self.rfile.read(n)
            if len(raw) != n:
                raise BadRequest(f"short body: expected {n} B, got {len(raw)}")
            return raw

        def _timelapse_ids(self, m: "re.Match[str]") -> tuple[str, str]:
            """Printer + job id from a /timelapses/<p>/<j> match. The
            regex runs on the raw path, so `%2F` / `%5C` / `..` only
            appear after decoding: both ids must be plain slugs."""
            printer = urllib.parse.unquote(m.group(1))
            job_id = urllib.parse.unquote(m.group(2))
            if not (is_safe_slug(printer) and is_safe_slug(job_id)):
                raise BadRequest("timelapse printer/job ids must match "
                                 "[A-Za-z0-9_][A-Za-z0-9._-]*")
            return printer, job_id

        def _require_printer(self, name: str) -> str:
            """`name` if it is a configured printer, else 400. Job
            records and UI rows carry this string, so arbitrary text
            (markup) must never get in."""
            if name not in names:
                raise BadRequest(f"unknown printer {name!r}; "
                                 f"available: {names}")
            return name

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _publish_via_client(self, printer: str, payload: dict, *,
                                expect_reply: bool) -> tuple[int, dict]:
            """Send one control command the way the CLI does
            (beambam.commands.send_command: print.* signed with the
            recovered key, which X2D/H2D firmware requires) and answer
            with the printer's verdict:

              200  printer acknowledged (`verified: true`)
              409  printer refused — err_code / reason / readable cause
              504  no reply within CONTROL_REPLY_TIMEOUT_S for a command
                   the firmware always answers (`expect_reply`)
              202  sent, no reply, for a command that may not answer
              502  publish itself failed
              503  no live client for this printer

            A client that cannot observe replies (no `wait_for`, e.g. a
            test double) gets 200 with `verified: false`.
            """
            cli = (clients or {}).get(printer)
            if cli is None:
                return 503, {"error": "no live MQTT client for printer "
                              f"{printer!r}; run with --http on the daemon"}
            from beambam.commands import refusal_message, send_command
            family = next(iter(payload))
            command = str(payload[family].get("command", ""))
            can_wait = callable(getattr(cli, "wait_for", None))
            try:
                reply = send_command(
                    cli, payload,
                    timeout=CONTROL_REPLY_TIMEOUT_S if can_wait else 0)
            except Exception as e:
                return 502, {"ok": False, "error": f"publish failed: {e}",
                              "printer": printer, "payload": payload}
            base = {"printer": printer, "payload": payload}
            if reply is None:
                if not can_wait:
                    return 200, {"ok": True, "verified": False, **base}
                if expect_reply:
                    return 504, {"ok": False, "verified": False,
                                 "error": f"{family}.{command} sent; the "
                                          "printer did not answer within "
                                          f"{CONTROL_REPLY_TIMEOUT_S:g} s",
                                 **base}
                return 202, {"ok": True, "verified": False, **base}
            verdict = {"err_code": reply.err_code, "reason": reply.reason,
                       "sequence_id": reply.sequence_id}
            if reply.ok:
                return 200, {"ok": True, "verified": True,
                             "reply": verdict, **base}
            return 409, {"ok": False, "verified": True,
                         "error": refusal_message(reply), **verdict, **base}

        def _handle_post(self):
            self._x2d_authed = (auth_token is not None) and bool(
                self.headers.get("Authorization", "").startswith("Bearer "))
            if not _check_bearer(self, auth_token, host):
                return
            url = urllib.parse.urlparse(self.path)
            path = url.path
            # Item #57: AI assistant — POST chat.
            if path == "/assistant/chat":
                body = self._json_body()
                msg = get_str(body, "message", "", max_len=4000).strip()
                if not msg:
                    self._send_json({"error":
                        "expected {message: str, provider?: str, history?: [...]}"},
                        status=400); return
                history = body.get("history") or []
                if not isinstance(history, list):
                    raise BadRequest("'history' must be a list")
                try:
                    from runtime.assistant.router import route as _route
                except ImportError as e:
                    self._send_json({"error": f"assistant import failed: {e}"},
                                      status=500); return
                result = _route(msg,
                                  provider=get_str(body, "provider", "auto",
                                                   max_len=32),
                                  history=history)
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
                # printer yet. The body is raw octets (the request guard
                # requires a non-form Content-Type such as
                # application/octet-stream) written to a tmp file.
                # Cap body at 64 MiB — Bambu .3mf bundles are typically
                # 1-20 MiB; anything bigger is almost certainly an
                # attack or a misuploaded video.
                raw = self._raw_body(64 * 1024 * 1024, ".gcode.3mf")
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
                printer, job_id = self._timelapse_ids(tl_match)
                body = self._json_body()
                fps = get_int(body, "fps", 30, lo=1, hi=120)
                result = timelapse_rec.stitch(printer, job_id, fps=fps)
                self._send_json(result, status=200 if result["ok"] else 500)
                return
            # Cloud-side POST routes (item #67).
            if path == "/cloud/login":
                body = self._json_body()
                code, resp = _http_cloud_login(
                    email=get_str(body, "email", ""),
                    password=get_str(body, "password", ""),
                    region=get_str(body, "region", "", max_len=16) or None,
                    email_code=get_str(body, "email_code", "",
                                       max_len=32) or None,
                    tfa_code=get_str(body, "tfa_code", "",
                                     max_len=32) or None)
                self._send_json(resp, status=code); return
            if path == "/cloud/logout":
                code, resp = _http_cloud_logout()
                self._send_json(resp, status=code); return
            if path == "/cloud/publish":
                body = self._json_body()
                serial = get_str(body, "serial", "", max_len=64)
                payload = body.get("payload")
                timeout = get_float(body, "timeout", 10.0, lo=0.1, hi=120.0)
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
                body = self._json_body()
                if qverb == "add":
                    if "gcode" not in body:
                        self._send_json({"error":
                            "expected {gcode, printer, slot?, label?}"},
                            status=400); return
                    from runtime.queue.manager import (
                        JobFileError, validate_job_file)
                    # Dispatch uploads this file verbatim: only an
                    # existing .gcode.3mf is accepted (never e.g.
                    # ~/.x2d/credentials).
                    try:
                        gcode = validate_job_file(
                            get_str(body, "gcode", max_len=4096))
                    except JobFileError as e:
                        raise BadRequest(str(e)) from None
                    job = queue_mgr.add(
                        printer=self._require_printer(
                            get_str(body, "printer", "", max_len=128)),
                        gcode=str(gcode),
                        # Global AMS slot, 0-based (unit*4 + tray),
                        # as `beambam print --slot` / start_print.
                        slot=get_int(body, "slot", 0, lo=0, hi=15),
                        label=get_str(body, "label", "", max_len=200))
                    self._send_json({"ok": True, "job": job.to_dict()})
                    return
                elif qverb == "cancel":
                    job_id = get_str(body, "id", "", max_len=64)
                    ok = queue_mgr.cancel(job_id)
                    self._send_json({"ok": ok})
                    return
                elif qverb == "remove":
                    job_id = get_str(body, "id", "", max_len=64)
                    ok = queue_mgr.remove(job_id)
                    self._send_json({"ok": ok})
                    return
                elif qverb == "move":
                    job_id = get_str(body, "id", "", max_len=64)
                    dest = body.get("dest_printer")
                    ok = queue_mgr.move(
                        job_id,
                        dest_printer=(None if dest is None else
                                      self._require_printer(get_str(
                                          body, "dest_printer",
                                          max_len=128))),
                        position=get_optional_int(body, "position",
                                                  lo=0, hi=10_000))
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
                # Header-driven kwargs — every header is optional; all
                # are validated BEFORE the upload is written or a
                # process spawned.
                printer_name = self._require_printer(
                    self.headers.get("X-Printer") or self._parse_printer())
                slot_hdr = (self.headers.get("X-Slot") or "").strip()
                if slot_hdr and not (slot_hdr.isdigit()
                                     and 0 <= int(slot_hdr) <= 15):
                    raise BadRequest("X-Slot must be an integer 0..15")
                bed_type = (self.headers.get("X-Bed-Type") or "auto").strip()
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", bed_type):
                    raise BadRequest(f"bad X-Bed-Type {bed_type!r}")
                no_ams = (self.headers.get("X-No-AMS") or "").lower() in (
                    "1", "true", "yes", "on")
                body = self._raw_body(200 * 1024 * 1024, "model")
                upload_dir = config_path("uploads")
                upload_dir.mkdir(parents=True, exist_ok=True)
                stamped = f"{int(time.time())}_{fname}"
                stl_path = upload_dir / stamped
                stl_path.write_bytes(body)
                cmd_argv = _slice_print_argv(
                    stl_path, printer=printer_name,
                    slot=int(slot_hdr) if slot_hdr else None,
                    bed_type=bed_type, no_ams=no_ams)
                log_path = upload_dir / (stl_path.stem + ".log")
                try:
                    job = slice_jobs.spawn(cmd_argv, log_path=log_path,
                                           model=stl_path)
                except OSError as e:
                    self._send_json({"error": f"spawn failed: {e}"},
                                      status=500); return
                self._send_json({
                    "ok":     True,
                    "job_id": job.pid,
                    "stl":    str(stl_path),
                    "log":    str(log_path),
                    "status": "spawned",
                    "poll":   f"/slice-print/jobs/{job.pid}",
                }, status=202)
                return
            verb = path[len("/control/"):]
            printer = self._parse_printer()
            self._x2d_printer = printer
            if printer not in names:
                self._send_json({"error": f"unknown printer {printer!r}",
                                  "available": names}, status=404)
                return
            body = self._json_body()
            if verb == "pause":
                payload = _print_cmd("pause", param="")
            elif verb == "resume":
                payload = _print_cmd("resume", param="")
            elif verb == "stop":
                payload = _print_cmd("stop", param="")
            elif verb == "light":
                state = body.get("state", "")
                if state not in ("on", "off", "flashing"):
                    self._send_json({"error":
                        "state must be on/off/flashing"}, status=400)
                    return
                payload = _system_cmd(
                    "ledctrl", led_node="chamber_light", led_mode=state,
                    led_on_time=get_int(body, "on_time", 500,
                                        lo=0, hi=60_000),
                    led_off_time=get_int(body, "off_time", 500,
                                         lo=0, hi=60_000),
                    loop_times=get_int(body, "loops", 0, lo=0, hi=10_000),
                    interval_time=get_int(body, "interval", 0,
                                          lo=0, hi=60_000))
            elif verb == "temp":
                target = body.get("target", "")
                value = body.get("value")
                if target not in ("bed", "nozzle", "chamber") \
                        or isinstance(value, bool) \
                        or not isinstance(value, (int, float)):
                    self._send_json({"error":
                        "expected target=bed|nozzle|chamber + value=int"},
                                      status=400)
                    return
                temp = int(get_float(body, "value", lo=0, hi=400))
                if target == "bed":
                    payload = _print_cmd("set_bed_temp", temp=temp)
                elif target == "nozzle":
                    payload = _print_cmd(
                        "set_nozzle_temp",
                        extruder_index=get_int(body, "idx", 0, lo=0, hi=1),
                        target_temp=temp)
                else:  # chamber
                    payload = _print_cmd(
                        "gcode_line", param=f"M141 S{temp}\n")
            elif verb == "ams_load":
                slot = body.get("slot")
                if isinstance(slot, bool) or not isinstance(slot, int) \
                        or not 1 <= slot <= 16:
                    self._send_json({"error":
                        "slot must be int 1..16"}, status=400)
                    return
                # The UI labels trays 1..16; the printer wants the AMS
                # unit + tray. Same builder as `beambam ams-load` (Bambu
                # Studio's command_ams_change_filament shape: ams_id,
                # slot_id, target with the tray-0 special case) instead
                # of the old bare target=slot-1.
                from beambam.commands import ams_load_payload
                global_slot = int(slot) - 1
                payload = ams_load_payload(
                    global_slot // 4, global_slot % 4,
                    curr_temp=get_int(body, "curr_temp", 215, lo=0, hi=400),
                    tar_temp=get_int(body, "tar_temp", 215, lo=0, hi=400))
            elif verb == "ams_set":
                # Push tray_info_idx + nozzle range + color to a single
                # AMS slot. UI sends raw fields (no profile loading on
                # the daemon side); CLI keeps the profile-JSON loader.
                # Slot here is 0..15 GLOBAL (matches `beambam ams set`),
                # NOT the 1..16 UI labeling used by ams_load.
                from beambam.ams import build_tray_metadata_payload
                slot = body.get("slot")
                if isinstance(slot, bool) or not isinstance(slot, int) \
                        or not 0 <= slot <= 15:
                    self._send_json({"error":
                        "slot must be int 0..15"}, status=400)
                    return
                try:
                    payload = build_tray_metadata_payload(
                        slot,
                        tray_type=get_str(body, "tray_type", "PLA",
                                          max_len=32),
                        tray_info_idx=get_str(body, "tray_info_idx",
                                              "GFL99", max_len=32),
                        nozzle_temp_min=get_int(body, "nozzle_temp_min",
                                                190, lo=0, hi=400),
                        nozzle_temp_max=get_int(body, "nozzle_temp_max",
                                                240, lo=0, hi=400),
                        tray_color=(get_str(body, "tray_color", "",
                                            max_len=16) or None),
                        setting_id=(get_str(body, "setting_id", "",
                                            max_len=64) or None),
                    )
                except (ValueError, TypeError) as e:
                    self._send_json({"error": f"bad input: {e}"},
                                      status=400)
                    return
                if get_bool(body, "dry_run"):
                    self._send_json({"ok": True, "dry_run": True,
                                      "payload": payload})
                    return
            elif verb == "gcode":
                line = body.get("line", "")
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
                state = body.get("state", "")
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
            status, resp = self._publish_via_client(
                printer, payload,
                expect_reply=verb in _CONTROL_VERBS_THAT_REPLY)
            self._send_json(resp, status=status)

    server = ThreadingHTTPServer((host, port), Handler)
    auth_state = "auth required" if auth_token else \
                 ("OPEN — loopback only" if _is_loopback(host) else "OPEN — exposed; pass --auth-token to require Bearer")
    print(f"[x2d-bridge] HTTP listening on http://{host}:{port}/state "
          f"(+ /healthz + /printers, max-staleness {max_staleness}s; "
          f"{auth_state}; printers={names})",
          file=sys.stderr)
    server.serve_forever()
