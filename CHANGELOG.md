# Changelog

All notable changes to this project.

## [Unreleased]

## v1.6.1 — print from another app over HTTP, track-switch auto nozzle files, Home Assistant fixes (2026-11-25)

### Added
- **Print through the daemon's HTTP API** (`docs/HTTP_API.md`), for web apps
  and scripts that have no terminal: `POST /print/plan` returns the filament →
  tray plan against the live AMS without touching the printer; `POST /print`
  with `"confirm": true` (and `"accept_warnings": true` once the user has seen
  the plan's warnings) uploads and starts it in the background;
  `GET /print/jobs/<id>` follows it to accepted / rejected. `POST /print/upload`
  takes a `.gcode.3mf` from another machine. Same rules as `beambam print`
  (`beambam/print_service.py`): errors always refuse, the bed comes from the file.
- **`POST /slice` slices a Bambu Studio project on the daemon's machine**
  (`beambam/slice_service.py`, bundled headless Bambu Studio), as a job
  (`GET /slice/jobs/<id>`); the resulting `.gcode.3mf` goes straight into
  `/print/plan` and `/print`. The project keeps its own process and filament
  settings, and its printer profile is reloaded from the bundled one of the
  same name. That profile is needed for projects saved by a newer Bambu Studio,
  whose start G-code this build cannot parse (pokefit's X2D export failed with
  "Not a variable name"). This lets a site that cannot slice (pokefit.party)
  print from a phone running beambam.
- `beambam daemon --cors-origin ORIGIN` (repeatable; `$BEAMBAM_CORS_ORIGINS`):
  CORS headers and preflight answers for a browser app on that origin,
  including Chrome's Private Network Access opt-in, so a public https site can
  call a daemon on `127.0.0.1`.
- `beambam ams map --json` and `/print/plan` include flat `errors` / `warnings`
  lists.

### Security
- **The daemon refuses POSTs from other web origins.** A loopback daemon needs
  no token, and it parsed any POST body as JSON, so any web page open in the
  browser could POST `/control/stop` (or, now, a print) to `127.0.0.1`. A POST
  with an `Origin` header that is neither the daemon's own nor allowed with
  `--cors-origin` now gets 403. Clients that send no Origin are unaffected.

### Fixed
- `POST /slice-print` never sliced anything: it ran `serve_http.py` (which has
  no command line) instead of beambam's CLI, and forced `--slot 1`. It now runs
  `python -m beambam.cli slice-print`, maps trays automatically unless `X-Slot`
  is given, and takes `X-Accept-Warnings: 1`.
- **Files sliced in the Filament Track Switch's automatic nozzle mode now
  print** (`enable_filament_dynamic_map`, read from the plate's
  `slice_info.config` first, then `project_settings.config`, as Bambu Studio
  does). Bambu Studio asks the printer for a nozzle mapping only on printers
  with a hotend rack (`fun` bit 60); the X2D and H2D have none, so nothing
  extra is sent. beambam now applies Bambu Studio's checks instead of
  refusing: the switch must be installed and set up, and since the gcode may
  move a filament between nozzles, each filament needs a tray that reaches
  both (any switch-routed AMS tray; an external spool feeds only one nozzle).
  The plan shows such filaments on nozzle `both`. Rack printers are still
  refused with an explanation.
- **Home Assistant bridge, audited against a live X2D.** Most X2D values were
  wrong or missing: the chamber temperature read a field the X2D does not send,
  the left nozzle had no sensors, the IP showed as an integer, the speed as `2`,
  "Firmware version" read a non-existent field, only the first AMS unit was
  exposed, the `light` entity had no state topic, and "Door open" was really the
  external-spool filament sensor. The publisher now derives these in Python
  (`derive_state`, unit-tested) and publishes them under `beambam` in the
  state JSON: both nozzles' temperatures, targets and loaded tray, the chamber,
  dotted IP, speed name, firmware (from a `get_version` request the daemon now
  sends on connect and hourly) and available update, the track switch, and
  every AMS tray (A1–D4) and unit (humidity %RH, temperature, damp). AMS
  entities are discovered for the units the printer reports; renamed entities
  are removed from HA. New: left-nozzle setpoint, tray-labelled load buttons
  (`x2d/<id>/ams/b3/load`) and an unload button.
- **The daemon no longer replaces its state with every MQTT message.**
  Command acks, `info` replies and delta pushes are merged into the cached
  report (`state_hub.merge_report`), so HA, `/state`, SSE, the queue and the
  timelapse recorder never see a state stripped of temperatures and AMS.
- `ams_change_filament` (`POST /control/ams_load`, `Printer.ams_load`, HA)
  now sends `ams_id` / `slot_id` / `target` as Bambu Studio does, taking a
  global tray id (`{"tray": 5}` = B2; legacy `{"slot": 1..16}` still works) and
  an optional `extruder`; new `POST /control/ams_unload`.

## v1.6.0 — filament mapping, AMS sync, man-page help (2026-11-24)

### Added
- **AMS colour sync for `slice-print`.** `--color` / `--colors` accept AMS trays
  (`A1`–`D4`, `ext-left`, `ext-right`) alongside hex and Bambu colour names:
  `beambam slice-print part.stl --copies 2 --colors A1,B3` reads the colours
  loaded in those trays from the live printer, slices with them, and pins each
  of those filaments to its tray for the print (`--map` still overrides). A
  missing or empty tray, or a tray whose material differs from the slice
  profile's, is refused before any slicing time is spent.
- **`--slot` takes one slot per used filament** (`--slot 4,0,12`) on `print`,
  `slice-print` and `ams map`, in the order of the plate's used filaments; a
  single slot keeps its old meaning. (Merged from the `--slot`-list work on
  main and routed through the filament planner, so every slot is checked
  against its own filament.)
- `beambam <command> help` (e.g. `beambam ams map help`) opens that command's
  page, like `--help`.
- `beambam help <word>` with no command of that name suggests near-miss names
  and lists every command whose summary mentions the word.
- **Full filament → tray mapping with confirmation before every print**
  (`beambam/filament_mapping.py`). `beambam print` and `beambam slice-print`
  now read the plate's *used* filaments and their nozzle assignments from the
  `.gcode.3mf`, read the printer's AMS trays, external spools and Filament
  Track Switch routing from live state, auto-map each filament to the best
  matching loaded tray (type-first, colour-first for 3+ colour prints —
  CIE76 ΔE, mirroring BambuStudio's `DevMappingUtil`), print the plan and ask
  before starting. Hard mismatches (empty tray, wrong material class, a tray
  that can't reach the sliced nozzle) always refuse; soft ones (brand /
  profile / colour) need a `y` on the terminal or `--yes`.
- **`--map F=TRAY,…` and `--plate N`** on `print` / `slice-print`: choose trays
  per filament (`--map 1=B3,4=ext-right`, using the touchscreen A1–D4 labels or
  a global slot), and select which sliced plate to print. `--slot` now defaults
  to auto-mapping.
- **`beambam ams map FILE`** — preview the exact filament→tray plan (with the
  `ams_mapping` arrays) for a sliced file without printing. Exit 0 clean, 2
  warnings, 1 errors.
- **The Filament Track Switch is now modelled.** `beambam ams status` shows
  which nozzle(s) each AMS feeds and the switch state; AMS units are labelled
  A–D and trays A1–D4 to match the printer. Files sliced for the switch's
  automatic nozzle mode (`enable_filament_dynamic_map`) are refused with a clear
  message (the printer-computed nozzle map isn't reproducible yet).
- The LAN print path now **waits for the printer's `project_file` reply** and
  reports acceptance vs. rejection (mapping the signed-command error ladder —
  84033543/5/8/4 — to plain English via `beambam.mqtt_sign.explain_ack`), and
  `beambam doctor --env` **checks the signing certificate's expiry** (an expired
  Handy app cert makes every signed command fail with err 84033545).

- **Man-page help for every command.** `beambam --help`, `beambam <cmd> --help`
  and `beambam help <cmd> [<sub>]` now render NAME / SYNOPSIS / DESCRIPTION /
  ARGUMENTS / OPTIONS / EXAMPLES / SEE ALSO pages (bold flags, underlined
  values, colour on a TTY honouring `NO_COLOR` / `FORCE_COLOR`, paged through
  `$PAGER` when longer than the screen). The root page groups all commands by
  topic and documents ENVIRONMENT, FILES and EXIT STATUS.
- `beambam help --all` (every page), `beambam help --man` (roff source) and
  `beambam help --install-man [DIR]` (writes `beambam.1` so `man beambam` works).
- Running a command with a missing argument prints that command's full page
  followed by the error (exit 2). A mistyped option or bad value prints the
  command's synopsis, the error, the closest real option (`--slto: did you mean
  --slot?`) and a pointer to the manual; an unknown command prints "did you
  mean" suggestions.
- Help text for the ~130 arguments that had none (`beambam/cli/arghelp.py`);
  `tests/test_cli_manhelp.py` fails if a new argument ships without help.
- `beambam slice` and `beambam slice-print` now expose the **full slicer option
  set** for STL input — `--colors`, `--color-by-region`, and `--orient` — matching
  `x2d_slice.py`. Previously only `--color` was forwarded.
- **Working dual-nozzle multi-colour from an STL.** `--colors A,B` + `--copies N`
  now assigns each copy to a nozzle (extruder 1/2, cycled across the printer's
  real nozzle count) so a 2-up print actually comes out in two colours — verified
  on a real X2D slice (copy 1 → `#E4BD68` 2.98 g, copy 2 → `#FF0000` 2.47 g).
- **`--color-by-region` is now structured**: accepts `[{"color": "...", "nozzle":
  N}, ...]` (or a `{"0": {...}}` dict) for explicit per-object colour **and**
  nozzle assignment, not just a colour-list alias. Falls back to the auto per-copy
  nozzle cycle when nozzles aren't specified.

### Fixed
- **Commands that need a printer now say how to set one up.** With no printer
  configured they print `no printer configured (missing: …)` and point at
  `beambam init`, `beambam find --add` and `beambam config add`, instead of an
  INI-file template. `beambam doctor` no longer exits on that error — it reports
  it as a failed check and runs the local environment checks — and `beambam
  doctor` output is no longer coloured when piped or under `NO_COLOR`.
- `beambam boo` printed a Python tuple list when no printer was reachable; it now
  prints one line per printer with the reason.
- Cloud commands without a session now say to run `beambam cloud-login` first
  (several still named the old `x2d_bridge.py`, others just "not logged in");
  `cloud-pause/resume/stop` without a resolvable printer explain how to choose
  one; `history` / `whoami` give the same login hint.
- `beambam cam` / `cam watch` with no camera proxy running now say to start it
  with `beambam cam start` instead of a bare "Connection refused".
- A missing input file is reported as `file not found: PATH` (some paths
  printed only the bare file name).
- **Windows:** `beambam help --install-man` crashed writing the page (cp1252
  can't encode `→`); it is written as UTF-8. Help, plans and errors no longer
  raise `UnicodeEncodeError` when stdout/stderr is a narrow-codec stream (a
  redirected Windows console); unencodable characters print as `?`.
- `beambam jog` validates the axis (X/Y/Z/E, any case) at parse time.
- `CloudClient.login_code_only` asked Bambu to email a login code *before*
  checking it had a way to read the code, so a call without a resolver sent a
  useless email (and its unit test made a live request). It now fails first.
- **Every `cloud-*` REST command failed with an unreadable HTTP 403.** Bambu's
  Cloudflare WAF began blocking requests whose `X-BBL-Client-Name` header is
  `OrcaSlicer`; it is now `BambuStudio` (what BambuStudio's own client sends).
  The HTTP-error path also gzip-decodes the response body, so a future block
  shows the real message instead of binary mojibake. (`cloud_client.py`)
- `beambam print --slot N` and the BambuStudio GUI shim / print queue sent the
  filament mapping **indexed from 0 regardless of which project filament the
  plate uses** — a single-colour plate printing filament 5 of 5 mapped
  filament 1, and the GUI shim used only `ams_mapping[0]`. Mapping is now
  indexed by project filament throughout, and bed-temp derivation reads the
  plate's actual first filament.
- `build_x2d_project_file` now honours bed-levelling / flow-cali / vibration-cali
  and the plate index (previously hard-coded to auto / plate 1).
- **`beambam status` (and every command that opens the printer's MQTT session)
  no longer dumps a Python traceback when the printer is unreachable.** It
  prints the target, the socket error and next steps — including "you are
  probably on a different network" when this device's address is outside the
  printer's /24 (seen when the phone joined a car's Android Auto Wi-Fi) — and
  exits 3. Other network / file errors (`OSError`) from any command print one
  line instead of a traceback (exit 3 for timeouts and connection errors, 1
  otherwise). Set `BEAMBAM_DEBUG=1` for the traceback.
- `slice-print --slot` help said "0-3"; it is the AMS global slot (0..15), as
  the code already validated.
- **`list_files` on a busy printer (`[SSL: INVALID_ALERT]`)**: subdir listings
  (e.g. `/cache`) failed with an invalid-alert during the data-channel handshake
  while a print was active — `list_files` used a bare `SSLContext(PROTOCOL_TLS_
  CLIENT)`, the exact pattern the module's own notes flag as alert-prone mid-print.
  It now routes through the same resilient `_manual_ftp_tls` transport (TLSv1.2-
  forced, PASV session reuse) that `download_file`/`upload_file` already use, with
  unwrap-before-close and a short retry for the flaky mid-print data channel. The
  now-unused `_ImplicitFTPTLS` subclass was removed.
- **Multi-colour `filament_map` bug**: expanding the colour palette copied
  nozzle 1 for every filament (`['1','1']`), so the second-nozzle colour never
  printed (BS emitted invalid tool commands). It now maps each filament to a
  distinct nozzle (`['1','2']`), cycling across the nozzle count — the fix that
  makes per-copy multi-colour actually slice. (Dual-nozzle = up to 2 distinct
  colours; more needs the AMS filament switcher via a painted 3MF + `slice-print
  --load-filament-ids`.)
- GUI one-line installer (`install.sh`) pulled the BambuStudio runtime tarball
  from `releases/latest`, which 404'd once the CLI-only pip releases (1.1.0+)
  became "latest". It now resolves the newest release that actually ships the
  tarball, and `pip install`s the current `beambam` CLI so the GUI's
  `libbambu_networking.so` shim has a modern `python -m beambam.cli` to spawn.

### Changed
- Removed the repo's Claude Code Stop hooks (`.claude/hooks/loop-*.sh`): their
  `#!/usr/bin/env bash` shebang failed under the hook runner on Termux, so they
  errored at the end of every session.
- **Relicensed from MIT to AGPL-3.0-or-later.** The pure-Python tree (previously
  MIT) is now AGPL-3.0, matching the already-AGPL BambuStudio fork +
  `runtime/network_shim/`. Bars closed-source forks and, via AGPL §13, closed
  hosted/network forks; aligns with upstream BambuStudio / OrcaSlicer / SFC
  `baltobu` licensing.

## v1.5.1 — Developer-Mode option documented + accuracy fixes (2026-06-16)

**Clarified — two ways to authorize writes on authorization-control firmware, your choice:**
- **(a) `beambam key`** — extract the per-installation key from a signed-in Bambu
  Handy (over adb). Control + print then run over LAN with no Developer Mode, and
  Bambu Cloud + remote access keep working.
- **(b) Developer Mode** — enable it on the printer and beambam drives it over the
  unsigned LAN path with no key/cert, like the other open clients (Developer Mode
  disconnects Bambu Cloud while on).

beambam already supported both via its key-present / key-absent fallback; this
release surfaces the choice in `beambam doctor` and documents it.

**Docs**
- Corrected the "no cloud account" overclaim (README, `docs/COMPARISON.md`,
  website, release notes). The no-cloud / no-Developer-Mode property is about
  *runtime*: the key path's signing key is Bambu-CA-issued, so a prior Handy cloud
  login provisions it — a one-time setup step (read out once over adb), not a
  runtime dependency. Reframed everywhere as the explicit key-vs-Developer-Mode
  choice; comparison matrix "LAN-only" cell for beambam is now ◐ (runtime).

## v1.5.0 — signed LAN control + Developer-Mode-free X2D/H2D print (2026-06-16)

Two headline capabilities land in this release, plus the final bridge-split
cleanup.

### Signed printer control + pure-LAN print on authorization-control firmware

Jan-2025+ Bambu firmware rejects unsigned `print.*` MQTT and requires a signing
cert whose CN matches the printer's own serial — a shared/leaked cert can't
satisfy it, which is why every other open client falls back to Developer Mode.
beambam now drives such printers **with no Developer Mode and no live cloud
connection at print time** — the signing key is Bambu-issued (a prior Handy cloud
login provisions it), so beambam's only cloud touchpoint is a one-time `adb`
extraction of that key, not an ongoing dependency:

- **`beambam key --adb <ip:port>`** recovers the per-installation RSA signing key
  from a running Bambu Handy app by scanning its Dart heap for a 128-byte window
  that divides the known modulus (a prime factor → full private key). No Frida, no
  hooking. Writes `~/.x2d/printer_sign_key.pem` + `printer_cert_id.txt`. Optional
  step in `beambam init` / surfaced by `beambam doctor`. See
  [`DART_HEAP_KEY_EXTRACTION.md`](runtime/handy_extract/DART_HEAP_KEY_EXTRACTION.md).
- **`pause` / `resume` / `stop` / `start` / `skip` / `gcode`** auto-route through
  the signed cloud-control path when the key is present (RSA-SHA256 over the
  header-stripped body). Validated live: `pause` + `resume` return
  `err_code:0, result:"SUCCESS"`. `beambam start` is ranked: resume → print next
  in queue → reprint.
- **X2D/H2D LAN print** — `beambam print` FTPs the `.gcode.3mf` to `cache/` then
  publishes a signed `print.project_file`. The file location is `url_enc` =
  base64(RSA-PKCS1v15(device_cert_pubkey, `ftp:///cache/<file>`)), which the
  printer decrypts with its own device key. **`beambam device-cert`** fetches +
  caches that cert (unsigned `security.app_cert_install` → `printer_cert`).
  Verified live (`err_code 0`, print starts) — no cloud task required, disproving
  the earlier "cloud task must pre-exist" assumption.
- **`beambam capture-params`** snapshots a cloud task's print parameters
  (ams mapping, bed type, filament) for replay; `get_auto_nozzle_mapping` building
  block for multi-filament auto-assignment.

### Security: pre-public-release scrub

- Redacted device-identifying example/test values (serial, uid, LAN IPs, access
  code, factory cert-id MD5s) to placeholders across docs + tests.
- Swapped the real captured signing vector in `test_mqtt_sign.py` for a frozen
  stand-in (no real key material in-tree).
- Untracked local device/account dumps (HA export, Handy data-audit); hardened
  `.gitignore` + wheel/sdist excludes against `~/.x2d` material.

### `x2d_bridge.py` removed

Final step of the bridge-split: the 255-LoC back-compat shim is gone.
Everything ships from `beambam.cli` proper.

**Removed**
- `x2d_bridge.py` — every caller now imports from the canonical home
  (`beambam.cli.control`, `beambam.mqtt`, `beambam.config`,
  `beambam.serve_http`, `beambam.ftps`, etc.).
- `tests/test_bridge_split_progress.py` — guard ratchet retired; the
  monolith it guarded no longer exists.
- pyproject.toml `force-include` + sdist `include` entries for
  `x2d_bridge.py`.

**Added**
- `x2d_bridge = "beambam.cli:main"` console-script alias in
  `[project.scripts]`. Gives `libbambu_networking.so` a stable PATH-
  lookup target after the literal-pathname spawn was rewritten.
- `beambam/cli/__main__.py` — `python -m beambam.cli` entry point.

**Changed**
- `runtime/network_shim/src/bridge_client.cpp` — spawns
  `python3 -m beambam.cli serve` instead of the three hardcoded
  `.../x2d_bridge.py` candidates. Falls back to `x2d_bridge` /
  `beambam` console-scripts on PATH. Live-verified end-to-end against
  real X2D `00M09A000000000 @ 192.168.1.42` via
  `runtime/network_shim/tests/test_shim_e2e.py`.
- `runtime/mcp/server.py` — `_run_bridge` defaults to
  `python -m beambam.cli`. `$X2D_BRIDGE` still honored for legacy
  pathname overrides.
- `runtime/mcp/test_live_client.py` + `runtime/mcp/test_mcp.py` +
  `runtime/test_phase2_smoke.py` — spawn via `-m beambam.cli`.
- `tests/test_cli_help_smoke.py` — discovers subcommands via
  `python -m beambam.cli --help`.
- `beambam/__init__.py` — `X2D_ROOT_PATH` + `_WEB_DIR_DEFAULT` are
  now module-level constants (hoisted out of the shim). Source-
  fallback version bumped to `1.5.0+source`.
- `beambam/cli/control.py` — `_publish_one` inlined with module-
  attribute lookup (`_config.Creds`, `_mqtt.X2DClient`) so
  monkeypatch can reach the publish path.
- `beambam/cam.py` — spawns `python -m beambam.cli` instead of
  `x2d_bridge.py`.

**Migration note for downstream users**
- `from x2d_bridge import X` → `from beambam.<canonical_module> import X`
  (most common: `beambam.mqtt.X2DClient`, `beambam.config.Creds`,
  `beambam.ftps.{upload_file, download_file, list_files}`).
- `python3 x2d_bridge.py <verb>` → `beambam <verb>` (or
  `python3 -m beambam.cli <verb>`).
- `x2d_bridge` itself still resolves on PATH as a console-script
  alias of `beambam` (same `main()` entry point).

Tests: 1027 passed, 9 skipped (live-printer guards). Shim e2e green
against live X2D.

## v1.4.0 — Phase 5e bridge split complete (2026-05-22)

The `x2d_bridge.py` monolith decomposition finished. The single
7,800-LoC file is now a **255-LoC pure re-export shim**, with every
handler living under its canonical home:

- `beambam.serve_socket` — GUI-shim Unix-socket JSON-RPC server
  (`ServeServer`, `_PrinterSession`, `_ConnHandler`, 14 `_op_*`,
  `_OPS` dispatch table)
- `beambam.serve_http` — multi-printer HTTP daemon body
- `beambam.cli.{control, cloud, info, daemon, lan}` — every `cmd_*`
- `beambam.cli:main` — argparse builder + dispatcher (new console-script
  entry point; `beambam = "beambam.cli:main"` in pyproject.toml)
- `beambam._version` — `PACKAGE_VERSION` + `_package_version()`

Phase 5e LoC ratchet across this release:

  3,470 → 3,462 (5e.1 PACKAGE_VERSION → beambam._version)
  3,462 → 2,501 (5e.2 ServeServer + 14 _op_* → beambam.serve_socket)
  2,501 → 1,543 (5e.3 _serve_http body → beambam.serve_http)
  1,543 → 1,531 (5e.4 _reboot_payload → beambam.cli.control)
  1,531 →   646 (5e.5 main() ~895 LoC → beambam.cli)
    646 →   255 (5e.6 shim collapse)

All `from x2d_bridge import X` and `x2d_bridge.X` access patterns
keep working through the back-compat re-exports — no consumer code
changes required. The `libbambu_networking.so` GUI shim still spawns
`python3 x2d_bridge.py serve` by literal pathname.

**Other v1.4.0 work**:

- `beambam ams sync` + `ams set` finalized for batch tray-metadata pushes
- Filament profile generator now lives in `beambam.filament_profiles`
  with cross-printer parametrization (X1C / X1E / P1S / P1P / A1 /
  A1mini / H2D / H2S / X2D × {0.2, 0.4, 0.6, 0.8} nozzles); the
  X2D-specific recipes stay in `tools/gen_x2d_filament_profiles.py`
  as a thin shim
- Stop hook + loop hook + ratchet tests for the bridge split — every
  new `cmd_*` lands in `beambam/cli/<group>.py` automatically
- Web UI: AMS humidity badges, Doctor card, Analyze drop-zone
- Daemon HTTP routes: `/ams`, `/doctor`, `/analyze`
- `StateHub` wiring for `/state.events` (push, not poll)
- `beambam tail`, `beambam reboot` (M999 wrapper), `beambam plate
  {list,select,skip}`
- Site stats are now generated from a build-time `stats.json` so the
  landing page can never drift from reality again
- 1,026 tests pass offline; 9 live-printer-gated tests skipped


## v1.2.0 — 12 new commands + bridge split phase 2

Twelve new CLI subcommands across pull/push/inspect/health surfaces,
plus phase 2 of the `x2d_bridge.py` decomposition into `beambam.*`
modules. No breaking changes — all `beambam X` invocations from v1.1.0
still work; the Python library API gained `Printer`, `Creds`, and
explicit submodules (`beambam.config`, `beambam.mqtt`, `beambam.ftps`).

### New commands

* **`beambam download <remote> [local]`** — pull a file off the
  printer's SD card via FTPS. First-class verb (was Python API only).
  Auto-resolves local path: bare → cwd basename, directory → inside
  it, else literal. Works mid-print (uses fixed TLS context).
* **`beambam ams {status,info,load,unload,dry}`** — pretty-printed
  AMS state with 24-bit ANSI color swatches, humidity bars, slot
  states (loaded/loading/ACTIVE), per-tray details. `dry <unit>
  --temp N --hours M` starts a drying cycle.
* **`beambam cam {watch,snap}`** — terminal camera viewer. Auto-
  detects backend: kitty graphics protocol → iTerm2 inline image →
  ANSI 24-bit half-blocks fallback. `--hz` polls at custom rate;
  `--max-frames` for testing.
* **`beambam slice <stl> -o <out.gcode.3mf>`** — standalone STL slice
  via BambuStudio CLI + X2D template profile. Complements existing
  `slice-print` (which slices + uploads + prints in one go).
* **`beambam find [--add NAME]`** — LAN SSDP M-SEARCH discovery.
  Returns ip/serial/model/name/signal/state per printer. `--add NAME`
  interactively writes a credentials section after prompting for the
  access code (not broadcast over SSDP).
* **`beambam cloud-fetch {--info,--instances,--design-cover,
  --user-tasks,--bound-devices}`** — MakerWorld + Bambu Cloud query
  CLI. Replaces the stale `fetch` MakerWorld endpoint (the legacy
  `/api/v1/design/design-detail` 404s since the 2026 backend rewrite).
* **`beambam history`** + **`beambam whoami`** — Bambu Cloud print
  history + logged-in user identity. Both require `cloud-login`.
* **`beambam config {list,show,add,rm,rename}`** — credentials file
  editor. Validates 8-digit access codes; always chmod 0600.
* **`beambam mqtt {sub,pub}`** — raw signed MQTT debug helpers.
  `sub` streams the printer's reply topic; `pub` signs + publishes
  arbitrary JSON. Protocol-archaeology tool.
* **`beambam queue {list,add,rm,cancel,clear,path}`** — print queue
  editor. Persists to ~/.x2d/queue.json; the daemon's `--queue` flag
  dispatches.
* **`beambam doctor [--json]`** — comprehensive health diagnostic.
  AMS humidity warnings, HMS error scanning + 9-code description
  catalog, thermistor sanity, wifi RSSI thresholds, camera state,
  print state surfacing. Exit code semantic: 0 pass, 1 warn, 2 fail.

### Bridge split (phase 2)

* **`Creds`** dataclass + resolution logic moved from `x2d_bridge.py`
  to `beambam.config`. `Creds.resolve_default()` + `from_section()`
  convenience constructors added.
* **`sign_payload`**, **`BAMBU_CERT_ID`**, and cert load logic moved
  from `x2d_bridge.py` to `beambam.mqtt`. Private key now cached
  after first load.
* **`beambam.ftps`** + lazy `X2DClient` access via module-level
  `__getattr__` to avoid the circular import that emerged when
  x2d_bridge started importing back from beambam.
* **`beambam.state_hub.StateHub`** primitive shipped — thread-safe
  pub/sub for printer state. Not wired into HA/MCP/WebUI yet
  (that's v1.3.0); ships as a building block.

### Fixed

* CI lint relaxed to `E/F/W` rules only — full ruleset surfaced ~80
  warnings on x2d_bridge.py mid-refactor.
* `mypy follow_imports=skip` — type-check beambam's public surface
  without recursing into legacy x2d_bridge.py.
* 2 real mypy errors in beambam (analyze return type, cloud_fetch
  None guard).
* `tests/test_remix_3mf.py` skips its module when its rumi_frame
  fixture isn't present (local-only artifact, gitignored).
* Test suite: 108 → **284** (+176) offline + 6 live deselected.

## v1.1.0 — Package rename + PyPI debut

Rebrand from `x2d` to **`beambam`** ([beambam.boo](https://beambam.boo))
and first publish to PyPI: `pip install beambam`. The bridge supports
every Bambu Lab printer (X1/P1/A1/H2/X2D, signed-MQTT family) — the
`x2d` name was too narrow.

### New since v1.0.0

* **PyPI distribution** — pyproject.toml (hatchling), 6 console scripts
  (`beambam`, `bb`, `beambam-mcp`, `beambam-slice`, `beambam-upload`,
  `beambam-print`), optional extras (`[slicing,mcp,webui,assistant,all,
  dev]`), GitHub Actions CI + OIDC trusted-publishing release workflow,
  per-Python-version matrix on Ubuntu + macOS.
* **`beambam` Python package** — re-exports the public API
  (`Printer`, `Creds`, `CloudClient`, `sign_payload`, `upload_file`,
  `download_file`, `list_files`).
* **`beambam.Printer`** — high-level stateful library facade with lazy
  MQTT + cloud connections, context-manager protocol. Methods cover
  state, start_print, pause/resume/stop, gcode, set_temp, chamber_light,
  ams_load/unload, home, upload/download/list_files.
* **`beambam.schemas`** — TypedDicts for the MQTT wire shapes
  (PrintState, AmsBus/AmsUnit/AmsTray, StartPrintCommand, …).
* **`beambam analyze <file.3mf>`** — print-plan dissector reporting
  filament/nozzle assignment, per-phase toolchanges, real flush volume,
  AMS-tray requirements, hints. `--json` for machine output.
* **`beambam simulate <subcmd>`** — dry-run MQTT payload preview
  returning the SIGNED envelope. Use for CI regression diffs.
* **`beambam cloud-fetch`** — MakerWorld + Bambu Cloud query CLI
  (`--info`, `--instances`, `--design-cover`, `--user-tasks`,
  `--bound-devices`). Replaces the stale `fetch <makerworld_url>` path.
* **`beambam frame --preset NAME`** — frame-STL generator with
  built-in presets (mira, rumi, zoey, huntrx).
* **`beambam.state_hub.StateHub`** — sync+async pub/sub primitive for
  printer state fan-out, foundation for v1.2.0's daemon refactor.

### Fixed

* `download_file()` `[SSL: INVALID_ALERT]` mid-print — port working
  `upload_file` TLS pattern (TLSv1.2 + manual session-reuse on PASV).
* `start_print` kwarg drift between Printer/simulate and the real
  signature.
* `tests/test_ams_mapping.py` 6 pre-existing failures fixed; suite
  21 → **108** offline + 3 live.

### Repo hygiene

* `dist/bambustudio-x2d-termux-aarch64/run_gui.sh` →
  `bs-runtime/aarch64-termux/run_gui.sh` (frees `dist/` for Python
  build output).
* `test_signed_mqtt.py` → `tools/probe_signed_mqtt.py`.
* `LICENSE` (MIT) for the Python package; AGPL-3.0 stays on the
  BambuStudio fork + network_shim.

### Deferred to v1.2.0 (plans written)

* `docs/BRIDGE_SPLIT_PLAN.md` — full `x2d_bridge.py` decomposition.
* `docs/SUBMODULE_MIGRATION_PLAN.md` — BambuStudio submodule.
* Wire HA/MCP/WebUI/timelapse to `StateHub`.

## v1.0.0 — Feature-complete LAN-first stack

86 commits, 62 ledger items closed, ~28 K lines added across the
bridge daemon, the runtime/ subsystems, the web UI, the test
harnesses, and the per-feature docs.

### Highlights

* **Six-surface daemon** built on top of the v0.1.0 signed-MQTT
  bridge: REST + Server-Sent Events, Prometheus `/metrics`,
  structured JSON access log, Home Assistant MQTT auto-discovery,
  WebRTC chamber-camera streaming, MCP stdio server, and a
  mobile-friendly web UI.
* **Multi-printer everywhere** — daemon, web UI, queue, HA
  publisher, MCP server. One `[printer:NAME]` section per printer
  in `~/.x2d/credentials` and every surface auto-discovers them.
* **Full BambuStudio Termux GUI port** — 12 source patches against
  upstream BambuStudio v02.06.00.51 plus an `LD_PRELOAD` GTK/locale
  shim, plus a 100-symbol `libbambu_networking.so` ABI shim that
  lets the GUI's Connect/AMS-sync/Print buttons drive printers
  through the bridge.
* **Native Home Assistant integration** with **32 entities** + 1
  Device per printer, including AMS-color → Bambu-profile
  auto-resolve. Live-tested against real Home Assistant Core
  2025.1.4 in a proot Ubuntu chroot — registry snapshots in
  `docs/ha-live-proof/`.
* **Claude Desktop / Cursor / Continue MCP server** wrapping every
  bridge op as a tool, plus a natural-language assistant in the web
  UI that calls the same toolset (with a no-API-key local fallback).

### New surfaces (Phase 1 daemon expansion: items 36-40)

* **#36 multi-printer daemon** — one X2DClient per credentials
  section, all sharing one HTTP server with `?printer=NAME`
  routing. Connection failures are isolated.
* **#37 per-printer `last_message_ts` persistence** at
  `~/.x2d/last_message_ts_<serial>` so `/healthz` reports a
  meaningful age immediately after a daemon restart.
* **#38 Prometheus `/metrics` endpoint** — per-printer gauges
  (nozzle/bed/chamber temps, AMS humidity, mc_percent) +
  per-printer counters (messages_total, mqtt_disconnects_total)
  + global counter (ssdp_notifies_total).
* **#39 structured JSON access log** — one line per HTTP request
  to `~/.x2d/access.log` with 1 MiB rotation; ts, method, path,
  status, duration_ms, printer, authed, client.
* **#40 proactive auto-connect on SSDP** — when an SSDP NOTIFY
  matches a credentials serial, the bridge opens MQTT before any
  shim asks. Cached state replays on every subscribe.

### Phase 2 surfaces (items 42-49)

* **#42 MCP stdio server** at `runtime/mcp/server.py` (callable
  as `python -m mcp_x2d`). 18 tools: status, pause, resume, stop,
  gcode, set_temp, chamber_light, ams_load/unload, jog, upload,
  print, camera_snapshot, list_printers, healthz, metrics, home,
  level. Two resources: `x2d://state`, `x2d://camera/snapshot`.
* **#43 Claude Desktop config docs** at `docs/MCP.md` with
  per-platform install (Termux / Linux / mac / Windows) and the
  SSH-tunnel pattern for running the bridge on Termux while the
  client lives on a laptop.
* **#44 live-tested every MCP tool** against the real X2D —
  `tools/call status` returned actual `nozzle=27 bed=25
  wifi=-58dBm` end-to-end through the JSON-RPC pipeline.
* **#45 WebRTC streaming** via aiortc + aiohttp — sub-second
  latency, browser viewer at `/cam.webrtc.html`. Pinned
  aiortc==1.10.1 + av==13.1.0 (newer versions need PyAV 14
  features that don't build on Termux). libsrtp built from
  source; covered in `docs/WEBRTC.md`.
* **#46 thin web UI** at the daemon's `/` — three static files
  (~17 KB), live state via SSE, control verbs over POST. No
  framework, no build step.
* **#47 mobile-friendly UI** verified at S25 Ultra viewport via
  real headless chromium-browser. CSS hardening: `overflow-x:
  hidden`, `* { min-width: 0 }`, `@media (max-width: 480px)`
  font shrinks. ≥44 px touch targets per Apple HIG / Google MD3.
* **#48 bearer-token login flow** with cookie + localStorage —
  `_check_bearer` accepts either source so EventSource (which
  can't set headers) works via the cookie path. New `/auth/info`
  + `/auth/check` + `/login.html` + `/login.js`.
* **#49 Phase 2 end-to-end soak** — `runtime/test_phase2_smoke.py
  --duration 600` runs all four daemons (bridge, camera, WebRTC,
  MCP) under continuous load for 10 minutes; **0 failures**, 0%
  RSS / thread / FD drift across every daemon.

### Phase 3: Home Assistant integration (items 50-54)

* **#50 MQTT auto-discovery publisher** at `runtime/ha/publisher.py`.
  32 entities per printer (12 sensors + 12 AMS slot entities + 1
  switch + 6 buttons + 3 number sliders + 1 image), all
  discovery-protocol-compliant under
  `<discovery_prefix>/<component>/x2d_<id>/<key>/config`.
* **#51 live-tested against real Home Assistant Core 2025.1.4** in
  a proot Ubuntu chroot. Registry snapshots at
  `docs/ha-live-proof/` show 32 x2d entities + 1 Bambu Lab X2D
  Device with **live values** (`ams_slot2_color="#F95D73"`,
  `ams_slot2_material="PLA"`, `ams_slot3_color="#A03CF7"`).
* **#52 ha-bambulab feature parity matrix** at
  `docs/HA_VS_BAMBULAB.md`. 34 of 36 X2D-applicable ha-bambulab
  entities at parity OR better. Added 13 missing entities to the
  publisher (4 fan speeds, speed_profile, hms_count, ip_address,
  firmware_version, printable/skipped objects, total_usage_hours,
  online + door_open binary sensors, home/level/buzzer_silence
  buttons).
* **#53 HA snapshot entity** — `/snapshot.jpg` proxy on the
  bridge daemon + publisher snapshot loop pushes JPEG bytes to
  `x2d/<id>/snapshot` every 10 s with `retain=True` so HA's
  image card always renders something even after a restart.
* **#54 multi-printer HA support** — `cmd_ha_publish` without
  `--printer` spawns one `HAPublisher` per credentials section
  in the same process. Each gets its own HA Device with
  namespaced topics; failures are isolated per-printer.

### Phase 4: features upstream BambuStudio doesn't have (items 55-58)

* **#55 multi-printer print queue** at `runtime/queue/manager.py`
  — file-backed FIFO at `~/.x2d/queue.json` with strict
  idle-detection auto-dispatch. Crash-safe (running → pending on
  reload). HTML5 native drag-and-drop reorder in the web UI.
* **#56 timelapse browser** at `runtime/timelapse/recorder.py` —
  per-printer state-driven capture (RUNNING → starts JPEG poll
  thread; FINISH → stops + writes meta). One-click `ffmpeg`
  stitch into MP4 with H.264 + faststart. Web UI Timelapse card
  with sampled thumbnail grid + inline `<video>`.
* **#57 AI assistant panel** at `runtime/assistant/router.py` —
  three providers (`local` rule-based, no API key; `anthropic`
  with the canonical MCP toolset; `auto` with graceful fallback).
  Web UI chat panel with color-coded user/assistant/tool turns.
* **#58 real-time AMS color sync** at
  `runtime/colorsync/mapper.py` — loads BambuStudio's official
  `filaments_color_codes.json` (~7000 entries) and resolves any
  RGB hex to the closest Bambu profile by Euclidean distance,
  with material-family filter. Web UI AMS swatches show the
  matched filament name + distance tooltip.

### Phase 5: docs + release (items 59-62)

* **#59 README reorg** — top-level "What is this" + "Who is this
  for" + 16-row feature matrix vs Bambu Studio + Cloud +
  ha-bambulab + 5-command quick-install + per-feature doc table.
  First three sections (~50 lines) tell a brand-new visitor
  everything within 60 s.
* **#60 per-feature docs** in `docs/`: 11 markdown files covering
  every Phase 1-4 feature with overview / install / API / examples /
  test-harness link.
* **#61 demo media** — five 1280×720 H.264 MP4s in `docs/demos/`
  (CLI, GUI, MCP, Web UI, HA dashboard) totalling ~3.2 min.
  Reproducible via `runtime/demos/render.py` (PIL + ffmpeg only).
* **#62 v1.0.0 release** — this changelog, the release notes, the
  refreshed dist tarball + SHA, and the GitHub release.

### Phase 0 fixes shipped after v0.1.0 (items 21-34)

Source-patches against BambuStudio v02.06.00.51 + LD_PRELOAD shim
hardening that landed before the daemon expansion:

* **#21** `GUI_App::config_wizard_startup` source-returns false
* **#22** BBLTopbar narrow-display padding shrink
* **#23** SelectMachinePop modal management — Hide() before
  spawning the Connect dialog so it z-orders correctly
* **#24** Swallow noisy wx sizer CheckExpectedParentIs asserts
* **#25** `EGL_PLATFORM=x11` (not surfaceless) — fixes 3D viewport
  black screen on llvmpipe + wxGLCanvas
* **#26** `cd $HOME` before launching so wxFileDialog defaults
  there instead of `/`
* **#27** Suppress gvfs "Could not read /" popup
* **#28** wxLocale `en_US` ICU bypass via source patch (replaces
  the shim symbol)
* **#29** Cache + replay latest MQTT state on every shim
  subscribe — AMS populates within milliseconds instead of
  waiting for the next 30 s pushall
* **#30** Show occupied LAN-mode printers in the Network combobox's
  "Other Device" list (was being filtered out by `is_avaliable()`)
* **#31** Inflate CheckBox hitbox to 42 px for touchscreens
* **#32** Register the "wx" WebView script-message handler so
  Home-tab WebView click events fire properly
* **#33** Resolved by #25 (same EGL surface root cause)
* **#34** Delete `patch_bambu_skip_wizard.py` + dead shim symbols
  now that #21+#28 supersede them

### Deferred to follow-up

Two ledger items intentionally pushed past v1.0.0 because they
require physical-print-time + an attached ADB device (or a
human at the printer):

* **#35 Final Phase 0 ADB verification** — wipe `~/.config/
  BambuStudioInternal/`, run `install.sh`, launch bambu-studio,
  manually verify zero papercuts.
* **#41 Print the rumi frame end-to-end via the GUI** — physical
  print run from a sliced plate. The bridge-side `start_print`
  C-ABI is already exercised end-to-end by
  `runtime/network_shim/tests/test_shim_e2e.py` against the real
  X2D, so the underlying code path is proven independent of this
  manual GUI run.

## v0.1.0 — initial release (2026-04-25)

* `bin/bambu-studio` — patched BambuStudio v02.06.00.51 (~77 MB
  stripped) with seven Termux/touchscreen patches.
* `runtime/libpreloadgtk.so` — LD_PRELOAD GTK/locale shim.
* `helpers/x2d_bridge.py` + `helpers/bambu_cert.py` — pure-Python
  signed-MQTT LAN client.
* `helpers/{lan_upload,lan_print,resolve_profile,inject_thumbnails,make_frame,test_signed_mqtt}.py` — slicing + LAN-print pipeline.
* Bambu cloud REST endpoints (login + a few read-only verbs).

Full v0.1.0 release notes:
https://github.com/tribixbite/x2d/releases/tag/v0.1.0
