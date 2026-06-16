# How beambam compares to other open-source Bambu projects

Bambu Lab's **"authorization control"** firmware (X1-series beta `01.08.03.00`,
Jan 17 2025; full `01.08.05.00`; P1/A1 later; always-on for the X2D/H2D family)
made the printer verify every MQTT **control** command (`print.*` plus
motion / AMS / fan / calibration / firmware) and reject anything unsigned or
signed by a cert it doesn't recognise (`MQTT command verification failed`,
HMS `0500-0500-0001-0007`). Per
[OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI), the printer wants an
RSA-SHA256 envelope signed by a **per-device cert that chains to the Bambu CA
with a CN matching the printer's own serial** — so a single shared / leaked
cert cannot satisfy it.

The open-source ecosystem responded in three ways:

1. **Read-only / Developer-Mode camp** (the large majority) — `ha-bambulab`
   (pybambu), `bambulabs_api`, `bambu-connect`, `bambu-node`, `bambu-cli`,
   OrcaSlicer, BambuStudio. They keep full LAN *reading*, but for *write / start*
   on new firmware they require the user to enable the printer's **Developer LAN
   Mode**, which by Bambu's own design **severs Bambu Cloud and disables
   auth-verification entirely**. That isn't beating the wall — it's using Bambu's
   official escape hatch (and you lose cloud, MakerWorld sync, and remote access
   while it's on).

2. **Genuine-signing camp** — only `bambu-mcp` actually emits a signed envelope,
   but it signs with a key/cert the **user must supply by hand**, and falls back
   to *unsigned* when absent; the "publicly-extracted Bambu Connect certificate"
   its README advertises is not what signs control commands. It still gates
   `.3mf` start (`print.project_file`) behind Developer Mode.

3. **The political flashpoint** — `jarczakpawel/OrcaSlicer-bambulab` (the project
   often associated with *Gamers Nexus*) is **not a control tool**. It is an
   OrcaSlicer *fork* that re-attached Orca to Bambu's **cloud** (login /
   MakerWorld / LAN+cloud print) without Bambu Connect. Bambu issued a C&D, the
   author wiped the repo, and Gamers Nexus + Louis Rossmann re-hosted it in
   protest (GN's page is an editorial plus a legal-defence offer, not software
   they wrote). It addresses cloud re-attachment, not the LAN signed-control wall.

## Where beambam sits

beambam is the only project that, on auth-control firmware, **(1)** recovers the
per-installation signing key *automatically* from a Bambu Handy install (reading
it out of the app's Dart heap — no Frida, no manual capture; see
[`DART_HEAP_KEY_EXTRACTION.md`](../runtime/handy_extract/DART_HEAP_KEY_EXTRACTION.md)),
**(2)** sends signed `pause / resume / stop / skip / gcode` (validated live), and
**(3)** actually **starts a print over pure LAN** — FTP the `.gcode.3mf`, then a
signed `print.project_file` whose file location is `url_enc` (RSA-encrypted to
the printer's own device cert) — with **no Developer Mode and no live cloud
connection at print time**. That last combination is the one every other tool
either can't do or only does by dropping into Developer Mode (which severs cloud
entirely). The honest cost: the signing key beambam uses is Bambu-issued, so it
only exists in Handy because of a prior cloud login — recovering it is a one-time
`adb` extraction, not a cloud-free bootstrap. So beambam trades a heavier *setup*
(adb + a signed-in Handy) for a lighter *runtime* (no DevMode, cloud/remote
stays intact), where the others trade the reverse.

It also adds X2D / H2D / H2S / H2C / P2S / X1E support, bundled slicing, and runs
on aarch64 / Termux / Android — none of which the one other signing-capable
project (`bambu-mcp`, Node-only, P1/X1/A1 only) offers.

## Comparison matrix

Legend: ✓ yes · ✗ no · ◐ partial / conditional · — N/A. **"Signed FW"** =
Jan-2025+ authorization-control firmware. **"DevMode"** = the printer's Developer
LAN Mode, which disables cloud + auth verification.

| Project | LAN-only (no cloud acct) | Supports signed FW | **Start print on signed FW over LAN** | X2D / H2D | Slicing | AMS read / map | HA / MCP / Web UI | Lang + aarch64/Termux | License · maintenance |
|---|:---:|:---:|:---:|:---:|:---:|:---:|---|---|---|
| **beambam** (this project) | ◐ runtime only — key needs a cloud-provisioned Handy | ✓ recovers per-install key, signs `print.*` | **✓ FTP + signed `project_file`+`url_enc`, no DevMode + no live cloud** | ✓ X2D/H2D/H2S/H2C/P2S/X1E | ✓ bundled BambuStudio | ✓ read + auto-match | HA + MCP + web UI | Python · ✓ aarch64/Termux/Android | — |
| [schwarztim/bambu-mcp](https://github.com/schwarztim/bambu-mcp) | ✓ | ◐ signs **only if user supplies** key/cert; unsigned fallback | ◐ control if key supplied; `.3mf` start needs DevMode | ✗ P1/X1/A1 only | ✗ | ✓ | MCP server | TS/Node 18+ | MIT · active |
| [greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab) (pybambu) | ◐ read LAN-only; cloud acct to set up | ◐ read ok; write needs **DevMode** | ◐ only via DevMode (severs cloud) | partial (read) | ✗ | ✓ | HA integration | Python (under HA) | active |
| [BambuTools/bambulabs_api](https://github.com/BambuTools/bambulabs_api) | ✓ (IP + access code) | ✗ no signing in client | ◐ `start_print_3mf()` but needs DevMode on signed FW | ✗ X1 partial, H2D untested | ✗ | ◐ read | library | Python | MIT · active |
| [mattcar15/bambu-connect](https://github.com/mattcar15/bambu-connect) | ◐ IP + access code | ✗ pre-wall | ✗ | ✗ P1S/A1mini | ✗ | ◐ | library | Python | MIT · stale (Aug 2024) |
| [THE-SIMPLE-MARK/bambu-node](https://github.com/THE-SIMPLE-MARK/bambu-node) | ✓ LAN MQTT | ✗ no signing | ✗ | ✗ | ✗ | ◐ | node lib | TS/Node | — |
| [davglass/bambu-cli](https://github.com/davglass/bambu-cli) | ◐ cloud login then local | ✗ | ✗ | ✗ | ✗ | ✓ AMS | CLI | Node | **archived Jan 2025, non-functional** |
| [Doridian/OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) | — (docs) | ✓ documents the per-device cert scheme | — (spec, not a tool) | — | — | — | docs | Markdown | GFDL-1.3 · active |
| jarczakpawel/OrcaSlicer-bambulab (GN/Rossmann re-host) | ✗ re-attaches to Bambu **cloud** | ✗ (not the signing problem) | ✗ (cloud slicer, not LAN signed control) | — | ✓ slicer | ✓ | slicer GUI | C++ desktop | AGPL-3.0 · **repo wiped after C&D** |
| OrcaSlicer / BambuStudio (official) | ◐ LAN print only via **DevMode** | ◐ Studio is an authorized cloud client; Orca needs DevMode | ◐ via DevMode or via Bambu cloud | ✓ (Studio) | ✓ | ✓ | desktop | C++ desktop (no Termux) | AGPL / mixed · active |

## Honest caveats

- **beambam's "no cloud" is a *runtime* claim, not "no cloud account ever."** The
  per-installation signing key is Bambu-CA-issued (a self-signed substitute is
  rejected with `84033545`), so it only exists in Handy because someone signed in
  with a Bambu account and bound the printer. beambam reads that key out of Handy
  (one-time, over `adb`) — after which control + print run over LAN with no
  Developer Mode and no live cloud connection. So a cloud account is a one-time
  *setup* prerequisite; it just isn't a *runtime* dependency. (This is a heavier
  setup than the DevMode toggle other tools use, but it doesn't sever cloud/remote
  access the way DevMode does.)
- **`bambu-mcp`'s "defeats signed firmware" framing is overstated, not wholly
  false.** Its README implies the bundled public Bambu Connect cert auto-signs
  every command; its actual code (`src/mqtt-client.ts`) signs only with a
  *user-provided* key and falls back to unsigned. We have no end-to-end report of
  it starting a `.3mf` print without Developer Mode — treat that capability as
  unverified.
- **The exact error codes** (`84033543` no signature → `84033545` cert not
  authorized → `84033548` bad signature → `84033544` sig+cert pass / downstream
  fail → `0` accepted) are beambam's own observations against an X2D. Public
  sources reference the symptom (`MQTT command verification failed`) but not these
  numeric codes.
- **The earlier "a cloud-registered task is required before ANY MQTT print"
  note is now disproven for beambam.** We start a print over pure LAN (FTP +
  signed `project_file` + `url_enc`) with no cloud task — see
  [`tests/test_x2d_project_file.py`](../tests/test_x2d_project_file.py), verified
  live (`err_code 0`, print starts).
- **`ha-bambulab` newer-model read support** (H2D/X2D sensors): write needs
  DevMode and read survives, but the exact set of implemented read models isn't
  enumerated here — check its `models.py` if you need that precise.

*Star counts / dates are approximate from search snippets; the structural facts
(DevMode requirement, signing approach, language, archival status) were
cross-verified against each project's source or README.*
