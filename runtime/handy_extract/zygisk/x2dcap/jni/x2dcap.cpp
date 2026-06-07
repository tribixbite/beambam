// x2dcap.cpp — Zygisk module that captures Bambu Handy's /f3mf request headers
// by hooking Conscrypt SSL_write from INSIDE the app process, with ZERO Frida
// footprint.
//
// Why a Zygisk module (vs Frida): Promon SHIELD defeats Frida by detecting the
// agent (gum-js-loop comm thread → 0xdead kill; or, once stealthed, fork+exec
// escaping the agent into a non-functional husk). A Zygisk module is mapped by
// the Magisk/ReZygisk loader at zygote-specialize time — no ptrace, no frida
// agent memfd, no gum threads — so SHIELD sees a normal process and never
// fork-escapes, and the app stays FUNCTIONAL with our hook live.
//
// Step 1 (this revision): minimal load+detection probe. In postAppSpecialize,
// if the process is Bambu Handy, append a marker line to the app's OWN cache
// file (the app SELinux domain can always write its own data dir). Read it
// back as root to confirm the module loaded AND Handy stayed functional (no
// SHIELD fork-escape). The SSL_write hook is added once this is verified.

#include <cstdio>
#include <cstring>
#include <cerrno>
#include <unistd.h>
#include <fcntl.h>
#include <sys/types.h>
#include <jni.h>
#include <android/log.h>

#include "zygisk.hpp"

using zygisk::Api;
using zygisk::AppSpecializeArgs;

#define LOG_TAG "x2dcap"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)

static const char *TARGET_PROC = "bbl.intl.bambulab.com";

// Append a line to <data_dir>/cache/x2d_zyg.log (app-writable under SELinux),
// logging the result to logcat (tag x2dcap) so we can see write failures.
static void marker(const char *data_dir, const char *msg) {
  char path[512];
  snprintf(path, sizeof(path), "%s/cache/x2d_zyg.log",
           (data_dir && data_dir[0]) ? data_dir : "/data/data/bbl.intl.bambulab.com");
  int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
  if (fd < 0) {
    LOGI("marker open(%s) FAILED errno=%d (%s)", path, errno, strerror(errno));
    return;
  }
  char buf[256];
  int n = snprintf(buf, sizeof(buf), "%s pid=%d uid=%d\n", msg, getpid(), getuid());
  if (n > 0) write(fd, buf, (size_t) n);
  close(fd);
  LOGI("marker WROTE %s -> %s", msg, path);
}

class X2DCapture : public zygisk::ModuleBase {
public:
  void onLoad(Api *api, JNIEnv *env) override {
    this->api = api;
    this->env = env;
  }

  void preAppSpecialize(AppSpecializeArgs *args) override {
    is_target = false;
    data_dir[0] = '\0';
    if (args->nice_name != nullptr) {
      const char *name = env->GetStringUTFChars(args->nice_name, nullptr);
      if (name != nullptr) {
        // DEBUG: surface any bambu-related process we see, to confirm our hooks
        // run and what nice_name/app_data_dir actually look like.
        if (strstr(name, "bambu") != nullptr) {
          const char *dd0 = args->app_data_dir
              ? env->GetStringUTFChars(args->app_data_dir, nullptr) : nullptr;
          LOGI("preAppSpecialize nice_name=[%s] app_data_dir=[%s]", name,
               dd0 ? dd0 : "(null)");
          if (dd0) env->ReleaseStringUTFChars(args->app_data_dir, dd0);
        }
        if (strcmp(name, TARGET_PROC) == 0) {
          is_target = true;
          if (args->app_data_dir != nullptr) {
            const char *dd = env->GetStringUTFChars(args->app_data_dir, nullptr);
            if (dd != nullptr) {
              strncpy(data_dir, dd, sizeof(data_dir) - 1);
              env->ReleaseStringUTFChars(args->app_data_dir, dd);
            }
          }
        }
        env->ReleaseStringUTFChars(args->nice_name, name);
      }
    }
    // Unload from every non-target process to keep our footprint minimal.
    if (!is_target) {
      api->setOption(zygisk::DLCLOSE_MODULE_LIBRARY);
    }
  }

  void postAppSpecialize(const AppSpecializeArgs *) override {
    if (!is_target) return;
    LOGI("postAppSpecialize TARGET hit, data_dir=[%s]", data_dir);
    marker(data_dir, "x2d-zygisk LOADED");
    // (SSL_write hook installed here in step 2.)
  }

private:
  Api *api = nullptr;
  JNIEnv *env = nullptr;
  bool is_target = false;
  char data_dir[256] = {0};
};

REGISTER_ZYGISK_MODULE(X2DCapture)
