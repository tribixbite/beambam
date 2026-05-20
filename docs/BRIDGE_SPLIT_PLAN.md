# Splitting `x2d_bridge.py` into modules (task #3) — deferred to v1.2.0

**Status:** parked. The Printer object (task #5), simulate (#10), cloud-fetch
(#11), and state hub (#12) don't actually require bridge.py to be split —
they can be implemented as new modules under `beambam/` that import the
existing flat layout. Splitting before those features ship would inflate
v1.1.0's diff without changing the public API and would risk regressing
the network_shim's IPC contract (`x2d_bridge.py serve`).

## Why not in v1.1.0

`x2d_bridge.py` is 6,275 lines and ~40 `cmd_*` handlers. Splitting it cleanly
requires:

1. Resolving forward references between handlers and shared helpers
   (`Creds`, `sign_payload`, `signed_publish`, `start_print`, the FTPS
   subclass + upload/download/list_files, the cloud HTTP client). Most
   handlers share 3–5 of these. A naive split produces circular imports
   without careful staging.

2. Updating every consumer:
   - `runtime/mcp/server.py`, `runtime/ha/publisher.py`,
     `runtime/queue/manager.py`, `runtime/webui/*`,
     `runtime/timelapse/recorder.py`, `runtime/assistant/router.py`,
     `runtime/colorsync/mapper.py`, `runtime/webrtc/server.py` all
     `import x2d_bridge` and reach into ~30 different symbols.
   - 10 `runtime/*/test_*.py` files do the same.
   - The `libbambu_networking.so` shim spawns `x2d_bridge.py serve`
     by literal pathname — changing the entry point breaks the GUI shim.

3. Ensuring the network_shim PROTOCOL.md contract stays byte-stable:
   the serve mode reads JSON-RPC from a Unix socket and replies with
   the same envelope. Refactoring `cmd_serve` requires running the
   `runtime/network_shim/tests/test_shim_e2e.py` end-to-end against
   a real printer.

## Recommended v1.2.0 split

```
src/beambam/
├── __init__.py            # re-exports public API (Printer, Creds, ...)
├── _version.py            # canonical __version__
├── config.py              # Creds, BAMBU_CERT_ID, env/credentials parsing
├── mqtt.py                # sign_payload, signed_publish, X2DClient
├── ftps.py                # _ImplicitFTPTLS, upload/download/list_files
├── cloud.py               # (alias to cloud_client; move impl here)
├── printer.py             # Printer high-level class (task #5)
├── analyze.py             # already done in v1.1.0 (task #7)
├── frame.py               # already done in v1.1.0 (task #14)
├── schemas.py             # TypedDicts (task #6)
├── slice.py               # x2d_slice impl moved
├── thumbnails.py          # inject_thumbnails impl
├── remix.py / preflight.py / profile.py / lan_print.py / lan_upload.py
├── cli/
│   ├── __init__.py        # `main()` argparse wiring
│   ├── status.py
│   ├── upload.py
│   ├── print.py
│   ├── pause_resume_stop.py
│   ├── gcode.py            # one file per related command group
│   ├── cloud_commands.py   # cloud-print, cloud-state, cloud-pause, ...
│   ├── serve.py            # Unix-socket RPC for libbambu_networking.so
│   ├── daemon.py           # HTTP + SSE + queue + timelapse aggregator
│   ├── analyze.py          # thin wrapper around beambam.analyze.cli_main
│   ├── frame.py            # thin wrapper around beambam.frame.cmd_frame
│   ├── ...
│   └── _helpers.py
└── runtime/                # ha/, mcp/, webui/, etc. — already namespaced
```

Each cli/*.py module is ≤300 lines and tests against its own fixtures.
The flat `x2d_bridge.py` at repo root becomes a 5-line shim:

```python
"""Backwards-compat shim. The implementation moved to beambam.cli.main()
in v1.2.0. New code should `import beambam.cli` or use the `beambam`
console script."""
from beambam.cli import main, sign_payload, BAMBU_CERT_ID, Creds, \
    upload_file, download_file, list_files, start_print
if __name__ == "__main__":
    raise SystemExit(main())
```

## Migration order (for v1.2.0)

1. Move `Creds` + `_resolve_credentials` → `beambam/config.py`
2. Move `BAMBU_CERT_PEM` + `sign_payload` + `signed_publish` → `beambam/mqtt.py`
3. Move FTPS subclass + upload/download/list → `beambam/ftps.py`
4. Update `x2d_bridge.py` to `from beambam.config import ...` etc.
   (each step keeps the suite green)
5. Move `cmd_*` handlers one at a time to `beambam/cli/<verb>.py`
6. Last: move `main()` argparse wiring to `beambam/cli/__init__.py`
7. Verify `python3 x2d_bridge.py status` still works (the shim path)
8. Verify `libbambu_networking.so` E2E test still passes

Total estimated effort: ~6 hours focused work + 1 hour testing per
real printer surface (status, print, daemon, serve).

## What v1.1.0 ships without it

- ✅ `pip install beambam` works
- ✅ `beambam analyze`, `beambam frame` are new modules in beambam/
- ✅ `beambam.Printer` (task #5) lives in beambam/printer.py
- ✅ TypedDicts (task #6) in beambam/schemas.py
- ✅ `beambam simulate` (task #10) in beambam/simulate.py
- ✅ `beambam cloud-fetch` (task #11) — added to cloud_client.py + CLI
- ✅ State hub (task #12) in beambam/runtime/state_hub.py
- 📦 x2d_bridge.py stays monolithic. The new beambam/* modules are
  the public Python API; x2d_bridge.py remains the CLI entry point.

This delivers all functional v1.1.0 goals without the split-risk.
