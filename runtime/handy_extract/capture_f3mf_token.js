// capture_f3mf_token.js — Frida hook to capture Bambu Handy's /f3mf
// download request, including the captcha-bypass auth header(s) baked
// into the SHIELD-packed binary.
//
// Why this exists: Bambu's /api/v1/design-service/instance/<id>/f3mf
// endpoint returns HTTP 418 (captcha) after ~10 anonymous-ish API
// downloads per IP window. Handy never gets captcha'd because it sends
// a hidden bearer/cookie/X-* header set by the Promon-SHIELD-protected
// libflutter.so at request time. The token has a per-session TTL but
// can be replayed by beambam to bypass the rate limit for that window.
//
// What we capture:
//   - Full plaintext HTTP request headers + method + URL right before
//     they enter TLS. Hooked at the TLS write boundary (BoringSSL
//     SSL_write, in libflutter.so or system libssl).
//   - Filtered to lines containing `/design-service/instance/` AND
//     `f3mf` so we ignore noise (other API calls, MQTT, etc.).
//
// Host-side runner: capture_f3mf_token.py (sister file). It saves
// captured tokens to ./captured_tokens/<timestamp>.txt and updates
// ~/.x2d/handy_token.txt with the most-recent valid set, which
// beambam's cloud_client can then read via `--use-handy-token`.
//
// Runbook:
//   1) Boot Handy on rooted device with frida-server running (see
//      handy_hook.js bootstrap).
//   2) frida -U -f bbl.intl.bambulab.com -l capture_f3mf_token.js
//      --no-pause
//   3) In Handy: open ANY MakerWorld design → tap Download.
//   4) Token + headers logged to stdout; copy to beambam config.
//
// LIMITATION: tokens are short-lived (best-effort 30 min). Re-capture
// when beambam starts returning 418 again.
//
// ============================================================
// 2026-06-02 STATUS — what works, and the remaining wall
// ============================================================
// WORKS (verified against bbl.intl.bambulab.com v3.19.0, frida 17.9.3,
// rooted Android 14):
//   * Spawn-gated injection defeats the Promon SHIELD anti-instrumentation
//     — the agent injects at zygote-fork before SHIELD arms; the
//     concatenated stalker_syscalls.js then SUPPRESSES SHIELD's raw `svc`
//     tamper-response (verified: "suppressed svc nr=131 (tgkill)"), so the
//     hook loads + survives where late-attach times out.
//   * Frida-17 module API (Module.findExportByName was REMOVED — we use
//     Process.enumerateModules + module.findExportByName / getGlobalExportByName).
//   * The in-app Stalker STARVES Frida's JS event loop — setTimeout AND
//     setInterval AND rpc callbacks never fire. So the SSL_write rescan is
//     driven from the dlopen Interceptor callback (app-thread-driven, works)
//     and from the Python side. NEVER rely on JS timers under stalker.
//   * SSL_write IS found + hooked on the SYSTEM libssl.so (554 exports,
//     SSL_read+SSL_write present).
//
// THE WALL:
//   * Bambu Handy sends ZERO HTTP through system libssl.so — its dart:io /
//     HTTP stack uses a BUNDLED BoringSSL. That BoringSSL is NOT a named
//     module: the SHIELD packer (assets/kqkticwjgzy.dat encrypted payload,
//     l6a18f19c loader stub) DECRYPTS the real code, including BoringSSL,
//     into ANONYMOUS RX memory at runtime. enumerateModules() shows only
//     base.odex / libssl / libcrypto — never libflutter.so / libapp.so.
//   * So SSL_write lives in unnamed executable memory. To hook it the next
//     iteration must EITHER:
//       (a) Memory.scan every RX anon range for the AArch64 BoringSSL
//           SSL_write prologue signature, then Interceptor.attach the match
//           (this is the same anon-memory code-scan the cert-extraction
//           hook in handy_hook.js wrestles with — share that machinery), OR
//       (b) hook the Dart VM's HttpClient at the Dart-runtime level
//           (needs Dart AOT snapshot RE), OR
//       (c) hook sendto/sendmsg syscalls — but that's POST-TLS-encryption,
//           so it yields ciphertext, not the plaintext auth headers we need.
//   Approach (a) is the most tractable. The captured `headers` plumbing,
//   token-export, and beambam-side replay (cloud_client) are all DONE and
//   waiting for a working SSL_write address.
// ============================================================
//
// Author convention: parallel to handy_hook.js — keep the script
// stand-alone so it can be loaded WITHOUT the heavy crypto hooks.

const LOG = (m) => { try { send({type: 'log', msg: '[f3mf] ' + m}); } catch (_) { } };
const EMIT = (kind, body) => {
  try { send({type: kind, body: body}); } catch (_) { }
};

// Top-level diagnostic — fires during initial script evaluation, before
// any setTimeout. If this line is absent from the runner log, the
// concatenated script never reached our half (eval aborted earlier).
LOG('script top-level reached');

// Install the SSL_write hook on dlopen of libssl.so / the flutter lib,
// AND retry on a short poll loop — at spawn time the SSL lib isn't mapped
// yet, so a single setTimeout(500) can fire too early and find nothing.
// Hook android_dlopen_ext so we catch the SSL lib the moment it loads.
// Frida 17.x removed Module.findExportByName(null, name) and
// Module.findExportByName(mod, name). Resolve global + per-module
// symbols by iterating Process modules instead (stable across 16/17).
function resolveGlobal(name) {
  try {
    if (Module.getGlobalExportByName) return Module.getGlobalExportByName(name);
  } catch (_) { }
  for (const m of Process.enumerateModules()) {
    try {
      const a = m.findExportByName ? m.findExportByName(name) : null;
      if (a) return a;
    } catch (_) { }
  }
  return null;
}
function resolveInModule(modname, name) {
  try {
    const m = Process.findModuleByName
      ? Process.findModuleByName(modname)
      : Process.getModuleByName(modname);
    if (m && m.findExportByName) return m.findExportByName(name);
  } catch (_) { }
  return null;
}

let _dlopen_logged = 0;
let _http_logged = 0;
function install_dlopen_watch() {
  const names = ['android_dlopen_ext', 'dlopen'];
  for (const n of names) {
    const a = resolveGlobal(n);
    if (!a) continue;
    try {
      Interceptor.attach(a, {
        onEnter(args) { try { this.path = args[0].readCString(); } catch (_) { } },
        onLeave() {
          if (!this.path) return;
          // Log every dlopen (first 80) so we can see exactly what libs
          // Handy loads + when the SSL/flutter lib appears. This runs on
          // the APP's thread (Interceptor callback), so it executes even
          // though Frida's own event loop is stalker-starved.
          if (_dlopen_logged < 80) {
            _dlopen_logged++;
            LOG('dlopen[' + _dlopen_logged + '] ' + this.path);
          }
          // Rescan SSL_write on EVERY dlopen — cheap (deduped via
          // SEEN_ADDRS) and guarantees we catch the SSL lib the moment a
          // native lib loads, regardless of its path substring.
          hook_ssl_write_everywhere();
        },
      });
      LOG('watching ' + n + ' for SSL lib loads');
    } catch (e) { LOG('dlopen watch failed ' + n + ': ' + e); }
  }
}

// Match the substrings we care about. Both must be present in the
// outgoing buffer for a hit.
const URL_NEEDLE = '/design-service/instance/';
const PATH_NEEDLE = '/f3mf';

// Tracks which SSL_write addresses we've already seen with a successful
// /f3mf hit (so we can skip re-classifying ambiguous SSL_lib exports).
const SEEN_ADDRS = new Set();

// Capture the plaintext bytes BEFORE TLS encryption. Bytes layout per
// BoringSSL: int SSL_write(SSL *ssl, const void *buf, int num).
//   arg0 = SSL*  (we don't need it)
//   arg1 = const void* buf
//   arg2 = int num
function attach_ssl_write(addr, label) {
  try {
    Interceptor.attach(addr, {
      onEnter(args) {
        try {
          const buf = args[1];
          const num = args[2].toInt32();
          if (num <= 16 || num > 65536) return;        // not HTTP-shaped
          // Peek first 4 bytes — must look like ASCII HTTP method or
          // similar. Avoids logging every binary write.
          const first = Memory.readByteArray(buf, 4);
          const fv = new Uint8Array(first);
          // Quick pre-filter: must start with G/P/D/H (GET/POST/PUT/DELETE/HEAD)
          const m0 = fv[0];
          if (m0 !== 0x47 && m0 !== 0x50 && m0 !== 0x44 && m0 !== 0x48) return;

          // Now read up to 16 KiB and check needles.
          const sz = Math.min(num, 16384);
          const data = Memory.readByteArray(buf, sz);
          // Convert to string (Latin-1 / ASCII for headers).
          const txt = Array.from(new Uint8Array(data))
            .map((b) => String.fromCharCode(b))
            .join('');

          // DIAGNOSTIC: log the request-line of EVERY HTTP request seen
          // through this SSL_write (first 60) so we can confirm whether
          // Handy's MakerWorld traffic — and specifically /f3mf — flows
          // through this (system libssl) SSL_write at all. Set
          // F3MF_LOG_ALL=0 in env-less builds to disable; here always on
          // until we've confirmed the path.
          const reqline = txt.split('\r\n')[0] || '';
          if (_http_logged < 60 &&
              /^(GET|POST|PUT|DELETE|HEAD) /.test(reqline)) {
            _http_logged++;
            // Pull the Host header too for context.
            const hm = txt.match(/\r\nHost:\s*([^\r\n]+)/i);
            LOG('HTTP[' + _http_logged + '] ' + reqline.substring(0, 90) +
                '  Host=' + (hm ? hm[1] : '?'));
          }

          if (txt.indexOf(URL_NEEDLE) < 0) return;
          if (txt.indexOf(PATH_NEEDLE) < 0) return;

          // Hit! Mark address as known-good for next time.
          SEEN_ADDRS.add(addr.toString());

          // Split header block (first \r\n\r\n).
          const hdr_end = txt.indexOf('\r\n\r\n');
          const headers = hdr_end > 0 ? txt.substring(0, hdr_end) : txt;

          LOG(`[f3mf] capture via ${label} @ ${addr} (${num} B)`);
          EMIT('f3mf_request', {
            label: label,
            addr: '' + addr,
            num_bytes: num,
            headers: headers,
            timestamp: Date.now() / 1000,
          });
        } catch (e) { LOG(`onEnter error: ${e}`); }
      },
    });
    LOG(`hooked ${label} @ ${addr} for /f3mf capture`);
  } catch (e) { LOG(`attach failed ${label} @ ${addr}: ${e}`); }
}

// Locate BoringSSL's SSL_write. Try multiple module candidates because
// Handy bundles BoringSSL inside libflutter.so AND can also link the
// system libssl.so. The Frida default Module.getExportByName('libssl.so',
// 'SSL_write') sometimes misses the in-flutter copy.
let _diag_runs = 0;
let _flutter_found = false;
function hook_ssl_write_everywhere() {
  // Diagnostic — re-runs (throttled) until the Flutter engine lib loads,
  // since at early-spawn it isn't mapped yet. Confirms what SSL surface
  // is reachable + locates the bundled-BoringSSL host for pattern-scan.
  if (!_flutter_found && _diag_runs < 40) {
    _diag_runs++;
    try {
      const mods = Process.enumerateModules();
      const ssl_mods = mods.filter((m) =>
        /ssl|crypto|flutter|libapp|boring|conscrypt/i.test(m.name));
      LOG(`DIAG: ${mods.length} modules; ssl-ish: ` +
          ssl_mods.map((m) => m.name).join(', '));
      // List Flutter/Dart/app native libs explicitly — these hold the
      // bundled BoringSSL that Handy's dart:io HttpClient actually uses
      // (system libssl carries ZERO Handy traffic, confirmed). The next
      // iteration must pattern-scan SSL_write inside whichever of these
      // is the Flutter engine lib (symbols are stripped).
      const flutter_mods = mods.filter((m) =>
        /flutter|libapp\.so|dart/i.test(m.name) ||
        /base\.apk|split_config|\.bambulab/i.test(m.path || ''));
      if (flutter_mods.length) {
        _flutter_found = true;
        LOG('DIAG: FLUTTER LIBS FOUND: ' +
            flutter_mods.map((m) => m.name + '@' + m.base + ' size=' +
                (m.size || 0) + ' path=' + (m.path || '?')).join(' || '));
        // Scan each for BoringSSL SSL_write. AArch64 BoringSSL SSL_write
        // prologue is non-trivial to signature-match generically; for now
        // report exports (likely empty = stripped → pattern-scan needed).
        flutter_mods.forEach((m) => {
          try {
            const exps = m.enumerateExports();
            const sslw = exps.filter((e) => /SSL_write|SSL_read/i.test(e.name));
            LOG('DIAG: ' + m.name + ' exports=' + exps.length +
                ' SSL_write-export=[' + sslw.map((e) => e.name).join(',') + ']');
          } catch (e) { LOG('DIAG: ' + m.name + ' enumExports failed: ' + e); }
        });
      } else if (_diag_runs <= 3 || _diag_runs % 10 === 0) {
        LOG('DIAG[' + _diag_runs + ']: flutter/app native libs not loaded yet');
      }
      // For each ssl-ish module, count SSL_* exports (so we know whether
      // BoringSSL symbols survived stripping).
      ssl_mods.forEach((m) => {
        try {
          const exps = m.enumerateExports();
          const sslw = exps.filter((e) => /SSL_write|SSL_read|ssl_write/i.test(e.name));
          LOG(`DIAG: ${m.name} exports=${exps.length} ` +
              `ssl_write-ish=[${sslw.map((e) => e.name).join(',')}]`);
        } catch (e) { LOG(`DIAG: ${m.name} enumerateExports failed: ${e}`); }
      });
    } catch (e) { LOG(`DIAG failed: ${e}`); }
  }

  const candidates = [
    'libssl.so',
    'libssl.so.1.1',
    'libflutter.so',
    'libapp.so',
    'libcrypto.so',
  ];
  let hit = 0;
  for (const modname of candidates) {
    const addr = resolveInModule(modname, 'SSL_write');
    if (addr && !SEEN_ADDRS.has('' + addr)) {
      attach_ssl_write(addr, modname + '!SSL_write');
      SEEN_ADDRS.add('' + addr);
      hit++;
    }
  }
  // Catch-all: scan every loaded module for an exported SSL_write.
  try {
    Process.enumerateModules().forEach((m) => {
      try {
        const sym = m.findExportByName && m.findExportByName('SSL_write');
        if (sym && !SEEN_ADDRS.has('' + sym)) {
          attach_ssl_write(sym, m.name + '!SSL_write');
          SEEN_ADDRS.add('' + sym);
          hit++;
        }
      } catch (_) { }
    });
  } catch (e) { LOG(`enumerateModules failed: ${e}`); }
  if (hit > 0) LOG(`SSL_write newly hooked: ${hit}`);
}

// dlopen watch installs synchronously at top-level (works at spawn —
// libdl is always present). It re-scans SSL_write each time an ssl/
// flutter lib loads.
install_dlopen_watch();

// IMPORTANT: Frida's JS timers (setTimeout/setInterval) do NOT fire once
// the stalker syscall-guard is following threads — the Stalker
// instrumentation starves the agent's timer loop. So we expose rescan
// via rpc and let the PYTHON side poll it (Python isn't stalkered). The
// rescan re-runs the SSL_write module hunt; the first call also emits the
// one-time DIAG of which SSL surfaces are reachable.
rpc.exports = {
  rescan: function () { try { hook_ssl_write_everywhere(); } catch (e) { LOG('rescan err: ' + e); } return true; },
};

LOG('capture_f3mf_token.js init complete — rpc.rescan exposed; python will poll');
