// scan_anon_ssl.js — locate BoringSSL SSL_write in ANONYMOUS memory.
//
// Bambu Handy's SHIELD packer unpacks its real code (incl. the BoringSSL
// that dart:io HttpClient uses) into anonymous RX memory — there is no
// named libflutter.so/libapp.so module, so Module/export resolution fails
// (see capture_f3mf_token.js header for the full finding).
//
// This script runs UNDER the same spawn-gated + stalker-guarded runner
// (capture_f3mf_token.py with F3MF_HOOK_JS=scan_anon_ssl.js). Because the
// in-app Stalker starves Frida's JS event loop, all work is driven from
// the Python side via rpc.exports.scan (polled each second). Each scan:
//
//   1. Enumerate executable + rw anon ranges not covered by a named module.
//   2. In each, look for in-memory ELF images (magic 7f 45 4c 46) and try
//      to resolve SSL_write/SSL_read from the in-memory dynamic symtab.
//   3. Also Memory.scan for the literal symbol strings "SSL_write" /
//      "ssl_write_internal" / "BoringSSL" so we know whether symbol info
//      survived the unpack (→ ELF dynsym path) or was stripped
//      (→ code-signature path needed).
//
// Diagnostic-first: it REPORTS what it finds. Once we know the structure,
// the resolver path (ELF dynsym vs signature scan) is wired to actually
// Interceptor.attach SSL_write and forward plaintext /f3mf headers.
//
// ============================================================
// 2026-06-03 BLOCKER — SHIELD's pre-init ptrace watchdog
// ============================================================
// After the app's files/.ss/ guard state was cleared (to fix a separate
// crash), SHIELD RE-PROVISIONED A STRICTER GUARD that forks an anti-debug
// WATCHDOG process during early library init (DT_INIT_ARRAY constructors
// in .ss/l6a18f19c.so). The watchdog ptrace-attaches to the app, detects
// Frida's gum agent in /proc/<pid>/maps, and forces the app's PC to jump
// to 0xdead50xx (SIGSEGV/SIGBUS) within ~1 s of spawn.
//
// Why our defenses don't reach it:
//   * The Stalker syscall guard suppresses exit/kill/ptrace/tgkill FROM
//     the instrumented threads, and now also blocks the watchdog's process
//     fork (clone without CLONE_THREAD → -EAGAIN). BUT the watchdog fork
//     fires during DT_INIT_ARRAY, which runs BEFORE Frida's spawn-gating
//     entry point — so the stalker isn't installed yet (blocked_forks=0,
//     and the app dies during script.load()).
//   * fdsan is disabled successfully (android_fdsan_set_error_level(0)),
//     so the fdsan abort path is gone — but the 0xdead PC-poison is a
//     separate, earlier kill.
//
// To beat this, instrumentation must be active BEFORE .ss/ constructors
// run: i.e. linker-level pre-init interception (hook the dynamic linker's
// call_constructors / __dl__ZL10call_arrayIPFviPPcES1_E before DT_INIT_ARRAY,
// or a Zygisk module that installs the guard at zygote-fork and neutralises
// the watchdog fork in the same breath). That is a substantial RE effort.
//
// Everything DOWNSTREAM of a surviving SSL_write hook is done + verified
// in earlier runs (signature scan found SSL_write+SSL_read prologues;
// /f3mf header capture → ~/.x2d/handy_token.json → cloud_client replay).
// The only missing piece is keeping the app alive under instrumentation.
// ============================================================

const LOG = (m) => { try { send({ type: 'log', msg: '[scan] ' + m }); } catch (_) { } };
const EMIT = (kind, body) => { try { send({ type: kind, body: body }); } catch (_) { } };
LOG('scan_anon_ssl.js top-level reached');

// --- fdsan disable (MUST run before SHIELD's loader unpacks) -------------
// The SHIELD loader (.ss/l6a18f19c.so) — and the Frida helper it races with
// — call raw close() on file descriptors that bionic's fdsan has tagged as
// owned by a FILE* stream. fdsan then SIGABRTs:
//   "fdsan: attempted to close fd N, expected to be unowned, actually owned
//    by FILE* 0x..."
// Disabling fdsan globally makes every fd effectively UNOWNED from fdsan's
// view, so the cross-owner close is tolerated and the app survives under
// instrumentation. android_fdsan_set_error_level(ANDROID_FDSAN_ERROR_LEVEL_DISABLED=0).
(function disableFdsan() {
  try {
    let fn = null;
    try { fn = Module.getGlobalExportByName('android_fdsan_set_error_level'); } catch (_) { }
    if (!fn) {
      for (const m of Process.enumerateModules()) {
        try { const s = m.findExportByName('android_fdsan_set_error_level'); if (s) { fn = s; break; } } catch (_) { }
      }
    }
    if (!fn) { LOG('fdsan: android_fdsan_set_error_level not found'); return; }
    const set = new NativeFunction(fn, 'uint32', ['uint32']);
    const prev = set(0);   // 0 = ANDROID_FDSAN_ERROR_LEVEL_DISABLED
    LOG('fdsan disabled (was level ' + prev + ') — cross-owner close tolerated');
  } catch (e) { LOG('fdsan disable failed: ' + e); }
})();

// --- STEALTH MODE switch ---------------------------------------------------
// Two mutually-exclusive strategies against SHIELD's watchdog:
//   * STEALTH (true)  — let the watchdog FORK + run normally, but make Frida
//     invisible to its /proc/<pid>/maps + task/*/comm scan (patched stealth
//     frida-server: no "frida"/"gum" tokens). The watchdog finds nothing,
//     completes its handshake, and the app boots to Flutter UI WITH our hook.
//     This is the winning path — the fork-block made SHIELD fail-CLOSED
//     (Flutter never loaded, killed after ~30 s).
//   * FORK-BLOCK (false) — block the watchdog fork entirely (legacy; defeats
//     the immediate 0xdead kill but SHIELD then stalls/fail-closes).
// Toggle without a host round-trip: set this const.
var STEALTH_MODE = true;

// The heavy anonymous-BoringSSL signature scan (Memory.scanSync over every
// non-system r-x range, every poll) adds enough overhead to STARVE Flutter's
// engine init under stealth — the app sits on its splash and never makes a
// network call. Bambu Handy's /f3mf goes over Conscrypt's NAMED libssl.so
// (hooked by export — see tryHookNamedLibssl), so the anon scan is redundant.
// Leave it OFF; flip on only to chase the Flutter dart:io BoringSSL path.
var ENABLE_ANON_SCAN = false;

// --- libc fork-block (armed at spawn-gate, before .ss/ loads) --------------
// SHIELD's .ss/l6a18f19c.so constructor forks an anti-debug WATCHDOG process
// (a clone WITHOUT CLONE_THREAD) that ptraces the app + maps-scans for gum,
// then forces a 0xdead crash. The frida SPAWN-gate suspends at app_process's
// entry point — BEFORE the app loads .ss/ via System.loadLibrary — so hooking
// the libc fork/clone wrappers HERE (at script.load, while suspended) arms the
// block before SHIELD's constructor runs. Lightweight Interceptor only (no
// Stalker — the Stalker following the main thread was what killed earlier
// runs). If SHIELD forks via a raw `svc` clone (bypassing libc), this misses
// it and we fall back to the Stalker guard (set F3MF_NO_STALKER=0).
var CLONE_THREAD = 0x00010000;
var blocked_forks = 0;
(function hookLibcForkBlock() {
  if (STEALTH_MODE) { LOG('STEALTH_MODE — fork-block DISABLED (watchdog allowed to run vs invisible frida)'); return; }
  ['fork', 'vfork', '__bionic_clone', 'clone'].forEach(function (name) {
    let a = null;
    try { a = Module.getGlobalExportByName(name); } catch (_) { a = null; }
    if (!a) return;
    try {
      Interceptor.attach(a, {
        onEnter(args) {
          this.block = false;
          if (name === 'clone' || name === '__bionic_clone') {
            if ((args[0].toInt32() & CLONE_THREAD) === 0) this.block = true;
          } else { this.block = true; }
        },
        onLeave(retval) {
          if (this.block) {
            blocked_forks++;
            if (blocked_forks <= 30) LOG('blocked libc ' + name + ' (#' + blocked_forks + ')');
            retval.replace(ptr(-1));
          }
        }
      });
      LOG('hooked libc ' + name + ' for fork-block');
    } catch (e) { LOG('hook ' + name + ' failed: ' + e); }
  });
})();

// ---- helpers --------------------------------------------------------------

function knownModuleRanges() {
  // Build a sorted list of [base, base+size) for every named module so we
  // can tell anon ranges apart.
  const ranges = [];
  try {
    for (const m of Process.enumerateModules()) {
      const base = m.base;
      const end = m.base.add(m.size);
      ranges.push([base, end, m.name]);
    }
  } catch (e) { LOG('enumerateModules failed: ' + e); }
  return ranges;
}

function addrInKnownModule(addr, modRanges) {
  for (const [b, e, name] of modRanges) {
    if (addr.compare(b) >= 0 && addr.compare(e) < 0) return name;
  }
  return null;
}

// Parse an in-memory ELF64 image at `base` and try to resolve `wanted`
// symbol names. Returns a map name->address (rebased) or {} on failure.
// `base` is the mapped load address of the FIRST PT_LOAD (== ELF header
// for a typical mapping). Robust to PIE: rebases dynsym virtual addresses
// by (base - min_PT_LOAD_vaddr).
function resolveElfSymbols(base, wanted) {
  const out = {};
  try {
    // ELF header
    if (base.readU32() !== 0x464c457f) return out;   // "\x7fELF" little-endian
    const ei_class = base.add(4).readU8();
    if (ei_class !== 2) return out;                   // ELFCLASS64 only
    const e_phoff = base.add(0x20).readU64();
    const e_phentsize = base.add(0x36).readU16();
    const e_phnum = base.add(0x38).readU16();
    if (e_phnum === 0 || e_phnum > 64) return out;

    // Walk program headers: find PT_DYNAMIC (2) + min PT_LOAD (1) vaddr.
    let dynVaddr = null, dynFilesz = null;
    let minLoadVaddr = null;
    for (let i = 0; i < e_phnum; i++) {
      const ph = base.add(e_phoff.add(e_phentsize * i));
      const p_type = ph.readU32();
      const p_vaddr = ph.add(0x10).readU64();
      const p_memsz = ph.add(0x28).readU64();
      if (p_type === 1) { // PT_LOAD
        if (minLoadVaddr === null || p_vaddr.compare(minLoadVaddr) < 0)
          minLoadVaddr = p_vaddr;
      } else if (p_type === 2) { // PT_DYNAMIC
        dynVaddr = p_vaddr; dynFilesz = p_memsz;
      }
    }
    if (dynVaddr === null) return out;
    if (minLoadVaddr === null) minLoadVaddr = ptr(0);
    // Rebase: file/virtual addr X maps to base + (X - minLoadVaddr).
    const rebase = (vaddr) => base.add(ptr(vaddr).sub(minLoadVaddr));

    // Walk PT_DYNAMIC entries: each is (int64 d_tag, uint64 d_val).
    let symtab = null, strtab = null, syment = 24, hashAddr = null, gnuHash = null;
    const dynBase = rebase(dynVaddr);
    const nEnt = dynFilesz.toNumber() / 16;
    for (let i = 0; i < nEnt; i++) {
      const ent = dynBase.add(i * 16);
      const tag = ent.readU64().toNumber();
      const val = ent.add(8).readU64();
      if (tag === 0) break;                 // DT_NULL
      else if (tag === 6) symtab = rebase(val);   // DT_SYMTAB
      else if (tag === 5) strtab = rebase(val);   // DT_STRTAB
      else if (tag === 11) syment = val.toNumber(); // DT_SYMENT
      else if (tag === 4) hashAddr = rebase(val);   // DT_HASH
      else if (tag === 0x6ffffef5) gnuHash = rebase(val); // DT_GNU_HASH
    }
    if (!symtab || !strtab) return out;

    // Determine symbol count. DT_HASH: nchain at word[1]. GNU_HASH needs a
    // bucket walk; fall back to scanning a bounded number of symbols.
    let nsyms = 0;
    if (hashAddr) {
      nsyms = hashAddr.add(4).readU32();    // nchain == nsyms
    }
    if (!nsyms || nsyms > 200000) nsyms = 60000;   // bounded fallback

    const wantSet = new Set(wanted);
    for (let i = 0; i < nsyms && wantSet.size; i++) {
      const sym = symtab.add(i * syment);
      const st_name = sym.readU32();
      if (st_name === 0) continue;
      const st_value = sym.add(8).readU64();
      if (st_value.isNull()) continue;
      let name;
      try { name = strtab.add(st_name).readCString(); } catch (_) { continue; }
      if (name && wantSet.has(name)) {
        out[name] = rebase(st_value);
        wantSet.delete(name);
      }
    }
  } catch (e) { /* malformed image — skip */ }
  return out;
}

// ---- scan driver ----------------------------------------------------------

let _scan_runs = 0;
let _resolved = false;
const SSL_NAMES = ['SSL_write', 'SSL_read'];

function do_scan() {
  if (_resolved) return true;
  _scan_runs++;
  const modRanges = knownModuleRanges();

  // Candidate ranges: executable, OR rw- anon (unpacked code is sometimes
  // staged rw then mprotect'd rx). We focus on executable first.
  let xRanges, rwRanges;
  try {
    xRanges = Process.enumerateRanges('r-x').concat(Process.enumerateRanges('rwx'));
    rwRanges = Process.enumerateRanges('rw-');
  } catch (e) { LOG('enumerateRanges failed: ' + e); return false; }

  // Anonymous executable ranges = the unpack target.
  const anonX = xRanges.filter((r) => !addrInKnownModule(r.base, modRanges));
  if (_scan_runs <= 2 || _scan_runs % 15 === 0) {
    LOG('run#' + _scan_runs + ': ' + xRanges.length + ' x-ranges, ' +
        anonX.length + ' anon-x, ' + rwRanges.length + ' rw ranges');
  }

  // 1) ELF-image scan in anon executable ranges.
  let elfHits = 0, symHit = null;
  for (const r of anonX) {
    // Quick magic check at range base (mapped ELFs start at a page boundary).
    let isElf = false;
    try { isElf = r.base.readU32() === 0x464c457f; } catch (_) { }
    if (isElf) {
      elfHits++;
      const syms = resolveElfSymbols(r.base, SSL_NAMES);
      if (syms.SSL_write) {
        symHit = syms;
        LOG('ELF@' + r.base + ' size=' + r.size +
            ' → SSL_write=' + syms.SSL_write +
            (syms.SSL_read ? ' SSL_read=' + syms.SSL_read : ''));
        break;
      }
    }
  }
  if (elfHits && !symHit) LOG('found ' + elfHits + ' anon ELF(s) but no SSL_write in dynsym');

  // Targeted: find the ELF image that CONTAINS the "BoringSSL" version
  // string, by scanning backward (page-aligned) from the string for the
  // ELF magic. The real BoringSSL image may not start at a range base.
  if (!symHit && _scan_runs <= 4) {
    try {
      const allRanges = anonX.concat(rwRanges);
      let bsAt = null;
      const bsPat = 'BoringSSL'.split('').map((c) =>
        c.charCodeAt(0).toString(16).padStart(2, '0')).join(' ');
      for (const r of allRanges) {
        if (r.size > 64 * 1024 * 1024) continue;
        try {
          const f = Memory.scanSync(r.base, r.size, bsPat);
          if (f.length) { bsAt = f[0].address; break; }
        } catch (_) { }
      }
      if (bsAt) {
        LOG('BoringSSL string @ ' + bsAt + ' — scanning back for ELF base');
        let p = bsAt.and(ptr('0xfffffffffffff000')); // page align
        let found = null;
        for (let i = 0; i < 8192; i++) {   // up to 32 MB back, page steps
          try {
            if (p.readU32() === 0x464c457f) { found = p; break; }
          } catch (_) { /* unmapped — keep stepping, may hit a gap */ }
          p = p.sub(0x1000);
        }
        if (found) {
          LOG('candidate BoringSSL ELF base @ ' + found);
          const syms = resolveElfSymbols(found, SSL_NAMES);
          if (syms.SSL_write) {
            symHit = syms;
            LOG('ELF-with-BoringSSL @ ' + found + ' → SSL_write=' + syms.SSL_write);
          } else {
            LOG('BoringSSL ELF @ ' + found + ' has NO SSL_write export (stripped) — signature scan needed');
          }
        } else {
          LOG('no ELF header within 32MB before BoringSSL string (flat blob?)');
        }
      }
    } catch (e) { LOG('targeted BoringSSL search err: ' + e); }
  }

  // 2) String-presence probe (only on the first couple runs — expensive).
  if (_scan_runs <= 2) {
    const needles = ['SSL_write', 'ssl_write_internal', 'BoringSSL', 'OpenSSL'];
    for (const needle of needles) {
      let count = 0, firstAt = null;
      const pat = needle.split('').map((c) =>
        c.charCodeAt(0).toString(16).padStart(2, '0')).join(' ');
      // Scan anon rw + x ranges (string tables live in rw, code refs in x).
      const scanRanges = anonX.concat(
        rwRanges.filter((r) => !addrInKnownModule(r.base, modRanges)));
      for (const r of scanRanges) {
        if (r.size > 64 * 1024 * 1024) continue;   // skip huge ranges
        try {
          const found = Memory.scanSync(r.base, r.size, pat);
          if (found.length) {
            count += found.length;
            if (!firstAt) firstAt = found[0].address;
          }
        } catch (_) { }
        if (count > 4) break;
      }
      LOG('string "' + needle + '": ' + count + (firstAt ? ' first@' + firstAt : ''));
    }
  }

  if (symHit && symHit.SSL_write) {
    _resolved = true;
    EMIT('ssl_write_addr', { addr: '' + symHit.SSL_write });
    LOG('RESOLVED SSL_write @ ' + symHit.SSL_write + ' — ready to hook');
    hook_ssl_write_at(symHit.SSL_write, 'dynsym');
    return true;
  }

  // 3) SIGNATURE scan — BoringSSL is stripped, so match SSL_write's
  // position-independent prologue (frame setup + reg saves + stack-canary
  // read; no relative branches). Template derived from the system
  // libssl.so SSL_write. We scan the anon executable ranges (the unpacked
  // Flutter BoringSSL lives there).
  if (!symHit && _scan_runs <= 10) {
    // Scan target = ALL executable ranges EXCEPT system libs (/system,
    // /apex, /vendor, /product). The Flutter BoringSSL .text is sometimes
    // anon, sometimes inside a named non-system mapping (base.apk / the
    // unpacked region) — the anon-only filter missed it, so we widen to
    // every non-system r-x range. exact24 is specific enough that this
    // still yields a small candidate set across app/flutter code.
    function isSystemAddr(addr) {
      for (const m of Process.enumerateModules()) {
        const p = m.path || '';
        if (addr.compare(m.base) >= 0 && addr.compare(m.base.add(m.size)) < 0) {
          return (p.indexOf('/system/') === 0 || p.indexOf('/apex/') === 0 ||
                  p.indexOf('/vendor/') === 0 || p.indexOf('/product/') === 0 ||
                  p.indexOf('/system_ext/') === 0);
        }
      }
      return false; // anon → not system → scan it
    }
    const scanX = xRanges.filter((r) => !isSystemAddr(r.base));

    // Exact 24-byte prologue, then looser anchors with wildcarded frame
    // size + register-save offsets to tolerate version drift.
    const sigs = [
      ['exact24', 'ff 43 01 d1 fd 7b 01 a9 fd 43 00 91 f7 13 00 f9 f6 57 03 a9 f4 4f 04 a9'],
      ['canary', '?? ?? ?? d1 fd 7b ?? a9 fd ?? ?? 91 ?? ?? ?? f9 ?? ?? ?? a9 ?? ?? ?? a9 56 d0 3b d5'],
      ['mrs', '56 d0 3b d5'],
    ];
    for (const [label, pat] of sigs) {
      const hits = [];
      for (const r of scanX) {
        if (r.size > 96 * 1024 * 1024) continue;
        try {
          const found = Memory.scanSync(r.base, r.size, pat);
          for (const f of found) hits.push(f.address);
        } catch (_) { }
        if (hits.length > 120) break;
      }
      LOG('sig[' + label + ']: ' + hits.length + ' match(es) in ' +
          scanX.length + ' non-sys x-ranges' +
          (hits.length && hits.length <= 8
            ? ' @ ' + hits.map((h) => '' + h).join(',') : ''));
      // exact24/canary: hook every candidate (≤24). SSL_read shares the
      // prologue but its onEnter buffer never holds an HTTP request line,
      // so only the real SSL_write fires the /f3mf capture. The HTTP[]
      // log shows which addresses actually carry traffic.
      if ((label === 'exact24' || label === 'canary') &&
          hits.length >= 1 && hits.length <= 24) {
        _resolved = true;
        LOG('RESOLVED via sig[' + label + ']: ' + hits.length +
            ' candidate(s) — hooking ALL (HTTP filter selects SSL_write)');
        EMIT('ssl_write_addr', { via: label,
                                 candidates: hits.map((h) => '' + h) });
        hits.forEach((h, i) => hook_ssl_write_at(h, 'sig:' + label + '#' + i));
        return true;
      }
    }
  }
  return false;
}

// Attach the /f3mf-capturing Interceptor to a resolved SSL_write address.
// SSL_write(SSL* ssl, const void* buf, int num): arg1=buf, arg2=num.
function hook_ssl_write_at(addr, how) {
  try {
    Interceptor.attach(addr, {
      onEnter(args) {
        try {
          const num = args[2].toInt32();
          if (num <= 16 || num > 65536) return;
          const first = args[1].readU8();
          // HTTP methods start with G/P/D/H.
          if (first !== 0x47 && first !== 0x50 && first !== 0x44 && first !== 0x48) return;
          const sz = Math.min(num, 16384);
          const txt = Array.from(new Uint8Array(args[1].readByteArray(sz)))
            .map((b) => String.fromCharCode(b)).join('');
          const reqline = txt.split('\r\n')[0] || '';
          if (!/^(GET|POST|PUT|DELETE|HEAD) /.test(reqline)) return;
          if (_http_logged < 80) {
            _http_logged++;
            const hm = txt.match(/\r\nHost:\s*([^\r\n]+)/i);
            LOG('HTTP[' + _http_logged + '] ' + reqline.substring(0, 90) +
                '  Host=' + (hm ? hm[1] : '?'));
          }
          if (txt.indexOf('/design-service/instance/') < 0 ||
              txt.indexOf('/f3mf') < 0) return;
          const hdrEnd = txt.indexOf('\r\n\r\n');
          const headers = hdrEnd > 0 ? txt.substring(0, hdrEnd) : txt;
          LOG('!!! /f3mf CAPTURED via ' + how + ' (' + num + ' B)');
          EMIT('f3mf_request', { label: how, addr: '' + addr,
                                 num_bytes: num, headers: headers,
                                 timestamp: safeNow() });
        } catch (e) { LOG('ssl_write onEnter err: ' + e); }
      },
    });
    LOG('hooked SSL_write @ ' + addr + ' (' + how + ') — waiting for /f3mf');
  } catch (e) { LOG('hook_ssl_write_at failed @ ' + addr + ': ' + e); }
}

let _http_logged = 0;

// --- Direct hook on system/Conscrypt libssl.so SSL_write -------------------
// Bambu Handy's /f3mf download travels over Android Conscrypt (the Java/OkHttp
// HTTP stack), whose libssl.so is a NAMED module exporting SSL_write — no
// anon-memory signature scan needed (the Flutter dart:io BoringSSL is stripped
// and its prologue doesn't match the system-libssl template, so that path
// stays a fallback). Idempotent: safe to re-run on every dlopen as conscrypt
// loads lazily when the network stack first initialises.
const _libsslHooked = new Set();
function tryHookNamedLibssl() {
  try {
    for (const m of Process.enumerateModules()) {
      const nm = m.name || '', pth = m.path || '';
      if (!/libssl\.so$/.test(nm) && !/libssl\.so$/.test(pth)) continue;
      let a = null;
      try { a = m.findExportByName('SSL_write'); } catch (_) { }
      if (!a) { try { a = Module.findExportByName(nm, 'SSL_write'); } catch (_) { } }
      if (a && !_libsslHooked.has('' + a)) {
        _libsslHooked.add('' + a);
        hook_ssl_write_at(a, 'conscrypt:' + (pth || nm));
        LOG('hooked NAMED libssl SSL_write @ ' + a + ' (' + (pth || nm) + ')');
      }
    }
  } catch (e) { LOG('tryHookNamedLibssl err: ' + e); }
}

// IMPORTANT: the in-app Stalker guard starves Frida's JS event loop, so
// rpc/timers never fire. We MUST drive do_scan() from app-thread
// Interceptor callbacks. Two triggers:
//   * mprotect(PROT_EXEC) — SHIELD makes the unpacked BoringSSL executable
//     here; this is exactly when + where the code appears. Ideal trigger.
//   * dlopen — coarse fallback (fires on lib loads / Flutter init).
// do_scan is throttled so the (expensive) Memory.scan doesn't run on every
// single mprotect.
let _last_scan_ms = 0;
function maybe_scan(reason) {
  // Conscrypt libssl loads lazily; re-try the named-module hook on every
  // trigger (idempotent, cheap). Independent of the anon-scan _resolved flag.
  tryHookNamedLibssl();
  if (!ENABLE_ANON_SCAN) return;   // skip the Flutter-starving heavy scan
  if (_resolved) return;
  // Throttle string-probe-heavy early runs; cheap ELF scan can run more.
  const now = (Date.now ? safeNow() : 0);
  if (now - _last_scan_ms < 250 && _scan_runs > 3) return;
  _last_scan_ms = now;
  try { do_scan(); } catch (e) { LOG('scan(' + reason + ') err: ' + e); }
}
// Date.now is unreliable under stalker; use a monotonic-ish counter fallback.
let _tick = 0;
function safeNow() { return (_tick += 50); }

function hookExec(name, fn) {
  let a = null;
  try { a = Module.getGlobalExportByName ? Module.getGlobalExportByName(name) : null; } catch (_) { }
  if (!a) {
    for (const m of Process.enumerateModules()) {
      try { const s = m.findExportByName(name); if (s) { a = s; break; } } catch (_) { }
    }
  }
  if (!a) { LOG('could not resolve ' + name); return; }
  try { Interceptor.attach(a, fn); LOG('hooked ' + name + ' @ ' + a); }
  catch (e) { LOG('hook ' + name + ' failed: ' + e); }
}

// mprotect(void* addr, size_t len, int prot); PROT_EXEC = 4.
hookExec('mprotect', {
  onEnter(args) { this.prot = args[2].toInt32(); },
  onLeave() { if ((this.prot & 4) !== 0) maybe_scan('mprotect+x'); },
});
// android_dlopen_ext / dlopen — coarse fallback trigger.
hookExec('android_dlopen_ext', { onLeave() { maybe_scan('dlopen'); } });
hookExec('dlopen', { onLeave() { maybe_scan('dlopen'); } });

// Also expose via rpc in case a future frida/agent combo isn't starved.
// Route the host poll through maybe_scan so it drives the cheap idempotent
// Conscrypt-libssl hook retry (and the heavy anon scan only if enabled),
// instead of forcing do_scan() every second.
rpc.exports = {
  scan: function () { try { maybe_scan('rpc-poll'); return _resolved; } catch (e) { LOG('scan err: ' + e); return false; } },
};

// Kick one scan synchronously at load (finds anything already mapped).
maybe_scan('init');
LOG('scan_anon_ssl.js init complete — driven by mprotect/dlopen callbacks');
