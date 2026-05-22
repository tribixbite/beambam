# Splitting `x2d_bridge.py` into modules — current state + remaining phases

**Last touched:** 2026-05-21 (post-Phase-5d). **State:** Phases 1–4
and 5a–5d **shipped**. Phase 5e is in flight (HTTP-server extraction
in batches). The monolith is down from ~7,800 LoC / 74 `cmd_*` to
**~3,470 LoC / 0 `cmd_*`** — see commit history of
`tests/test_bridge_split_progress.py` for the batch-by-batch ledger.

## Why this plan exists

The split is the boring-but-important work that keeps the public API
(`beambam.Printer`, `beambam.Creds`, etc.) clean while the CLI keeps
gaining features. Without active phase-by-phase drainage, every new
subcommand piles into the monolith instead of `beambam/cli/`, and the
monolith never shrinks. The `tests/test_bridge_split_progress.py`
guard test fails CI on any new `cmd_*` in `x2d_bridge.py`, so the
drainage is now ratchet-only.

## What shipped (Phases 1–5d, v1.2.0 → v1.3.0)

| Phase | Symbols moved | New home | Verification |
|---|---|---|---|
| **1–3** | `Creds`, env/file resolver | `beambam/config.py` | `__all__ = ["Creds"]` |
| **1–3** | `BAMBU_CERT_ID`, `sign_payload` | `beambam/mqtt.py` | sign+verify against firmware |
| **1–3** | `_ImplicitFTPTLS`, `upload_file`, `download_file`, `list_files` | `beambam/ftps.py` | tests pass |
| **1–3** | `Printer` high-level facade | `beambam/printer.py` | `beambam ams set/sync` uses it |
| **1–3** | Per-feature: `analyze`, `frame`, `slice`, `simulate`, `state_hub`, `cam`, `find`, `init_wizard`, `install_completion`, `upgrade`, `plate`, `orient`, `download`, `cloud_data`, `cloud_fetch`, `filament_profiles`, `doctor`, `ams`, `mqttcli`, `queuecli`, `configcli`, `schemas` | `beambam/*.py` (31 modules) | each has its own test file |
| **4** | `X2DClient` + `_metric_inc` + `_METRICS` | `beambam/mqtt.py` | `grep "class X2DClient" beambam/mqtt.py` |
| **5a** | `pause`/`resume`/`stop`/`gcode`/`home`/`level`/`set-temp`/`chamber-light`/`reboot`/`jog`/`record`/`timelapse`/`resolution`/`fod-check`/`ams-load`/`ams-unload` | `beambam/cli/control.py` | 16 handlers |
| **5b** | every `cmd_cloud_*` (~30) | `beambam/cli/cloud.py` | 2,050 LoC, every cloud verb |
| **5c** | `status`/`health`/`watch`/`tail`/`notify`/`printers`/`fetch`/`analyze`/`fcm-harvest` | `beambam/cli/info.py` | read-only handlers, zero coupling |
| **5d** (partial) | `daemon`, `serve` shell, `camera`, `webrtc`, `ha-publish`, parser registrations | `beambam/cli/daemon.py` | 930 LoC; cli/__init__.py wires sub-parsers |
| **5d** (partial) | LAN ops: `upload`, `print`, `files`, `slice-print` | `beambam/cli/lan.py` | 363 LoC |
| **5d** (partial) | HTTP helpers, preset loader, HTTP cloud routes | `beambam/serve_http_helpers.py`, `beambam/presets.py` | batch 2–4 extractions |

**Guard ratchet history** (full ledger in
`tests/test_bridge_split_progress.py`):
74 → 71 → 65 → 58 → 54 → 49 → 41 → 36 → 33 → 31 → 29 → 27 → 26 → 21
→ 20 → 19 → 18 → 16 → 14 → 12 → 10 → 8 → **0**.

## Phase 5e — what's left in the monolith

`x2d_bridge.py` is 3,470 LoC with **no CLI handlers** and **no public
class anyone imports by name**. What's left:

| Lines | Block | LoC | Target |
|---|---|---:|---|
| 1–117 | Module header, imports, `PACKAGE_VERSION` | ~100 | shim header only |
| 117–124 | `_signing_key()` (lazy private key loader) | ~10 | `beambam.mqtt` (already has `sign_payload`) |
| 218–1180 | `_serve_http()` body (~960 LoC) | ~960 | `beambam/serve_http.py` or `beambam/cli/daemon.py` |
| 1226–1900 | `_PrinterSession` + `ServeServer` + `_ConnHandler` | ~675 | `beambam/serve_socket.py` (new) |
| 1900–2210 | 14 `_op_*` handlers (`_op_hello`, `_op_connect_printer`, …) | ~310 | same `beambam/serve_socket.py` |
| 2210–2570 | `_publish_one`, `_reboot_payload`, `_package_version`, misc | ~360 | distribute |
| 2574–end | `main()` argparse builder | ~900 | `beambam/cli/__init__.py:main()` |

### Phase 5e batch list (smallest blast radius first)

1. **5e.1 — `beambam/_version.py`** (~30 LoC moved)
   * Pull `PACKAGE_VERSION` + `_package_version()` out. Used by ~6
     places (status banner, MCP server, daemon HTTP `/healthz`,
     `--version` flag, OG image gen, install-completion).
   * Risk: trivial. Imports flow downward, no cycles.

2. **5e.2 — `beambam/serve_socket.py`** (~1,000 LoC moved)
   * Move `_PrinterSession`, `ServeServer`, `_ConnHandler`, every
     `_op_*` handler. **This is the GUI-shim JSON-RPC server.**
   * Only consumer: `libbambu_networking.so` which spawns
     `python3 x2d_bridge.py serve` by literal pathname. The shim
     reaches `cmd_serve` (already in `beambam.cli.daemon`), which
     calls `ServeServer.run()`. Moving `ServeServer` only requires
     `cmd_serve` to import from the new location.
   * Risk: medium. `runtime/network_shim/tests/test_shim_e2e.py`
     should run against a real printer after. CI tests (which spawn
     the bridge as a subprocess) confirm the socket protocol stays
     byte-stable.

3. **5e.3 — `beambam/serve_http.py`** (~960 LoC moved)
   * Continue the batched extraction the other agent started:
     `_serve_http()` body becomes a thin dispatcher that imports
     per-route handlers from `beambam/serve_http_helpers.py` and
     `beambam/serve_http_routes/*.py`.
   * Risk: low — each route is independent; can be split across
     several commits using the same batch pattern as 5b.

4. **5e.4 — `_publish_one` + `_reboot_payload`** (~50 LoC moved)
   * `_publish_one` → `beambam/cli/_helpers.py` (already exists).
     Used by `cmd_chamber_light`, `cmd_ams_load`, etc. — handlers
     that already live in `beambam/cli/control.py` and import via
     `from x2d_bridge import _publish_one`.
   * `_reboot_payload` → `beambam/cli/control.py` (where `cmd_reboot`
     already lives). Drop the back-compat re-export from x2d_bridge.

5. **5e.5 — `main()` → `beambam/cli/__init__.py`** (~900 LoC moved)
   * Last big move. The argparse builder + subparser registrations
     + dispatcher loop. Most subparser bodies already live in
     `beambam/cli/<group>.py`'s `register(sub)` function — `main()`
     just calls them. Moving `main()` is mechanical.
   * Risk: low. `pyproject.toml` console-script entries
     (`beambam = x2d_bridge:main`, `bb = x2d_bridge:main`) get
     updated to `beambam = beambam.cli:main`. `x2d_bridge.py`
     keeps a `main = beambam.cli.main` re-export so the
     network_shim's literal-pathname spawn still finds it.

6. **5e.6 — `x2d_bridge.py` → 5-line shim** (~3,400 LoC deleted)
   * Final state:
     ```python
     """Backwards-compat shim. Implementation lives under beambam.cli.
     The libbambu_networking.so GUI shim spawns this file by literal
     pathname; keep the entry point byte-stable."""
     from beambam.cli import main
     if __name__ == "__main__":
         raise SystemExit(main())
     ```

## What the guard test enforces

`tests/test_bridge_split_progress.py` pins `cmd_*` at 0 and FAILS CI
if it grows. Every Phase 5e batch should also progressively shrink
`wc -l x2d_bridge.py` — consider extending the guard to track LoC
once batch 5e.6 lands, so the shim stays a shim.

## What the network_shim contract requires us to preserve

`runtime/network_shim/PROTOCOL.md` is the wire-level contract. The
JSON-RPC envelope on the Unix socket must stay byte-stable:

```json
{"kind": "req", "id": <int>, "op": "<verb>", "args": {...}}
{"kind": "rsp", "id": <int>, "ok": <bool>, "result": {...}, "error": {...}}
```

Moving the `_op_*` handlers into `beambam/serve_socket.py` doesn't
change the wire format — it just relocates the dispatch table. The
E2E test (`runtime/network_shim/tests/test_shim_e2e.py`) is the
regression gate; it spins up a real Unix socket against the actual
bridge and exercises the full op set.

## What is NOT changing in 5e

* The `beambam` and `bb` console-script entry points still work.
* `python3 x2d_bridge.py status` still works (via the shim).
* `libbambu_networking.so` still spawns `x2d_bridge.py serve` by
  pathname. The shim's `if __name__ == "__main__"` path stays valid.
* All public `from x2d_bridge import X2DClient, sign_payload, Creds,
  upload_file, download_file, list_files, start_print`-style imports
  keep working via re-exports — `runtime/*` doesn't have to change.
