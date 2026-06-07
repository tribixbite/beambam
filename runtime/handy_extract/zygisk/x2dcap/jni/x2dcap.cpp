// x2dcap.cpp — Zygisk module that captures Bambu Handy's /f3mf request headers
// by inline-hooking Conscrypt SSL_write from INSIDE the app process, with ZERO
// Frida footprint.
//
// Why a Zygisk module (vs Frida): Promon SHIELD defeats Frida by detecting the
// agent (gum-js-loop comm thread → 0xdead kill; or, once stealthed, fork+exec
// escaping the agent into a non-functional husk — see ../../README.md #5-#6).
// A Zygisk module is mapped by ReZygisk at zygote-specialize; with NoHello
// hiding root + module traces (Enforce-DenyList OFF + Handy on the denylist),
// SHIELD sees a normal process and the app stays FUNCTIONAL with our hook live.
//
// Flow: postAppSpecialize (target == Handy) → spawn a waiter thread that polls
// /proc/self/maps until Conscrypt's libssl.so is mapped (it loads lazily on
// first TLS), resolves SSL_write from its in-memory dynsym, and inline-hooks it
// (And64InlineHook). The hook captures any request whose header block carries
// an "x-bbl" signed header or a "/f3mf" / "design-service" path — that's the
// captcha-bypass set — appending it to the app's own cache file, which we read
// back as root. The /f3mf download travels over Conscrypt (flutter_downloader's
// native path; an earlier Frida run confirmed /f3mf at the system libssl
// SSL_write).

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cerrno>
#include <unistd.h>
#include <fcntl.h>
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
// Separate trampolines: conscrypt's and the anon BoringSSL's SSL_write are
// different functions, so each replacement must call its OWN original.
static ssl_write_t g_orig_conscrypt = nullptr;
static ssl_write_t g_orig_anon = nullptr;
static int g_captured = 0;

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

static int my_ssl_write_conscrypt(void *ssl, const void *buf, int num) {
  capture_request(buf, num, "conscrypt");
  return g_orig_conscrypt(ssl, buf, num);
}
static int my_ssl_write_anon(void *ssl, const void *buf, int num) {
  capture_request(buf, num, "anon");
  return g_orig_anon(ssl, buf, num);
}

// ---------------------------------------------------------------------------
// Namespace-proof symbol resolution: find a loaded lib by path-substring via
// /proc/self/maps, then resolve an exported symbol from its in-memory dynsym.
// (Conscrypt's libssl.so lives in a dedicated linker namespace, so dlopen/dlsym
// from the app's default namespace are unreliable — maps scanning is not.)
// ---------------------------------------------------------------------------
static uintptr_t find_lib_base(const char *substr) {
  FILE *f = fopen("/proc/self/maps", "r");
  if (!f) return 0;
  char line[512];
  uintptr_t base = 0;
  while (fgets(line, sizeof(line), f)) {
    // format: <start>-<end> <perms> <offset> <dev> <inode> <path>
    if (!strstr(line, substr)) continue;
    // require the file-offset-0 mapping (ELF header) for a clean base
    unsigned long start = 0, off = 1;
    char perms[8] = {0};
    if (sscanf(line, "%lx-%*lx %7s %lx", &start, perms, &off) != 3) continue;
    if (off != 0) continue;
    base = (uintptr_t) start;
    break;
  }
  fclose(f);
  return base;
}

static void *resolve_sym(uintptr_t base, const char *want) {
  auto *ehdr = (Elf64_Ehdr *) base;
  if (memcmp(ehdr->e_ident, ELFMAG, SELFMAG) != 0) return nullptr;
  auto *phdr = (Elf64_Phdr *) (base + ehdr->e_phoff);

  uintptr_t dyn_vaddr = 0;
  uintptr_t min_vaddr = (uintptr_t) -1;
  for (int i = 0; i < ehdr->e_phnum; i++) {
    if (phdr[i].p_type == PT_LOAD && phdr[i].p_vaddr < min_vaddr)
      min_vaddr = phdr[i].p_vaddr;
    if (phdr[i].p_type == PT_DYNAMIC) dyn_vaddr = phdr[i].p_vaddr;
  }
  if (!dyn_vaddr) return nullptr;
  if (min_vaddr == (uintptr_t) -1) min_vaddr = 0;
  // load bias: a vaddr X is mapped at base + (X - min_vaddr)
  uintptr_t bias = base - min_vaddr;
  auto *dyn = (Elf64_Dyn *) (dyn_vaddr + bias);

  const char *strtab = nullptr;
  Elf64_Sym *symtab = nullptr;
  size_t syment = sizeof(Elf64_Sym);
  uintptr_t strsz = 0;
  for (Elf64_Dyn *d = dyn; d->d_tag != DT_NULL; d++) {
    switch (d->d_tag) {
      case DT_STRTAB: strtab = (const char *) (d->d_un.d_ptr + bias); break;
      case DT_SYMTAB: symtab = (Elf64_Sym *) (d->d_un.d_ptr + bias); break;
      case DT_SYMENT: syment = d->d_un.d_val; break;
      case DT_STRSZ:  strsz = d->d_un.d_val; break;
      default: break;
    }
  }
  if (!strtab || !symtab) return nullptr;
  // Trick: dynsym immediately precedes dynstr, so the symbol count is
  // (strtab - symtab) / syment (robust without DT_HASH / GNU_HASH walking).
  (void) strsz;
  if ((uintptr_t) strtab <= (uintptr_t) symtab) return nullptr;
  size_t nsyms = ((uintptr_t) strtab - (uintptr_t) symtab) / syment;
  if (nsyms == 0 || nsyms > 200000) return nullptr;   // implausible — skip (avoid OOB read)
  for (size_t i = 0; i < nsyms; i++) {
    auto *s = (Elf64_Sym *) ((uintptr_t) symtab + i * syment);
    if (s->st_name == 0 || s->st_value == 0) continue;
    const char *nm = strtab + s->st_name;
    if (strcmp(nm, want) == 0) return (void *) (s->st_value + bias);
  }
  return nullptr;
}

// ---------------------------------------------------------------------------
// Bambu's /f3mf (and all its API) flows through Flutter's dart:io BoringSSL,
// which SHIELD unpacks into ANONYMOUS executable memory (no named libssl.so
// mapping) with SSL_write stripped from the dynsym. We hunt it in-process:
// for each anon r-x range, locate the BoringSSL image by its version string,
// backtrack to the ELF base, and resolve SSL_write — logging all SSL_* exports
// for diagnosis when the dynsym is stripped.
// ---------------------------------------------------------------------------

// All ranges with r/x/anon flags (so reads can be bounds-checked: dereferencing
// an unmapped page would SIGSEGV and crash Handy).
struct Range { uintptr_t lo, hi; bool read, exec, anon; };
static int enum_ranges(Range *out, int max) {
  FILE *f = fopen("/proc/self/maps", "r");
  if (!f) return 0;
  char line[512];
  int n = 0;
  while (n < max && fgets(line, sizeof(line), f)) {
    unsigned long lo = 0, hi = 0;
    char perms[8] = {0};
    char path[256] = {0};
    int m = sscanf(line, "%lx-%lx %7s %*x %*x:%*x %*d %255[^\n]", &lo, &hi, perms, path);
    if (m < 3) continue;
    out[n].lo = lo; out[n].hi = hi;
    out[n].read = perms[0] == 'r';
    out[n].exec = perms[2] == 'x';
    out[n].anon = (m < 4) || path[0] == '\0' || path[0] == '[';
    n++;
  }
  fclose(f);
  return n;
}

// Is [p, p+len) fully inside a readable range? (bounds-check before deref)
static bool is_readable(const Range *r, int nr, uintptr_t p, size_t len) {
  for (int i = 0; i < nr; i++)
    if (r[i].read && p >= r[i].lo && p + len <= r[i].hi) return true;
  return false;
}

// Log the SSL_* exports of an in-memory ELF (diagnosis when stripped).
static void log_ssl_exports(uintptr_t base) {
  auto *ehdr = (Elf64_Ehdr *) base;
  if (memcmp(ehdr->e_ident, ELFMAG, SELFMAG) != 0) return;
  auto *phdr = (Elf64_Phdr *) (base + ehdr->e_phoff);
  uintptr_t dyn_vaddr = 0, min_vaddr = (uintptr_t) -1;
  for (int i = 0; i < ehdr->e_phnum; i++) {
    if (phdr[i].p_type == PT_LOAD && phdr[i].p_vaddr < min_vaddr) min_vaddr = phdr[i].p_vaddr;
    if (phdr[i].p_type == PT_DYNAMIC) dyn_vaddr = phdr[i].p_vaddr;
  }
  if (!dyn_vaddr) return;
  if (min_vaddr == (uintptr_t) -1) min_vaddr = 0;
  uintptr_t bias = base - min_vaddr;
  auto *dyn = (Elf64_Dyn *) (dyn_vaddr + bias);
  const char *strtab = nullptr; Elf64_Sym *symtab = nullptr; size_t syment = sizeof(Elf64_Sym);
  for (Elf64_Dyn *d = dyn; d->d_tag != DT_NULL; d++) {
    if (d->d_tag == DT_STRTAB) strtab = (const char *) (d->d_un.d_ptr + bias);
    else if (d->d_tag == DT_SYMTAB) symtab = (Elf64_Sym *) (d->d_un.d_ptr + bias);
    else if (d->d_tag == DT_SYMENT) syment = d->d_un.d_val;
  }
  if (!strtab || !symtab || (uintptr_t) strtab <= (uintptr_t) symtab) return;
  size_t nsyms = ((uintptr_t) strtab - (uintptr_t) symtab) / syment;
  if (nsyms == 0 || nsyms > 200000) return;
  int logged = 0;
  for (size_t i = 0; i < nsyms && logged < 12; i++) {
    auto *s = (Elf64_Sym *) ((uintptr_t) symtab + i * syment);
    if (s->st_name == 0) continue;
    const char *nm = strtab + s->st_name;
    if (strncmp(nm, "SSL_", 4) == 0) { LOGI("  anon-elf export: %s", nm); logged++; }
  }
  LOGI("  (anon ELF @ %p: %zu dynsyms, %d SSL_* shown)", (void *) base, nsyms, logged);
}

// Try to resolve+hook the anon Flutter BoringSSL SSL_write. Returns true once
// hooked. All memory reads are bounds-checked against readable ranges.
static bool hook_anon_boringssl() {
  static Range ranges[1024];
  int nr = enum_ranges(ranges, 1024);
  for (int i = 0; i < nr; i++) {
    if (!ranges[i].anon || !ranges[i].exec || !ranges[i].read) continue;
    size_t len = ranges[i].hi - ranges[i].lo;
    if (len > 96u * 1024 * 1024) continue;
    const char *needle = "BoringSSL";
    void *at = memmem((void *) ranges[i].lo, len, needle, strlen(needle));
    if (!at) { needle = "OpenSSL"; at = memmem((void *) ranges[i].lo, len, needle, strlen(needle)); }
    if (!at) continue;
    LOGI("found '%s' @ %p in anon r-x [%p,%p) — backtracking for ELF base",
         needle, at, (void *) ranges[i].lo, (void *) ranges[i].hi);
    // backtrack page-by-page for ELF magic (up to 64 MB), only reading pages
    // that are inside a mapped readable range (else SIGSEGV).
    uintptr_t p = (uintptr_t) at & ~0xfffULL;
    for (int back = 0; back < 16384; back++, p -= 0x1000) {
      if (!is_readable(ranges, nr, p, 4)) break;   // hit an unmapped gap
      if (*(uint32_t *) p == 0x464c457fu) {         // "\x7fELF"
        void *sw = resolve_sym(p, "SSL_write");
        if (sw && is_readable(ranges, nr, (uintptr_t) sw, 4)) {
          A64HookFunction(sw, (void *) my_ssl_write_anon, (void **) &g_orig_anon);
          LOGI("hooked ANON BoringSSL SSL_write @ %p (ELF base %p)", sw, (void *) p);
          marker("x2d-zygisk ANON_SSL_HOOK_INSTALLED");
          return true;
        }
        log_ssl_exports(p);   // stripped — show what IS exported, for diagnosis
        break;                // found the ELF base; stop backtracking this string
      }
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// Waiter thread: hook Conscrypt libssl.so SSL_write (named export) AND hunt
// the anon Flutter BoringSSL SSL_write. Conscrypt carries Firebase; Bambu's
// /f3mf goes over the anon BoringSSL — we want both.
// ---------------------------------------------------------------------------
static void *hook_waiter(void *) {
  bool conscrypt_done = false, anon_done = false;
  for (int tries = 0; tries < 900; tries++) {   // up to ~90s
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
    if (!anon_done) {
      anon_done = hook_anon_boringssl();
    }
    if (anon_done) return nullptr;   // anon is the /f3mf path — done once hooked
    usleep(100 * 1000);              // 100 ms
  }
  LOGI("waiter done (conscrypt=%d anon=%d)", conscrypt_done, anon_done);
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
