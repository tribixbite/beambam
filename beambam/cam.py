"""beambam.cam — terminal-based camera viewer.

Pulls JPEG snapshots from the printer's camera (via the daemon's
/snapshot.jpg proxy) and renders them in the terminal at N Hz.

Supports three rendering backends, auto-detected from environment:

  1. Kitty graphics protocol  — $TERM contains 'kitty' or $TERM_PROGRAM=ghostty
  2. iTerm2 inline image      — $TERM_PROGRAM=iTerm.app or $LC_TERMINAL=iTerm2
  3. ANSI half-block fallback — works in any 24-bit-color terminal
                                 (Termux, alacritty, GNOME, xterm-256color, …)

Examples:

    beambam cam watch                         # http://127.0.0.1:8765/snapshot.jpg, 2 Hz
    beambam cam watch --url http://...        # custom snapshot URL
    beambam cam watch --hz 5 --width 80
    beambam cam watch --backend blocks        # force ANSI half-blocks
    beambam cam snap [out.jpg]                # one-shot save (no display)

Requires the bridge daemon to be running (or any HTTP server that
serves the printer's current camera frame at the given URL). For the
raw RTSP/MJPEG stream use `beambam camera` to spin up a local proxy.
"""
from __future__ import annotations

import argparse
import base64
import os
import signal
import sys
import time
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Literal


# ----- backend detection --------------------------------------------------


Backend = Literal["kitty", "iterm2", "blocks", "save"]


def detect_backend(env: dict[str, str] | None = None) -> Backend:
    """Pick the best image-display backend for the current terminal."""
    e = env if env is not None else dict(os.environ)
    term = (e.get("TERM") or "").lower()
    term_program = (e.get("TERM_PROGRAM") or "").lower()
    lc_terminal = (e.get("LC_TERMINAL") or "").lower()

    if "kitty" in term or "ghostty" in term_program:
        return "kitty"
    if term_program == "iterm.app" or lc_terminal == "iterm2":
        return "iterm2"
    # Half-blocks work in any 24-bit terminal. Termux + most modern
    # terminals support it.
    return "blocks"


# ----- snapshot fetch ------------------------------------------------------


def fetch_jpeg(url: str, *, timeout: float = 10.0) -> bytes:
    """GET a JPEG from `url`. Raises HTTPError on non-2xx."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "beambam/cam",
        "Accept": "image/jpeg, image/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ----- renderers -----------------------------------------------------------


def render_kitty(jpeg: bytes, *, width_chars: int | None = None) -> str:
    """Kitty graphics protocol — direct JPEG embed, no resize needed.
    The terminal scales to fit. Uses 'T' (transmit + display) chunked."""
    encoded = base64.b64encode(jpeg).decode("ascii")
    chunks = [encoded[i:i + 4096] for i in range(0, len(encoded), 4096)]
    out = []
    for i, chunk in enumerate(chunks):
        more = 1 if i + 1 < len(chunks) else 0
        ctrl = f"a=T,f=100,m={more}" if i == 0 else f"m={more}"
        out.append(f"\033_G{ctrl};{chunk}\033\\")
    return "".join(out) + "\n"


def render_iterm2(jpeg: bytes, *, width_chars: int | None = None) -> str:
    """iTerm2 inline image escape — \\e]1337;File=inline=1:<b64>\\a"""
    encoded = base64.b64encode(jpeg).decode("ascii")
    width_attr = f"width={width_chars};" if width_chars else ""
    return f"\033]1337;File=inline=1;{width_attr}size={len(jpeg)}:{encoded}\007\n"


def render_blocks(jpeg: bytes, *, width_chars: int = 80) -> str:
    """ANSI half-block renderer. Each character cell = 2 vertical pixels;
    foreground = upper pixel, background = lower pixel.

    Requires PIL. Output is `width_chars` wide, height auto-scaled to
    preserve aspect (terminal cells are ~2x tall so the actual pixel
    grid is width × 2*height_chars, and we use 1 char per (1 wide,
    2 tall) pixel pair)."""
    try:
        from PIL import Image
    except ImportError:
        return ("install Pillow to use the blocks backend "
                "(`pip install beambam[slicing]`)\n")

    img = Image.open(BytesIO(jpeg)).convert("RGB")
    iw, ih = img.size
    # Each terminal char ≈ 1 col wide, 2 rows tall.
    # So char-grid: width_chars × (height_pixels/2).
    px_per_col = iw / width_chars
    target_h = int(ih / px_per_col / 2) * 2          # round to even
    if target_h < 2:
        target_h = 2
    img = img.resize((width_chars, target_h), Image.BILINEAR)

    out_lines = []
    pixels = img.load()
    for y in range(0, target_h - 1, 2):
        line_parts = []
        for x in range(width_chars):
            r1, g1, b1 = pixels[x, y]
            r2, g2, b2 = pixels[x, y + 1]
            line_parts.append(
                f"\033[38;2;{r1};{g1};{b1}m"
                f"\033[48;2;{r2};{g2};{b2}m▀"
            )
        out_lines.append("".join(line_parts) + "\033[0m")
    return "\n".join(out_lines) + "\n"


def render(jpeg: bytes, backend: Backend, *, width_chars: int = 80) -> str:
    if backend == "kitty":
        return render_kitty(jpeg, width_chars=width_chars)
    if backend == "iterm2":
        return render_iterm2(jpeg, width_chars=width_chars)
    if backend == "blocks":
        return render_blocks(jpeg, width_chars=width_chars)
    raise ValueError(f"unknown backend: {backend}")


# ----- watch loop ----------------------------------------------------------


def watch_loop(url: str, *, hz: float, backend: Backend,
               width_chars: int, max_frames: int | None = None,
               out_stream=sys.stdout) -> int:
    """Pull + render in a loop until SIGINT or max_frames reached."""
    interval = 1.0 / max(0.1, hz)
    stopped = False

    def _stop(*_a):
        nonlocal stopped
        stopped = True
    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):
        # Some environments (Windows, sub-threads) can't install handlers;
        # fall back to KeyboardInterrupt-based exit.
        pass

    # Clear screen + hide cursor on entry; restore on exit.
    out_stream.write("\033[2J\033[H\033[?25l")
    out_stream.flush()
    frames = 0
    try:
        while not stopped:
            t0 = time.monotonic()
            try:
                jpeg = fetch_jpeg(url, timeout=interval + 5.0)
            except Exception as e:                          # noqa: BLE001
                out_stream.write(f"\033[H\033[2K[error] fetch: {e}\n")
                out_stream.flush()
                time.sleep(interval)
                continue
            try:
                rendered = render(jpeg, backend, width_chars=width_chars)
            except Exception as e:                          # noqa: BLE001
                out_stream.write(f"\033[H\033[2K[error] render: {e}\n")
                out_stream.flush()
                time.sleep(interval)
                continue
            # Re-home cursor each frame so subsequent renders overwrite
            # in place rather than scrolling.
            out_stream.write("\033[H" + rendered)
            out_stream.flush()
            frames += 1
            if max_frames is not None and frames >= max_frames:
                break
            elapsed = time.monotonic() - t0
            sleep = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)
    finally:
        out_stream.write("\033[?25h")
        out_stream.flush()
    return 0


# ----- CLI ----------------------------------------------------------------


def add_subparser(sub: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Build the `cam` subparser tree.

    Bare `beambam cam` = one-shot snapshot (was `cam snap`). Explicit
    subcommands: `watch` (live loop), `snap` (back-compat alias), `start`
    (run the MJPEG-over-HTTP proxy in the background; was `beambam
    camera`), `stop` (kill the running proxy via its PID file).
    """
    p = sub.add_parser(
        "cam",
        help="Camera. Bare `cam`=snapshot; subcmds: start, stop, watch, snap.",
    )
    # --url is a parent flag so `beambam cam --url X` works without a
    # subcommand. No parent positional — adding one collides with the
    # subparser slot ('snap' would be eaten as the `out` positional).
    # To override the output path, the user gives an explicit `cam snap
    # <path>`. Bare `cam` saves to ./cam.jpg.
    p.add_argument("--url",
                    default=os.environ.get(
                        "BEAMBAM_CAM_URL",
                        "http://127.0.0.1:8765/snapshot.jpg"))
    # Sub is optional — `cam` alone runs snapshot via cmd_cam.
    cam_sub = p.add_subparsers(dest="cam_cmd")

    w = cam_sub.add_parser("watch", help="Live snapshot loop")
    w.add_argument("--url",
                   default=os.environ.get("BEAMBAM_CAM_URL",
                                          "http://127.0.0.1:8765/snapshot.jpg"),
                   help="JPEG snapshot URL (default: local daemon)")
    w.add_argument("--hz", type=float, default=2.0,
                   help="Refresh rate in Hz (default 2)")
    w.add_argument("--width", type=int, default=80,
                   help="Width in terminal cells (default 80; blocks backend)")
    w.add_argument("--backend", choices=["auto", "kitty", "iterm2", "blocks"],
                   default="auto",
                   help="Render backend (default auto-detect)")
    w.add_argument("--max-frames", type=int, default=None,
                   help="Exit after N frames (testing)")

    s = cam_sub.add_parser("snap",
                            help="One-shot snapshot save (same as bare cam)")
    s.add_argument("--url",
                   default=os.environ.get("BEAMBAM_CAM_URL",
                                          "http://127.0.0.1:8765/snapshot.jpg"))
    s.add_argument("out", nargs="?", help="Output path; default: ./cam.jpg")

    # `cam start` = background-spawn the RTSPS/MJPEG proxy that used to
    # be `beambam camera`. Args mirror the camera subparser one-for-one
    # so existing scripts can rename the verb without touching flags.
    cs = cam_sub.add_parser(
        "start",
        help="Spawn the MJPEG-over-HTTP proxy in the background "
             "(was: `beambam camera`). PID stored in ~/.x2d/cam.pid; "
             "logs to ~/.x2d/cam.log.",
    )
    cs.add_argument("--bind", default="127.0.0.1:8766")
    cs.add_argument("--port", type=int, default=322)
    cs.add_argument("--skip-check", action="store_true")
    cs.add_argument("--proto", choices=["rtsp", "local"], default="rtsp")
    cs.add_argument("--auth-token",
                     default=os.environ.get("X2D_AUTH_TOKEN", ""))
    cs.add_argument("--idle-timeout", type=float, default=30.0)
    cs.add_argument("--foreground", action="store_true",
                     help="Don't fork; run in the foreground (handy for "
                          "debugging — Ctrl+C to stop).")

    cstop = cam_sub.add_parser(
        "stop",
        help="Stop the running camera proxy via its PID file.",
    )
    cstop.add_argument("--signal", default="TERM",
                        help="Signal to send (default TERM; SIGKILL via KILL)")

    p.set_defaults(fn=cmd_cam)
    return p


_CAM_PID_FILE = Path.home() / ".x2d" / "cam.pid"
_CAM_LOG_FILE = Path.home() / ".x2d" / "cam.log"


def _do_snap(url: str, out_path: str | None) -> int:
    out = Path(out_path or "cam.jpg").expanduser()
    try:
        data = fetch_jpeg(url)
    except Exception as e:                                  # noqa: BLE001
        print(f"snapshot failed: {e}", file=sys.stderr)
        return 1
    out.write_bytes(data)
    print(f"saved {len(data):,} B → {out}")
    return 0


def _cmd_cam_start(args: argparse.Namespace) -> int:
    """Background-spawn `beambam camera` with the forwarded flags.
    Writes PID + log path to ~/.x2d/cam.pid so `cam stop` finds it."""
    import subprocess
    import x2d_bridge
    bridge = Path(x2d_bridge.__file__)

    # Block double-start.
    if _CAM_PID_FILE.exists():
        try:
            old_pid = int(_CAM_PID_FILE.read_text().strip())
            # /proc/<pid> check works on Linux/Android. On macOS we
            # fall back to os.kill(pid, 0) which raises ESRCH if dead.
            alive = False
            try:
                os.kill(old_pid, 0)
                alive = True
            except OSError:
                alive = False
            if alive:
                print(f"cam proxy already running (pid {old_pid}); "
                      f"use `beambam cam stop` first", file=sys.stderr)
                return 1
        except (OSError, ValueError):
            pass    # stale pidfile — ignore + overwrite

    argv = ["camera",
            "--bind",  args.bind,
            "--port",  str(args.port),
            "--proto", args.proto,
            "--idle-timeout", str(args.idle_timeout)]
    if args.skip_check:  argv.append("--skip-check")
    if args.auth_token:  argv += ["--auth-token", args.auth_token]

    if args.foreground:
        # Foreground mode: just exec into x2d_bridge.main with the
        # remapped argv so Ctrl+C semantics are unchanged.
        saved = sys.argv[:]
        try:
            sys.argv = ["x2d_bridge.py", *argv]
            return x2d_bridge.main()
        finally:
            sys.argv = saved

    _CAM_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = _CAM_LOG_FILE.open("ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(bridge), *argv],
            stdout=log_fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_fh.close()
    _CAM_PID_FILE.write_text(str(proc.pid) + "\n")
    print(f"cam proxy started (pid {proc.pid}), bind {args.bind}; "
          f"logs → {_CAM_LOG_FILE}")
    return 0


def _cmd_cam_stop(args: argparse.Namespace) -> int:
    """Read PID file, signal the running proxy, remove the file."""
    import signal
    if not _CAM_PID_FILE.exists():
        print("no PID file at " + str(_CAM_PID_FILE) +
              " — cam proxy isn't running (or wasn't started via "
              "`cam start`).", file=sys.stderr)
        return 1
    try:
        pid = int(_CAM_PID_FILE.read_text().strip())
    except (OSError, ValueError) as e:
        print(f"bad PID file: {e}", file=sys.stderr); return 1
    sig_name = args.signal.upper().lstrip("SIG")
    try:
        sig = getattr(signal, "SIG" + sig_name)
    except AttributeError:
        print(f"unknown signal: {args.signal!r}", file=sys.stderr); return 2
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        print(f"pid {pid} not running (stale PID file); cleaning up",
              file=sys.stderr)
    except PermissionError as e:
        print(f"can't signal pid {pid}: {e}", file=sys.stderr); return 1
    else:
        print(f"sent SIG{sig_name} → pid {pid}")
    try:
        _CAM_PID_FILE.unlink()
    except OSError:
        pass
    return 0


def cmd_cam(args: argparse.Namespace) -> int:
    # No subcommand → snapshot (the new default).
    if args.cam_cmd is None or args.cam_cmd == "snap":
        return _do_snap(args.url, getattr(args, "out", None))
    if args.cam_cmd == "watch":
        backend: Backend = (detect_backend() if args.backend == "auto"
                            else args.backend)
        return watch_loop(
            args.url, hz=args.hz, backend=backend,
            width_chars=args.width, max_frames=args.max_frames,
        )
    if args.cam_cmd == "start":
        return _cmd_cam_start(args)
    if args.cam_cmd == "stop":
        return _cmd_cam_stop(args)
    print(f"unknown cam subcommand: {args.cam_cmd}", file=sys.stderr)
    return 2
