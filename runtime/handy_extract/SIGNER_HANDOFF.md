# Cracking the Bambu Handy printer-control signature — handoff brief

**Objective:** let `beambam` issue printer-control commands (start/stop/skip/
filament/live-view). Reading is solved (cloud status + `/f3mf` model downloads).
Control is gated by an **RSA-SHA256 signature the printer verifies** and we can't
yet produce. This is the whole problem.

## The wall (all verified live, 2026-06-09/10)

Handy controls the printer over MQTT: cloud broker `us.mqtt.bambulab.com:8883`,
user `u_<uid>`, password = cloud `access_token`; publishes JSON to
`device/<serial>/request`, subscribes `device/<serial>/report`. (LAN broker is the
same; LAN-direct is also cert-walled — issues #65/#66/#68.)

Control commands carry a `header` sibling:
```json
{"print":{"command":"pause","sequence_id":"…","timestamp":…},
 "user_id":"<uid>",
 "header":{"sign_ver":"v1.0","sign_alg":"RSA_SHA256",
           "sign_string":"<base64 256-byte RSA-2048 sig>",
           "cert_id":"0123456789abcdef0123456789abcdefCN=GLOF1000000000.bambulab.com",
           "payload_len":98}}
```
- `cert_id` = `<md5(SubjectPublicKeyInfo DER)>` + `CN=GLOF<printerSerial>.bambulab.com`.
  The app installs its X.509 cert into the printer via the **unsigned**
  `security.app_cert_install` (cert CN=`GLOF<serial>`, issued by Bambu CA, valid
  2025→2026, **identical across app restarts → keypair is persistent**).
- `payload_len` = `len(compact-JSON of the command object)` **+ a constant 33
  bytes**. Reverse this exact pre-image from one captured (payload, sig) pair —
  it's needed whether you extract the key or build an oracle.

**Enforcement is per-command (proved by sending unsigned cmds via paho with our
own token):**
- `system.get_access_code` UNSIGNED → `result:success` (NOT verified — bootstrap exempt).
- `print.pause` UNSIGNED → `{"reason":"mqtt message verify failed","err_code":84033543,"result":"failed"}`, print untouched. **print.\* requires a valid sig.**

So: token alone lets you READ (pushall/subscribe) and call a few exempt cmds, but
NOT control. You must produce the signature.

## Where the key is NOT (ruled out — don't re-walk these)

- **AndroidKeyStore:** only 2 aliases — `…FlutterSecureStoragePluginKey` (storage
  wrap RSA) + `_androidx_security_master_key_` (prefs AES). No signing key.
- **FlutterSecureStorage:** fully decrypted (run `keystore_dumper` — it runs a DEX
  *as Bambu's uid* via Magisk `su <uid>`, unwraps the Tink keysets with the app's
  own master key). 7 entries: cloud access_token ×2, email, two expiries, a
  timestamp, and `{"secret":…,"iv":…,"tag":"BamBu…"}` device-secret. **No RSA key.**
- **App files / DBs:** no file contains the cert (`MIIDXDCC…`) or any PEM/DER key.
- **libflutter.so BoringSSL:** built + deployed + LIVE-TESTED Zygisk hooks on
  `EVP_PKEY_sign` (0x6eabcc), `EVP_DigestSignFinal` (0x6d1b04),
  `rsa_private_transform_no_self_test` (0x6d9a68, RSA* is arg0),
  `RSA_parse_private_key` (0x6f5fb8). Handy sent **6 RSA-signed commands; ZERO
  hooks fired** — while the SSL_write hook in the *same* lib (same
  dl_iterate_phdr+offset mechanism) captures fine. **The signing does NOT use
  libflutter's BoringSSL.**

## Where the key IS (narrowed, not pinned)

The signing strings (`sign_alg`/`sign_string`/`RSA_SHA256`/`app_cert_install`)
are absent as plain C strings from libgojni.so / libflutter.so / libapp.so — BUT
**libapp.so is the Dart AOT snapshot** (its strings live in a Dart pool invisible
to `strings`). So the command build + signing is **Dart app code**. Therefore the
signer is one of:
1. **pure-Dart RSA** (e.g. pointycastle): BigInt math, NO native crypto call →
   invisible to native Zygisk hooks. Key = a Dart `RSAPrivateKey` object on the
   Dart heap. **(most likely; worst case)**
2. **Dart → Go FFI** into **libgojni.so** (19.8 MB gomobile lib; confirmed
   contains `crypto/rsa`, `crypto/ecdsa`, `crypto/x509`). Key = a Go
   `*rsa.PrivateKey`. **(hookable but deep)**
3. **SHIELD whitebox** (`/data/data/bbl.intl.bambulab.com/files/.ss/l6a18f19c.so`,
   Promon) — designed to resist key extraction.

## Concrete next experiments (in rough priority)

1. **Distinguish Dart-vs-Go quickly.** Add a Zygisk hook on libgojni's
   `crypto/rsa` sign (resolve via Go pclntab — Go keeps func names; find
   `crypto/rsa.signPKCS1v15` / `(*PrivateKey).Sign` / the FIPS
   `crypto/internal/fips140/rsa` path). Mind the **Go register ABI** (args in
   x0.. for Go 1.17+; libgojni is recent Go — `crypto/internal/fips140` implies
   Go ≥1.24). If it fires on a control-command sign → key is a Go big.Int slice,
   walk it. If it never fires → it's pure-Dart (go to 3).
2. **Catch the SHA-256 over the pre-image.** Whatever lib signs, it first hashes
   `(command-json + 33 bytes)`. Hook `SHA256_Update`/`EVP_DigestUpdate` in
   *every* mapped libcrypto (conscrypt/art/system are exported, hook by NAME; the
   33-byte tail is the prize) to recover the exact signed pre-image even if you
   never get the key — useful for an oracle and for verifying any forged sig.
3. **Dart-heap / AOT attack (if pure-Dart).** No native call to intercept. Options:
   scrape the Dart heap for the `RSAPrivateKey` (n,d,p,q as Dart BigInts —
   understand the AOT object layout), or instrument the Dart VM. Very hard.
4. **Signing oracle instead of key.** If you can hook *whatever* produces
   `sign_string` (Go func, or a Dart FFI entry), expose it over a unix socket so
   beambam asks the running Handy to sign its own payloads. Doesn't need the raw
   key; needs Handy alive + the right hook point (same discovery problem).
5. **Re-confirm hook installs (sanity).** SSL_write proves A64HookFunction +
   offsets work in libflutter, so the "0 fires" is real, not a broken hook.

## Tooling already built (reuse, don't rebuild)

- `runtime/handy_extract/zygisk/x2dcap/` — the Zygisk module (SHIELD-tolerated via
  NoHello + denylist). Hooks SSL_write (capture) + the inert signer hooks. Resolve
  any libflutter symbol by offset from the **unstripped engine** (`symbols.zip`,
  engine `587c18f8…`, build-id `0a7fde9b…`; `nm` it). `safe_read` (pipe-EFAULT)
  guards every out-of-our-memory deref so wrong offsets can't crash Handy.
- `decode_raw_h2.py` / `analyze_capture.py` — decode the raw SSL_write capture
  (HTTP/2 HPACK + MQTT). `dump_keys.py` (Frida, blocked) has the BIGNUM→PKCS#8
  `reconstruct_pkcs8()` you'll want offline. `keystore_dumper/` (run-as-app-uid
  DEX) dumps any EncryptedSharedPreferences/keystore-wrapped secret.
- Full protocol + endpoint catalog: `HANDY_API.md`. Raw captures + decoded MQTT +
  a downloaded sample 3mf: user's `~/storage/shared/Download/x2d-extract/`.

## Operational gotchas (this device)

- Loading a new module `.so` needs a **full reboot** (zygiskd caches at boot).
- **Every reboot corrupts Handy's launcher resolution** (getActivityInfo→null;
  `am`/`monkey`/`pm` all fail to launch it). The only WARM fix is a framework
  restart (`stop; start`) — **BANNED** on this phone: it deep-bricks to a state a
  power-press can't wake. `pm clear`, in-place split reinstall, package toggle,
  and cold boot all FAIL to clear it. Workaround that DOES work: **the user taps
  the home-screen icon** (Pixel Launcher's `LauncherApps.startMainActivity` uses a
  cached component, bypassing the broken resolve). So: reboot → user taps icon →
  app opens (login survives if you skip `pm clear`).
- Trigger a control-command sign WITHOUT a real print: just open the **Devices**
  tab — Handy auto-sends signed `get_access_code` + `liveview.prepare` on connect.
- adb-over-wifi is pinned to **:5555** (module `service.sh`, persist prop); IP is
  DHCP — rediscover with `nmap -p5555 --open 192.168.0.0/24` + model==Saga.
