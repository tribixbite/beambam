"""beambam.cli.daemon — long-running background-service handlers.

Phase 5d scaffold (`docs/BRIDGE_SPLIT_PLAN.md`). Hosts the daemons
that run indefinitely until SIGINT/SIGTERM — each one stands up its
own server/socket/MQTT-loop and parks the main thread waiting for
the OS signal.

Currently:
  cmd_webrtc       — WebRTC video gateway (delegates to runtime.webrtc.server)
  cmd_ha_publish   — Home Assistant MQTT publisher (one HAPublisher
                     per [printer:NAME] credentials section)

The bigger daemons (cmd_camera, cmd_serve, cmd_daemon) still live in
x2d_bridge.py because they share infrastructure with classes
(ServeServer, the multi-printer connection pool, the chamber-camera
ffmpeg pump) that haven't been hoisted into their own modules yet.
A later batch will move those once the supporting class hierarchies
relocate too.

x2d_bridge.py re-exports each handler so external + test callers
keep working.
"""
from __future__ import annotations

import argparse
import sys


def cmd_daemon(args: argparse.Namespace) -> int:
    """Multi-printer daemon (item #36).

    Spawns one X2DClient per printer section in ~/.x2d/credentials. If
    --printer is passed, only that one is started. State,
    last_message_ts and pushall polling are tracked per printer. The
    HTTP server routes `?printer=NAME` to the matching client.
    Connection failures are isolated: a single unreachable printer
    doesn't take down the others.
    """
    import argparse as _argparse
    import json
    import signal as _signal
    import sys
    import time
    from pathlib import Path
    from threading import Event, Thread
    from beambam.cli._helpers import _next_seq
    from beambam.config import Creds
    from beambam.ftps import upload_file
    from beambam.mqtt import X2DClient
    from beambam.print_job import (
        _derive_print_params_from_3mf,
        _validate_ams_slot,
        start_print,
    )
    from beambam.state_hub import StateHub
    # Lazy thunks for bridge-internal helpers that haven't moved yet:
    #   LOG_QUEUE          — module-scope logger configured by the
    #                        monolith (queue dispatcher warnings).
    #   _serve_http        — the ~580-LoC HTTP daemon body that backs
    #                        the per-printer routing layer.
    #   _WEB_DIR_DEFAULT   — path to the built-in /web static bundle.
    from x2d_bridge import LOG_QUEUE, _serve_http, _WEB_DIR_DEFAULT

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
    hubs: dict[str, StateHub] = {
        n: StateHub(maxqueue=8) for n in names_to_run}

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
        host_part, _, port_part = (args.http
                                    or "127.0.0.1:8765").rpartition(":")
        snap_host = (host_part
                     if host_part not in ("", "0.0.0.0")
                     else "127.0.0.1")
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
                LOG_QUEUE.warning(
                    "queue dispatch: no client for printer %r",
                    job.printer)
                return False
            try:
                with _dispatch_lock:
                    creds = cli.creds
                    upload_file(creds, Path(job.gcode),
                                  remote_name=Path(job.gcode).name)
                    # Per code-review #2: queue dispatch must validate
                    # AMS state at dispatch time — a job enqueued an
                    # hour ago may target a slot whose spool was
                    # swapped out since. local_path also lets
                    # start_print() auto-derive bed_type/bed_temp so
                    # heat profile matches the slice.
                    queued_3mf = Path(job.gcode)
                    derived = _derive_print_params_from_3mf(
                        queued_3mf, filament_index=0)
                    live = cli.request_state(timeout=15.0)
                    _validate_ams_slot(live, int(job.slot), derived,
                                        force=False)
                    start_print(cli, queued_3mf.name,
                                use_ams=True,
                                ams_slot=int(job.slot),
                                local_path=queued_3mf)
                LOG_QUEUE.info(
                    "queue dispatched %s → %s slot %d",
                    job.label or job.gcode, job.printer, job.slot)
                return True
            except Exception as e:
                LOG_QUEUE.exception(
                    "queue dispatch failed for %s: %s",
                    job.label or job.gcode, e)
                return False

        queue_mgr = QueueManager(dispatch_cb=_dispatch_job)
        print(f"[x2d-bridge] queue enabled; persisted at "
              f"{queue_mgr._path}", file=sys.stderr)

    def make_on_state(name: str):
        def on_state(state: dict) -> None:
            states[name] = state
            # Fan out to every subscriber of the per-printer hub.
            # Slow consumers drop oldest entries; the MQTT thread
            # never blocks.
            hub = hubs.get(name)
            if hub is not None:
                try:
                    hub.publish(state)
                except Exception as e:
                    print(f"[x2d-bridge] hub.publish({name}) "
                          f"failed: {e}", file=sys.stderr)
            if queue_mgr is not None:
                try:
                    queue_mgr.on_state(name, state)
                except Exception as e:
                    print(f"[x2d-bridge] queue.on_state({name}) "
                          f"failed: {e}", file=sys.stderr)
            if timelapse_rec is not None:
                try:
                    timelapse_rec.on_state(name, state)
                except Exception as e:
                    print(f"[x2d-bridge] timelapse.on_state({name}) "
                          f"failed: {e}", file=sys.stderr)
            if not args.quiet:
                print(json.dumps({"ts": time.time(),
                                  "printer": name,
                                  "state": state}), flush=True)
        return on_state

    clients: dict[str, X2DClient] = {}
    failed: list[tuple[str, str]] = []
    for name in names_to_run:
        try:
            ns = _argparse.Namespace(ip=None, code=None, serial=None,
                                      printer=(name or None))
            creds = Creds.resolve(ns)
            cli = X2DClient(creds, on_state=make_on_state(name))
            cli.connect()
            clients[name] = cli
            print(f"[x2d-bridge] {name or '<default>'}: connected to "
                  f"{creds.ip}", file=sys.stderr)
        except SystemExit as e:
            failed.append(
                (name, f"creds resolve failed (exit {e.code})"))
        except Exception as e:
            failed.append((name, str(e)))
            print(f"[x2d-bridge] {name or '<default>'}: connect "
                  f"failed: {e} — other printers continue",
                  file=sys.stderr)
    if not clients:
        print(f"[x2d-bridge] no printers reachable: {failed}",
              file=sys.stderr)
        return 2

    def _safe_pushall(name: str, cli: X2DClient) -> None:
        try:
            cli.publish({"pushing": {"sequence_id": _next_seq(),
                                      "command": "pushall"}})
        except Exception as e:
            print(f"[x2d-bridge] {name or '<default>'}: pushall "
                  f"failed: {e}", file=sys.stderr)

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
    print(f"[x2d-bridge] daemon up; {len(clients)} printer(s); "
          f"polling every {period}s. Ctrl-C / SIGTERM to quit.",
          file=sys.stderr)

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


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the UNIX-socket daemon that the libbambu_networking.so shim
    talks to. See runtime/network_shim/PROTOCOL.md for the wire format.

    The ServeServer class (~960 LoC including its supporting
    _PrinterSession / _ConnHandler / _OpError types + the 14 _op_*
    handlers) lives in `beambam/serve_socket.py` as of Phase 5e batch 2.
    `x2d_bridge` re-exports `ServeServer` for back-compat with anything
    inspecting the old surface."""
    from pathlib import Path
    from beambam.serve_socket import ServeServer
    sock_path = Path(args.sock).expanduser()
    server = ServeServer(sock_path)
    return server.serve_forever()


def cmd_camera(args: argparse.Namespace) -> int:
    """Chamber-camera proxy daemon. Pulls H.264 from the printer's
    RTSPS endpoint (or the LVL_Local fallback on `--proto local`) and
    re-exposes it as:

      GET /cam.mjpeg     multipart/x-mixed-replace — browser-renderable
      GET /cam.jpg       single latest JPEG snapshot
      GET /cam.m3u8      HLS playlist (~12s sliding window of mpegts)
      GET /cam<N>.ts     HLS segments

    On-demand: the ffmpeg / lvl_local pump only runs while at least
    one /cam.mjpeg client is connected OR a touch endpoint was hit in
    the last `--idle-timeout` seconds. Item #89 (battery drain triage)
    — the eagerly-spawned pump used to burn ~66% CPU 24/7.
    """
    import http.server
    import re as _re
    import shutil
    import signal as _signal
    import socketserver
    import subprocess as _sp
    import sys
    import tempfile as _tempfile
    import time
    from pathlib import Path
    from threading import Event, Lock, Thread
    from beambam.config import Creds
    from beambam.mqtt import X2DClient
    # Lazy-import bridge helpers — _check_bearer is the daemon's shared
    # auth gate; _x2d_search_roots walks the dev-checkout + dist
    # install roots for module discovery (lvl_local).
    from x2d_bridge import _check_bearer, _x2d_search_roots

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
                    "[camera] printer reports "
                    "ipcam.rtsp_url=\"disable\".\n"
                    "         Enable LAN-mode liveview on the "
                    "printer's\n"
                    "         touchscreen (Settings → Network → "
                    "Liveview)\n"
                    "         and re-run. Or pass --skip-check to try "
                    "anyway.",
                    file=sys.stderr,
                )
                return 2
            elif (rtsp_url and not rtsp_url.startswith(
                    ("rtsp://", "rtsps://"))):
                print(f"[camera] unexpected ipcam.rtsp_url: "
                      f"{rtsp_url}", file=sys.stderr)
                return 2
            print(f"[camera] printer rtsp_url=ok "
                  f"({rtsp_url[:40]}...)", file=sys.stderr)
        except Exception as e:
            print(f"[camera] state-pre-flight failed: {e} — "
                  f"continuing anyway", file=sys.stderr)

    if shutil.which("ffmpeg") is None:
        print("[camera] ffmpeg not installed. "
              "`pkg install ffmpeg` first.", file=sys.stderr)
        return 2

    rtsp_url_full = (
        f"rtsps://bblp:{creds.code}@{creds.ip}:{args.port}"
        f"/streaming/live/1"
    )

    # Single shared frame buffer + cv. ffmpeg writes JPEG frames here;
    # every HTTP client reads the latest. We never queue history — old
    # frames are dropped, viewers see live.
    state_lock = Lock()
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
                # Output 1: MJPEG-on-stdout, consumed by the JPEG
                # buffer below for /cam.mjpeg + /cam.jpg.
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
                "-hls_flags",
                "delete_segments+append_list+omit_endlist",
                "-hls_segment_filename", str(hls_segment_pattern),
                str(hls_playlist),
            ]
            print(f"[camera] spawning ffmpeg (port {args.port})",
                  file=sys.stderr)
            proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.PIPE,
                             close_fds=True)
            # PIPE was requested above, so stdout/stderr are guaranteed
            # non-None — assert for the type-checker.
            assert proc.stdout is not None
            assert proc.stderr is not None
            try:
                jpeg_buf = b""
                # stdout.read() blocks indefinitely; we add a tiny
                # poll loop on local_stop via select-with-timeout so
                # the idle reaper can stop ffmpeg promptly without
                # waiting for the next chunk arrival.
                import select as _select
                while (not local_stop.is_set()
                        and not global_stop.is_set()):
                    rlist, _, _ = _select.select(
                        [proc.stdout], [], [], 0.5)
                    if not rlist:
                        continue
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        err = proc.stderr.read().decode(
                            errors="replace")[-400:]
                        print(f"[camera] ffmpeg eof; stderr tail: "
                              f"{err}", file=sys.stderr)
                        break
                    jpeg_buf += chunk
                    # MJPEG single-image output writes back-to-back
                    # JPEGs. Split on SOI marker (0xFFD8) — keep the
                    # most-recent complete frame.
                    while True:
                        idx = jpeg_buf.find(b"\xff\xd8", 1)
                        if idx == -1:
                            break
                        frame, jpeg_buf = (jpeg_buf[:idx],
                                            jpeg_buf[idx:])
                        if (frame.startswith(b"\xff\xd8")
                                and frame.endswith(b"\xff\xd9")):
                            with state_lock:
                                latest_frame["data"] = frame
                                latest_frame["ts"]   = time.time()
            finally:
                # Reap the subprocess cleanly — terminate, wait,
                # escalate to kill if necessary, then wait again.
                # Without the final wait() after kill() the process
                # becomes a zombie (<defunct> in ps) until the daemon
                # itself exits.
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
            print(f"[camera] reconnecting in {backoff:.1f}s",
                  file=sys.stderr)
            local_stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
        # On pump exit (idle reaper or shutdown), invalidate the
        # cached frame so the next supervisor spawn can't serve a
        # stale image.
        with state_lock:
            latest_frame["data"] = b""
            latest_frame["ts"] = 0.0

    def lvl_local_pump(local_stop: Event):
        # Push module path so a repo-checkout install also imports
        # it. Same dev-vs-dist multi-root lookup as
        # _x2d_search_roots().
        for root in _x2d_search_roots():
            cand = root / "runtime" / "network_shim"
            if (cand / "lvl_local.py").exists():
                sys.path.insert(0, str(cand))
                break
        try:
            import lvl_local
        except ImportError as e:
            print(f"[camera] lvl_local module unavailable: {e}",
                  file=sys.stderr)
            return

        def _store(jpeg, ts):
            if local_stop.is_set() or global_stop.is_set():
                raise SystemExit
            with state_lock:
                latest_frame["data"] = jpeg
                latest_frame["ts"] = time.time()

        try:
            lvl_local.stream_frames(
                creds.ip, creds.code, on_frame=_store)
        except SystemExit:
            pass
        except lvl_local.LVLLocalError as e:
            # Fatal vs transient is hard to know — surface and let
            # the outer reconnect logic in stream_frames handle the
            # retry (which it does until it gets a non-LVLLocalError).
            print(f"[camera] LVL_Local fatal: {e}",
                  file=sys.stderr)
        # On pump exit, invalidate the cached frame (see ffmpeg_pump
        # for rationale).
        with state_lock:
            latest_frame["data"] = b""
            latest_frame["ts"] = 0.0

    if args.proto == "local":
        print("[camera] proto=local — using TLS:6000 LVL_Local stream",
              file=sys.stderr)
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
        # max seconds to wait for ffmpeg's first JPEG
        FIRST_FRAME_TIMEOUT = 8.0

        def __init__(self):
            self._lock = Lock()
            self._refs = 0
            self._last_touch = 0.0
            self._local_stop: Event | None = None
            self._thread: Thread | None = None
            self._reaper = Thread(target=self._reap_loop,
                                   name="camera-reaper",
                                   daemon=True)
            self._reaper.start()

        def _ensure_running_locked(self) -> None:
            if (self._thread is not None
                    and self._thread.is_alive()):
                return
            self._local_stop = Event()
            local = self._local_stop
            self._thread = Thread(
                target=lambda: pump_factory(local),
                name=f"camera-pump-{pump_label}",
                daemon=True)
            print(f"[camera] starting {pump_label} pump "
                  f"(refs={self._refs}, "
                  f"idle_timeout={self.IDLE_TIMEOUT}s)",
                  file=sys.stderr)
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
            Used by /cam.jpg and /cam.m3u8 on the cold-start path so
            the first request after idle doesn't 503 immediately."""
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
                    if (self._thread is None
                            or not self._thread.is_alive()):
                        continue
                    if self._refs > 0:
                        continue
                    idle = time.time() - self._last_touch
                    if idle <= self.IDLE_TIMEOUT:
                        continue
                    print(f"[camera] idle {idle:.0f}s ≥ "
                          f"{self.IDLE_TIMEOUT}s with no viewers; "
                          f"stopping {pump_label} pump",
                          file=sys.stderr)
                    if self._local_stop is not None:
                        self._local_stop.set()
                    # Don't join here — the pump thread cleans up its
                    # own subprocess in its finally block. is_alive()
                    # check on the next reap pass tells us when it's
                    # done.

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
                # Long-poll viewer — refcount the supervisor so the
                # pump stays alive while this client is connected,
                # even if no touch endpoints are being hit.
                supervisor.acquire()
                try:
                    # Wait briefly for the pump's first frame so we
                    # don't send headers and then immediately stall.
                    supervisor.wait_for_frame(
                        supervisor.FIRST_FRAME_TIMEOUT)
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        "multipart/x-mixed-replace; "
                        "boundary=frame")
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
                                self.wfile.write(
                                    b"Content-Type: image/jpeg\r\n")
                                self.wfile.write(
                                    f"Content-Length: {len(frame)}"
                                    f"\r\n\r\n".encode())
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
                    # Cold-start path: pump might have just spawned
                    # and is waiting for its first JPEG. Block briefly
                    # so the caller gets a frame instead of 503.
                    supervisor.wait_for_frame(
                        supervisor.FIRST_FRAME_TIMEOUT)
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
                # HLS playlist (item #20). 503 until ffmpeg has
                # emitted at least one segment and the playlist file
                # exists.
                supervisor.touch()
                if not hls_playlist.exists():
                    # Cold-start path: wait briefly for ffmpeg to emit
                    # the first segment after a lazy spawn.
                    deadline = (time.time()
                                + supervisor.FIRST_FRAME_TIMEOUT)
                    while (time.time() < deadline
                            and not hls_playlist.exists()):
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
                self.send_header(
                    "Content-Type",
                    "application/vnd.apple.mpegurl")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif (self.path.startswith("/cam")
                    and self.path.endswith(".ts")):
                # HLS segment. Validate the filename to prevent path
                # traversal (only `cam<digits>.ts` shape allowed).
                supervisor.touch()
                seg_name = self.path[1:]  # strip leading slash
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

    class ThreadingServer(socketserver.ThreadingMixIn,
                          http.server.HTTPServer):
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
          f"(idles after {CameraStreamSupervisor.IDLE_TIMEOUT}s of "
          f"no viewers).", file=sys.stderr)
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
        try:
            shutil.rmtree(hls_dir, ignore_errors=True)
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
              f"  See docs/MCP.md §2 for Termux-specific libsrtp "
              f"build steps.", file=sys.stderr)
        return 2
    host_part, _, port_part = args.bind.rpartition(":")
    host = host_part or "127.0.0.1"
    port = int(port_part)
    stun = ([s.strip() for s in args.stun.split(",") if s.strip()]
            if args.stun else None)
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
    import logging
    import os
    import signal as _signal
    from threading import Event
    from beambam.config import Creds

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
                ns = argparse.Namespace(ip=None, code=None,
                                         serial=None,
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
                  f"device_id={pub.device_id} "
                  f"base_topic={pub.base_topic}",
                  file=sys.stderr, flush=True)
        except Exception as e:
            failed.append((name, str(e)))
            print(f"[x2d-ha] {name or '<default>'}: start failed: "
                  f"{e} — other printers continue", file=sys.stderr)

    if not publishers:
        print(f"[x2d-ha] no publishers started: {failed}",
              file=sys.stderr)
        return 2

    # Run until interrupted.
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
