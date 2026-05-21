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
- [x] `beambam upgrade` — pip self-upgrade. Queries PyPI JSON API for
  the latest stable (filters yanked + pre-releases unless `--pre`),
  diffs against `importlib.metadata.version("beambam")`, then invokes
  `<python> -m pip install --upgrade beambam`. Special-cases:
  source-checkout (no install), dev-ahead (local > PyPI), uvx-managed
  venv (prints `uvx --refresh-package` hint). `--check` for dry-run.
  21 unit tests in `tests/test_upgrade.py`; live-verified against real
  PyPI (1.2.0 latest).
- [x] `beambam install-completion {bash,zsh,fish}` — shell tab-completion
  via a static, zero-dep generator (commits: this round). Subcommand-set
  snapshot from the live argparse tree; `--install` writes to
  `~/.local/share/bash-completion/completions/beambam` / `~/.zfunc/_beambam` /
  `~/.config/fish/completions/beambam.fish`. Bash live-verified
  `beambam cl<Tab>` → 34 `cloud-*` subcommands. 15 unit tests in
  `tests/test_install_completion.py`.
- [x] `beambam print --dry-run` — runs `beambam analyze` on the .3mf,
  prints the human report, and refuses (exit 2) if total purge waste
  exceeds `--max-flush-g` (default 10 g). Short-circuits BEFORE
  Creds.resolve / FTPS / MQTT, so it works on a workstation without a
  `~/.x2d/credentials` file. Live-verified: rumi_frame (0 g flush) →
  exit 0; same file with `--max-flush-g=-1` → exit 2 with REFUSED
  message. 5 unit tests in `tests/test_print_dry_run.py` (incl. one
  e2e against the bundled rumi fixture).
- [ ] `beambam fw-update` — cloud-driven firmware update (needs Bambu Cloud auth)

### Cloud API surface (additive — read-only catalog from 76-endpoint research)
Shipped in commit 2015b20: cloud-history / cloud-task / cloud-messages /
cloud-tickets / cloud-feed / cloud-firmware / cloud-filaments /
cloud-search-suggest.
Shipped in commit cb84385: cloud-search / cloud-browse / cloud-design /
cloud-design-remixes / cloud-favorites / cloud-liked / cloud-presets /
cloud-app-config.
Shipped in commit 75cba23: cloud-pull-design / cloud-print-design.
Shipped in commit 0115af6: cloud-like / cloud-comments / print-search.
Remaining endpoints from the catalog worth wiring:
- [ ] `cloud-project [list|show <id>]` — `/v1/iot-service/api/user/project`
  is POST-only (creates a new project). GET fails 405. Wiring write-side
  needs a careful "are you sure" prompt because it permanently creates
  account-side state.
- [ ] `cloud-ttcode <serial>` — `/v1/iot-service/api/user/ttcode`
  **gated 403** on regular cloud-login sessions; restricted to Bambu
  Connect / Handy via additional auth headers we don't have. Method
  defined on `CloudClient` (best-effort) but the CLI wrapper would just
  surface the 403. Defer until we have Handy-style auth.
- [ ] `cloud-device-info <serial>` — `/v1/iot-service/api/user/device/info`
  is 405 on GET, format of POST body undocumented. Defer until needed.
- [ ] `cloud-spool {add|update|delete}` — `/v1/design-user-service/my/filament/v2`
  CRUD for the spool inventory (extends the read-only `cloud-filaments`).
  Each is a single POST/PUT/DELETE — but live-testing risks mutating the
  real account; needs an `--allow-write` opt-in.
- [ ] `cloud-comment-reply <commentId>` — POST `/v1/comment-service/comment/{id}/reply`
  reply to a comment. Read-side already shipped as cloud-comments.

### Slicer power-features
- [x] **`--copies N` / `--quantity N`** in `x2d_slice` (commit 678a0df) —
  duplicate the model N times on the plate via 3MF instance multipliers;
  pre-validates that the grid fits the 256×256 mm X2D build volume.
- [x] **`--scale-pct 75`** convenience flag (commit 678a0df).
- [x] **`--mm <height>`** absolute-size scaling (commit 678a0df).
- [ ] **`--orient {auto|flat|tall|original}`** auto-orient the model so the
  flattest face is on the build plate (or tallest dimension is Z).
- [x] **`--colors c1,c2,c3,c4` / `--color-by-region map.json`** (commit
  41deb0f) — multi-AMS-slot provisioning. Expands every parallel
  `filament_*` list + `flush_volumes_matrix` (N²) +
  `flush_volumes_vector` (2N). Real-slice-verified at 4 copies × 4
  colours: 8.14g, clean BS CLI exit. Per-copy slot binding (so each
  copy actually picks a different slot) still needs BS GUI paint maps
  — out of scope today because the slicer's `<metadata key="extruder"
  value="N"/>` is the nozzle index (X2D has 2 nozzles), not the AMS slot.

### Search → slice → upload pipelines
- [x] **`beambam cloud-search <query>`** (commit cb84385) — MakerWorld
  full-text search via `/v1/search-service/select/design`. 10000+ hits
  for typical queries.
- [x] **`beambam cloud-browse <nav>`** (commit cb84385) — browse by nav key.
- [x] **`beambam cloud-print-design <id>`** (commit 75cba23) — MW search →
  design → slice → upload chain. `cloud-pull-design` for download-only.
- [x] **`beambam print-search <query>`** (commit 0115af6) — interactive
  picker: search MW → numbered list → user picks → chain into
  cloud-print-design. `--pick N` for non-interactive selection.
- [x] **`beambam printables-search <query>`** (commit 50aa9d9) —
  Printables GraphQL search via anonymous `searchPrints2`; same output
  shape as cloud-search.
- [ ] **`beambam thingiverse-search <query>`** — Thingiverse REST search
  (needs the 2026 browser-cookie auth that lands per #34).
- [ ] **`beambam print-search --source <printables|thingiverse>`** —
  extend `print-search` to multi-source. MakerWorld backend works today.

### First-run experience (FRE) for `uvx beambam`
- [x] **Device-code style email-code login** (commit 75cba23) — `cloud-login
  --code-only` skips the password prompt entirely; Bambu emails a 6-digit
  code that proves possession of the inbox.
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
- [x] Promoted `runtime/handy_extract/fcm_snapshot_harvest.py` to bridge
  subcommand `beambam fcm-harvest --device <ip:port> [--daemon --interval 60]`
  (commit 75cba23).
- [x] Serve route on the bridge daemon: `GET /history/<print_id>.jpg`
  (commit 50aa9d9) reads from `~/.x2d/snapshots/`.

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
