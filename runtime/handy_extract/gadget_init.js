// gadget_init.js — self-contained Frida GADGET script for capturing Bambu
// Handy's /f3mf auth headers, loaded by zygiskfrida at the Zygisk stage
// (postAppSpecialize) — i.e. BEFORE the app's own DT_INIT_ARRAY
// constructors run. This timing is the whole point: the SHIELD packer's
// .ss/l6a18f19c.so forks an anti-debug WATCHDOG during its constructor,
// and the watchdog ptraces the app + maps-scans for gum, then forces a
// 0xdead50xx crash. A frida-SERVER spawn-gate installs too late (at the
// entry point, after constructors). The gadget runs early enough that our
// fork-block + scanner are armed before SHIELD's constructor executes.
//
// Deploy: this file is referenced by libsysrt.config.so /
// libgadget.config.so as interaction.type=script,path=<this>. zygiskfrida
// injects libsysrt.so into bbl.intl.bambulab.com per config.json targets.
//
// No python host — all I/O is via Frida's File API:
//   * log    → /data/local/tmp/re.zyg.fri/scan.log
//   * token  → /data/local/tmp/re.zyg.fri/f3mf_capture.json (on /f3mf hit)
//
// Pull both from the device after a capture.
//
// ============================================================
// 2026-06-03 FINDING — ZygiskFrida's late-inject is detected
// ============================================================
// Wiring this script via zygiskfrida (config.json target → libsysrt.so →
// libsysrt.config.so → this script) DID get the gadget targeting Handy
// (logcat: "ZygiskFrida: App detected: bbl.intl.bambulab.com"). BUT the
// timeline shows SHIELD kills the app BEFORE the gadget even loads:
//   t+0ms   ZygiskFrida: App detected
//   t+1ms   ZygiskFrida: "Wait for process to complete init"  (waiter thread)
//   t+99ms  Fatal SIGSEGV 0xdead5014                          (SHIELD kill)
//   t+131ms ZygiskFrida: "Process init completed" → "Injecting libsysrt.so"
// So SHIELD detects ZygiskFrida's OWN injection footprint — the lingering
// waiter thread + the ReZygisk zygisk-ptrace64 tracer held across the
// delayed-inject window — NOT our gadget/gum. Clean Handy (other zygisk
// modules: shamiko/nohello/sui) runs fine because those inject IMMEDIATELY
// at postAppSpecialize and their tracer detaches before SHIELD's watchdog
// (forked during the app's DT_INIT_ARRAY) checks. ZygiskFrida's hardcoded
// "wait for init" keeps the footprint present during SHIELD's check window.
//
// ZygiskFrida exposes only start_up_delay_ms (no immediate-inject option),
// so it can't be made early. THE FIX is a custom Zygisk module that dlopens
// this gadget IMMEDIATELY in postAppSpecialize (no waiter thread, no
// lingering tracer) — see zygisk_x2d/ — so the fork-block below is armed
// before SHIELD's constructor forks the watchdog, and there's no anomalous
// thread/tracer for SHIELD to catch. This script is unchanged; only the
// injection vehicle changes.
// ============================================================

'use strict';

// World-writable dir (chmod 777) — the gadget runs as the Handy app uid,
// which can't write to the root-owned re.zyg.fri dir.
var LOG_PATH = '/data/local/tmp/x2dcap/scan.log';
var CAP_PATH = '/data/local/tmp/x2dcap/f3mf_capture.json';

function LOG(m) {
  try {
    var f = new File(LOG_PATH, 'a');
    f.write('[' + Date.now() + '] ' + m + '\n');
    f.flush(); f.close();
  } catch (e) { /* best effort */ }
}
LOG('=== gadget_init.js loaded (pid=' + Process.id + ') ===');

// ---- (1) fdsan disable -----------------------------------------------------
// SHIELD's loader does cross-owner close() that bionic fdsan SIGABRTs on.
(function () {
  try {
    var a = Module.getGlobalExportByName('android_fdsan_set_error_level');
    var set = new NativeFunction(a, 'uint32', ['uint32']);
    var prev = set(0); // ANDROID_FDSAN_ERROR_LEVEL_DISABLED
    LOG('fdsan disabled (was ' + prev + ')');
  } catch (e) { LOG('fdsan disable failed: ' + e); }
})();

// ---- (2) watchdog fork-block (libc + raw-svc) ------------------------------
// SHIELD forks the watchdog (a process clone WITHOUT CLONE_THREAD). Block it
// so the watchdog never exists to ptrace + maps-scan. We hook BOTH the libc
// wrappers (cheap) AND — as a fallback for raw `svc` clones — a Stalker
// syscall guard on the main thread (heavier; only the main thread, and we
// unfollow once the fork window passes).
var CLONE_THREAD = 0x00010000;
var blocked_forks = 0;

function hookLibcFork() {
  ['fork', 'vfork', '__bionic_clone', 'clone'].forEach(function (name) {
    var a = null;
    try { a = Module.getGlobalExportByName(name); } catch (e) { a = null; }
    if (!a) return;
    try {
      Interceptor.attach(a, {
        onEnter: function (args) {
          this.block = false;
          if (name === 'clone' || name === '__bionic_clone') {
            // clone(flags, ...): arg0 = flags. Block process forks only.
            var flags = args[0].toInt32();
            if ((flags & CLONE_THREAD) === 0) this.block = true;
          } else {
            this.block = true; // fork/vfork are always process forks
          }
        },
        onLeave: function (retval) {
          if (this.block) {
            blocked_forks++;
            if (blocked_forks <= 30) LOG('blocked libc ' + name + ' (#' + blocked_forks + ')');
            retval.replace(ptr(-1)); // pretend fork failed (EAGAIN-ish)
          }
        }
      });
      LOG('hooked libc ' + name + ' for fork-block');
    } catch (e) { LOG('hook ' + name + ' failed: ' + e); }
  });
}
hookLibcFork();

// Raw-svc fallback: follow the MAIN thread with Stalker and neutralise a
// clone svc (nr 220) / clone3 (435) without CLONE_THREAD. We exclude system
// modules to keep overhead sane, and unfollow after FORK_WINDOW_MS once the
// watchdog-creation window has passed (SHIELD forks it during early init).
var FORK_WINDOW_MS = 12000;
function installStalkerForkGuard() {
  try {
    // Exclude system libs so we don't rewrite all of libart/libc.
    var excluded = 0;
    Process.enumerateModules().forEach(function (m) {
      var p = m.path || '';
      if (p.indexOf('/system/') === 0 || p.indexOf('/apex/') === 0 ||
          p.indexOf('/vendor/') === 0 || p.indexOf('/product/') === 0) {
        try { Stalker.exclude({ base: m.base, size: m.size }); excluded++; } catch (e) {}
      }
    });
    LOG('stalker: excluded ' + excluded + ' system ranges');

    var mainTid = Process.getCurrentThreadId();
    Stalker.follow(mainTid, {
      events: { call: false, ret: false, exec: false, block: false },
      transform: function (iterator) {
        var inst;
        while ((inst = iterator.next()) !== null) {
          if (inst.mnemonic === 'svc') {
            iterator.putCallout(function (ctx) {
              var nr = ctx.x8.toInt32();
              if (nr === 220) {                       // clone
                if ((ctx.x0.toInt32() & CLONE_THREAD) === 0) {
                  blocked_forks++;
                  if (blocked_forks <= 30) LOG('blocked svc clone (#' + blocked_forks + ') pc=' + ctx.pc);
                  ctx.x0 = ptr(-11);                  // -EAGAIN
                  ctx.pc = ctx.pc.add(4);
                }
              } else if (nr === 435) {                // clone3
                try {
                  var flags = ctx.x0.readU64();
                  if (flags.and(CLONE_THREAD).valueOf() === 0) {
                    blocked_forks++;
                    if (blocked_forks <= 30) LOG('blocked svc clone3 (#' + blocked_forks + ')');
                    ctx.x0 = ptr(-11);
                    ctx.pc = ctx.pc.add(4);
                  }
                } catch (e) {}
              } else if (nr === 101 || nr === 117) {
                // nanosleep(101)/ptrace(117) from main are not us; leave.
              }
            });
          }
          iterator.keep();
        }
      }
    });
    LOG('stalker: following main tid=' + mainTid + ' for fork-block');
    setTimeout(function () {
      try { Stalker.unfollow(mainTid); Stalker.flush(); LOG('stalker: unfollowed main (window elapsed), blocked_forks=' + blocked_forks); }
      catch (e) { LOG('stalker unfollow err: ' + e); }
    }, FORK_WINDOW_MS);
  } catch (e) { LOG('installStalkerForkGuard failed: ' + e); }
}
installStalkerForkGuard();

// ---- (3) anon-memory BoringSSL SSL_write scanner + /f3mf capture ----------
// Same approach as scan_anon_ssl.js: SHIELD unpacks the Flutter BoringSSL
// into anon memory with stripped symbols; we byte-signature-scan SSL_write's
// position-independent prologue (derived from system libssl.so) and hook it.
var SSL_WRITE_PROLOGUE = 'ff 43 01 d1 fd 7b 01 a9 fd 43 00 91 f7 13 00 f9 f6 57 03 a9 f4 4f 04 a9';
var resolved = false;
var http_logged = 0;
var SEEN = {};

function knownModuleRanges() {
  var r = [];
  Process.enumerateModules().forEach(function (m) {
    r.push([m.base, m.base.add(m.size)]);
  });
  return r;
}
function inKnownModule(addr, mods) {
  for (var i = 0; i < mods.length; i++) {
    if (addr.compare(mods[i][0]) >= 0 && addr.compare(mods[i][1]) < 0) return true;
  }
  return false;
}

function hookSslWrite(addr, how) {
  if (SEEN[addr.toString()]) return;
  SEEN[addr.toString()] = true;
  try {
    Interceptor.attach(addr, {
      onEnter: function (args) {
        try {
          var num = args[2].toInt32();
          if (num <= 16 || num > 65536) return;
          var first = args[1].readU8();
          if (first !== 0x47 && first !== 0x50 && first !== 0x44 && first !== 0x48) return;
          var sz = Math.min(num, 16384);
          var bytes = new Uint8Array(args[1].readByteArray(sz));
          var txt = '';
          for (var i = 0; i < bytes.length; i++) txt += String.fromCharCode(bytes[i]);
          var reqline = txt.split('\r\n')[0] || '';
          if (!/^(GET|POST|PUT|DELETE|HEAD) /.test(reqline)) return;
          if (http_logged < 100) {
            http_logged++;
            var hm = txt.match(/\r\nHost:\s*([^\r\n]+)/i);
            LOG('HTTP[' + http_logged + '] ' + reqline.substring(0, 90) + '  Host=' + (hm ? hm[1] : '?'));
          }
          if (txt.indexOf('/design-service/instance/') < 0 || txt.indexOf('/f3mf') < 0) return;
          var hdrEnd = txt.indexOf('\r\n\r\n');
          var headers = hdrEnd > 0 ? txt.substring(0, hdrEnd) : txt;
          LOG('!!! /f3mf CAPTURED via ' + how + ' (' + num + ' B)');
          try {
            var cf = new File(CAP_PATH, 'w');
            cf.write(JSON.stringify({ via: how, captured_at: Date.now(), headers: headers }, null, 2));
            cf.flush(); cf.close();
            LOG('wrote ' + CAP_PATH);
          } catch (e) { LOG('write capture failed: ' + e); }
        } catch (e) { LOG('ssl_write onEnter err: ' + e); }
      }
    });
    LOG('hooked SSL_write @ ' + addr + ' (' + how + ')');
  } catch (e) { LOG('hook ssl_write @ ' + addr + ' failed: ' + e); }
}

function scanForSslWrite() {
  if (resolved) return;
  var mods = knownModuleRanges();
  var xRanges;
  try { xRanges = Process.enumerateRanges('r-x').concat(Process.enumerateRanges('rwx')); }
  catch (e) { return; }
  var anonX = xRanges.filter(function (r) { return !inKnownModule(r.base, mods); });
  var hits = [];
  for (var i = 0; i < anonX.length; i++) {
    var r = anonX[i];
    if (r.size > 96 * 1024 * 1024) continue;
    try {
      var found = Memory.scanSync(r.base, r.size, SSL_WRITE_PROLOGUE);
      for (var j = 0; j < found.length; j++) hits.push(found[j].address);
    } catch (e) {}
    if (hits.length > 40) break;
  }
  if (hits.length >= 1 && hits.length <= 6) {
    resolved = true;
    LOG('RESOLVED SSL_write: ' + hits.length + ' candidate(s) — hooking all');
    hits.forEach(function (h, idx) { hookSslWrite(h, 'sig#' + idx); });
  } else if (hits.length > 6) {
    LOG('sig matched ' + hits.length + ' (too many — refine signature)');
  }
}

// Drive the scan from app-thread Interceptor callbacks (mprotect/dlopen) so
// it re-runs as SHIELD unpacks BoringSSL; also poll via setInterval (gadget
// event loop works since we don't keep Stalker following all threads).
['mprotect', 'android_dlopen_ext', 'dlopen'].forEach(function (name) {
  var a = null; try { a = Module.getGlobalExportByName(name); } catch (e) {}
  if (!a) return;
  try {
    Interceptor.attach(a, { onLeave: function () { try { scanForSslWrite(); } catch (e) {} } });
  } catch (e) {}
});
var polls = 0;
var pollTimer = setInterval(function () {
  polls++;
  scanForSslWrite();
  if (resolved || polls > 120) { clearInterval(pollTimer); }
}, 1000);

LOG('gadget_init.js init complete (fork-block + ssl scanner armed)');
