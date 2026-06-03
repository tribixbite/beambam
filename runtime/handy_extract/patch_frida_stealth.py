#!/usr/bin/env python3
"""patch_frida_stealth.py — anti-detection string patcher for frida-server.

Bambu Handy's Promon SHIELD packer forks a ptrace watchdog during early
library init that scans the app's /proc/<pid>/maps and task/*/comm for the
tokens "frida" and "gum"; on a hit it forces PC to 0xdead50xx (crash).

The only frida footprint reaching those observable surfaces (verified by
spawning a benign app under the unpatched server and reading its /proc):

  maps : /memfd:frida-agent-64.so (deleted)        ← "frida"
  comm : gum-js-loop, pool-frida                   ← "gum","frida"

This patcher does SAME-LENGTH (offset-preserving, so the ELF stays valid
without re-linking) byte replacements of the detectable ASCII tokens with
innocuous look-alikes, so the watchdog's grep finds nothing and completes
its handshake — the app then boots normally WITH our SSL_write hook live.

We keep the binary at frida 17.9.3 (matches the host frida-python in use);
only the surface strings change, never code/offsets.

Replacements (each pair is equal length):
  frida        -> monco        (covers frida-agent-64.so, pool-frida,
                                frida:rpc, re.frida.server, frida-server)
  gum-js-loop  -> qnx-js-loop  (the only "gum" that surfaces as a comm name)
  gmain        -> gpain        (GLib default thread name; harmless but cheap)
  gdbus        -> gdbqs        (ditto)

"frida" is unique enough that it never appears as a substring of an
unrelated required string, so a blanket replace is safe. "gum" is NOT
(e.g. "argument"), so we only patch the exact "gum-js-loop" token, never
bare "gum".
"""
from __future__ import annotations

import sys
from pathlib import Path

# (needle, replacement) — MUST be equal length.
#
# SURGICAL set only: a blanket "frida"->x replace corrupts GResource lookups
# because the embedded gresource hash table stores BUILD-TIME djb2 hashes of
# the original path strings; renaming the path bytes (but not the hashes)
# makes g_resources_lookup miss -> "backend_class != null" assertion at start.
#
# So we touch ONLY strings that surface in the TARGET process's observable
# /proc and are NOT gresource keys / GType names:
#   * frida-agent-64.so   — the memfd_create() display name (shows in
#                           /proc/<pid>/maps as "/memfd:frida-agent-64.so").
#                           The embedded agent BLOB is keyed by the SEPARATE
#                           string "frida-agent-arm64.so" (left intact), so
#                           the blob still loads.
#   * frida-agent-32.so   — 32-bit memfd display name (harmless on arm64).
#   * gum-js-loop         — pthread_setname_np() literal in the agent (the
#                           JS loop thread; shows in /proc/<pid>/task/*/comm).
# The thread-pool name "pool-frida" derives from g_get_prgname() (argv[0]),
# so renaming the server binary to "msrv" makes it "pool-msrv" with no patch.
PATCHES: list[tuple[bytes, bytes]] = [
    (b"frida-agent-64.so", b"monco-agent-64.so"),
    (b"frida-agent-32.so", b"monco-agent-32.so"),
    (b"gum-js-loop",       b"qnx-js-loop"),
]


def main() -> int:
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} <in> <out>")
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    data = bytearray(src.read_bytes())

    for needle, repl in PATCHES:
        assert len(needle) == len(repl), f"length mismatch {needle!r}->{repl!r}"
        count = 0
        start = 0
        while True:
            i = data.find(needle, start)
            if i < 0:
                break
            data[i:i + len(needle)] = repl
            count += 1
            start = i + len(needle)
        print(f"  {needle.decode():<12} -> {repl.decode():<12}  {count} hit(s)")

    # Sanity: confirm the SURFACE tokens are gone; gresource/GType "frida"
    # strings are intentionally LEFT (they never reach the target's /proc).
    for tok in (b"frida-agent-64.so", b"gum-js-loop"):
        n = data.count(tok)
        print(f"  residual surface {tok.decode():<18}: {n}")
    # These SHOULD remain (proves we didn't nuke gresource):
    print(f"  preserved frida-agent-arm64.so : {data.count(b'frida-agent-arm64.so')}")
    print(f"  preserved /re/frida (gresource): {data.count(b'/re/frida')}")

    dst.write_bytes(bytes(data))
    print(f"wrote {dst} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
