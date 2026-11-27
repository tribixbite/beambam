# beambam — Agent & Developer Handbook

High-level guide for AI agents and developers working on beambam. It routes you to the
canonical spec for whatever you are changing, and names the invariants that are easy to break.
More-specific `AGENTS.md` / `CLAUDE.md` files override this one within their directory.

## 1. Project Overview

**beambam** drives a Bambu Lab printer (built and tested on the **X2D**, an H2D-class dual-nozzle
printer, model id `N6`) entirely from a local daemon — no cloud at print time. It:

- **slices** a user's 3MF / STL / OBJ on-device with a bundled Bambu Studio build (`bs-bionic`),
  re-targeting projects made for other printers to the X2D;
- **maps** the file's filaments to the AMS spools and nozzles, and **starts and watches** the print
  over the LAN with RSA-signed MQTT commands;
- serves a loopback **HTTP API** (`beambam daemon`) that the hosted web UIs **beambam.boo** and
  **pokefit.party** call to slice, map, paint, print and watch;
- integrates with **Home Assistant** (MQTT) and exposes an **MCP** server.

Two code trees:
- **`beambam/`** — the Python daemon, CLI and slice/print pipeline.
- **`site/`** — the SvelteKit web UI served at beambam.boo (`site/src/lib/print/` is the print app).

## 2. Instruction & Spec Routing

**Before changing files**, read [`CLAUDE.md`](CLAUDE.md) (hard-won environment + workflow rules),
[`ROADMAP.md`](ROADMAP.md) (what's left), and the spec for your work area:

| Work area | Canonical spec / doc |
|---|---|
| Slicing, profiles, start G-code, re-targeting, "printed air" | [`docs/specs/slice-pipeline.md`](docs/specs/slice-pipeline.md) |
| Colour painting on the web UI (tap-to-paint, paint_color) | [`docs/specs/colour-painting.md`](docs/specs/colour-painting.md) |
| Filament ↔ spool/nozzle mapping, project-sync, append | [`docs/specs/filament-mapping.md`](docs/specs/filament-mapping.md) |
| HTTP API (endpoints, capabilities, CORS) | [`docs/HTTP_API.md`](docs/HTTP_API.md) |
| Web UI (the beambam.boo print page) | [`docs/WEB_UI.md`](docs/WEB_UI.md), [`docs/specs/colour-painting.md`](docs/specs/colour-painting.md) |
| LAN control, signing, device cert | [`docs/LOCAL_CONTROL_PATHS.md`](docs/LOCAL_CONTROL_PATHS.md), [`docs/SIGNED_VS_UNSIGNED.md`](docs/SIGNED_VS_UNSIGNED.md) |
| Home Assistant | [`docs/HA.md`](docs/HA.md), [`docs/HA_SETUP.md`](docs/HA_SETUP.md) |
| X2D runtime (bs-bionic libs, GUI shims) | [`docs/X2D_RUNTIME_PIPELINE.md`](docs/X2D_RUNTIME_PIPELINE.md), [`CLAUDE.md`](CLAUDE.md) |
| Release, version, PyPI | ask first — never bump/tag/push a release without explicit permission |

Each spec follows [`docs/specs/SPEC_TEMPLATE.md`](docs/specs/SPEC_TEMPLATE.md). The spec **index**
is [`docs/specs/README.md`](docs/specs/README.md). User-facing pages (wiki-compatible) are under
[`docs/wiki/`](docs/wiki/). When you make a durable architecture or workflow decision, record it in
the relevant spec (not only in chat or a commit message).

## 3. Build, Run, Test

- **Daemon / CLI**: the `beambam` command is a wrapper → the repo via `PYTHONPATH` + system
  `python3` (not a `uv tool` install; `uv` can't rebuild cryptography on Termux). Run the daemon with
  `beambam daemon --http 127.0.0.1:8765`. beambam.boo and pokefit.party are allowed CORS origins by
  default.
- **Python tests**: `python3 -m pytest tests/` (on this box, prefix `env -u BUN_BINARY_PATH`). The
  real-slicer tests are gated behind `BEAMBAM_REAL_SLICER=1`; the HA tests need the `amqtt` broker
  (see `CLAUDE.md`). The full suite rewrites `docs/webui-*.png` — don't commit those.
- **Site**: from `site/`, `bun run build`, `bun ./scripts/check.ts` (svelte-check), `bun test src/lib`.
  Use `env -u BUN_BINARY_PATH bun` on this box (a running Claude Code leaks `BUN_BINARY_PATH`).
- **Slicing on-device**: `bs-bionic` slices for the X2D; it needs flattened `*_full` profile folders
  (`beambam.profile_flatten`), which the daemon builds automatically. See the slice-pipeline spec.

## 4. Invariants that are easy to break

- **A slice must load filament.** The sliced G-code must contain the machine's real start sequence
  with the AMS load (`M620`); a bare start G-code prints air. This depends on `profile_flatten`
  resolving `include` templates and the slice loading the **flattened** `machine_full` profile. Bump
  `FLATTEN_VERSION` when the flattener changes. See [slice-pipeline](docs/specs/slice-pipeline.md).
- **Filament arrays stay consistent.** Appending a filament (painted colour) must grow only genuine
  per-filament keys (`project_sync.FILAMENT_KEYS`); growing machine/process arrays corrupts the
  project. Always verify a filament change with a **real slice**, not only unit tests.
- **Signing is a setup prereq, not a runtime cloud dependency.** LAN print commands are RSA-signed
  with a key provisioned once via cloud login (`beambam key`); don't claim "no cloud account".
- **Commits**: beambam commits go directly to `main`; sign with an em-dash + model version; no
  "Claude" mention, no Co-Authored-By. Never push a release without explicit permission.

## 5. Repo map (orientation)

```
beambam/
  serve_http.py        HTTP API (daemon); CAPABILITIES advertised on /healthz
  slice_service.py     slice a project/mesh on-device (slice_project); machine_profile
  slice_options.py     X-Slice-Options, retarget(), mesh_args, machine_file()
  project_sync.py      adopt the spools' filaments into a project; append filaments (FILAMENT_KEYS)
  profile_flatten.py   resolve Bambu profiles into *_full/ (inherits + include)
  filament_mapping.py  filament ↔ AMS slot / nozzle; plate markers
  print_job.py / print_service.py   build + send the signed print command
  cli/                 argparse CLI (manhelp.py builds help; daemon.py hosts daemons)
site/
  src/lib/print/       the beambam.boo print app (load, slice, map, paint, print)
    paint*.ts, ColorPaint.svelte, Preview.svelte   colour painting (see its spec)
docs/specs/            canonical feature specs (this handbook routes to them)
docs/wiki/             user-facing, wiki-compatible pages
```
