#!/usr/bin/env python3
"""extract_token_memscan.py — pull Bambu auth material from a running Handy's
RAM, as root, WITHOUT instrumenting the app (no Frida → no SHIELD tamper trip).

Promon SHIELD defeats in-process instrumentation by fork+exec-escaping the
Frida agent, but it cannot stop a ROOT reader of /proc/<pid>/mem (kernel
mediated). Bambu Handy decrypts its access token (an "eyJ…" JWT) out of
flutter_secure_storage into the heap to sign every cloud API call, and the
assembled HTTPS request blocks (Authorization: Bearer …, x-bbl-*, Cookie …)
live in the BoringSSL / OkHttp write buffers. We scan the app's rw anonymous
regions on-device (only matches cross adb, so 1.7 GB scans in seconds) and
surface JWTs + auth-header fragments.

Usage:
  python3 extract_token_memscan.py [package] [--serial 192.168.0.201:39211]

Output: prints unique JWT/Bearer/auth-header candidates and writes the richest
full HTTP request header block found to ~/.x2d/handy_token.json (same shape
cloud_client replays), if one is present in memory.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PKG = "bbl.intl.bambulab.com"
HOME_TOKEN = Path.home() / ".x2d" / "handy_token.json"
DEVICE_SCRIPT = "/data/local/tmp/x2d_memscan.sh"
DEVICE_OUT = "/data/local/tmp/x2d_memscan.out"

# Extended-regex patterns handed to the device's toybox `grep -aoE`. Covers
# JWTs (url-safe AND standard base64), the makerworld session Cookie, and the
# Bambu request headers (x-bbl-*, x-jiange-*) that gate /f3mf.
PATTERNS = [
    r"eyJ[A-Za-z0-9_/+-]{8,}\.[A-Za-z0-9_/+=-]{8,}\.[A-Za-z0-9_/+=-]{4,}",  # JWT
    r"[Aa]uthorization: ?[Bb]earer [!-~]{16,}",
    r"[Cc]ookie: ?[!-~]{12,}",
    r"[Ss]et-[Cc]ookie: ?[!-~]{12,}",
    r"x-bbl-[A-Za-z0-9-]+: ?[!-~]+",
    r"x-jiange-[A-Za-z0-9-]+: ?[!-~]+",
    r"x-csrf[A-Za-z0-9-]*: ?[!-~]+",
    r"(GET|POST|PUT|DELETE) /[!-~]* HTTP/[12]",
    r"[Hh]ost: ?[A-Za-z0-9.-]*(bambu|makerworld)[A-Za-z0-9.-]*",
    # NOTE: patterns must contain NO single-quote — the device script wraps
    # the whole alternation in '...' for grep -aoE, and a literal ' would
    # terminate the shell quoting and corrupt the command on every region.
    r"access[_-]?[Tt]oken.{0,4}[!-~]{16,}",
    r"refresh[_-]?[Tt]oken.{0,4}[!-~]{16,}",
    r"/design-service/[!-~]*f3mf[!-~]*",
]


def adb(serial: str, *args: str, binary: bool = False):
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    raw = subprocess.run(cmd, capture_output=True).stdout
    if binary:
        return raw
    # /proc paths can contain non-UTF8 bytes; decode leniently.
    return raw.decode("utf-8", errors="replace")


def shell_su(serial: str, script: str, binary: bool = False):
    """Run a command as root on the device via `su -c`."""
    return adb(serial, "exec-out", "su", "-c", script, binary=binary)


def main() -> int:
    serial = os.environ.get("ANDROID_SERIAL", "")
    pkg = PKG
    argv = sys.argv[1:]
    while argv:
        a = argv.pop(0)
        if a == "--serial":
            serial = argv.pop(0)
        else:
            pkg = a

    # pidof can return several pids (app + a SHIELD child); the main app is
    # the lowest pid (earliest-started). Pick it deterministically.
    raw_pids = (shell_su(serial, f"pidof {pkg}") or "").split()
    pid = min(raw_pids, key=int) if raw_pids else ""
    if not pid:
        sys.exit(f"{pkg} not running — launch it (and log in) first")
    print(f"[memscan] target {pkg} pid={pid} on {serial or 'default'}")

    maps = shell_su(serial, f"cat /proc/{pid}/maps")
    regions: list[tuple[int, int]] = []
    total = 0
    for line in maps.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        addr, perm = parts[0], parts[1]
        if "rw" not in perm or "-" not in addr:
            continue
        # Skip device-backed / special regions; keep anonymous + heap.
        path = parts[5] if len(parts) >= 6 else ""
        if path.startswith("/dev/") or path.startswith("/system/fonts"):
            continue
        try:
            lo_s, hi_s = addr.split("-")
            lo, hi = int(lo_s, 16), int(hi_s, 16)
        except ValueError:
            continue
        regions.append((lo, hi - lo))
        total += hi - lo
    print(f"[memscan] {len(regions)} rw regions, {total/1024/1024:.0f} MB")

    # Build an on-device scanner: dd each page-aligned region from
    # /proc/<pid>/mem (root) and grep for the patterns; only matches (tiny)
    # leave the device. conv=noerror,sync tolerates guard pages inside a
    # region. PAGE=4096.
    assert all("'" not in p for p in PATTERNS), \
        "patterns must not contain ' (breaks the device-side single-quoting)"
    pat = "|".join(PATTERNS)
    # Fast path: toybox dd supports iflag=skip_bytes,count_bytes, so we read
    # each region at its EXACT byte offset in 1 MiB chunks (256x fewer reads
    # than page-by-page). conv=noerror,sync tolerates reserved/uncommitted
    # pages inside a region (Dart/ART heaps over-reserve).
    lines = ["#!/system/bin/sh", f"PID={pid}", f': > {DEVICE_OUT}']
    for lo, ln in regions:
        lines.append(
            f"dd if=/proc/$PID/mem bs=1048576 iflag=skip_bytes,count_bytes "
            f"skip={lo} count={ln} conv=noerror,sync 2>/dev/null | "
            f"grep -aoE '{pat}' >> {DEVICE_OUT} 2>/dev/null")
    script = "\n".join(lines) + "\n"

    local_script = Path(os.environ.get("PREFIX", "/tmp")) / "tmp" / "x2d_memscan.sh"
    local_script.parent.mkdir(parents=True, exist_ok=True)
    local_script.write_text(script)
    adb(serial, "push", str(local_script), DEVICE_SCRIPT)
    shell_su(serial, f"chmod 755 {DEVICE_SCRIPT}")
    print(f"[memscan] scanning {len(regions)} regions on-device…")
    t0 = time.time()
    shell_su(serial, f"sh {DEVICE_SCRIPT}")
    out = shell_su(serial, f"cat {DEVICE_OUT}")
    print(f"[memscan] done in {time.time()-t0:.1f}s")

    hits = [h for h in out.splitlines() if h.strip()]
    uniq = sorted(set(hits), key=len, reverse=True)
    jwts = sorted({h for h in uniq if h.startswith("eyJ")}, key=len, reverse=True)

    print(f"\n[memscan] {len(hits)} raw hits, {len(uniq)} unique")
    print(f"[memscan] {len(jwts)} distinct JWT candidate(s):")
    for j in jwts[:12]:
        print(f"   {j[:80]}{'…' if len(j) > 80 else ''}  (len {len(j)})")

    print("\n[memscan] non-JWT auth fragments:")
    for h in uniq:
        if not h.startswith("eyJ"):
            print(f"   {h[:140]}")

    # Persist the longest JWT (most likely the access token) for replay.
    if jwts:
        HOME_TOKEN.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if HOME_TOKEN.exists():
            try:
                existing = json.loads(HOME_TOKEN.read_text())
            except Exception:
                existing = {}
        existing["captured_at"] = int(time.time())
        existing["source"] = "memscan"
        existing.setdefault("headers", {})
        existing["headers"]["Authorization"] = f"Bearer {jwts[0]}"
        existing["jwt_candidates"] = jwts[:6]
        HOME_TOKEN.write_text(json.dumps(existing, indent=2))
        print(f"\n[memscan] wrote longest JWT → {HOME_TOKEN}")
    else:
        print("\n[memscan] no JWT found — app may not have made a cloud call "
              "yet; browse a MakerWorld model and re-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
