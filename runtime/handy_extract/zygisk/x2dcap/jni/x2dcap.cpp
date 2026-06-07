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
static ssl_write_t g_orig_ssl_write = nullptr;
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
// The SSL_write hook: SSL_write(ssl, buf, num). Capture matching plaintext
// request header blocks, then forward to the original.
// ---------------------------------------------------------------------------
static int my_ssl_write(void *ssl, const void *buf, int num) {
  if (buf != nullptr && num >= 24 && num <= 262144) {
    const char *p = (const char *) buf;
    char c0 = p[0];
    if (c0 == 'G' || c0 == 'P' || c0 == 'D' || c0 == 'H') {   // GET/POST/PUT/DELETE/HEAD
      int lim = num < 16384 ? num : 16384;
      if (mem_contains(p, lim, "x-bbl") || mem_contains(p, lim, "/f3mf") ||
          mem_contains(p, lim, "design-service")) {
        // header block up to CRLFCRLF, else lim
        int hdrlen = lim;
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
          LOGI("CAPTURED SSL_write req: %s", line);
        }
      }
    }
  }
  return g_orig_ssl_write(ssl, buf, num);
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
  size_t nsyms = ((uintptr_t) strtab - (uintptr_t) symtab) / syment;
  if (nsyms == 0 || nsyms > 500000) nsyms = 200000;   // bounded fallback
  for (size_t i = 0; i < nsyms; i++) {
    auto *s = (Elf64_Sym *) ((uintptr_t) symtab + i * syment);
    if (s->st_name == 0 || s->st_value == 0) continue;
    const char *nm = strtab + s->st_name;
    if (strcmp(nm, want) == 0) return (void *) (s->st_value + bias);
  }
  return nullptr;
}

// ---------------------------------------------------------------------------
// Waiter thread: poll until Conscrypt libssl.so is mapped, resolve SSL_write,
// inline-hook it.
// ---------------------------------------------------------------------------
static void *hook_waiter(void *) {
  for (int tries = 0; tries < 600; tries++) {   // up to ~60s
    uintptr_t base = find_lib_base("/libssl.so");
    if (base != 0) {
      void *sw = resolve_sym(base, "SSL_write");
      if (sw != nullptr) {
        A64HookFunction(sw, (void *) my_ssl_write, (void **) &g_orig_ssl_write);
        LOGI("hooked SSL_write @ %p (libssl base %p) — waiting for /f3mf",
             sw, (void *) base);
        marker("x2d-zygisk SSL_HOOK_INSTALLED");
        return nullptr;
      }
      LOGI("libssl.so @ %p but SSL_write unresolved — retrying", (void *) base);
    }
    usleep(100 * 1000);   // 100 ms
  }
  LOGI("waiter gave up — libssl.so/SSL_write not found");
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
