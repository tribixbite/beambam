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
