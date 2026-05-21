# Splitting `x2d_bridge.py` into modules — current state + actionable phases

**Last touched:** 2026-05-21. **State:** Phases 1–3 shipped in v1.2.0;
Phase 4 + 5 are the active work and have been wrongly marked "blocked"
in prior versions of this doc. They are not blocked — Phase 4 is one
small commit, and Phase 5 is a stack of bounded sub-phases that can
each land independently.

## Why this plan exists

`x2d_bridge.py` has grown from ~6,275 LoC at v1.0.0 to **~7,800 LoC**
with **74 `cmd_*` handlers** today. The split is the boring-but-important
work that keeps the public API (`beambam.Printer`, `beambam.Creds`,
etc.) clean while the CLI continues to gain features. Without active
phase-by-phase drainage, every new subcommand piles into the monolith
instead of `beambam/cli/`, and the monolith never shrinks.

## What's already done (Phase 1–3, shipped in v1.2.0)

| Symbol(s) | New home | Verification |
|---|---|---|
| `Creds`, env / file resolver | `beambam/config.py` | `__all__ = ["Creds"]`, x2d_bridge re-imports. |
| `BAMBU_CERT_ID`, `sign_payload` | `beambam/mqtt.py` | Sign + verify works against firmware. |
| `_ImplicitFTPTLS`, `upload_file`, `download_file`, `list_files` | `beambam/ftps.py` | `pytest tests/test_ftps_*.py` passes. |
| `Printer` high-level facade | `beambam/printer.py` | Used by `beambam ams set`, `beambam.Printer.state()`. |
| Per-feature modules: `analyze`, `frame`, `slice`, `simulate`, `state_hub`, `cam`, `find`, `init_wizard`, `install_completion`, `upgrade`, `plate`, `orient`, `download`, `cloud_data`, `cloud_fetch`, `filament_profiles`, `doctor`, `ams`, `mqttcli`, `queuecli`, `configcli`, `schemas` | `beambam/*.py` | Each has its own test file. |

`beambam/mqtt.py` exposes `X2DClient` via `__getattr__` as a lazy
re-export from x2d_bridge — that's the seam Phase 4 closes.

## Phase 4 — move `X2DClient` into `beambam.mqtt` (single commit)

**Why this phase is not "blocked on `_metric_inc` extraction":**
`_metric_inc` is a 10-line counter at `x2d_bridge.py:131`. It can
move with `X2DClient` in the same commit. Calling it a separate
blocker was an over-cautious framing — there's no other consumer of
`_metric_inc` outside `X2DClient`.

**Step list (estimated 30–45 min, no live-printer required):**

1. Cut `_metric_inc` + the `_METRICS` dict + helper constants from
   `x2d_bridge.py` and paste verbatim into `beambam/mqtt.py`.
2. Cut the `X2DClient` class from `x2d_bridge.py` (lines ~148–390 in
   today's file) and paste into `beambam/mqtt.py`. Imports already
   work: `paho.mqtt`, `cryptography`, `Creds` (from `beambam.config`),
   `sign_payload` (same file), `BAMBU_CERT_ID` (same file).
3. Delete the `__getattr__("X2DClient")` lazy-import shim at the bottom
   of `beambam/mqtt.py`. Add `"X2DClient"` to `__all__`.
4. In `x2d_bridge.py`, add `from beambam.mqtt import X2DClient, sign_payload, BAMBU_CERT_ID`
   at the top of the file. Verify every callsite finds it via the
   import (there are ~30; `grep -nE "\bX2DClient\b" x2d_bridge.py`).
5. Run `pytest -q`. Aside from the cmd_* count moving by 0, the suite
   should pass unchanged.

**Risk:** very low. `X2DClient` has no incoming runtime dependencies
from the cmd_* handlers other than via constructor; moving it doesn't
disturb the daemon's startup order.

## Phase 5 — drain `cmd_*` into `beambam/cli/`

74 `cmd_*` handlers, ~5,800 LoC of `cmd_*` code, plus the `_serve_http`
HTTP server (~1,200 LoC). The diff is too big for one PR. Phases 5a–e
are independent and each ≤300 LoC of net move (i.e. functions are cut
from `x2d_bridge.py`, pasted into `beambam/cli/<group>.py`, and the
`sub.add_parser(...)` call in `main()` is updated to import the
handler from its new home).

### Phase 5a — `beambam/cli/_helpers.py` + `beambam/cli/control.py`

**Move:** the `_print_cmd`, `_system_cmd`, `_camera_cmd`, `_publish_one`,
`_next_seq` helpers to `beambam/cli/_helpers.py`. Then move the
control-verb handlers (each is 5–15 lines, all wrap one of those
helpers): `cmd_pause`, `cmd_resume`, `cmd_stop`, `cmd_reboot`,
`cmd_gcode`, `cmd_home`, `cmd_level`, `cmd_set_temp`, `cmd_chamber_light`,
`cmd_jog`, `cmd_fod_check`, `cmd_ams_load`, `cmd_ams_unload`,
`cmd_record`, `cmd_timelapse`, `cmd_resolution`, `cmd_files`.

**Expected LoC move:** ~600 in, ~50 deletion in x2d_bridge.py for the
parser registrations (which become 1-line `from beambam.cli.control import register; register(sub)`).

### Phase 5b — `beambam/cli/cloud.py`

**Move:** every `cmd_cloud_*` handler (currently ~30, ~2,000 LoC).
Already a natural cluster — every cloud handler uses
`_resolve_cloud_serial` + `_cloud_publish_payload` from
`x2d_bridge.py`. Both helpers move with them.

**Live-test surface:** any cloud handler that publishes to a real
account (`cloud-pause`, `cloud-stop`, `cloud-chamber-light`,
`cloud-comment-reply`). Should be live-tested but is opt-in.

### Phase 5c — `beambam/cli/info.py`

**Move:** read-only / observability commands: `cmd_status`, `cmd_health`,
`cmd_watch`, `cmd_tail`, `cmd_notify`, `cmd_printers`, `cmd_files`,
`cmd_fetch`, `cmd_files`. None of these touch the daemon's long-running
state; they're one-shot.

### Phase 5d — `beambam/cli/daemon.py`

**Move:** `cmd_daemon`, `cmd_serve`, `cmd_camera`, `cmd_webrtc`,
`cmd_ha_publish`, plus the entire `_serve_http` function (~1,200 LoC).
This is the load-bearing piece and the largest sub-phase. Move LAST,
after the rest of the migration has reduced x2d_bridge.py to a
manageable size.

**Critical:** `libbambu_networking.so` spawns `x2d_bridge.py serve` by
literal pathname. The shim at `x2d_bridge.py` MUST keep working as
the entry point. The actual `cmd_serve` body moves; the parser entry
stays callable through the shim.

### Phase 5e — `main()` to `beambam/cli/__init__.py`

**Move:** the argparse builder + the discover-and-dispatch loop.
`x2d_bridge.py` becomes the ~5-line shim shown below. `beambam` and
`bb` console-script entries already point at `x2d_bridge:main`; they
get updated to `beambam.cli:main` in `pyproject.toml`.

**Final x2d_bridge.py:**

```python
"""Backwards-compat shim — implementation moved to beambam.cli.main()
in v1.3.x. New code should `import beambam.cli` or run `beambam`/`bb`.

The libbambu_networking.so GUI shim spawns this file by literal
pathname; keep the entry point byte-stable."""
from beambam.cli import main
if __name__ == "__main__":
    raise SystemExit(main())
```

## Migration discipline — the guard test

`tests/test_bridge_split_progress.py` pins the current `cmd_*` count
in `x2d_bridge.py` and asserts it MUST NOT grow. The test fails with
a pointer to this doc whenever a new feature lands in the monolith
instead of `beambam/cli/`. Bumping the budget downward (after a
successful sub-phase) is a deliberate edit; bumping it upward requires
deleting the test or adding the lying number — both visible in PR
review.

**Today's pinned count:** 74. **Target after Phase 4 + 5a:** ~57.
**Target after Phase 5b:** ~27. **Target after Phase 5c–e:** 0
(`x2d_bridge.py` is the shim).

## Why I don't propose a single big-bang split

A single PR moving 7,000 LoC is unreviewable. Each Phase 5 sub-phase
is independently verifiable: full suite green + relevant live test
(if any) passes. If a sub-phase breaks anything, the revert is one
commit, not "the entire split".

## What's still allowed to live in x2d_bridge.py during the migration

* `start_print()` — used by `lan_print.py`, `lan_upload.py`, slicing
  helpers. Move with Phase 5a (the helpers don't shadow it).
* The argparse `main()` and `_build_epilog()` — Phase 5e's job.
* Backwards-compat re-exports — keep until v2.0 cut.
