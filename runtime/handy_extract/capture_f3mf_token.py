#!/usr/bin/env python3
"""capture_f3mf_token.py — host-side runner for capture_f3mf_token.js.

Spawns Bambu Handy with Frida, loads the hook, prints any captured
/f3mf requests, and writes the parsed headers to:

    ./captured_tokens/<unix-ts>.txt          — raw header dump per hit
    ~/.x2d/handy_token.json                  — most-recent valid set,
                                                consumed by beambam's
                                                cloud_client.

The captured `Authorization`, `Cookie`, and any `X-BBL-*` headers are
what bypass Bambu's anti-bot captcha when fetching /api/v1/
design-service/instance/<id>/f3mf?type=download.

Limitation: tokens are short-lived (Bambu uses ~30 min TTLs and rotates
on certain triggers — login, app foreground, etc.). Re-capture when
beambam starts returning HTTP 418 again.

Bootstrap (assumes setup_rooted_device.sh has run):

    adb forward tcp:27042 tcp:27042
    python3 capture_f3mf_token.py
    # In Handy: open ANY MakerWorld design → tap Download
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Hook script is overridable for iterating on alternate hooks (e.g. the
# anon-memory SSL_write scanner) without touching the capture logic.
HOOK_JS = Path(os.environ.get("F3MF_HOOK_JS") or (HERE / "capture_f3mf_token.js"))
CAPTURES_DIR = HERE / "captured_tokens"
HOME_TOKEN = Path.home() / ".x2d" / "handy_token.json"

APP_ID = "bbl.intl.bambulab.com"

# Headers we treat as "carries auth" — only these are stored to
# ~/.x2d/handy_token.json for beambam to replay. Everything else
# (Accept, User-Agent, etc.) is logged but not exported.
AUTH_HEADER_PREFIXES = (
    "authorization:",
    "cookie:",
    "x-bbl-",
    "x-bambu-",
    "x-jiange-",      # Bambu's internal service identifier
    "x-csrf-",
    "x-amz-",
)


def parse_headers(raw: str) -> dict[str, str]:
    """Pull the auth-relevant headers out of a raw HTTP request block."""
    out: dict[str, str] = {}
    lines = raw.split("\r\n")
    if not lines:
        return out
    # First line is the request line: "GET /path HTTP/1.1"
    out["__request_line"] = lines[0]
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key_lower = key.strip().lower()
        if any(key_lower.startswith(p) for p in AUTH_HEADER_PREFIXES):
            out[key.strip()] = value.strip()
    return out


def on_message(message: dict, _data: bytes | None) -> None:
    # Surface JS-side errors (frida sends type=="error" with a stack) so
    # script-eval failures don't fail silently.
    if message.get("type") == "error":
        print(f"[JS ERROR] {message.get('description', '')}")
        stk = message.get("stack")
        if stk:
            print(f"           {stk.splitlines()[0] if stk else ''}")
        return
    if message.get("type") != "send":
        return
    body = message.get("payload") or {}
    kind = body.get("type")
    if kind == "log":
        print(f"[hook] {body.get('msg', '')}")
        return
    if kind != "f3mf_request":
        return

    cap = body.get("body") or {}
    raw = cap.get("headers") or ""
    ts = int(cap.get("timestamp") or time.time())
    parsed = parse_headers(raw)

    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    fname = CAPTURES_DIR / f"{ts}.txt"
    fname.write_text(
        f"# captured via {cap.get('label')} @ {cap.get('addr')}\n"
        f"# captured_at: {ts}  size: {cap.get('num_bytes')} B\n"
        f"\n{raw}\n"
    )
    print(f"\n=== /f3mf request captured → {fname} ===")
    print(f"  request_line: {parsed.get('__request_line', '?')}")
    for k, v in parsed.items():
        if k == "__request_line":
            continue
        # Truncate long values (cookies can be huge)
        disp = v if len(v) < 120 else v[:117] + "..."
        print(f"  {k}: {disp}")

    # Persist to ~/.x2d/handy_token.json for beambam to reuse
    HOME_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    HOME_TOKEN.write_text(
        json.dumps(
            {
                "captured_at": ts,
                "request_line": parsed.get("__request_line", ""),
                "headers": {k: v for k, v in parsed.items()
                            if k != "__request_line"},
            },
            indent=2,
        )
    )
    print(f"  → wrote {HOME_TOKEN}")


def main() -> int:
    if not HOOK_JS.exists():
        sys.exit(f"missing {HOOK_JS} — capture_f3mf_token.js must be "
                 f"next to this script")
    try:
        import frida
    except ImportError:
        sys.exit("frida not installed. `pip install frida` (matching "
                 "your frida-server version).")
    # Prefer USB; fall back to TCP socket (WiFi-adb / `adb forward`).
    dev = None
    try:
        dev = frida.get_usb_device(timeout=3)
    except Exception:                                       # noqa: BLE001
        pass
    if dev is None:
        try:
            # frida.get_remote_device() = first 'socket' (assumes
            # `adb forward tcp:27042 tcp:27042` is up)
            dev = frida.get_remote_device()
        except Exception as e:                              # noqa: BLE001
            sys.exit(f"no frida device (USB nor remote socket). "
                     f"Run `adb forward tcp:27042 tcp:27042` first. {e}")

    print(f"[runner] attaching to {APP_ID} via {dev.name}")
    pid = None
    session = None
    # Attach to the RUNNING process. Resolve the PID first via
    # enumerate_processes (works over the socket transport where
    # attach-by-name sometimes fails). Handy's SHIELD anti-debug checks
    # ptrace at fork-from-zygote, so late-attach to an existing pid
    # usually survives where spawn() is killed.
    # Strategy: SPAWN by default. Frida injects the agent at the
    # zygote-fork boundary, BEFORE the SHIELD packer's tamper-response
    # arms — so the Stalker syscall guard installs in time. Late-attach
    # to an already-armed process times out during agent sync ("failed
    # to sync up with agent"). Pass FRIDA_ATTACH=1 to force late-attach.
    force_attach = os.environ.get("FRIDA_ATTACH") == "1"
    if force_attach:
        target_pid = None
        try:
            for p in dev.enumerate_processes():
                if p.name == APP_ID or APP_ID in (p.name or ""):
                    target_pid = p.pid
                    break
        except Exception as e:                              # noqa: BLE001
            print(f"[runner] enumerate_processes failed: {e}",
                  file=sys.stderr)
        if target_pid is None:
            sys.exit(f"{APP_ID} not running (FRIDA_ATTACH=1 needs it up)")
        session = dev.attach(target_pid)
        pid = target_pid
        print(f"[runner] late-attached to pid {target_pid}")
    else:
        # Kill any running instance so the spawn is a true cold start.
        try:
            for p in dev.enumerate_processes():
                if p.name == APP_ID:
                    dev.kill(p.pid)
                    print(f"[runner] killed running pid {p.pid} for "
                          f"clean spawn")
        except Exception:                                  # noqa: BLE001
            pass
        print(f"[runner] spawning {APP_ID} (gated)")
        # Android spawn API takes the package string, NOT a list.
        pid = dev.spawn(APP_ID)
        session = dev.attach(pid)
        print(f"[runner] spawned + attached pid {pid}")

    # Concatenate stalker_syscalls.js (same guard dump_keys.py uses): it
    # rewrites every basic block to neutralise the SHIELD packer's raw
    # `svc 0` tamper-response syscalls (exit_group/kill/tgkill/ptrace/
    # seccomp) that otherwise kill the Frida agent mid-sync ("unexpectedly
    # timed out trying to sync up with agent"). Self-disables after 8 s.
    # F3MF_NO_STALKER=1 skips the heavy Stalker guard — used by the
    # libc-fork-block approach (scan_anon_ssl.js arms an Interceptor
    # fork-block at spawn-gate, before .ss/ loads, so the raw-svc Stalker
    # guard isn't needed and its main-thread following — which killed
    # earlier runs — is avoided).
    script_src = HOOK_JS.read_text()
    if os.environ.get("F3MF_NO_STALKER") != "1":
        stalker_js = HOOK_JS.parent / "stalker_syscalls.js"
        if stalker_js.exists():
            script_src += "\n\n// === stalker_syscalls.js (concatenated) ===\n"
            script_src += stalker_js.read_text()
    else:
        print("[runner] F3MF_NO_STALKER=1 — Stalker guard skipped "
              "(libc fork-block only)", file=sys.stderr)
    # V8 runtime — matches dump_keys.py; needed for the Java bridge if the
    # stalker guard touches Conscrypt, and is the known-working config.
    script = session.create_script(script_src, runtime="v8")
    script.on("message", on_message)
    script.load()
    # In spawn mode the process is suspended at entry — resume it now
    # that the Stalker guard + SSL hook are installed. (Late-attach mode
    # via FRIDA_ATTACH=1 leaves pid None-ish; only resume on spawn.)
    if not force_attach and pid is not None:
        try:
            dev.resume(pid)
        except Exception as e:                              # noqa: BLE001
            print(f"[runner] resume failed: {e}", file=sys.stderr)

    # The in-app Stalker guard starves Frida's JS timer loop, so we drive
    # the SSL_write rescan from here (Python is not instrumented). Poll
    # every second — each call re-hunts SSL_write across freshly-loaded
    # modules and emits the one-time DIAG.
    import threading as _threading

    def _poll_rescan() -> None:
        # The hook script exposes either rescan() (capture) or scan()
        # (anon-memory scanner). Call whichever is present.
        ex = script.exports_sync
        fn = getattr(ex, "rescan", None) or getattr(ex, "scan", None)
        if fn is None:
            return
        while True:
            try:
                fn()
            except Exception:                              # noqa: BLE001
                return  # script unloaded / process gone
            time.sleep(1.0)

    _t = _threading.Thread(target=_poll_rescan, daemon=True)
    _t.start()
    print(f"[runner] hook loaded; PID={pid}; "
          f"open ANY MakerWorld design → Download to trigger capture")
    print(f"[runner] captures dir: {CAPTURES_DIR}")
    print(f"[runner] token export: {HOME_TOKEN}")
    print(f"[runner] Ctrl-C to stop\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[runner] detaching")
        try:
            session.detach()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
