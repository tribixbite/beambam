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

## Path #6 — fork-surviving CModule hook: tested, CONCLUSIVE (2026-06-07)

`capture_f3mf_cmodule.js` — a pure-native frida **CModule** SSL_write hook
(no JS round-trip per call, so it survives `fork()` into the child). It
**compiles + attaches cleanly** (the libc `open`/`write`/`close` it uses must
be supplied via the CModule symbols dict — TinyCC does NOT auto-link the
process libc; the resolved pointers stay valid across fork). Run with
`F3MF_CHILD_GATING=1 F3MF_CHILD_RESUME_ONLY=1` (gating's atfork prevents the
fork child's inherited-locked-mutex deadlock; resume-only avoids re-attaching,
which re-trips SHIELD).

**Result — the hook works but cannot capture, for two independent reasons:**

1. **SHIELD's fork-escape produces a non-functional half-app.** Child-gating
   raises the surviving fork child from 1 thread (deadlocked husk) to 5
   threads, but a `fork()`-without-`exec` child loses every Flutter / Dart VM /
   ART thread (only the forking thread survives a fork) — so it sits frozen on
   the splash and makes **zero network calls**. The inherited CModule hook is
   valid (conscrypt mapped at the same address) but has nothing to capture.
   SHIELD's REAL functional app is the fork+**exec** child — and `exec` wipes
   the CModule, so a fork-surviving hook can't reach it; re-instrumenting the
   exec child re-trips SHIELD and it dies.

2. **The conscrypt hook is the wrong TLS stack for Bambu anyway.** A root
   `/proc/mem` scan of the NORMAL (un-instrumented, functional) app — even
   while actively scrolling the feed to force fresh requests — catches ONLY
   Firebase/Google traffic (`POST /v1/firelog/legacy/batchlog`, via Conscrypt
   `libssl.so`). Bambu's own API (design-service, user, and the /f3mf
   download) flows through **Flutter's dart:io BoringSSL** (the stripped, anon
   one), whose plaintext request buffers are freed too fast to catch via
   memscan and which the Conscrypt export hook never sees.

### Bottom line

In-app instrumentation of Bambu Handy is **fundamentally blocked** on this
build: SHIELD either 0xdead-kills (defeated by the stealth server) or, failing
that, fork-escapes the app into a non-functional husk. And the only catchable
TLS stack (Conscrypt) doesn't carry Bambu traffic. Capturing a live /f3mf
request would require ONE of these MAJOR efforts:

- a **Zygisk module** doing inline SSL_write hooking with an in-tree hook lib
  (Dobby/ShadowHook) — ZERO frida footprint, present from zygote-fork, so
  SHIELD sees a normal process and never fork-escapes; OR
- **rebuild frida from source** with a renamed agent memfd (+ whatever else
  SHIELD's residual scan keys on) for FULL stealth so the fork-escape never
  fires, then hook the Flutter dart:io BoringSSL SSL_write (still needs a
  working signature/resolver for the stripped BoringSSL).

Neither is a quick patch. The pragmatic status: the 0xdead wall is broken and
documented; normal printing works without any of this.

## Path #7 — Zygisk module, in-app SSL_write hook (2026-06-07) ✅ live

`zygisk/x2dcap/` — a Zygisk module (ReZygisk, API v4) mapped at zygote
specialization, ZERO frida footprint. With NoHello hiding root/module traces
(Enforce-DenyList OFF + Handy on the denylist), SHIELD sees a normal process,
never fork-escapes, and Handy stays **fully functional** (96–111 threads, login
+ print flow all work) with our inline hook live. This defeats the Path #6
fork-escape wall completely.

The module hooks SSL_write via And64InlineHook in two places:

- **Conscrypt** `libssl.so!SSL_write` — named export, resolved from
  `/proc/self/maps` + a safe_read-guarded dynsym walk. Carries Firebase only,
  confirmed live (`SSL_HOOK_INSTALLED`).
- **Flutter dart:io BoringSSL** `SSL_write` — the /f3mf path.

### Key correction: Flutter BoringSSL is APK-backed, NOT anonymous

Paths #4–#6 assumed SHIELD unpacks libflutter.so into anonymous executable
memory. **Wrong on this build.** A `/proc/<pid>/maps` audit of functional Handy
shows **zero** anonymous executable ranges. The app ships `extractNativeLibs=
false`, so the linker mmaps every `.so` **straight out of the split APK**
(`split_config.arm64_v8a.apk`, 35 exec mappings) — maps show the APK path, never
`libflutter.so`. So the anon-string-hunt + ELF-backtrack of earlier builds could
never resolve it (there is no anon ELF to backtrack to).

### How SSL_write is located now

1. `libflutter.so` exports only 50 symbols (Flutter GPU + JNI_OnLoad); SSL_write
   is stripped from `.dynsym` and there is no `.symtab`.
2. The binary embeds the engine commit (`587c18f873b8…`) and BoringSSL source
   paths. Flutter publishes the matching **unstripped** engine
   `symbols.zip`; its `libflutter.so` has the SAME Build ID
   (`0a7fde9baaf490ad50a8480ebc422ea4ee862a2e`) as the device binary, so its
   symbols are exact. `nm` → `SSL_write` at vaddr **`0x717ef0`**.
3. At runtime the module calls `dl_iterate_phdr` — the linker still tracks the
   APK-mapped object as `…/split_config.arm64_v8a.apk!/lib/arm64-v8a/
   libflutter.so`, so we match on substring `libflutter.so` and read its load
   bias. `SSL_write = bias + 0x717ef0`, sanity-checked against the prologue bytes
   `ff 03 01 d1 fe 5f 01 a9` (`sub sp,#0x40 ; stp x30,x23,[sp,#0x10]`) so an
   engine bump can never make us hook a wrong address.

The capture filters request header blocks carrying `x-bbl` / `/f3mf` /
`design-service` into `<data_dir>/cache/x2d_f3mf.txt` (read back as root), and
logs the request-line of every HTTP write to logcat (`REQ [flutter] …`) for
diagnosis. A module `.so` change needs a FULL REBOOT — zygiskd64 caches the
module at boot, and killing it breaks 64-bit zygisk irrecoverably.

**Status:** module + Flutter hook deployed via the offline-resolved offset;
pending a reboot + a live /f3mf download to confirm the dart:io path is what
the hook sees (the alternative, if it is not, is `libgojni.so`'s Go crypto/tls
— a separate, non-BoringSSL stack).
