# Bambu Handy key extraction (rooted-Android Frida hook)

Goal: recover the per-installation X.509 cert + RSA private key the Bambu
Handy Android app uses to sign LAN MQTT publishes against an X-series Bambu
printer, so our `x2d_bridge.py` can sign LAN `print.*` commands without
going through Bambu's cloud.

## Why this isn't already done elsewhere

- Static APK extraction failed on Bambu Handy v3.19.0: `libapp.so` is Flutter
  Dart AOT, all strings encrypted in-snapshot; `assets/l6a18f19c_a64.so` is a
  packer loader stub (closest match: Promon SHIELD ≥7.0 + Tencent Tinker
  hot-patch via `assets/patch.dex`); `assets/kqkticwjgzy.dat` is the
  encrypted payload, decoded only at runtime.
- Bambu's desktop plugin `libbambu_networking.so` is fully Virbox-protected.
- The Jan-2025 Bambu Connect cert leak doesn't help — the printer's trust
  list (`security.app_cert_list`) on firmware 01.01.00.00 doesn't include
  it. Per-installation Handy certs have not publicly leaked.

## What this does instead

Hooks every plausible signing primitive (`EVP_PKEY_sign`, `RSA_sign`,
`RSA_private_encrypt`, `EVP_PKEY_get1_RSA`, `mbedtls_pk_sign`) and every
plausible AES decrypt that could carry a PEM/DER-wrapped cert/key
(`EVP_DecryptUpdate`, `EVP_DecryptFinal_ex`, `mbedtls_aes_crypt_cbc`)
inside the running Handy process, walks the in-memory RSA/AES context bytes
out, and reconstructs PKCS#8 PEMs on the host.

Uses `hzzheyang/strongR-frida-android` (anti-detect Frida) because the
packer scans for vanilla `frida-server` symbols + process names.

## Files

| File | Purpose |
|---|---|
| `setup_rooted_device.sh` | One-shot: pushes StrongR-Frida server, installs Bambu Handy from your local backup tarball, exposes :27042 via `adb forward`. |
| `handy_hook.js` | The Frida script — hooks crypto, sniffs decrypts, emits structured events. |
| `dump_keys.py` | Host runner — feeds the hook in, reassembles PKCS#8 PEMs from BIGNUM hex, classifies sniffed blobs, writes a session dir. |
| `capture_f3mf_token.js` | Standalone Frida hook — captures Handy's plaintext HTTP request to `/api/v1/design-service/instance/<id>/f3mf` right before TLS encrypts, including the SHIELD-baked auth header(s) that bypass Bambu's 418-captcha rate limit. |
| `capture_f3mf_token.py` | Host runner for `capture_f3mf_token.js`. Writes each captured request to `./captured_tokens/<ts>.txt` and exports the most-recent auth set to `~/.x2d/handy_token.json`. The beambam `cloud_client.get_instance_download_url` auto-replays this token (if <30 min old) on every `/f3mf` call → bypasses captcha. |
| `cache/` | Cached frida-server binaries (gitignored). |

## /f3mf captcha bypass — quick runbook

The `/api/v1/design-service/instance/<id>/f3mf?type=download` endpoint hard-rate-limits at ~10 anonymous-ish calls per IP per window (HTTP 418 with `captchaId`). Bambu Handy never hits this because the SHIELD-packed `libapp.so` injects a per-session auth header set our Python client doesn't have. Capture them once, replay from beambam:

```bash
# 1) Bootstrap a rooted device + frida-server (same as the cert-extraction runbook).
adb forward tcp:27042 tcp:27042

# 2) Run the capture hook. Spawns Handy and watches every TLS-bound SSL_write.
python3 capture_f3mf_token.py

# 3) On the device: open ANY MakerWorld design → tap Download. The hook
#    fires once. ~/.x2d/handy_token.json is written.

# 4) Ctrl-C the runner. From now on, beambam's cloud_client auto-includes
#    the captured headers when they're <30 min old. Try:
beambam cloud-pull-design 2706164 --instance-id 3 --out-dir /tmp/charm
# (would have 418'd before — now succeeds while the token is fresh)
```

Tokens rotate quickly (≈30 min Bambu-side TTL). When beambam starts returning 418 again, re-capture.

## Runbook

```bash
# 1) Plug in the rooted device (or `adb connect <ip:port>` over WiFi).
adb devices

# 2) Bootstrap. Idempotent — re-runnable.
./setup_rooted_device.sh

# 3) Launch Bambu Handy on the device, log in.

# 4) From the host, attach:
python3 dump_keys.py --attach
# (or omit --attach to spawn fresh).

# 5) On the device: tap your printer in the device list, try pause / resume
# / light toggle / send-print. Each operation that touches LAN-MQTT will
# fire one or more hooks and emit `rsa_key` / `blob` events.

# 6) Ctrl-C. Output lands in:
ls ~/.local/share/x2d/handy_dump/<unix-ts>/
#   trace.log
#   rsa_1.pem        ← the private key we want
#   cert_1.pem       ← matching X.509 (if sniffed via AES decrypt)
#   SUMMARY.md       ← cert subjects, fingerprints, candidate cert_ids

# 7) Wire into our bridge:
cp ~/.local/share/x2d/handy_dump/<ts>/rsa_1.pem  ~/.x2d/bambu_app.key
cp ~/.local/share/x2d/handy_dump/<ts>/cert_1.pem ~/.x2d/bambu_app.crt
# Then tell sign_payload() to load these instead of bambu_cert.py's hardcoded leak.
```

## What success looks like

`SUMMARY.md` contains a section like:

```markdown
## rsa_1.pem (RSA-2048, hook=`libcrypto.so!EVP_PKEY_sign`)
- pubkey MD5  : `0123456789abcdef0123456789abcdef`
- pubkey SHA1 : `…`
- candidate cert_ids the printer might trust:
  - `0123456789abcdef0123456789abcdefCN=GLOF1000000000.bambulab.com`
```

If MD5 matches one of the X2D's `app_cert_list` entries (which we already
know are `4a63…` and `77bcfb…`), we have a key whose pubkey is in the
factory trust list. Sign with that key + that cert_id and `print.*`
should clear `84033545/47/48` to `result: success`.

## If it doesn't work

In rough order, things that could go wrong and how to diagnose them:

1. **frida-server crashes immediately on launch.** Packer detected the
   server. Try a newer StrongR release: `FRIDA_VER=16.6.x ./setup_rooted_device.sh`.
   Fall back to renaming the on-disk binary and TCP port:
   `adb shell su -c '/data/local/tmp/frida-server -l 0.0.0.0:9999'`.
2. **`dump_keys.py` exits with `_frida.ProcessNotFoundError`.** App is in
   tamper-detect kill-loop. Disable Magisk's MagiskHide for the package, or
   re-launch via `dump_keys.py` (no `--attach`) so we spawn pre-init.
3. **No `rsa_key` events fire even when the app signs.** The packer
   relocates libcrypto symbols, or signing happens in a statically-linked
   crypto blob inside `libapp.so`. Drop `Module.findExportByName` and use
   the SensePost pattern-scan technique against the Dart AOT — find the
   sign primitive by byte signature: PKCS#1v15 padding produces a
   distinctive `00 01 ff ff ff … 00` prefix that's emitted just before
   the modular exponentiation.
4. **Hook fires but `n/d/p/q` are empty.** The RSA struct offset probe
   missed; the loop tries 16/24/32/40/48 — extend if needed. Latest
   BoringSSL puts BIGNUMs at offset 16 from the RSA* (after the refs +
   ENGINE pointer); OpenSSL 3.x uses a different layout via providers.
5. **AES decrypts emit nothing.** App is using libsodium / ChaCha20 instead
   of AES. Add hooks for `crypto_aead_chacha20poly1305_decrypt` and
   `crypto_secretbox_open_easy`.

Each of these has a documented workaround; the README in the parent dir
captures any updates we make as we run this against your actual device.

## Path #4 — process-memory dump of unpacked shield (2026-04-30)

Goal: capture the unpacked Promon SHIELD code from anonymous executable
mappings so we can statically locate the conditional branch that gates the
0xdead5019 tamper-die `BR x0`, then patch it.

### Pipeline

| File | Purpose |
|---|---|
| `dump_unpacker.sh` | Force-stops Bambu, optionally flips `enabled:false` in `/data/local/tmp/re.zyg.fri/config.json` to disable Frida targeting (no Magisk module mutation, no reboot), launches via monkey, polls `/proc/PID/maps` every 200 ms for `r-xp 00000000 00:00 0` mappings tagged `[anon:.bss]`, then `dd if=/proc/PID/mem` each region to `/data/local/tmp/handy_anon_*.bin` and pulls to `cache/anon_dumps/`. |
| `analyze_shield.py` | Capstone-disassembles each dump and scans for (a) MOVZ+MOVK pairs producing 0xdead5019 across all 31 GP regs and both half orderings, (b) raw 32-bit and 64-bit literal occurrences of 0xdead5019, (c) every LDR-literal that references such a literal. For each hit: prints the function prologue offset, the conditional-branch gate, and the patch byte to write. |
| `find_brx0.py` | Lists every `BR x0` (`0xd61f0000`) site in the shield region with 16-instruction context — used to confirm Promon obfuscation pattern. |
| `scan_xor_keys.py` | Scans for XOR-encoded representations of 0xdead5019 and the rev/rbit/~/- variants in the shield's data section. |
| `shield_patch.js` | Frida shim that locates the shield region by BR-x0 density, then installs a `Process.setExceptionHandler` to absorb the SIGBUS at 0xdead5019. **TODO**: replace the absorb-only handler with a `pthread_exit`-on-Thread-2 jump to keep the process running. |

### Findings

1. **Three anonymous executable mappings appear in the running process**:
   - `[anon:.bss] 0x705e482000 size 0x2e4000` (3.03 MB) — **the shield**, BR-x0 count 141
   - `[anon:.bss] 0x7030179000 size 0x9f2000` (10.4 MB) — Flutter VM JIT, 0 BR-x0
   - `[anon:.bss] 0x7030b78000 size 0x142f000` (20.5 MB) — Flutter VM heap, 0 BR-x0
2. **Static analysis cannot find a patch site.** Across all dumps:
   - 0 MOVZ+MOVK pairs that materialize 0xdead5019 (any reg, any order).
   - 0 raw 32-bit literal `0xdead5019` occurrences.
   - 0 raw 64-bit literal occurrences.
   - 0 hits for rev/rbit/~/- transformations.
   - Only 1 non-trivial XOR pair (a XOR b == 0xdead5019), 1.4 MB apart in
     the dump → almost certainly coincidental.
3. **The shield is fully Promon-obfuscated.** Every BR x0 site is preceded
   by an XOR-swap-style identity sequence
   (`add Xn,Xn,Xm; sub Xm,Xn,Xm; sub Xn,Xn,Xm`) that reduces to a no-op,
   plus stack loads and small-immediate sub/adds. The magic value
   0xdead5019 is **constructed at runtime** through arithmetic over
   register values whose origins are themselves obfuscated stack loads.
   There is **no static instruction byte sequence that always produces
   0xdead5019** — meaning there is no static patch-the-decision target.

### Implication

Path #4 (memory dump → static patch) does not yield a clean fix. The shield
must be defeated either:

a. **Dynamically** by intercepting all 141 BR x0 sites with Frida + Stalker
   and overwriting x0 just before the BR — but the shield CRCs its own
   pages once per second (typical Promon design), so any patched
   instruction triggers a re-tamper-detection.

b. **By absorbing SIGBUS** at 0xdead5019 and re-routing the dying thread to
   `pthread_exit` so only the tamper-detection thread dies (the rest of the
   process keeps running). This is what `shield_patch.js` sets up.

c. **By disabling the unpacker entirely** — patch `assets/l6a18f19c_a64.so`'s
   loader stub in `libapp.so` so the shield never gets mapped. This is
   path #2 and lives in the parent `runtime/handy_extract/` discussion.

The dumps themselves remain valuable for offline analysis of the shield's
syscall set and for any future signature-based detection of the shield
across other Bambu app versions.

## Path #5 — frida-stealth defeats SHIELD's 0xdead kill (2026-06-03) ✅

Breakthrough: a **surgically string-patched frida-server** lets Bambu Handy
boot to its UI **under live Frida instrumentation** — no more 0xdead crash.

### What the SHIELD watchdog actually keys on

Empirically established by spawning a benign app under an unpatched
frida-server and reading the target's own `/proc/<pid>/{maps,task/*/comm}`:

| Surface | Token the watchdog greps |
|---|---|
| `/proc/<pid>/maps` | `/memfd:frida-agent-64.so` (the injected agent) |
| `/proc/<pid>/task/*/comm` | `gum-js-loop`, `pool-frida` (frida thread names) |

A controlled test proved the **kill trigger is the `gum-js-loop` comm thread
name**, NOT the maps memfd: after patching only `gum-js-loop`→`qnx-js-loop`
the app survived even though `/memfd:frida-agent-64.so` was STILL in maps.

### The stealth server (`patch_frida_stealth.py`)

Same-length, offset-preserving byte patches to a copy of frida-server 17.9.3
(kept at 17.9.3 to match the host's frida-python). **Surgical, not blanket** —
a blanket `frida`→`monco` replace corrupts GResource lookups: the gresource
hash table stores BUILD-TIME djb2 hashes of the original path strings, so
renaming the path bytes (not the hashes) makes `g_resources_lookup` miss →
`monco_android_helper_service_do_start: assertion failed (backend_class != null)`
at startup. So we touch ONLY target-observable, non-gresource literals:

- `frida-agent-64.so`/`-32.so` → `monco-agent-…` (memfd display name; the
  embedded blob is keyed by the SEPARATE string `frida-agent-arm64.so`, left
  intact, so the agent still loads)
- `gum-js-loop` → `qnx-js-loop` (the JS-loop thread name)

Plus run the binary RENAMED (`msrv`) on a NON-default port so `ps`/port
probes see nothing. `pool-frida` derives from the helper's prgname and is
runtime-constructed (not a static literal we can patch) — it remains, but it
is not the kill trigger.

```bash
# regenerate the stealth server
python3 patch_frida_stealth.py frida-server.orig frida-server.stealth
adb push frida-server.stealth /data/local/tmp/msrv
adb shell su -c 'chmod 755 /data/local/tmp/msrv; nohup /data/local/tmp/msrv -l 127.0.0.1:47999 >/dev/null 2>&1 &'
adb forward tcp:47999 tcp:47999
# capture_f3mf_token.py honours F3MF_FRIDA_HOST=127.0.0.1:47999
```

`scan_anon_ssl.js` gained `STEALTH_MODE=true` (disables the old libc
fork-block — stealth wants the watchdog to RUN and find nothing) and
`tryHookNamedLibssl()` (hooks Conscrypt `libssl.so` SSL_write by export, no
anon signature scan; `ENABLE_ANON_SCAN=false` since the per-second
`Memory.scan` starved Flutter's engine init and stuck the app on its splash).

### SHIELD's deeper defence: fork+exec escape

With 0xdead defeated, SHIELD's fallback is to **fork()+exec()** the app into a
fresh process to shed the agent. The Frida-spawned (instrumented) process
forks; the child inherits the agent's MAPPINGS but not its live threads, and a
mutex held by a vanished agent thread is inherited locked → the child
**deadlocks in `futex_wait` on the splash** (single-threaded husk, `ppid=1`).

Counter: `capture_f3mf_token.py` now enables Frida **child-gating**
(`F3MF_CHILD_GATING=1`, default on) and re-instruments each forked/exec'd
child. This DOES follow the escape (logs `child-added … origin=fork` then
`origin=exec`), but the specific instrumented children still get killed while
an UNINSTRUMENTED fork survives (its agent-attach times out — SHIELD re-arms
in the surviving process and rejects late-attached agents). **This is the
current frontier.**

### Why the token isn't in RAM (memscan dead-end) — `extract_token_memscan.py`

A root `/proc/<pid>/mem` scan (no Frida → no tamper trip) of the
normally-running app — fast path via `dd iflag=skip_bytes,count_bytes`
bs=1 MiB, on-device grep so only matches cross adb — finds:

- 3 **Firebase** ES256 JWTs (`appId/exp/fid/projectNumber`), via Conscrypt,
- `bambulab.com` ×332 / `api.bambu` ×12 ASCII strings (so memory IS readable
  and Dart strings are 1-byte ASCII, not UTF-16),
- but **NO** `Authorization: Bearer`, no `accessToken`, no Bambu JWT, no
  `Cookie`.

Conclusion: Bambu's cloud/MakerWorld API auth is **per-request SIGNED
headers** (`x-bbl-*`/`x-jiange-*`/`x-csrf-*`), computed transiently and freed
— there is no stable token to lift from memory. That is precisely why the
design has always been **live SSL_write capture**. Firebase (Java/OkHttp →
Conscrypt) plaintext is catchable; the Bambu calls likely flow over Flutter's
dart:io BoringSSL (stripped, anon) — so a surviving-instrumented capture must
hook Conscrypt (confirmed working) AND, if Bambu uses dart:io, the Flutter
BoringSSL SSL_write.

### Net state

- ✅ SHIELD's immediate 0xdead tamper-kill is DEFEATED (stealth server) — the
  app reaches its UI under instrumentation. The "hard wall" is broken.
- ⏳ Capturing a live /f3mf request still needs the instrumented process to
  survive SHIELD's fork+exec re-spawn (child-gating refinement) — next step.
- ✓ Normal printing is unaffected: `cloud-task-export` + `--from-3mf` already
  cover it; the token is only needed to bypass the MakerWorld /f3mf captcha.
