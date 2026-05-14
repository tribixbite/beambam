// dump_driver_c.c — pure-C version of dump_driver.cpp.
//
// We hand-roll libstdc++ std::string memory layout so we don't pull in
// libc++ from zig (which has a different ABI than the dylib expects).
//
// Build:
//   zig cc -target x86_64-linux-gnu -O1 dump_driver_c.c -ldl -o dump_driver_c
//
// libstdc++ std::string (x86_64 SysV) is 32 bytes:
//   off  +0:  char*  _M_dataplus._M_p   (pointer to data; for SSO points to inline buf)
//   off  +8:  size_t _M_string_length
//   off +16:  union { char _M_local_buf[16]; size_t _M_allocated_capacity; }
//
// Convention used here:
//   - For ALL strings we pass to the dylib, malloc a buffer big enough,
//     point _M_p at it, set length, and zero the union. The dylib's
//     internal destructor will free(_M_p) since _M_p != &_M_local_buf.
//     Means we MUST malloc EVERY string we pass by value, even short
//     ones — never use SSO. Allocations leak after destructor runs but
//     we don't care; this is a one-shot probe.

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <dlfcn.h>

typedef struct {
    char*    _M_p;
    size_t   _M_length;
    union {
        char   _M_local_buf[16];
        size_t _M_capacity;
    } u;
} stdstring_t;

// Build a stdstring_t (libstdc++ ABI) holding a copy of `s`. Always
// allocates (no SSO) — the dylib's destructor will free what we
// allocate, so we don't double-free.
static stdstring_t mkstr(const char* s) {
    stdstring_t out;
    size_t n = strlen(s);
    char* buf = (char*)malloc(n + 1);
    memcpy(buf, s, n + 1);
    out._M_p = buf;
    out._M_length = n;
    out.u._M_capacity = n;
    return out;
}

// Function pointer types — match libstdc++ C++ ABI (sysv x86_64).
// Member functions and overloaded operators are not used here; we only
// call the extern "C" entrypoints which take std::string by VALUE.
//
// Calling convention for std::string (a 32-byte trivially-destructible
// param) on x86_64 SysV: passed via stack with a pointer in rdi
// (it's a non-trivial-for-purposes-of-call type; libstdc++ marks
// std::string as having a non-trivial destructor so callers ALWAYS
// pass by hidden pointer to a heap/stack copy).
//
// In practice we wrap so the compiler emits the right call sequence:
// declare each symbol as taking a stdstring_t by VALUE. The compiler
// will pass it as a hidden pointer just like libstdc++ does.

// Per Itanium C++ ABI (SysV-x86_64), arg types with non-trivial dtors
// (std::string is one) are passed BY HIDDEN POINTER (a single
// const std::string&), and the CALLEE runs the destructor on it.
// So our extern "C" prototypes use `stdstring_t*`, NOT `stdstring_t`.
typedef void* (*create_agent_fn)(const stdstring_t*);
typedef int   (*set_config_dir_fn)(void*, const stdstring_t*);
typedef int   (*change_user_fn)(void*, const stdstring_t*);
typedef void  (*install_device_cert_fn)(void*, const stdstring_t*, bool);
typedef int   (*set_country_code_fn)(void*, const stdstring_t*);
typedef int   (*set_cert_file_fn)(void*, const stdstring_t*, const stdstring_t*);
typedef int   (*start_fn)(void*);
typedef int   (*init_log_fn)(void*);
typedef int   (*connect_server_fn)(void*);
typedef bool  (*is_user_login_fn)(void*);
typedef int   (*update_cert_fn)(void*);

int main(int argc, char** argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    if (argc < 4) {
        fprintf(stderr,
            "usage: %s <libbambu_networking.so> <access_token> <user_id>\n",
            argv[0]);
        return 2;
    }
    const char* so_path = argv[1];
    const char* token   = argv[2];
    const char* user_id = argv[3];
    const char* config_dir = getenv("BAMBU_CONFIG_DIR");
    const char* log_dir    = getenv("BAMBU_LOG_DIR");
    const char* dev_id     = getenv("BAMBU_DEV_ID");
    if (!config_dir) config_dir = "/tmp/bambu_certs";
    if (!log_dir)    log_dir    = "/tmp/bambu_log";
    if (!dev_id)     dev_id     = "";

    fprintf(stderr, "[driver] dlopen(%s) ...\n", so_path);
    void* h = dlopen(so_path, RTLD_NOW | RTLD_GLOBAL);
    if (!h) {
        fprintf(stderr, "[driver] dlopen failed: %s\n", dlerror());
        return 1;
    }
    fprintf(stderr, "[driver] dlopen ok: handle=%p\n", h);

    create_agent_fn        create_agent        = (create_agent_fn)dlsym(h, "bambu_network_create_agent");
    set_config_dir_fn      set_config_dir      = (set_config_dir_fn)dlsym(h, "bambu_network_set_config_dir");
    change_user_fn         change_user         = (change_user_fn)dlsym(h, "bambu_network_change_user");
    install_device_cert_fn install_device_cert = (install_device_cert_fn)dlsym(h, "bambu_network_install_device_cert");
    set_country_code_fn    set_country_code    = (set_country_code_fn)dlsym(h, "bambu_network_set_country_code");
    set_cert_file_fn       set_cert_file       = (set_cert_file_fn)dlsym(h, "bambu_network_set_cert_file");
    start_fn               network_start       = (start_fn)dlsym(h, "bambu_network_start");
    init_log_fn            init_log            = (init_log_fn)dlsym(h, "bambu_network_init_log");
    connect_server_fn      connect_server      = (connect_server_fn)dlsym(h, "bambu_network_connect_server");
    is_user_login_fn       is_user_login       = (is_user_login_fn)dlsym(h, "bambu_network_is_user_login");
    update_cert_fn         update_cert         = (update_cert_fn)dlsym(h, "bambu_network_update_cert");

    fprintf(stderr, "[driver] symbols: create=%p set_cfg=%p change_user=%p install_cert=%p\n",
            (void*)create_agent, (void*)set_config_dir,
            (void*)change_user, (void*)install_device_cert);

    if (!create_agent || !change_user || !install_device_cert) {
        fprintf(stderr, "[driver] missing required symbol(s); abort\n");
        return 1;
    }

    stdstring_t s_log = mkstr(log_dir);
    void* agent = create_agent(&s_log);
    fprintf(stderr, "[driver] agent=%p\n", agent);
    if (!agent) return 1;

    // Canonical init sequence per BS GUI_App.cpp:3488-3515:
    //   set_config_dir → init_log → set_cert_file → set_country_code → start
    if (set_config_dir) {
        stdstring_t s = mkstr(config_dir);
        int rc = set_config_dir(agent, &s);
        fprintf(stderr, "[driver] set_config_dir(%s)=%d\n", config_dir, rc);
    }
    if (init_log) fprintf(stderr, "[driver] init_log=%d\n", init_log(agent));

    // set_cert_file (BS GUI_App.cpp:3507) — points the plugin at its
    // bundled TLS CA bundle for verifying api.bambulab.com etc.
    // The file is `slicer_base64.cer` inside BS's resources/cert/.
    // We provide our own copy.
    const char* cert_folder = getenv("BAMBU_CERT_FOLDER");
    const char* cert_filename = getenv("BAMBU_CERT_FILE");
    if (!cert_folder) cert_folder = "/data/data/com.termux/files/home/git/x2d/bs-bionic/resources/cert";
    if (!cert_filename) cert_filename = "slicer_base64.cer";
    if (set_cert_file) {
        stdstring_t f = mkstr(cert_folder);
        stdstring_t n = mkstr(cert_filename);
        int rc = set_cert_file(agent, &f, &n);
        fprintf(stderr, "[driver] set_cert_file(%s/%s)=%d\n",
                cert_folder, cert_filename, rc);
    }

    if (set_country_code) {
        stdstring_t s = mkstr("us");
        fprintf(stderr, "[driver] set_country_code(us)=%d\n",
                set_country_code(agent, &s));
    }
    if (network_start)    fprintf(stderr, "[driver] network_start=%d\n",
                                   network_start(agent));

    // user_info JSON — Bambu's /v1/user-service/user/login returns
    // camelCase keys (accessToken / refreshToken / loginType etc.).
    // Include both naming conventions for safety; the dylib parser
    // ignores unknown keys.
    size_t bufsz = strlen(token)*2 + strlen(user_id) + 512;
    char* user_info = (char*)malloc(bufsz);
    snprintf(user_info, bufsz,
        "{"
        "\"accessToken\":\"%s\",\"access_token\":\"%s\","
        "\"refreshToken\":\"%s\",\"refresh_token\":\"%s\","
        "\"expiresIn\":31536000,\"expires_in\":31536000,"
        "\"refreshExpiresIn\":31536000,\"refresh_expires_in\":31536000,"
        "\"userId\":\"%s\",\"user_id\":\"%s\","
        "\"region\":\"us\","
        "\"loginType\":\"\""
        "}",
        token, token, token, token, user_id, user_id);
    {
        stdstring_t s = mkstr(user_info);
        int rc = change_user(agent, &s);
        fprintf(stderr, "[driver] change_user=%d\n", rc);
    }
    free(user_info);
    // connect_server (BS GUI_App.cpp:5073) — connects to the cloud
    // MQTT broker. is_user_login flips to true only after this.
    if (connect_server) {
        int rc = connect_server(agent);
        fprintf(stderr, "[driver] connect_server=%d\n", rc);
    }

    // Give the cloud handshake a moment to complete.
    fprintf(stderr, "[driver] waiting 8s for cloud handshake...\n");
    sleep(8);

    if (is_user_login)
        fprintf(stderr, "[driver] is_user_login=%s\n",
                is_user_login(agent) ? "true" : "false");

    if (update_cert) {
        int rc = update_cert(agent);
        fprintf(stderr, "[driver] update_cert=%d\n", rc);
    }

    fprintf(stderr, "[driver] install_device_cert(dev_id='%s', lan_only=0) ...\n", dev_id);
    {
        stdstring_t s = mkstr(dev_id);
        install_device_cert(agent, &s, false);
    }
    fprintf(stderr, "[driver] install_device_cert RETURNED\n");

    fprintf(stderr, "[driver] sleeping 30s for async work to complete...\n");
    sleep(30);

    fprintf(stderr, "[driver] done. inspect %s for cert/key files written.\n",
            config_dir);
    return 0;
}
