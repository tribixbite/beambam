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
