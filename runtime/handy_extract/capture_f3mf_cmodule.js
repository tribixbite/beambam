// capture_f3mf_cmodule.js — FORK-SURVIVING Conscrypt SSL_write capture.
//
// SHIELD's last line of defence (once the stealth frida-server defeats the
// 0xdead kill) is to fork()+exec the app into a clean process to shed the
// Frida agent. The SURVIVING real app is a plain fork() child (origin=fork,
// never exec'd) — it inherits the parent's memory MAPPINGS but not Frida's
// live JS agent threads, so any ordinary Interceptor.attach (whose onEnter
// calls back into JS) goes dead in the child.
//
// A frida CModule is PURE NATIVE code compiled into the process. Its onEnter
// runs entirely in native context with NO JS round-trip, so the inline hook
// trampoline + CModule survive the fork into the child and keep capturing.
// The CModule writes captured /f3mf (and any x-bbl-signed) request header
// blocks to the APP's OWN cache file — the app's SELinux domain can write
// there, and we read it back as root.
//
//   capture file (on device): /data/data/<pkg>/cache/x2d_f3mf.txt
//
// Driven by capture_f3mf_token.py with:
//   F3MF_HOOK_JS=capture_f3mf_cmodule.js F3MF_NO_STALKER=1 (stealth server)
// After spawn+resume, SHIELD forks the real app; poll the capture file on the
// host (extract_f3mf_capture below / the runner) until the headers appear,
// then trigger a MakerWorld model Download in Handy.

'use strict';

const LOG = (m) => { try { send({ type: 'log', msg: '[cmod] ' + m }); } catch (_) { } };
LOG('capture_f3mf_cmodule.js top-level reached');

// App-writable capture path (app SELinux domain can create files in its own
// cache). The CModule appends "\n===X2DCAP===\n"-delimited header blocks.
const CAP_PATH = '/data/data/bbl.intl.bambulab.com/cache/x2d_f3mf.txt';

// --- fdsan disable (cross-owner close tolerance under instrumentation) ------
(function disableFdsan() {
  try {
    let fn = null;
    try { fn = Module.getGlobalExportByName('android_fdsan_set_error_level'); } catch (_) { }
    if (!fn) {
      for (const m of Process.enumerateModules()) {
        try { const s = m.findExportByName('android_fdsan_set_error_level'); if (s) { fn = s; break; } } catch (_) { }
      }
    }
    if (fn) { new NativeFunction(fn, 'uint32', ['uint32'])(0); LOG('fdsan disabled'); }
  } catch (e) { LOG('fdsan disable failed: ' + e); }
})();

// --- the fork-surviving CModule --------------------------------------------
// SSL_write(SSL* ssl, const void* buf, int num): arg1=buf, arg2=num.
// Captures the header block of any request carrying an "x-bbl" signed header
// or a "/f3mf" / "design-service" path — that's the full captcha-bypass set.
const CMODULE_SRC = `
#include <gum/guminterceptor.h>

extern int open (const char * path, int flags, int mode);
extern long write (int fd, const void * buf, unsigned long n);
extern int close (int fd);

#define O_WRONLY 1
#define O_CREAT  0100
#define O_APPEND 02000

static int
mem_contains (const char * hay, int haylen, const char * needle, int nlen)
{
  int i, j;
  for (i = 0; i + nlen <= haylen; i++)
  {
    for (j = 0; j < nlen && hay[i + j] == needle[j]; j++) ;
    if (j == nlen) return 1;
  }
  return 0;
}

void
onEnter (GumInvocationContext * ic)
{
  const char * buf = (const char *) gum_invocation_context_get_nth_argument (ic, 1);
  unsigned long num = (unsigned long) gum_invocation_context_get_nth_argument (ic, 2);
  char c0;
  int lim, hdrlen, i, fd;

  if (buf == 0) return;
  if (num < 24 || num > 262144) return;
  c0 = buf[0];
  if (c0 != 'G' && c0 != 'P' && c0 != 'D' && c0 != 'H') return;   /* GET/POST/PUT/DELETE/HEAD */

  lim = (num < 16384) ? (int) num : 16384;
  if (!mem_contains (buf, lim, "x-bbl", 5) &&
      !mem_contains (buf, lim, "/f3mf", 5) &&
      !mem_contains (buf, lim, "design-service", 14))
    return;

  fd = open ("${CAP_PATH}", O_WRONLY | O_CREAT | O_APPEND, 0600);
  if (fd < 0) return;

  hdrlen = lim;
  for (i = 0; i + 4 <= lim; i++)
    if (buf[i] == '\\r' && buf[i+1] == '\\n' && buf[i+2] == '\\r' && buf[i+3] == '\\n')
    { hdrlen = i + 4; break; }

  write (fd, buf, hdrlen);
  write (fd, "\\n===X2DCAP===\\n", 14);
  close (fd);
}
`;

let _cm = null;
const _attached = new Set();

function installCModuleHook(addr, label) {
  if (_attached.has('' + addr)) return;
  try {
    if (_cm === null) _cm = new CModule(CMODULE_SRC);
    // Pure-native attach: frida wires the CModule's onEnter export directly,
    // so no JS callback is invoked per-write → survives fork into the child.
    Interceptor.attach(addr, _cm);
    _attached.add('' + addr);
    LOG('CModule SSL_write hook attached @ ' + addr + ' (' + label + ') — fork-surviving; writes ' + CAP_PATH);
  } catch (e) {
    LOG('CModule attach @ ' + addr + ' failed: ' + e + ' — retrying with onEnter ptr');
    try {
      Interceptor.attach(addr, { onEnter: _cm.onEnter });
      _attached.add('' + addr);
      LOG('CModule (onEnter ptr) attached @ ' + addr + ' (' + label + ')');
    } catch (e2) { LOG('CModule onEnter-ptr attach also failed: ' + e2); }
  }
}

// Resolve Conscrypt's NAMED libssl.so SSL_write export (idempotent; conscrypt
// loads lazily, so re-try on dlopen and from the host poll).
function tryHookConscrypt() {
  for (const m of Process.enumerateModules()) {
    const nm = m.name || '', pth = m.path || '';
    if (!/libssl\.so$/.test(nm) && !/libssl\.so$/.test(pth)) continue;
    let a = null;
    try { a = m.findExportByName('SSL_write'); } catch (_) { }
    if (!a) { try { a = Module.findExportByName(nm, 'SSL_write'); } catch (_) { } }
    if (a) installCModuleHook(a, 'conscrypt:' + (pth || nm));
  }
}

// Trigger on dlopen (conscrypt loads when the net stack initialises) + at init.
function hookExec(name, cb) {
  let a = null;
  try { a = Module.getGlobalExportByName ? Module.getGlobalExportByName(name) : null; } catch (_) { }
  if (!a) {
    for (const m of Process.enumerateModules()) {
      try { const s = m.findExportByName(name); if (s) { a = s; break; } } catch (_) { }
    }
  }
  if (a) { try { Interceptor.attach(a, cb); } catch (_) { } }
}
hookExec('android_dlopen_ext', { onLeave() { try { tryHookConscrypt(); } catch (_) { } } });
hookExec('dlopen', { onLeave() { try { tryHookConscrypt(); } catch (_) { } } });

rpc.exports = {
  // host poll drives re-resolution until conscrypt appears + is hooked.
  scan: function () { try { tryHookConscrypt(); } catch (e) { LOG('scan err: ' + e); } return _attached.size; },
};

tryHookConscrypt();
LOG('init complete — fork-surviving CModule capture armed (' + _attached.size + ' hook(s))');
