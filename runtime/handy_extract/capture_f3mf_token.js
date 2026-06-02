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
          if (this.path.indexOf('ssl') >= 0 ||
              this.path.indexOf('flutter') >= 0 ||
              this.path.indexOf('libapp') >= 0) {
            LOG('dlopen ' + this.path + ' → re-scanning SSL_write');
            hook_ssl_write_everywhere();
          }
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
function hook_ssl_write_everywhere() {
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
// flutter lib loads, which is the robust catch-all.
install_dlopen_watch();

// Belt-and-suspenders: also poll a handful of times in case the SSL lib
// was already mapped before our dlopen hook (late-attach mode), or the
// dlopen path didn't match our substrings.
let _poll = 0;
const _pollTimer = setInterval(() => {
  _poll++;
  hook_ssl_write_everywhere();
  if (_poll >= 10) { clearInterval(_pollTimer); LOG('SSL_write poll done'); }
}, 1000);

LOG('capture_f3mf_token.js init complete — waiting for SSL_write on /f3mf');
