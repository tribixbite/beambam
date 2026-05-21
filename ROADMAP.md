# beambam roadmap

Single canonical "what's left" file. Updated when work ships or is
deferred — if a task isn't here, in [IMPROVEMENTS.md](IMPROVEMENTS.md),
or one of the [docs/*_PLAN.md](docs/) files, it doesn't exist as a
commitment.

**Three roadmap files; here's the split:**

| File | Scope | When to update |
|---|---|---|
| **ROADMAP.md** (this one) | High-level milestones + cross-cutting deferred work. Forward-looking. | Edit when ranking, planning a release, or deferring an item. |
| **IMPROVEMENTS.md** | The historical 95+ phased item ledger (pre-v1.0.0 → present). Append-only. | Add a new numbered item when a concrete change lands or is queued. |
| **CHANGELOG.md** | What shipped per `vX.Y.Z`. Backward-looking. | Append a section per release. |

[Bridge split phases](docs/BRIDGE_SPLIT_PLAN.md) and
[BambuStudio submodule migration](docs/SUBMODULE_MIGRATION_PLAN.md)
are tracked in their own deep-dive plan files.

---

## Shipped

**v1.2.0** (2026-05-21) — [changelog](CHANGELOG.md#v120--12-new-commands--bridge-split-phase-2)
- 12 new CLI subcommands (download / ams / cam / slice / find / cloud-fetch /
  history / whoami / config / mqtt / queue / doctor)
- Bridge split phases 1–3 (re-export modules → Creds + sign_payload inline → FTPS inline)
- `beambam init` first-run wizard
- HA publisher: +6 entities (humidity_warn × 4 + queue_pending + hms_active_count)
- MCP server: +7 tools (analyze, ams_status, ams_tray_info, doctor, download, queue_list, find_printers)
- StateHub pub/sub primitive (consumers still TBD)
- SvelteKit + Tailwind v4 docs site at [beambam.boo](https://beambam.boo)
- Test suite: 108 → **300** offline + 6 live skipped

**v1.1.0** (2026-05-20) — [changelog](CHANGELOG.md#v110--package-rename--pypi-debut)
- Rename `x2d` → `beambam`. First PyPI release: `pip install beambam`.

---

## v1.3.0 candidates

Ordered by user-visible impact. Pick any 3-5 for the next release cut.

### Surface wiring (high-value, low-risk)
- [ ] **Wire `StateHub` consumers** — HA / MCP / WebUI / timelapse subscribe via the v1.2.0
  primitive instead of polling. Touches 4 modules; biggest latency win we can ship.
- [ ] **Daemon HTTP routes for v1.2.0 commands** — `/ams`, `/doctor`, `/analyze` (POST
  with file upload). `/queue/*` already exists. Lets the web UI surface them.
- [ ] **Web UI updates for v1.2.0** — `web/index.js` doesn't show AMS humidity warnings,
  doctor results, queue contents, or analyze output.

### New commands (each ~1-2 hr, well-scoped)
- [ ] `beambam reboot` — soft reboot via M-code or signed control payload
- [ ] `beambam plate {select,skip}` — multi-plate operations on the current file
- [ ] `beambam tail` — stream HMS errors + state changes as a live log
- [ ] `beambam upgrade` — pip self-upgrade + post-upgrade migration prompts
- [ ] `beambam install-completion {bash,zsh,fish}` — shell tab-completion via argcomplete
- [ ] `beambam print --dry-run` — auto-run analyze before send; refuse on flush > N grams
- [ ] `beambam fw-update` — cloud-driven firmware update (needs Bambu Cloud auth)

### Cloud API surface (additive — read-only catalog from 76-endpoint research)
Shipped in commit 2015b20: cloud-history / cloud-task / cloud-messages /
cloud-tickets / cloud-feed / cloud-firmware / cloud-filaments /
cloud-search-suggest. Remaining endpoints from the catalog worth wiring:
- [ ] `cloud-project [list|show <id>]` — `/v1/iot-service/api/user/project`
  paginated project list + per-project full record. Returns the .3mf bundle
  signed-S3 URL — analog of cloud-task but project-level.
- [ ] `cloud-design <id>` — `/v1/design-service/design/{id}` MakerWorld design
  details (title, creator, instances, tags, like/download counts).
- [ ] `cloud-design-remixes <id>` — `/v1/design-service/design/{id}/remixed`
  the remix tree for a model.
- [ ] `cloud-like <designId>` — POST `/v1/design-service/design/{id}/like`
  (write — needs explicit opt-in flag).
- [ ] `cloud-favorites [list]` — `/v1/design-service/my/favorites/listlite`
  caller's favorite designs (light-weight ID list).
- [ ] `cloud-presets [list]` — `/v1/iot-service/api/slicer/setting?public=false`
  user's saved slicer presets.
- [ ] `cloud-ttcode <serial>` — `/v1/iot-service/api/user/ttcode` returns
  ThroughTek P2P codes; would enable cloud-native camera streaming without
  the existing LAN MJPEG path.
- [ ] `cloud-device-info <serial>` — `/v1/iot-service/api/user/device/info`
  full device specs + firmware metadata.
- [ ] `cloud-spool {add|update|delete}` — `/v1/design-user-service/my/filament/v2`
  CRUD for the spool inventory (extends the read-only `cloud-filaments`
  we already ship).
- [ ] `cloud-app-config` — `/v1/operation-service/configuration` global
  feature-flag manifest (exposes pre-release flags Bambu has provisioned).

### Slicer power-features
- [ ] **`--copies N` / `--quantity N`** in `x2d_slice` — duplicate the model
  N times on the plate via Bambu's `<part>` instance multiplier. Wires
  through `cmd_slice_print` so `beambam print stl.stl --copies 4` works.
- [ ] **`--scale-pct 75`** convenience flag (currently `--scale 0.75` works
  but `--scale-pct 75` reads more naturally for the CLI).
- [ ] **`--mm <height>`** absolute-size scaling — auto-compute the scale
  factor that makes the model's bounding-box Z equal to <height>mm.
- [ ] **`--orient {auto|flat|tall|original}`** auto-orient the model so the
  flattest face is on the build plate (or tallest dimension is Z).
- [ ] **`--color-by-region <map.json>`** — per-AMS-slot colour assignment
  driven by a JSON map of mesh region → filament index. Extends today's
  `--color` (single global colour) for multi-colour prints.

### Search → slice → upload pipelines
Today: `beambam fetch <url>` downloads from MakerWorld / Printables /
Thingiverse. Today's gaps: no search, no automatic chain to slice +
upload, no per-source auth re-use.
- [ ] **`beambam mw-search <query>`** — MakerWorld search via
  `/v1/search-service/select/design/nav?navKey=…` (or the search endpoint
  schwarztim's bambu-mcp docs at `/v1/design-service/search?query=…`).
  Returns a paginated table of (designId, title, creator, likes, downloads).
- [ ] **`beambam printables-search <query>`** — Printables GraphQL search;
  same output shape.
- [ ] **`beambam thingiverse-search <query>`** — Thingiverse REST search
  (needs the 2026 browser-cookie auth that lands per #34).
- [ ] **`beambam print-search <source> <query> [--copies N --scale X]`** —
  the meta-command: search → user picks a hit → fetch → slice → upload to
  the configured printer. The whole point of the FRE.

### First-run experience (FRE) for `uvx beambam`
- [ ] **Device-code OAuth flow** for cloud-login. Currently `beambam cloud-login`
  asks for email + password (potentially with 2FA prompt). For `uvx beambam`
  fresh-OS users, password entry into a terminal is a hostile UX — Bambu's
  cloud accepts a device-code-style flow (POST `/v1/user-service/user/sendemail/code`
  → user clicks link on phone → returns ticket → `/v1/user-service/user/ticket/{ticket}`
  exchanges for tokens). Implement under `cloud-login --device-code` and
  promote to default on next major.
- [ ] **`beambam doctor --fix`** — auto-detect missing prerequisites
  (no `~/.x2d/credentials`, no `~/.x2d/cloud_session.json`, no `bambu-studio`
  binary in PATH for slicing) and offer to install/configure.
- [ ] **Dynamic help / command discovery** — argparse already produces a
  flat help table but it's long. Group by section (LAN, Cloud, Slicing,
  MakerWorld, Daemon, Doctor) and add `beambam help <topic>` aliasing
  to `beambam <topic> --help`. Stretch: argcomplete + a `beambam tldr`
  showing the 5 most-used commands.
- [ ] **`beambam init --cloud-only`** — current `init` wizard requires
  a LAN-reachable printer; add a path for users who only want cloud-mode
  control (skips discovery, just runs the OAuth login).

### FCM snapshot harvester ([HANDY_DATA_AUDIT_PART2.md](runtime/handy_extract/HANDY_DATA_AUDIT_PART2.md))
- [ ] Promote `runtime/handy_extract/fcm_snapshot_harvest.py` to a daemon
  subcommand `beambam fcm-harvest --daemon --interval 60`.
- [ ] Serve route on the bridge daemon: `GET /history/<print_id>.jpg` reads
  from `~/.x2d/snapshots/`.

### Refactor (boring but unblocks v2.0 surface)
- [ ] **Bridge split phase 4** — `X2DClient` (~250 lines) → `beambam.mqtt`. Blocked on
  extracting the `_metric_inc` helper out of x2d_bridge.py first. See
  [BRIDGE_SPLIT_PLAN.md](docs/BRIDGE_SPLIT_PLAN.md).
- [ ] **Bridge split phase 5** — `cmd_*` handlers (~40, ~6k LoC) → `beambam/cli/*.py`.
  Largest diff in the bridge's history; defer until #1 daemon stops being load-bearing.

### CI / repo hygiene
- [ ] **GitHub Actions Node.js 20 → 24** — every workflow warns; hard deadline 2026-09-16.
  Trivial bump (actions/checkout@v4 already supports both).
- [ ] **`runtime/*` test promotion** — most `runtime/{ha,mcp,webui,timelapse,queue}/
  test_*.py` are standalone scripts (not pytest-collected). Refactor to pytest +
  add to `testpaths`.
- [ ] **Delete the stranded `tribixbite/beambam-boo` repo** — needs `gh auth refresh
  -s delete_repo` then `gh repo delete tribixbite/beambam-boo --yes`. User-only.

---

## Parked / blocked

- **BambuStudio → git submodule** ([SUBMODULE_MIGRATION_PLAN.md](docs/SUBMODULE_MIGRATION_PLAN.md))
  BLOCKED-on-user-review. Working tree has 9 uncommitted mods (~107 LoC across
  CMakeLists + deps + src) that must be exported as patches before submodule
  conversion can land safely.

- **MCP `healthz` / `metrics` / `camera_snapshot` tools** — argv builders return `[]`
  and are "handled specially" inline. Should route through dedicated handlers
  (HTTP + base64 image content block for camera).

- **macOS/Windows live tests** — `@pytest.mark.live` only runs when `BEAMBAM_TEST_IP`
  is set. No actual matrix for "does signed-MQTT work against a real printer on
  every platform" — we test it manually on Termux against the X2D.

---

## Long-tail (IMPROVEMENTS.md backlog)

[IMPROVEMENTS.md](IMPROVEMENTS.md) has a 95+ item phased ledger going back to pre-v1.0.0,
organised as:

- **Items 1–10** — bridge bootstrap
- **Items 11–20** — UX gaps + hardening
- **Items 21–58** — multi-phase feature-complete build (queue, HA, MCP, WebUI, …)
- **Post-v1.0 backlog** — human-attended verification
- **[Future enhancements (parked)](IMPROVEMENTS.md#future-enhancements-parked-not-blocking)**
- **Phase 6** (items 87–88) — phone-display polish + camera bridge
- **Phase 7** (items 89–94) — finish parked items
- **Phase 8** (items 95+) — long-tail follow-ups
- **Phase 9** (items 100+) — real-world verification

When picking what to do next, scan Phase 7 / 8 for items already scoped, vs cut
fresh from the v1.3.0 candidates list above.

---

## How this file flows with TaskList

The [TaskCreate tool](#) is **session-local** scratch space — it tracks the
agent's current work, and the loop hook (`~/.claude/hooks/loop-tasks.sh`) keeps
sessions going while tasks are pending. ROADMAP.md is the **durable** record.
On session start, a new agent should read ROADMAP.md first, pick a v1.3.0
candidate, and create a TaskCreate entry for the in-session work. On session
end (everything completed), update ROADMAP.md so the next agent can see what
shipped.
