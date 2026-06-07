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

// SSL_write's vaddr inside libflutter.so, resolved offline from the matching
// unstripped engine symbols (engine 587c18f873…, build-id 0a7fde9baaf4…). The
// device binary is byte-identical (same build-id), so this offset is exact.
static const uintptr_t FLUTTER_SSL_WRITE_VADDR = 0x717ef0;
// First 8 bytes of SSL_write's prologue: sub sp,#0x40 ; stp x30,x23,[sp,#0x10].
// A sanity check that bias+vaddr still lands on the right function; if Handy
// ships a new Flutter engine the bytes won't match and we refuse to hook.
static const unsigned char FLUTTER_SSL_WRITE_SIG[8] = {
    0xff, 0x03, 0x01, 0xd1, 0xfe, 0x5f, 0x01, 0xa9};

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

static int my_ssl_write_conscrypt(void *ssl, const void *buf, int num) {
  capture_request(buf, num, "conscrypt");
  return g_orig_conscrypt(ssl, buf, num);
}
static int my_ssl_write_flutter(void *ssl, const void *buf, int num) {
  capture_request(buf, num, "flutter");
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
// Waiter thread: hook Conscrypt libssl.so SSL_write AND Flutter's dart:io
// BoringSSL SSL_write (the /f3mf path). libflutter.so loads lazily when the
// Flutter engine spins up, so we keep polling until both are hooked.
// ---------------------------------------------------------------------------
static void *hook_waiter(void *) {
  bool conscrypt_done = false, flutter_done = false;
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
    if (conscrypt_done && flutter_done) {
      LOGI("both SSL_write hooks installed");
      return nullptr;
    }
    usleep(100 * 1000);                      // 100 ms
  }
  LOGI("waiter done (conscrypt=%d flutter=%d)", conscrypt_done, flutter_done);
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
