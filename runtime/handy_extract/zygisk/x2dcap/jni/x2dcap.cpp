// x2dcap.cpp — Zygisk module that captures Bambu Handy's /f3mf request headers
// by inline-hooking BoringSSL SSL_write from INSIDE the app process, with ZERO
// Frida footprint.
//
// Why a Zygisk module (vs Frida): Promon SHIELD defeats Frida by detecting the
// agent (gum-js-loop comm thread → 0xdead kill; or, once stealthed, fork+exec
// escaping the agent into a non-functional husk — see ../../README.md #5-#6).
// A Zygisk module is mapped by ReZygisk at zygote-specialize; with NoHello
// hiding root + module traces (Enforce-DenyList OFF + Handy on the denylist),
// SHIELD sees a normal process and the app stays FUNCTIONAL with our hook live.
//
// Flow: postAppSpecialize (target == Handy) → spawn a waiter thread that
//   (a) hooks Conscrypt libssl.so SSL_write (named export — carries Firebase),
//   (b) hooks Flutter's dart:io BoringSSL SSL_write — the /f3mf path. Handy's
//       native code (libflutter.so) is mmap'd straight out of the split APK
//       (extractNativeLibs=false), so /proc/maps shows the APK path, never
//       "libflutter.so", and SSL_write is stripped from .dynsym. We locate it
//       by load bias (dl_iterate_phdr — the linker still knows it as
//       "libflutter.so") + a fixed vaddr resolved offline from the matching
//       unstripped engine symbols (build-id 0a7fde9b…, engine 587c18f8…),
//       guarded by a prologue-byte sanity check so an engine bump can't make us
//       hook the wrong address.
// The hook captures any request whose header block carries "x-bbl" / "/f3mf" /
// "design-service" into the app's own cache file, read back as root.
//
// SAFETY: all out-of-our-own-memory reads go through safe_read() (a pipe-EFAULT
// probe) so a false/partial ELF or an unmapped page can NEVER SIGSEGV and crash
// Handy.

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cerrno>
#include <unistd.h>
#include <fcntl.h>
#include <sys/uio.h>
#include <pthread.h>
#include <elf.h>
#include <link.h>
#include <sys/types.h>
#include <jni.h>
#include <android/log.h>

#include "zygisk.hpp"
#include "And64InlineHook.hpp"

using zygisk::Api;
using zygisk::AppSpecializeArgs;

#define LOG_TAG "x2dcap"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)

static const char *TARGET_PROC = "bbl.intl.bambulab.com";

// ---------------------------------------------------------------------------
// Capture state (file-scope so the waiter thread + hook can reach it).
// ---------------------------------------------------------------------------
static char g_data_dir[256] = {0};

typedef int (*ssl_write_t)(void *ssl, const void *buf, int num);
// Separate trampolines: conscrypt's and Flutter's BoringSSL SSL_write are
// different functions, so each replacement must call its OWN original.
static ssl_write_t g_orig_conscrypt = nullptr;
static ssl_write_t g_orig_flutter = nullptr;
static int g_captured = 0;
static int g_seen = 0;   // diagnostic: count of HTTP request-lines logged
// Raw outbound-stream capture budget. Bambu's API is HTTP/2, so request headers
// reach SSL_write already HPACK-compressed — unreadable as text. We instead log
// the raw SSL_write bytes per TLS connection (keyed by the SSL* pointer) so the
// full client→server h2 byte stream can be reassembled and HPACK-decoded
// offline. Bounded so a chatty connection (MQTT keep-alives etc.) can't fill the
// disk; reset the file before triggering the action you want to capture.
static long g_raw_bytes = 0;
static const long RAW_CAP = 12L * 1024 * 1024;   // ~12 MB ceiling

// SSL_write's vaddr inside libflutter.so, resolved offline from the matching
// unstripped engine symbols (engine 587c18f873…, build-id 0a7fde9baaf4…). The
// device binary is byte-identical (same build-id), so this offset is exact.
static const uintptr_t FLUTTER_SSL_WRITE_VADDR = 0x717ef0;
// First 8 bytes of SSL_write's prologue: sub sp,#0x40 ; stp x30,x23,[sp,#0x10].
// A sanity check that bias+vaddr still lands on the right function; if Handy
// ships a new Flutter engine the bytes won't match and we refuse to hook.
static const unsigned char FLUTTER_SSL_WRITE_SIG[8] = {
    0xff, 0x03, 0x01, 0xd1, 0xfe, 0x5f, 0x01, 0xa9};

// BoringSSL signing primitives inside libflutter.so (offline-resolved vaddrs;
// build-id 0a7fde9b…). Printer-control MQTT commands are RSA-SHA256 signed with
// a SHIELD-held key that never hits disk — so we lift it at the moment Handy
// signs, by hooking the signer and walking EVP_PKEY → RSA → BIGNUMs. The key
// (n/e/d/p/q) is written hex to x2d_rsa.txt for offline PKCS#8 reconstruction.
static const uintptr_t FLUTTER_EVP_PKEY_SIGN_VADDR     = 0x6eabcc;
static const uintptr_t FLUTTER_EVP_DIGESTSIGNFINAL_VADDR = 0x6d1b04;
// Handy SHA-256s the payload itself and calls the raw RSA path (not the EVP
// digest-sign API), so the always-hit target is the core private transform —
// rsa_private_transform_no_self_test(RSA *rsa, …): RSA* is arg0 directly. And if
// the key is loaded from DER, RSA_parse_private_key's CBS arg0 IS the key.
static const uintptr_t FLUTTER_RSA_PRIV_TRANSFORM_VADDR = 0x6d9a68;
static const uintptr_t FLUTTER_RSA_PARSE_PRIVKEY_VADDR  = 0x6f5fb8;
typedef int (*evp_pkey_sign_t)(void *ctx, unsigned char *sig, size_t *siglen,
                               const unsigned char *tbs, size_t tbslen);
typedef int (*evp_digestsignfinal_t)(void *ctx, unsigned char *sig, size_t *siglen);
typedef int (*rsa_priv_transform_t)(void *rsa, unsigned char *out,
                                    const unsigned char *in, size_t len);
typedef void *(*rsa_parse_privkey_t)(void *cbs);
static evp_pkey_sign_t g_orig_evp_pkey_sign = nullptr;
static evp_digestsignfinal_t g_orig_evp_digestsignfinal = nullptr;
static rsa_priv_transform_t g_orig_rsa_priv_transform = nullptr;
static rsa_parse_privkey_t g_orig_rsa_parse_privkey = nullptr;
static int g_sign_calls = 0;     // diagnostic: how many times the signer fired
static int g_key_dumped = 0;

// ---------------------------------------------------------------------------
// safe_read: probe-read [src, src+len) into dst WITHOUT faulting. Writing the
// source bytes to a pipe makes the kernel return EFAULT for unmapped memory
// instead of delivering SIGSEGV. len must be < the pipe buffer; we only read
// small ELF structs / short symbol names. Returns false (no crash) on any
// unmapped/partial read.
// ---------------------------------------------------------------------------
static int g_pipe[2] = {-1, -1};
static bool safe_read(void *dst, const void *src, size_t len) {
  if (len == 0 || len > 4096) return false;
  if (g_pipe[0] < 0) { if (pipe(g_pipe) != 0) return false; }
  ssize_t w = write(g_pipe[1], src, len);
  if (w != (ssize_t) len) {
    if (w > 0) {                       // drain the partial write
      char t[4096]; ssize_t got = 0;
      while (got < w) { ssize_t r = read(g_pipe[0], t, sizeof t); if (r <= 0) break; got += r; }
    }
    return false;
  }
  size_t got = 0;
  while (got < len) {
    ssize_t r = read(g_pipe[0], (char *) dst + got, len - got);
    if (r <= 0) return false;
    got += (size_t) r;
  }
  return true;
}

// Append a line to <data_dir>/cache/<name> (app-writable under SELinux).
static void append_file(const char *name, const char *data, int len) {
  char path[512];
  snprintf(path, sizeof(path), "%s/cache/%s",
           g_data_dir[0] ? g_data_dir : "/data/data/bbl.intl.bambulab.com", name);
  int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
  if (fd < 0) return;
  write(fd, data, (size_t) len);
  close(fd);
}

static void marker(const char *msg) {
  char buf[256];
  int n = snprintf(buf, sizeof(buf), "%s pid=%d uid=%d\n", msg, getpid(), getuid());
  if (n > 0) append_file("x2d_zyg.log", buf, n);
  LOGI("%s", msg);
}

static bool mem_contains(const char *hay, int haylen, const char *needle) {
  return memmem(hay, (size_t) haylen, needle, strlen(needle)) != nullptr;
}

// ---------------------------------------------------------------------------
// Shared capture: SSL_write(ssl, buf, num) plaintext. Capture matching request
// header blocks to the app cache. Two thin replacement functions below each
// forward to their own original trampoline.
// ---------------------------------------------------------------------------
static void capture_request(const void *buf, int num, const char *via) {
  if (buf == nullptr || num < 24 || num > 262144) return;
  const char *p = (const char *) buf;
  char c0 = p[0];
  if (c0 != 'G' && c0 != 'P' && c0 != 'D' && c0 != 'H') return;   // GET/POST/PUT/DELETE/HEAD
  int lim = num < 16384 ? num : 16384;
  // Diagnostic: log the request-line of EVERY HTTP write (regardless of filter)
  // so we can confirm the hook fires and see which host/path /f3mf rides on.
  if (g_seen < 80) {
    g_seen++;
    char line[200];
    int n = 0;
    for (; n < lim && n < 150 && p[n] != '\r' && p[n] != '\n'; n++) line[n] = p[n];
    line[n] = '\0';
    LOGI("REQ [%s] %s", via, line);
  }
  if (!mem_contains(p, lim, "x-bbl") && !mem_contains(p, lim, "/f3mf") &&
      !mem_contains(p, lim, "design-service"))
    return;
  int hdrlen = lim;   // header block up to CRLFCRLF
  for (int i = 0; i + 4 <= lim; i++)
    if (p[i] == '\r' && p[i + 1] == '\n' && p[i + 2] == '\r' && p[i + 3] == '\n') {
      hdrlen = i + 4;
      break;
    }
  append_file("x2d_f3mf.txt", p, hdrlen);
  append_file("x2d_f3mf.txt", "\n===X2DCAP===\n", 14);
  if (g_captured < 50) {
    g_captured++;
    char line[160];
    int n = 0;
    for (; n < lim && n < 110 && p[n] != '\r' && p[n] != '\n'; n++) line[n] = p[n];
    line[n] = '\0';
    LOGI("CAPTURED [%s] %s", via, line);
  }
}

// Append one length-framed record of this SSL_write's plaintext to x2d_raw.bin:
//   magic 'X2RW' (4) | ssl ptr (8, LE) | via tag (1) | len (4, LE) | bytes…
// Offline: group records by ssl ptr (one TLS connection), concatenate `bytes`
// in file order to recover the outbound h2 stream, then HPACK-decode its HEADERS
// frames. Per-write capture is capped so a single huge write can't dominate.
static void capture_raw(void *ssl, const void *buf, int num, char via) {
  if (buf == nullptr || num <= 0) return;
  if (g_raw_bytes >= RAW_CAP) return;
  int n = num > 16384 ? 16384 : num;            // cap per-write
  char hdr[17];
  memcpy(hdr, "X2RW", 4);
  uint64_t sp = (uint64_t) ssl;
  memcpy(hdr + 4, &sp, 8);
  hdr[12] = via;
  uint32_t ln = (uint32_t) n;
  memcpy(hdr + 13, &ln, 4);
  // One iovec write so the record is atomic under O_APPEND across threads.
  char path[512];
  snprintf(path, sizeof(path), "%s/cache/x2d_raw.bin",
           g_data_dir[0] ? g_data_dir : "/data/data/bbl.intl.bambulab.com");
  int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
  if (fd < 0) return;
  struct iovec iov[2] = {{hdr, sizeof hdr}, {(void *) buf, (size_t) n}};
  ssize_t w = writev(fd, iov, 2);
  close(fd);
  if (w > 0) g_raw_bytes += w;
}

static int my_ssl_write_conscrypt(void *ssl, const void *buf, int num) {
  capture_request(buf, num, "conscrypt");
  capture_raw(ssl, buf, num, 'C');
  return g_orig_conscrypt(ssl, buf, num);
}
static int my_ssl_write_flutter(void *ssl, const void *buf, int num) {
  capture_request(buf, num, "flutter");
  capture_raw(ssl, buf, num, 'F');
  return g_orig_flutter(ssl, buf, num);
}

// ---------------------------------------------------------------------------
// Resolve an exported symbol from an in-memory ELF at `base`. Every field
// access uses safe_read(), so a false ELF magic / unmapped dynsym yields
// nullptr instead of a crash. Returns the symbol address or nullptr.
// ---------------------------------------------------------------------------
static void *resolve_sym(uintptr_t base, const char *want) {
  Elf64_Ehdr eh;
  if (!safe_read(&eh, (void *) base, sizeof eh)) return nullptr;
  if (memcmp(eh.e_ident, ELFMAG, SELFMAG) != 0) return nullptr;
  if (eh.e_phnum == 0 || eh.e_phnum > 128) return nullptr;
  if (eh.e_phentsize < sizeof(Elf64_Phdr)) return nullptr;

  uintptr_t dyn_vaddr = 0, min_vaddr = (uintptr_t) -1;
  for (int i = 0; i < eh.e_phnum; i++) {
    Elf64_Phdr ph;
    if (!safe_read(&ph, (void *) (base + eh.e_phoff + (uintptr_t) i * eh.e_phentsize), sizeof ph))
      return nullptr;
    if (ph.p_type == PT_LOAD && ph.p_vaddr < min_vaddr) min_vaddr = ph.p_vaddr;
    if (ph.p_type == PT_DYNAMIC) dyn_vaddr = ph.p_vaddr;
  }
  if (!dyn_vaddr) return nullptr;
  if (min_vaddr == (uintptr_t) -1) min_vaddr = 0;
  uintptr_t bias = base - min_vaddr;          // vaddr X maps at base + (X - min_vaddr)

  uintptr_t strtab = 0, symtab = 0;
  size_t syment = sizeof(Elf64_Sym);
  for (int i = 0; i < 8192; i++) {
    Elf64_Dyn d;
    if (!safe_read(&d, (void *) (dyn_vaddr + bias + (uintptr_t) i * sizeof d), sizeof d))
      return nullptr;
    if (d.d_tag == DT_NULL) break;
    if (d.d_tag == DT_STRTAB) strtab = d.d_un.d_ptr + bias;
    else if (d.d_tag == DT_SYMTAB) symtab = d.d_un.d_ptr + bias;
    else if (d.d_tag == DT_SYMENT) syment = d.d_un.d_val;
  }
  if (!strtab || !symtab || strtab <= symtab || syment == 0) return nullptr;
  // dynsym immediately precedes dynstr → count = (strtab - symtab) / syment.
  size_t nsyms = (strtab - symtab) / syment;
  if (nsyms == 0 || nsyms > 200000) return nullptr;

  size_t wantlen = strlen(want);
  for (size_t i = 0; i < nsyms; i++) {
    Elf64_Sym s;
    if (!safe_read(&s, (void *) (symtab + i * syment), sizeof s)) return nullptr;
    if (s.st_name == 0 || s.st_value == 0) continue;
    char nm[48];
    if (!safe_read(nm, (void *) (strtab + s.st_name), sizeof nm)) continue;
    nm[sizeof nm - 1] = '\0';
    if (strncmp(nm, want, wantlen) == 0 && nm[wantlen] == '\0')
      return (void *) (s.st_value + bias);
  }
  return nullptr;
}

// ---------------------------------------------------------------------------
// Find a loaded lib by path-substring via /proc/self/maps (namespace-proof —
// dlsym across Conscrypt's dedicated linker namespace is unreliable).
// ---------------------------------------------------------------------------
static uintptr_t find_lib_base(const char *substr) {
  FILE *f = fopen("/proc/self/maps", "r");
  if (!f) return 0;
  char line[512];
  uintptr_t base = 0;
  while (fgets(line, sizeof(line), f)) {
    if (!strstr(line, substr)) continue;
    unsigned long start = 0, off = 1;
    char perms[8] = {0};
    if (sscanf(line, "%lx-%*lx %7s %lx", &start, perms, &off) != 3) continue;
    if (off != 0) continue;                    // file-offset-0 mapping = ELF header
    base = (uintptr_t) start;
    break;
  }
  fclose(f);
  return base;
}

// ---------------------------------------------------------------------------
// Hook Flutter's dart:io BoringSSL SSL_write. libflutter.so is mapped straight
// out of the split APK, so it never appears as "libflutter.so" in /proc/maps —
// but the dynamic linker still tracks it by that name, so dl_iterate_phdr hands
// us its load bias. SSL_write is stripped from .dynsym; we add the offline-
// resolved vaddr to the bias and sanity-check the prologue bytes before hooking.
// ---------------------------------------------------------------------------
struct flutter_find { uintptr_t bias; };
static int flutter_phdr_cb(struct dl_phdr_info *info, size_t, void *data) {
  const char *nm = info->dlpi_name;
  if (nm != nullptr && strstr(nm, "libflutter.so") != nullptr) {
    ((flutter_find *) data)->bias = (uintptr_t) info->dlpi_addr;
    return 1;                                  // stop iteration
  }
  return 0;
}

static bool hook_flutter_boringssl() {
  flutter_find ff = {0};
  dl_iterate_phdr(flutter_phdr_cb, &ff);
  if (ff.bias == 0) return false;              // libflutter.so not loaded yet
  uintptr_t sw = ff.bias + FLUTTER_SSL_WRITE_VADDR;
  unsigned char got[8];
  if (!safe_read(got, (void *) sw, sizeof got)) return false;   // not mapped yet
  if (memcmp(got, FLUTTER_SSL_WRITE_SIG, sizeof got) != 0) {
    LOGI("flutter SSL_write @ %p prologue mismatch %02x%02x%02x%02x — engine bump?",
         (void *) sw, got[0], got[1], got[2], got[3]);
    return false;                              // wrong offset — refuse to hook
  }
  A64HookFunction((void *) sw, (void *) my_ssl_write_flutter, (void **) &g_orig_flutter);
  LOGI("hooked FLUTTER BoringSSL SSL_write @ %p (bias %p)", (void *) sw, (void *) ff.bias);
  marker("x2d-zygisk FLUTTER_SSL_HOOK_INSTALLED");
  return true;
}

// ---------------------------------------------------------------------------
// Printer-control signer extraction. When Handy RSA-signs an MQTT control
// command it loads its (SHIELD-held, never-on-disk) RSA key into a BoringSSL
// EVP_PKEY and calls EVP_PKEY_sign / EVP_DigestSignFinal. We hook those, walk
// EVP_PKEY_CTX → EVP_PKEY → RSA → its BIGNUMs (ALL via safe_read — every deref
// is fault-probed so a wrong offset guess can't crash Handy), and write the
// key components hex to x2d_rsa.txt. Offline, dump_keys.py:reconstruct_pkcs8()
// rebuilds the PEM. Struct offsets vary by build, so we PROBE candidate slots
// and only commit when we find a full RSA private key (≥5 large BIGNUMs).
// ---------------------------------------------------------------------------
struct bn_st { uintptr_t d; int width; int dmax; int neg; int flags; };

// Scan an RSA*'s first 24 pointer slots for BIGNUMs; emit RSA-2048-plausible
// ones (e=1 limb, n/d=32 limbs, p/q/dmp1/dmq1/iqmp=16 limbs) big-endian-hex.
// Returns the count of "large" components; commits to file only when ≥5.
static int dump_rsa(void *rsa) {
  if (g_key_dumped || rsa == nullptr) return 0;
  static char out[8192];
  int o = 0, big = 0;
  for (int slot = 0; slot < 24; slot++) {
    uintptr_t p;
    if (!safe_read(&p, (char *) rsa + slot * 8, 8) || p < 0x1000) continue;
    bn_st bn;
    if (!safe_read(&bn, (void *) p, sizeof bn)) continue;
    if (bn.width <= 0 || bn.width > 40 || bn.d < 0x1000) continue;
    bool plausible = (bn.width == 1) || (bn.width >= 15 && bn.width <= 33);
    if (!plausible) continue;
    unsigned long long limbs[40];
    if (!safe_read(limbs, (void *) bn.d, bn.width * 8)) continue;
    if (bn.width > 1) big++;
    o += snprintf(out + o, sizeof out - o, "slot%d w%d ", slot, bn.width);
    for (int i = bn.width - 1; i >= 0 && o < (int) sizeof out - 24; i--)
      o += snprintf(out + o, sizeof out - o, "%016llx", limbs[i]);
    o += snprintf(out + o, sizeof out - o, "\n");
    if (o > (int) sizeof out - 720) break;
  }
  if (big >= 5) {                       // full RSA private key (n,d,p,q,dmp1,…)
    append_file("x2d_rsa.txt", out, o);
    g_key_dumped = 1;
    marker("x2d-zygisk RSA_KEY_DUMPED");
  }
  return big;
}

// Given a candidate EVP_PKEY_CTX*, probe its pkey field, then that EVP_PKEY's
// rsa union ptr, then dump. All reads are safe_read-guarded.
static void try_extract_from_pkey_ctx(uintptr_t pctx) {
  if (g_key_dumped || pctx < 0x1000) return;
  for (int po = 8; po <= 32 && !g_key_dumped; po += 8) {       // EVP_PKEY_CTX.pkey
    uintptr_t pkey;
    if (!safe_read(&pkey, (char *) pctx + po, 8) || pkey < 0x1000) continue;
    for (int ro = 8; ro <= 40 && !g_key_dumped; ro += 8) {     // EVP_PKEY.pkey.rsa
      uintptr_t rsa;
      if (!safe_read(&rsa, (char *) pkey + ro, 8) || rsa < 0x1000) continue;
      dump_rsa((void *) rsa);
    }
  }
}

static int my_evp_pkey_sign(void *ctx, unsigned char *sig, size_t *siglen,
                            const unsigned char *tbs, size_t tbslen) {
  g_sign_calls++;
  try_extract_from_pkey_ctx((uintptr_t) ctx);
  if (g_sign_calls <= 12) LOGI("EVP_PKEY_sign #%d tbslen=%zu dumped=%d",
                               g_sign_calls, tbslen, g_key_dumped);
  return g_orig_evp_pkey_sign(ctx, sig, siglen, tbs, tbslen);
}

static int my_evp_digestsignfinal(void *mdctx, unsigned char *sig, size_t *siglen) {
  g_sign_calls++;
  if (!g_key_dumped && mdctx != nullptr) {
    // EVP_MD_CTX holds an EVP_PKEY_CTX* (pctx) in one of its first slots; probe.
    for (int s = 0; s < 8 && !g_key_dumped; s++) {
      uintptr_t cand;
      if (!safe_read(&cand, (char *) mdctx + s * 8, 8) || cand < 0x1000) continue;
      try_extract_from_pkey_ctx(cand);
    }
  }
  if (g_sign_calls <= 12) LOGI("EVP_DigestSignFinal #%d dumped=%d",
                               g_sign_calls, g_key_dumped);
  return g_orig_evp_digestsignfinal(mdctx, sig, siglen);
}

// The core RSA private-key op — arg0 IS the RSA*. Hit by every sign/decrypt, so
// this catches the raw-RSA signing path the EVP entry points miss.
static int my_rsa_priv_transform(void *rsa, unsigned char *out,
                                 const unsigned char *in, size_t len) {
  g_sign_calls++;
  dump_rsa(rsa);
  if (g_sign_calls <= 12) LOGI("rsa_private_transform #%d dumped=%d",
                               g_sign_calls, g_key_dumped);
  return g_orig_rsa_priv_transform(rsa, out, in, len);
}

// RSA_parse_private_key(CBS *cbs) — if the key is loaded from DER, cbs->data is
// the full PKCS#1 RSA private key. CBS { const uint8_t *data; size_t len; }.
static void *my_rsa_parse_privkey(void *cbs) {
  if (!g_key_dumped && cbs != nullptr) {
    uintptr_t data; size_t len;
    if (safe_read(&data, cbs, 8) && safe_read(&len, (char *) cbs + 8, 8) &&
        data > 0x1000 && len >= 64 && len <= 4096) {
      static unsigned char der[4096];
      if (safe_read(der, (void *) data, len)) {
        append_file("x2d_rsa_der.bin", (const char *) der, (int) len);
        g_key_dumped = 1;
        marker("x2d-zygisk RSA_DER_CAPTURED");
      }
    }
  }
  return g_orig_rsa_parse_privkey(cbs);
}

static bool hook_flutter_signer() {
  flutter_find ff = {0};
  dl_iterate_phdr(flutter_phdr_cb, &ff);
  if (ff.bias == 0) return false;
  void *ps = (void *) (ff.bias + FLUTTER_EVP_PKEY_SIGN_VADDR);
  void *ds = (void *) (ff.bias + FLUTTER_EVP_DIGESTSIGNFINAL_VADDR);
  void *pt = (void *) (ff.bias + FLUTTER_RSA_PRIV_TRANSFORM_VADDR);
  void *pp = (void *) (ff.bias + FLUTTER_RSA_PARSE_PRIVKEY_VADDR);
  unsigned char probe[4];
  if (!safe_read(probe, pt, 4)) return false;
  A64HookFunction(ps, (void *) my_evp_pkey_sign, (void **) &g_orig_evp_pkey_sign);
  A64HookFunction(ds, (void *) my_evp_digestsignfinal, (void **) &g_orig_evp_digestsignfinal);
  A64HookFunction(pt, (void *) my_rsa_priv_transform, (void **) &g_orig_rsa_priv_transform);
  A64HookFunction(pp, (void *) my_rsa_parse_privkey, (void **) &g_orig_rsa_parse_privkey);
  LOGI("hooked FLUTTER signer: rsa_private_transform @ %p RSA_parse_private_key @ %p", pt, pp);
  marker("x2d-zygisk SIGNER_HOOKS_INSTALLED");
  return true;
}

// ---------------------------------------------------------------------------
// Waiter thread: hook Conscrypt libssl.so SSL_write AND Flutter's dart:io
// BoringSSL SSL_write (the /f3mf path). libflutter.so loads lazily when the
// Flutter engine spins up, so we keep polling until both are hooked.
// ---------------------------------------------------------------------------
static void *hook_waiter(void *) {
  bool conscrypt_done = false, flutter_done = false, signer_done = false;
  for (int tries = 0; tries < 1200; tries++) {   // up to ~120s
    if (!conscrypt_done) {
      uintptr_t base = find_lib_base("/libssl.so");
      if (base != 0) {
        void *sw = resolve_sym(base, "SSL_write");
        if (sw != nullptr && g_orig_conscrypt == nullptr) {
          A64HookFunction(sw, (void *) my_ssl_write_conscrypt, (void **) &g_orig_conscrypt);
          LOGI("hooked CONSCRYPT SSL_write @ %p (base %p)", sw, (void *) base);
          marker("x2d-zygisk SSL_HOOK_INSTALLED");
        }
        conscrypt_done = (sw != nullptr);
      }
    }
    if (!flutter_done) flutter_done = hook_flutter_boringssl();
    // The signer lives in the same libflutter.so; hook it once SSL_write proved
    // the bias is valid.
    if (flutter_done && !signer_done) signer_done = hook_flutter_signer();
    if (conscrypt_done && flutter_done && signer_done) {
      LOGI("all hooks installed (ssl + signer)");
      return nullptr;
    }
    usleep(100 * 1000);                      // 100 ms
  }
  LOGI("waiter done (conscrypt=%d flutter=%d signer=%d)",
       conscrypt_done, flutter_done, signer_done);
  return nullptr;
}

class X2DCapture : public zygisk::ModuleBase {
public:
  void onLoad(Api *api, JNIEnv *env) override {
    this->api = api;
    this->env = env;
  }

  void preAppSpecialize(AppSpecializeArgs *args) override {
    is_target = false;
    if (args->nice_name != nullptr) {
      const char *name = env->GetStringUTFChars(args->nice_name, nullptr);
      if (name != nullptr) {
        if (strcmp(name, TARGET_PROC) == 0) {
          is_target = true;
          if (args->app_data_dir != nullptr) {
            const char *dd = env->GetStringUTFChars(args->app_data_dir, nullptr);
            if (dd != nullptr) {
              strncpy(g_data_dir, dd, sizeof(g_data_dir) - 1);
              env->ReleaseStringUTFChars(args->app_data_dir, dd);
            }
          }
        }
        env->ReleaseStringUTFChars(args->nice_name, name);
      }
    }
    if (!is_target) {
      api->setOption(zygisk::DLCLOSE_MODULE_LIBRARY);
    }
  }

  void postAppSpecialize(const AppSpecializeArgs *) override {
    if (!is_target) return;
    marker("x2d-zygisk LOADED");
    pthread_t t;
    if (pthread_create(&t, nullptr, hook_waiter, nullptr) == 0) {
      pthread_detach(t);
    } else {
      LOGI("pthread_create(hook_waiter) failed errno=%d", errno);
    }
  }

private:
  Api *api = nullptr;
  JNIEnv *env = nullptr;
  bool is_target = false;
};

REGISTER_ZYGISK_MODULE(X2DCapture)
