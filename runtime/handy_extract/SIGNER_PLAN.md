# Printer-control signer — plan & findings

Goal: let beambam produce the RSA-SHA256 signature that the printer requires for
`print.*` MQTT control (verified enforced — unsigned `print.pause` →
`"mqtt message verify failed"`; only `system.get_access_code` is exempt). See
`HANDY_API.md` and `[[project_printer_control_rsa_wall]]`.

## Where the signing key is NOT (ruled out 2026-06-09)

Reviewed + ran the existing `keystore_dumper/` (runs a DEX **as Bambu's uid** via
Magisk `su <uid>` → uses the app's own AndroidKeyStore master key to unwrap the
Tink keysets) and `dump_keys.py` (Frida RSA extractor — blocked by SHIELD):

- **AndroidKeyStore aliases (ListAliases):** only `…FlutterSecureStoragePluginKey`
  (RSA, the flutter_secure_storage wrap key) and `_androidx_security_master_key_`
  (AES). **No dedicated MQTT-signing key.**
- **FlutterSecureStorage (SecureStorageDumper, fully decrypted):** 7 entries —
  the cloud access_token (×2), email, two token expiries, a timestamp, and a
  `{"secret":"…","iv":"…","tag":"BamBu…"}` device-secret. **No RSA private key.**
- **App files / DBs:** no file contains the cert (`MIIDXDCC…`) or any PEM/DER key.

So the signing keypair is **SHIELD-protected** (whiteboxed in `.ss/l6a18f19c.so`
and/or generated/held in process memory) — not statically extractable. The cert
is persistent across app restarts (same cert each `app_cert_install`), so the key
is stable; it just never touches disk in the clear.

## The path: runtime BoringSSL signer hook in the Zygisk module

The signing primitives are in **libflutter.so's BoringSSL**, resolvable by offset
exactly like `SSL_write` (build-id `0a7fde9b…`, engine `587c18f8…`):

| symbol | vaddr |
|---|---|
| `EVP_DigestSignFinal` | `0x6d1b04` |
| `EVP_PKEY_sign` | `0x6eabcc` |
| `EVP_PKEY_get0_RSA` | `0x6f02e8` |
| `BN_num_bytes` | `0x6cb358` |

Plan (port `handy_hook.js`'s logic into `zygisk/x2dcap/jni/x2dcap.cpp`, hooking
via And64InlineHook at `flutter_base + vaddr`):

1. Hook `EVP_DigestSignFinal(EVP_MD_CTX*, out, *outlen)` (and/or `EVP_PKEY_sign`).
   When it fires for an MQTT publish, walk `EVP_MD_CTX → EVP_PKEY_CTX → EVP_PKEY`,
   call `EVP_PKEY_get0_RSA` to get `RSA*`, then read the BIGNUM limbs (n,e,d,p,q)
   directly (no `BN_bn2bin` export — read the `BIGNUM{d,width}` struct, all
   safe_read-guarded). Emit hex → reconstruct the PKCS#8 PEM with
   `dump_keys.py:reconstruct_pkcs8()` (already written) and verify the pubkey
   fingerprint matches the captured `cert_id` (`77bcfb…CN=GLOF…`).
2. If the RSA key is whitebox/non-materialised (custom `RSA_METHOD`), fall back to
   a **signing oracle**: the hook captures `(tbs, signature)` and/or signs
   beambam-supplied payloads via a local unix socket while Handy runs.
3. Also pin the signed bytes: `payload_len` = serialized-command length **+ 33**
   (fixed addition — nonce/salt/cert-id; reverse it from one captured pair).
4. Wire into beambam: add `header{sign_ver:"v1.0", sign_alg:"RSA_SHA256",
   sign_string, cert_id}` to `beambam/printer.py` / `print_job.py` publishes over
   the cloud broker (`u_<uid>`/token → `device/<serial>/request`).

## UPDATE 2026-06-10: the signer is NOT in libflutter BoringSSL — it's in Dart

Built + deployed the libflutter signer hooks (EVP_PKEY_sign, EVP_DigestSignFinal,
rsa_private_transform_no_self_test @ 0x6d9a68, RSA_parse_private_key @ 0x6f5fb8 —
same dl_iterate_phdr+offset mechanism that makes the SSL_write hook work). Tested
live: Handy sent **6 RSA-signed commands** (`get_access_code`, `liveview.prepare`)
and **not one hook fired**. SSL_write in the same lib captures fine, so the hooks
install correctly — the signing simply does not go through libflutter's BoringSSL.

Where it is instead (narrowed, not yet pinned):
- The signing strings (`sign_alg`/`sign_string`/`RSA_SHA256`/`app_cert_install`)
  are absent as plain C strings from libgojni.so, libflutter.so, AND libapp.so —
  but libapp.so is the **Dart AOT snapshot**, which stores strings in its own pool
  invisible to `strings`. So the command construction + signing lives in **Dart
  app code** (libapp.so).
- Handy IS Flutter; its IoT SDK ships a 19.8 MB Go lib (libgojni.so, contains
  `crypto/rsa`) and the SHIELD whitebox (`.ss/l6a18f19c.so`). The Dart signer is
  therefore one of: (a) **pure-Dart RSA** (e.g. pointycastle — BigInt arithmetic,
  NO native crypto call, key is a Dart object); (b) Dart→**Go FFI** into libgojni's
  `crypto/rsa` (key is a Go `*rsa.PrivateKey`); (c) SHIELD whitebox.

Implications:
- (a) pure-Dart is effectively **unreachable by native (Zygisk) hooks** — there is
  no EVP/RSA native call to intercept; extraction would need AOT-Dart
  instrumentation or Dart-heap scraping of the `RSAPrivateKey` object (very hard).
- (b) Go is hookable but a separate, deeper effort: find the `crypto/rsa` sign in
  libgojni's pclntab, hook with the Go ABI, walk Go `big.Int`s. Uncertain it's even
  the path.
- (c) whitebox is designed to resist exactly this.

Net: the native-BoringSSL signer-extraction approach is a **dead end**; further
progress needs a Dart-layer or Go-layer attack, both substantially harder and of
uncertain payoff. beambam remains fully **read-capable** (cloud status, /f3mf
model downloads) but cannot command the printer without the Dart-held signing key.

## Build note (historical)

Deploying the new module `.so` needs a **full reboot** (zygiskd caches modules at
boot), which on this device also corrupts Handy's launcher (needs the
`pm clear` + reboot recovery → re-login). That's disruptive while a print is
running / the user is logged in, so the hook build is gated on a convenient
reboot window. The hook *code* can be written + built without a reboot; only
deploy+test needs one.
